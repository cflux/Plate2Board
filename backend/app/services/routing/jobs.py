"""In-memory job store for routing requests.

Workflow: the HTTP endpoint kicks off an asyncio task and stores its state
here keyed by a UUID. The frontend polls `/route-jobs/{id}` to read the
state and `/route-jobs/{id}/result` to fetch the cached ZIP once finished.

Single-process: this lives in the same Python process as the uvicorn
worker. If we ever run multiple workers, this needs to move to Redis or
similar — there's no cross-process sharing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

# How long completed / failed jobs linger before they're pruned. Long
# enough to handle "user clicked download, then their connection dropped,
# they reopen the tab to retry"; short enough that we don't leak ZIPs in
# memory forever.
JOB_TTL_S = 600.0  # 10 minutes


@dataclass
class RouteStats:
    routed_count: int = 0
    unrouted_count: int = 0
    total_count: int = 0
    via_count: int = 0
    # Pads whose net has copper but no wire actually touching them after
    # the splice (see ses.find_unattached_pads) — shown to the user as
    # connections they must finish in KiCad, on top of `unrouted_count`.
    unattached_pads: int = 0
    # Optional live signal from the freerouting log scraper — lets the UI
    # show "Pass 3 — score 991.2" instead of just a static percent.
    pass_number: int = 0
    last_log: str = ""


@dataclass
class RouteJob:
    job_id: str
    # state: pending | running | done | failed
    state: str = "pending"
    # phase: short human label (e.g. "generating-pcb", "routing", "packaging")
    phase: str = "queued"
    percent: float = 0.0
    error: Optional[str] = None
    stats: Optional[RouteStats] = None
    # Set only on success — the assembled project ZIP bytes.
    result: Optional[bytes] = None
    # Set only on success — filename to suggest to the browser.
    result_filename: Optional[str] = None
    # Wall-clock when the job was created — surfaced as `elapsed_s` in the
    # status payload so the UI can show "Routing… 47s" without computing
    # times client-side.
    created_at: float = field(default_factory=time.time)
    # mtime used by `prune_expired` for TTL eviction. Updated on every
    # status update so an actively-polled job doesn't expire mid-run.
    updated_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, RouteJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, job_id: str) -> RouteJob:
        async with self._lock:
            self._prune_locked()
            job = RouteJob(job_id=job_id)
            self._jobs[job_id] = job
            return job

    async def update(
        self,
        job_id: str,
        *,
        state: Optional[str] = None,
        phase: Optional[str] = None,
        percent: Optional[float] = None,
        error: Optional[str] = None,
        stats: Optional[RouteStats] = None,
        result: Optional[bytes] = None,
        result_filename: Optional[str] = None,
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if state is not None:
                job.state = state
            if phase is not None:
                job.phase = phase
            if percent is not None:
                job.percent = percent
            if error is not None:
                job.error = error
            if stats is not None:
                job.stats = stats
            if result is not None:
                job.result = result
            if result_filename is not None:
                job.result_filename = result_filename
            job.updated_at = time.time()

    async def get(self, job_id: str) -> Optional[RouteJob]:
        async with self._lock:
            self._prune_locked()
            return self._jobs.get(job_id)

    async def pop_result(self, job_id: str) -> Optional[tuple[bytes, str]]:
        """Atomically take + clear a job's result blob. Subsequent
        `/result` GETs will return 410 Gone because the job entry stays
        but its `result` is None."""
        async with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
            if job is None or job.result is None:
                return None
            data = job.result
            filename = job.result_filename or "routed-project.zip"
            job.result = None
            job.updated_at = time.time()
            return (data, filename)

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [jid for jid, j in self._jobs.items() if now - j.updated_at > JOB_TTL_S]
        for jid in stale:
            del self._jobs[jid]


# Process-global singleton; the API endpoints import this directly.
STORE = JobStore()
