"""Footprint name registry shared by `schematic.py` and `pcb.py`.

Both generators must emit identical footprint names per component instance,
otherwise "Update PCB from Schematic" fails because KiCad can't match the
schematic's footprint reference to the PCB instance. They also must use
names that resolve in the project's embedded `keeb` library — see
`embed_footprints.py` for how the .kicad_mod templates are produced.

All names live under the single `keeb:` library prefix. The `keeb` library
ships with every generated project ZIP (`{project}/footprints.pretty/`) so
no external KiCad footprint install is required to open + sync the board.
"""

from __future__ import annotations

from typing import Literal

LIB_NAME = "keeb"

SwitchType = Literal["soldered", "hotswap"]
DiodeType = Literal["tht", "smd"]
StabilizerType = Literal["pcb_mount", "plate_mount"]


def switch_footprint(switch_type: SwitchType) -> str:
    if switch_type == "soldered":
        return f"{LIB_NAME}:SW_Cherry_MX_PCB_1.00u"
    return f"{LIB_NAME}:SW_Hotswap_Kailh_MX_1.00u"


def diode_footprint(diode_type: DiodeType) -> str:
    if diode_type == "tht":
        return f"{LIB_NAME}:D_DO-35_SOD27_P7.62mm_Horizontal"
    return f"{LIB_NAME}:D_SOD-123"


def mcu_footprint() -> str:
    return f"{LIB_NAME}:Arduino_Pro_Micro"


def stabilizer_footprint(stab_type: StabilizerType) -> str:
    if stab_type == "pcb_mount":
        return f"{LIB_NAME}:Stabilizer_PCB_Mount"
    return f"{LIB_NAME}:Stabilizer_Plate_Mount"


def mounting_hole_footprint(diameter_mm: float) -> str:
    return f"{LIB_NAME}:MountingHole_{diameter_mm:.1f}mm"


def bare_name(qualified: str) -> str:
    """Strip the ``lib:`` prefix. ``keeb:SW_Cherry_MX_PCB_1.00u`` →
    ``SW_Cherry_MX_PCB_1.00u``. Used when emitting .kicad_mod files
    (which take a bare name) and when matching templates."""
    return qualified.split(":", 1)[-1]
