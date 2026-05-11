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


class ParseResult(BaseModel):
    svg_width_mm: float
    svg_height_mm: float
    pcb_outline: PcbOutline
    switches: list[SwitchDef]
    stabilizers: list[StabilizerDef]
    mounting_holes: list[MountingHoleDef] = []
    unclassified: list[UnclassifiedShape]


class SvgParseError(ValueError):
    pass
