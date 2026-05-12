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
from ..services.project import DEFAULT_PROJECT_NAME, generate_project_zip
from ..services.schematic import generate_schematic

router = APIRouter()


class NetlistRequest(BaseModel):
    switches: list[SwitchDef]


@router.post("/generate-netlist", response_class=PlainTextResponse)
async def post_generate_netlist(req: NetlistRequest) -> PlainTextResponse:
    text = generate_netlist(req.switches)
    return PlainTextResponse(
        content=text,
        headers={"Content-Disposition": 'attachment; filename="keyboard.net"'},
    )


@router.post("/generate-schematic", response_class=PlainTextResponse)
async def post_generate_schematic(req: NetlistRequest) -> PlainTextResponse:
    if not req.switches:
        raise HTTPException(
            status_code=400, detail="cannot generate schematic from zero switches"
        )
    try:
        text = generate_schematic(req.switches)
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
    text = generate_pcb(
        req,
        switch_type=switch_type,
        diode_type=diode_type,
        stabilizer_type=stabilizer_type,
    )
    return PlainTextResponse(
        content=text,
        headers={
            "Content-Disposition": 'attachment; filename="keyboard.kicad_pcb"'
        },
    )


@router.post("/generate-project")
async def post_generate_project(
    req: ParseResult,
    project_name: str = DEFAULT_PROJECT_NAME,
    switch_type: str = "soldered",
    diode_type: str = "tht",
    stabilizer_type: str = "pcb_mount",
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
    try:
        zip_bytes = generate_project_zip(
            req,
            project_name=project_name,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
        )
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
