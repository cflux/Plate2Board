from pydantic import BaseModel


class SwitchDef(BaseModel):
    id: int
    cx_mm: float
    cy_mm: float
    rotation_deg: float = 0.0
    row: int = 0
    col: int = 0


class StabilizerDef(BaseModel):
    id: int
    cx_mm: float
    cy_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float = 0.0


class PcbOutline(BaseModel):
    width_mm: float
    height_mm: float
    path_d: str


class MountingHoleDef(BaseModel):
    id: int
    cx_mm: float
    cy_mm: float
    diameter_mm: float


class UnclassifiedShape(BaseModel):
    id: int
    cx_mm: float
    cy_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float = 0.0


class McuPlacement(BaseModel):
    cx_mm: float
    cy_mm: float
    rotation_deg: float = 0.0


class ParseResult(BaseModel):
    svg_width_mm: float
    svg_height_mm: float
    pcb_outline: PcbOutline
    switches: list[SwitchDef]
    stabilizers: list[StabilizerDef]
    mounting_holes: list[MountingHoleDef] = []
    unclassified: list[UnclassifiedShape]
    mcu_placement: McuPlacement | None = None
    # PCB inset: the outline is the keyboard PLATE; the PCB edge is pulled
    # IN by this much on every side (assemblies need the PCB ≤ the plate,
    # never larger). The plate SVG export always keeps the raw outline.
    outline_shrink_mm: float = 0.0
    matrix_strategy: str = "row_first"
    # User-edited outline polygon (set when the user is in edit-plate mode).
    # When non-null, every generator uses this as the plate outline instead
    # of `pcb_outline.path_d`; `outline_shrink_mm` still applies on top.
    edited_outline_path_d: str | None = None
    # Unit info — the parser detects the SVG's unit (mm / cm / in / pt / pc /
    # px-inferred via switch-cutout heuristic) and reports it for the UI.
    detected_svg_unit: str = "mm"
    mm_per_unit: float = 1.0


class SvgParseError(ValueError):
    pass
