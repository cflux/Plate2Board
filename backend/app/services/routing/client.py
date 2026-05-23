"""Async client for the freerouting REST API (v1).

We treat the freerouting sidecar like a remote function:

    ses_text, stats = await route(dsn_text, progress_cb=cb)

The client handles the full session+job lifecycle (create session → enqueue
job → upload DSN → set router options → start → poll → download SES), with
a hard wall-clock timeout and best-effort cancellation on timeout.

The freerouting REST API is documented at
https://github.com/freerouting/freerouting/blob/master/docs/API/API_v1.md.
We talk to v1 specifically and target image tag `2.2.4` (pinned in
docker-compose.yml) to keep field names stable.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# Stable identity sent with every request so the freerouting server's audit
# log groups our calls. Even with auth disabled the server validates the
# profile ID format (must be a real RFC 4122 UUID, 8-4-4-4-12 hex), so
# this is a fixed valid UUID rather than a placeholder.
_PROFILE_ID = "00000000-0000-0000-0000-000000000001"
_PROFILE_EMAIL = "keeb-layout-bot@local"
_API_VERSION = "v1.0"
_CLIENT_NAME = "keeb-layout-bot"
_CLIENT_VERSION = "0.8.0"

DEFAULT_BASE_URL = "http://freerouting:37864"
DEFAULT_MAX_PASSES = 100
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_TIMEOUT_S = 300.0  # 5 minutes hard cap on routing


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class RouterStats:
    """Mirror of the freerouting statistics object — see the API_v1.md doc.
    `routed_net_count` / `unrouted_net_count` are the headline numbers; the
    others are useful in logs but optional. `pass_number` / `score` /
    `last_log` are scraped from /v1/jobs/{id}/logs during polling so the
    UI can show what the router is actually doing right now."""
    routed_net_count: int = 0
    unrouted_net_count: int = 0
    via_count: int = 0
    layer_count: int = 0
    component_count: int = 0
    pass_number: int = 0
    last_log: str = ""

    @property
    def total_net_count(self) -> int:
        return self.routed_net_count + self.unrouted_net_count


@dataclass
class RouteResult:
    ses_text: str
    stats: RouterStats


# `progress_cb(phase, percent, stats?)` — phase is a short string like
# "starting" / "routing" / "downloading"; percent is 0..100; stats is
# whatever the latest poll showed (None until the first stats update).
ProgressCB = Callable[[str, float, Optional[RouterStats]], Awaitable[None] | None]


class FreeroutingError(RuntimeError):
    """Raised when the sidecar reports a failed job or is unreachable."""


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Freerouting-Profile-ID": _PROFILE_ID,
        "Freerouting-Profile-Email": _PROFILE_EMAIL,
        "Freerouting-API-Version": _API_VERSION,
        "Freerouting-Environment-Client": _CLIENT_NAME,
        "Freerouting-Environment-Version": _CLIENT_VERSION,
        # Required by the sidecar even when auth is disabled — identifies
        # the calling EDA tool in the freerouting audit log.
        "Freerouting-Environment-Host": f"{_CLIENT_NAME}/{_CLIENT_VERSION}",
    }


async def _emit(progress_cb: Optional[ProgressCB], phase: str, percent: float,
                stats: Optional[RouterStats] = None) -> None:
    if progress_cb is None:
        return
    res = progress_cb(phase, percent, stats)
    if asyncio.iscoroutine(res):
        await res


def _decode_b64(s: str) -> str:
    """Base64 → UTF-8 string. Freerouting wraps every file payload in
    base64 — both the DSN we upload and the SES we get back."""
    return base64.b64decode(s).decode("utf-8")


def _encode_b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _parse_stats(stats_obj: dict | None) -> RouterStats:
    if not stats_obj:
        return RouterStats()
    return RouterStats(
        routed_net_count=int(stats_obj.get("routed_net_count") or 0),
        unrouted_net_count=int(stats_obj.get("unrouted_net_count") or 0),
        via_count=int(stats_obj.get("via_count") or 0),
        layer_count=int(stats_obj.get("layer_count") or 0),
        component_count=int(stats_obj.get("component_count") or 0),
    )


async def route(
    dsn_text: str,
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    progress_cb: Optional[ProgressCB] = None,
) -> RouteResult:
    """Route a DSN through the freerouting sidecar and return the SES + stats.

    Raises `FreeroutingError` on hard failures (sidecar unreachable, job in
    FAILED/CANCELLED state, or wall-clock timeout). Partial routing (some
    nets remaining unrouted) is NOT a hard failure — it returns normally
    and the caller reads `stats.unrouted_net_count` to decide what to show
    the user.
    """
    url = base_url or os.environ.get("FREEROUTING_URL", DEFAULT_BASE_URL)
    job_name = f"keeb-{uuid.uuid4().hex[:8]}"
    filename = f"{job_name}.dsn"

    async with httpx.AsyncClient(
        base_url=url, headers=_headers(),
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as client:
        await _emit(progress_cb, "starting", 0.0)
        try:
            session = await _create_session(client)
        except httpx.HTTPError as exc:
            raise FreeroutingError(
                f"auto-router service is unavailable ({exc})"
            ) from exc
        session_id = session["id"]
        logger.info("freerouting session %s", session_id)

        job = await _enqueue_job(client, session_id, job_name)
        job_id = job["id"]

        await _upload_input(client, job_id, filename, dsn_text)
        await _set_settings(client, job_id, max_passes=max_passes)
        await _start_job(client, job_id)
        await _emit(progress_cb, "routing", 5.0)

        ses_text, stats = await _wait_for_output(
            client,
            job_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            progress_cb=progress_cb,
        )
        await _emit(progress_cb, "downloading", 95.0, stats)

    return RouteResult(ses_text=ses_text, stats=stats)


# ---------------------------------------------------------------------------
# Low-level: one call per REST endpoint we touch
# ---------------------------------------------------------------------------


async def _create_session(client: httpx.AsyncClient) -> dict:
    r = await client.post("/v1/sessions/create", json={})
    r.raise_for_status()
    return r.json()


async def _enqueue_job(client: httpx.AsyncClient, session_id: str, name: str) -> dict:
    r = await client.post(
        "/v1/jobs/enqueue",
        json={"session_id": session_id, "name": name, "priority": "NORMAL"},
    )
    r.raise_for_status()
    return r.json()


async def _upload_input(
    client: httpx.AsyncClient, job_id: str, filename: str, dsn_text: str
) -> None:
    r = await client.post(
        f"/v1/jobs/{job_id}/input",
        json={"filename": filename, "data": _encode_b64(dsn_text)},
    )
    r.raise_for_status()


async def _set_settings(
    client: httpx.AsyncClient, job_id: str, *, max_passes: int
) -> None:
    # Reasonable defaults for keyboard matrices: full optimisation passes,
    # via cost biased moderately so the router prefers single-layer paths
    # when possible (we still allow vias — see project.py Matrix netclass).
    r = await client.post(
        f"/v1/jobs/{job_id}/settings",
        json={
            "max_passes": max_passes,
            "via_costs": 50,
            "start_pass_no": 1,
            "start_ripup_costs": 100,
        },
    )
    # Settings endpoint may 200/204 even if it ignores unknown fields; only
    # treat 4xx/5xx as a hard error.
    if r.status_code >= 400:
        r.raise_for_status()


async def _start_job(client: httpx.AsyncClient, job_id: str) -> None:
    r = await client.put(f"/v1/jobs/{job_id}/start")
    r.raise_for_status()


_PASS_LOG_RE = re.compile(
    r"Auto-router pass #(\d+).*?score of ([\d.]+)(?:\s*\((\d+)\s+unrouted\))?"
)
_FINAL_LOG_RE = re.compile(
    r"Auto-router session (?:completed|cancelled): "
    r"started with (\d+) unrouted nets.*?"
    r"final score: ([\d.]+)(?:\s*\((\d+)\s+unrouted\))?"
)


async def _fetch_logs(client: httpx.AsyncClient, job_id: str) -> list[dict]:
    """Best-effort fetch of the job's log entries. Returns [] on any error
    so callers can treat empty + fetch-failure identically."""
    try:
        r = await client.get(f"/v1/jobs/{job_id}/logs")
        if r.status_code != 200:
            return []
        logs = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    return logs if isinstance(logs, list) else []


async def _scrape_pass_info(client: httpx.AsyncClient, job_id: str) -> dict:
    """Return ``{pass_number, score, unrouted, last_log}`` scraped from the
    most recent pass-completion line. Used for live progress while routing
    is still running."""
    for entry in reversed(await _fetch_logs(client, job_id)):
        msg = entry.get("message") if isinstance(entry, dict) else str(entry)
        if not msg:
            continue
        m = _PASS_LOG_RE.search(msg)
        if m:
            return {
                "pass_number": int(m.group(1)),
                "score": float(m.group(2)),
                "unrouted": int(m.group(3)) if m.group(3) else 0,
                "last_log": msg,
            }
    return {}


async def _scrape_final_summary(client: httpx.AsyncClient, job_id: str) -> dict:
    """Scrape the final "Auto-router session completed/cancelled" line to
    get authoritative ``total`` + ``unrouted`` counts. Freerouting's
    /output ``statistics`` block frequently returns null for these on
    short jobs, so we fall back to the log line which always carries
    them. Returns ``{}`` if the session hasn't ended yet."""
    for entry in reversed(await _fetch_logs(client, job_id)):
        msg = entry.get("message") if isinstance(entry, dict) else str(entry)
        if not msg:
            continue
        m = _FINAL_LOG_RE.search(msg)
        if m:
            return {
                "total": int(m.group(1)),
                "score": float(m.group(2)),
                "unrouted": int(m.group(3)) if m.group(3) else 0,
            }
    return {}


async def _wait_for_output(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
    progress_cb: Optional[ProgressCB],
) -> tuple[str, RouterStats]:
    """Poll until the job finishes (200) or fails (4xx). Reports live stats
    via `progress_cb` while the job runs (HTTP 202 carries partial stats)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_stats = RouterStats()
    log_poll_counter = 0
    while True:
        if asyncio.get_event_loop().time() > deadline:
            # Best-effort cancel — we don't fail the routing if cancel
            # itself errors out (sidecar might already be done).
            try:
                await client.put(f"/v1/jobs/{job_id}/cancel")
            except httpx.HTTPError:
                pass
            raise FreeroutingError(f"routing timed out after {timeout_s:.0f}s")

        r = await client.get(f"/v1/jobs/{job_id}/output")
        if r.status_code == 200:
            body = r.json()
            stats = _parse_stats(body.get("statistics"))
            ses_text = _decode_b64(body["data"])
            # Freerouting often returns null/0 for routed_net_count and
            # unrouted_net_count even when the log clearly reports both.
            # The final summary line is authoritative — fall back to it so
            # the UI sees real "K of N routed" numbers instead of zeros.
            summary = await _scrape_final_summary(client, job_id)
            if summary:
                if stats.total_net_count == 0 and summary.get("total"):
                    stats.unrouted_net_count = summary["unrouted"]
                    stats.routed_net_count = summary["total"] - summary["unrouted"]
                # Even when /output stats exist, trust the log's unrouted
                # count if it's higher (freerouting sometimes reports
                # routed_net_count > 0 with unrouted_net_count=null).
                elif summary["unrouted"] > stats.unrouted_net_count:
                    stats.unrouted_net_count = summary["unrouted"]
                    if summary.get("total"):
                        stats.routed_net_count = summary["total"] - summary["unrouted"]
            return ses_text, stats
        if r.status_code == 202:
            # In-progress with partial output. Update stats + progress.
            try:
                body = r.json()
                last_stats = _parse_stats(body.get("statistics"))
            except Exception:
                pass
            pct = _routing_percent(last_stats)
            await _emit(progress_cb, "routing", pct, last_stats)
        elif r.status_code == 204:
            # No output yet — still queuing / pre-routing.
            await _emit(progress_cb, "routing", 5.0, last_stats)
        elif r.status_code >= 400:
            # 400 can mean "job hasn't produced output yet" (when it's still
            # READY_TO_START / RUNNING in early passes) OR a real failure.
            # Inspect the job's state to distinguish — only terminal failure
            # states should abort polling.
            try:
                jr = await client.get(f"/v1/jobs/{job_id}")
                state = jr.json().get("state", "UNKNOWN")
            except httpx.HTTPError:
                state = "UNKNOWN"
            if state in ("CANCELLED", "TERMINATED", "FAILED", "ERROR"):
                raise FreeroutingError(
                    f"freerouting job failed (state={state}, http={r.status_code})"
                )
            # Otherwise treat as transient and keep polling.
            await _emit(progress_cb, "routing", _routing_percent(last_stats), last_stats)

        # Scrape the /logs endpoint every few polls (not every tick — logs
        # can be large) to surface pass numbers + scores to the UI even
        # when freerouting's /output stats haven't updated yet.
        log_poll_counter += 1
        if log_poll_counter % 3 == 0:
            info = await _scrape_pass_info(client, job_id)
            if info:
                last_stats.pass_number = info["pass_number"]
                if info.get("unrouted") and last_stats.unrouted_net_count == 0:
                    last_stats.unrouted_net_count = info["unrouted"]
                last_stats.last_log = info["last_log"]
                await _emit(
                    progress_cb, "routing", _routing_percent(last_stats), last_stats
                )

        await asyncio.sleep(poll_interval_s)


def _routing_percent(stats: RouterStats) -> float:
    """Map routed/total → 5..90 percent so the UI's routing phase fills
    that band. Final 5..100 is reserved for download+splice+packaging."""
    total = stats.total_net_count
    if total <= 0:
        return 5.0
    routed = stats.routed_net_count
    return 5.0 + 85.0 * (routed / total)
