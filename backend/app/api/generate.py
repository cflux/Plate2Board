import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..models.schemas import ParseResult, SwitchDef
from ..services.netlist import generate_netlist
from ..services.pcb import (
    DIODE_TYPES,
    STABILIZER_TYPES,
    SWITCH_TYPES,
    generate_pcb,
)
from ..services.mcu import MCU_TYPES
from ..services.plate_svg import generate_plate_svg
from ..services.project import DEFAULT_PROJECT_NAME, generate_project_zip
from ..services.routing import client as routing_client
from ..services.routing import jobs as routing_jobs
from ..services.routing import runner as routing_runner
from ..services.routing.dsn import pad_world_positions
from ..services.routing.islands import reconnect_islands
from ..services.routing.ses import apply_ses_to_pcb
from ..services.schematic import generate_schematic

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_mcu_type(mcu_type: str) -> None:
    if mcu_type not in MCU_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"mcu_type must be one of {sorted(MCU_TYPES)}, got {mcu_type!r}",
        )


class NetlistRequest(BaseModel):
    switches: list[SwitchDef]


@router.post("/generate-netlist", response_class=PlainTextResponse)
async def post_generate_netlist(
    req: NetlistRequest,
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> PlainTextResponse:
    _check_mcu_type(mcu_type)
    text = generate_netlist(
        req.switches, ground_pour=ground_pour, rgb=rgb, mcu_type=mcu_type
    )
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": 'attachment; filename="keyboard.net"'},
    )


@router.post("/generate-schematic", response_class=PlainTextResponse)
async def post_generate_schematic(
    req: NetlistRequest,
    switch_type: str = "soldered",
    diode_type: str = "tht",
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> PlainTextResponse:
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate schematic from zero switches"
        )
    _check_mcu_type(mcu_type)
    if switch_type not in SWITCH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"switch_type must be one of {sorted(SWITCH_TYPES)}, got {switch_type!r}",
        )
    if diode_type not in DIODE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"diode_type must be one of {sorted(DIODE_TYPES)}, got {diode_type!r}",
        )
    try:
        text = generate_schematic(
            req.switches, switch_type=switch_type, diode_type=diode_type,
            ground_pour=ground_pour, rgb=rgb, mcu_type=mcu_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SKiDL failed: {exc}") from exc
    return PlainTextResponse(
        content=text,
        headers={
            "Content-Disposition": 'attachment; filename="keyboard.kicad_sch"'
        },
    )


@router.post("/generate-pcb", response_class=PlainTextResponse)
async def post_generate_pcb(
    req: ParseResult,
    switch_type: str = "soldered",
    diode_type: str = "tht",
    stabilizer_type: str = "pcb_mount",
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> PlainTextResponse:
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate PCB from zero switches"
        )
    if switch_type not in SWITCH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"switch_type must be one of {sorted(SWITCH_TYPES)}, "
                f"got {switch_type!r}"
            ),
        )
    if diode_type not in DIODE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"diode_type must be one of {sorted(DIODE_TYPES)}, "
                f"got {diode_type!r}"
            ),
        )
    if stabilizer_type not in STABILIZER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"stabilizer_type must be one of {sorted(STABILIZER_TYPES)}, "
                f"got {stabilizer_type!r}"
            ),
        )
    _check_mcu_type(mcu_type)
    try:
        text = generate_pcb(
            req,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
        )
    except ValueError as exc:
        # Board-level validation (pad edge setback, degenerate shrink,
        # GPIO budget) — user-fixable, so 400 with the explanation.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlainTextResponse(
        content=text,
        headers={
            "Content-Disposition": 'attachment; filename="keyboard.kicad_pcb"'
        },
    )


@router.post("/generate-plate-svg", response_class=PlainTextResponse)
async def post_generate_plate_svg(req: ParseResult) -> PlainTextResponse:
    """Emit a clean plate SVG from the parsed result. The plate is the
    raw (edited or parsed) outline — `outline_shrink_mm` applies to the
    PCB only, never the plate export."""
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate plate SVG from zero switches"
        )
    text = generate_plate_svg(req)
    return PlainTextResponse(
        content=text,
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": 'attachment; filename="keyboard-plate.svg"'
        },
    )


@router.post("/generate-project")
async def post_generate_project(
    req: ParseResult,
    project_name: str = DEFAULT_PROJECT_NAME,
    switch_type: str = "soldered",
    diode_type: str = "tht",
    stabilizer_type: str = "pcb_mount",
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> Response:
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate project from zero switches"
        )
    if switch_type not in SWITCH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"switch_type must be one of {sorted(SWITCH_TYPES)}, "
                f"got {switch_type!r}"
            ),
        )
    if diode_type not in DIODE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"diode_type must be one of {sorted(DIODE_TYPES)}, "
                f"got {diode_type!r}"
            ),
        )
    if stabilizer_type not in STABILIZER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"stabilizer_type must be one of {sorted(STABILIZER_TYPES)}, "
                f"got {stabilizer_type!r}"
            ),
        )
    _check_mcu_type(mcu_type)
    try:
        zip_bytes = generate_project_zip(
            req,
            project_name=project_name,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"project generation failed: {exc}"
        ) from exc
    safe_name = (
        project_name.replace("..", "").replace("/", "").replace("\\", "")
        or DEFAULT_PROJECT_NAME
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}-project.zip"'
        },
    )


# ---------------------------------------------------------------------------
# Auto-routed project (Freerouting sidecar)
# ---------------------------------------------------------------------------


def _validate_project_args(
    req: ParseResult, switch_type: str, diode_type: str, stabilizer_type: str
) -> None:
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate project from zero switches"
        )
    if switch_type not in SWITCH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"switch_type must be one of {sorted(SWITCH_TYPES)}, got {switch_type!r}",
        )
    if diode_type not in DIODE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"diode_type must be one of {sorted(DIODE_TYPES)}, got {diode_type!r}",
        )
    if stabilizer_type not in STABILIZER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"stabilizer_type must be one of {sorted(STABILIZER_TYPES)}, got {stabilizer_type!r}",
        )


@router.post("/generate-routed-project")
async def post_generate_routed_project(
    req: ParseResult,
    project_name: str = DEFAULT_PROJECT_NAME,
    switch_type: str = "soldered",
    diode_type: str = "tht",
    stabilizer_type: str = "pcb_mount",
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> dict:
    """Kick off an auto-routed project build. Returns immediately with a
    job id; the actual routing happens in a background task. Poll
    `/route-jobs/{job_id}` for progress and `/route-jobs/{job_id}/result`
    once `state == "done"` to download the routed ZIP.
    """
    _validate_project_args(req, switch_type, diode_type, stabilizer_type)
    _check_mcu_type(mcu_type)
    # Pre-flight: run the full board validation (pad edge setback, shrink
    # degeneracy, GPIO budget) before spinning up a job, so the user gets
    # an instant 400 instead of a failed job.
    try:
        generate_pcb(
            req,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = str(uuid.uuid4())
    await routing_jobs.STORE.create(job_id)
    asyncio.create_task(
        _run_routed_job(
            job_id, req, project_name, switch_type, diode_type,
            stabilizer_type, ground_pour, rgb, mcu_type,
        )
    )
    return {
        "job_id": job_id,
        "status_url": f"/api/route-jobs/{job_id}",
        "result_url": f"/api/route-jobs/{job_id}/result",
    }


@router.get("/route-jobs/{job_id}")
async def get_route_job(job_id: str) -> dict:
    import time as _time
    job = await routing_jobs.STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found or expired")
    body: dict = {
        "job_id": job.job_id,
        "state": job.state,
        "phase": job.phase,
        "percent": job.percent,
        "elapsed_s": round(_time.time() - job.created_at, 1),
    }
    if job.error:
        body["error"] = job.error
    if job.stats:
        body["stats"] = {
            "routed": job.stats.routed_count,
            "unrouted": job.stats.unrouted_count,
            "total": job.stats.total_count,
            "vias": job.stats.via_count,
            "unattached": job.stats.unattached_pads,
            "island_warnings": job.stats.island_warnings,
            "pass": job.stats.pass_number,
            "log": job.stats.last_log,
        }
    return body


@router.get("/route-jobs/{job_id}/result")
async def get_route_job_result(job_id: str) -> Response:
    job = await routing_jobs.STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found or expired")
    if job.state == "failed":
        raise HTTPException(
            status_code=500, detail=job.error or "routing failed (no detail)"
        )
    if job.state != "done":
        raise HTTPException(
            status_code=409,
            detail=f"job not finished (state={job.state})",
        )
    popped = await routing_jobs.STORE.pop_result(job_id)
    if popped is None:
        # Result already collected — clients should download once.
        raise HTTPException(status_code=410, detail="result already downloaded")
    data, filename = popped
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


async def _run_routed_job(
    job_id: str,
    req: ParseResult,
    project_name: str,
    switch_type: str,
    diode_type: str,
    stabilizer_type: str,
    ground_pour: bool = True,
    rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> None:
    """Background task: full pipeline from ParseResult to routed ZIP.

    Phases (each mapped to a job-store percent):
        generating-pcb  →  5%
        routing         → 20–90% (DSN export + freerouting attempts across
                          the via-cost ladder, driven by progress callbacks)
        parsing-ses     → 92%
        packaging       → 97%
        done            → 100%
    """
    store = routing_jobs.STORE
    try:
        await store.update(job_id, state="running", phase="generating-pcb", percent=5.0)
        pcb_text = generate_pcb(
            req,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
        )

        await store.update(job_id, phase="routing", percent=20.0)

        async def on_progress(phase: str, percent: float, stats) -> None:
            # Map client's 0..100 to our 25..90 band.
            job_pct = 25.0 + max(0.0, min(percent, 100.0)) * (90.0 - 25.0) / 100.0
            mapped_stats = None
            if stats is not None:
                mapped_stats = routing_jobs.RouteStats(
                    routed_count=stats.routed_net_count,
                    unrouted_count=stats.unrouted_net_count,
                    total_count=stats.total_net_count,
                    via_count=stats.via_count,
                    pass_number=stats.pass_number,
                    last_log=stats.last_log,
                )
            await store.update(
                job_id, phase="routing", percent=job_pct, stats=mapped_stats
            )

        # Routes across the via-cost ladder: a plateaued first attempt is
        # retried with cheaper vias, and the best attempt wins.
        route_result = await routing_runner.route_board(
            req,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
            progress_cb=on_progress,
        )

        await store.update(job_id, phase="parsing-ses", percent=92.0)
        routed_pcb, splice_stats = apply_ses_to_pcb(
            pcb_text,
            route_result.ses_text,
            total_connections=route_result.stats.total_net_count,
            unrouted_connections=route_result.stats.unrouted_net_count,
            # Geometry tripwire: verify routed copper actually lands on the
            # kicad_pcb's pads (catches DSN coordinate-convention drift,
            # which freerouting itself can't see).
            pad_positions=pad_world_positions(
                req,
                switch_type=switch_type,
                diode_type=diode_type,
                stabilizer_type=stabilizer_type,
                rgb=rgb,
                ground_pour=ground_pour,
                mcu_type=mcu_type,
            ),
        )
        if splice_stats.unattached_pad_count:
            logger.warning(
                "routing job %s: %d unattached pad(s) after splice",
                job_id, splice_stats.unattached_pad_count,
            )

        # Reconnect any GND/VCC pour islands the routed traces fenced off
        # (vias where the net is on the other layer, else short jumpers).
        await store.update(job_id, phase="reconnecting-islands", percent=94.0)
        routed_pcb, island_warnings = reconnect_islands(routed_pcb)
        if island_warnings:
            logger.warning(
                "routing job %s: %d island warning(s): %s",
                job_id, len(island_warnings), "; ".join(island_warnings[:10]),
            )

        await store.update(job_id, phase="packaging", percent=97.0)
        zip_bytes = generate_project_zip(
            req,
            project_name=project_name,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            ground_pour=ground_pour,
            rgb=rgb,
            mcu_type=mcu_type,
            pcb_text_override=routed_pcb,
        )

        safe_name = (
            project_name.replace("..", "").replace("/", "").replace("\\", "")
            or DEFAULT_PROJECT_NAME
        )
        # `route_result.stats` already reconciles /output's statistics
        # with the log's final-summary line, so the per-net counts here
        # are trustworthy. Via count we take from whichever source has
        # it (the SES splice always knows; /output sometimes does too).
        stats = routing_jobs.RouteStats(
            routed_count=route_result.stats.routed_net_count,
            unrouted_count=route_result.stats.unrouted_net_count,
            total_count=route_result.stats.total_net_count,
            via_count=route_result.stats.via_count or splice_stats.via_count,
            unattached_pads=splice_stats.unattached_pad_count,
            island_warnings=len(island_warnings),
        )
        await store.update(
            job_id,
            state="done",
            phase="done",
            percent=100.0,
            stats=stats,
            result=zip_bytes,
            result_filename=f"{safe_name}-project.zip",
        )
    except routing_client.FreeroutingError as exc:
        logger.warning("routing job %s failed: %s", job_id, exc)
        await store.update(job_id, state="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — log everything else as a hard failure
        logger.exception("routing job %s crashed", job_id)
        await store.update(job_id, state="failed", error=str(exc))
