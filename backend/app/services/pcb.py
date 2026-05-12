"""Generate a KiCad .kicad_pcb file with switches placed at SVG coordinates.

Footprints are written inline in modern KiCad 7+ format with full silkscreen,
fab, and courtyard layers — so the file opens cleanly with no "footprint not
in library" warnings, and the user gets a finished-looking layout.

Switch types:
- "soldered": Cherry MX PCB-mount, 2× signal pads (1.5 mm drill / 2.5 mm pad)
- "hotswap":  Kailh CPG151101S11 socket on B.Cu, with 3 mm NPTHs above on F.Cu
              for switch pin clearance + 2× SMD pads on B.Cu for the socket

Both share Cherry MX peg holes (1.75 mm at ±5.08, 0) and a 4 mm center stem.

Wiring matches the schematic: COL → SW.1 → SW.2 ↔ D.A → D.K → ROW (COL2ROW).
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable
from typing import Literal

from ..models.schemas import (
    MountingHoleDef,
    ParseResult,
    StabilizerDef,
    SwitchDef,
)
from .matrix import renumber_switches

KICAD_PCB_VERSION = "20240108"
DIODE_OFFSET_MM = 5.5
HEADER_GAP_MM = 12.0
TRACE_WIDTH_MM = 0.25
MCU_REF = "U1"
MCU_FOOTPRINT = "Module:Arduino_Pro_Micro"
PRO_MICRO_GPIO_PINS = [
    5, 6, 7, 8, 9, 10, 11, 12,
    13, 14, 15, 16, 17, 18, 19, 20,
    1, 2,
]

SwitchType = Literal["soldered", "hotswap"]
SWITCH_TYPES: tuple[SwitchType, ...] = ("soldered", "hotswap")


def _kicad_angle(svg_rotation_deg: float) -> float:
    """SVG and KiCad both render Y-down on screen, but SVG positive rotation
    is clockwise while KiCad positive rotation is counter-clockwise. Negate
    at every footprint emit boundary so the PCB visually matches the plate
    SVG. (Position math in helpers stays in SVG convention — it's only the
    angle written into `(at X Y A)` lines that needs flipping.) Normalize
    -0.0 → 0.0 so non-rotated footprints don't pick up a stray minus sign."""
    angle = -svg_rotation_deg
    return 0.0 if angle == 0.0 else angle
DiodeType = Literal["tht", "smd"]
DIODE_TYPES: tuple[DiodeType, ...] = ("tht", "smd")
StabilizerType = Literal["pcb_mount", "plate_mount"]
STABILIZER_TYPES: tuple[StabilizerType, ...] = ("pcb_mount", "plate_mount")


def generate_pcb(
    parse: ParseResult,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
    stabilizer_type: StabilizerType = "pcb_mount",
) -> str:
    if switch_type not in SWITCH_TYPES:
        raise ValueError(
            f"unknown switch_type: {switch_type!r} (expected one of {SWITCH_TYPES})"
        )
    if diode_type not in DIODE_TYPES:
        raise ValueError(
            f"unknown diode_type: {diode_type!r} (expected one of {DIODE_TYPES})"
        )
    if stabilizer_type not in STABILIZER_TYPES:
        raise ValueError(
            f"unknown stabilizer_type: {stabilizer_type!r} (expected one of {STABILIZER_TYPES})"
        )

    # Renumber switches to row-major order so PCB refdes (`SW{id}`/`D{id}`)
    # match the schematic's grid layout: top-left = SW1, bottom-right = SWN.
    switches = renumber_switches(list(parse.switches))
    nets = _enumerate_nets(switches)
    rows = sorted({s.row for s in switches})
    cols = sorted({s.col for s in switches})

    if len(rows) + len(cols) > len(PRO_MICRO_GPIO_PINS):
        raise ValueError(
            f"matrix has {len(rows) + len(cols)} row+col pins, but Pro Micro "
            f"only has {len(PRO_MICRO_GPIO_PINS)} GPIO pins available"
        )

    out: list[str] = []
    out.append("(kicad_pcb")
    out.append(f"\t(version {KICAD_PCB_VERSION})")
    out.append('\t(generator "keeb-layout-bot")')
    out.append("\t(general (thickness 1.6))")
    out.append('\t(paper "A4")')
    out.append(_layers_section())
    out.append(_setup_section())

    out.append('\t(net 0 "")')
    for name, code in nets.items():
        out.append(f'\t(net {code} "{name}")')

    for sw in sorted(switches, key=lambda s: s.id):
        out.append(_switch_footprint(sw, nets, switch_type))
        out.append(_diode_footprint(sw, nets, diode_type, switch_type))

    if switches:
        # Pro Micro footprint is 2 × 12 thru-hole, 17.78 mm wide. Anchor at
        # pin 1 (top-left of the module, which is the USB end on the
        # physical Pro Micro). User-controlled placement via parse.mcu_placement;
        # fall back to "off the right edge, vertically centered" for callers
        # that didn't populate the field (older clients, raw tests).
        if parse.mcu_placement is not None:
            mcu_x = parse.mcu_placement.cx_mm
            mcu_y = parse.mcu_placement.cy_mm
            mcu_rot = parse.mcu_placement.rotation_deg
        else:
            mcu_x = parse.svg_width_mm + HEADER_GAP_MM
            mcu_y = (parse.svg_height_mm - 11 * 2.54) / 2
            mcu_rot = 0.0
        out.append(_pro_micro_footprint(mcu_x, mcu_y, rows, cols, nets, mcu_rot))

    for switch, stabs in _pair_stabs_to_switches(switches, parse.stabilizers):
        if stabilizer_type == "pcb_mount":
            out.append(_stabilizer_pcb_mount(switch, stabs))
        else:
            out.append(_stabilizer_plate_mount(switch, stabs))

    for hole in parse.mounting_holes:
        out.append(_mounting_hole_footprint(hole))

    out.extend(_edge_cuts(parse.pcb_outline.path_d, parse.outline_grow_mm))

    out.append(")")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Nets
# ---------------------------------------------------------------------------


def _enumerate_nets(switches: Iterable[SwitchDef]) -> dict[str, int]:
    swlist = sorted(switches, key=lambda s: s.id)
    rows = sorted({s.row for s in swlist})
    cols = sorted({s.col for s in swlist})
    nets: dict[str, int] = {}
    code = 1
    for r in rows:
        nets[f"ROW{r}"] = code
        code += 1
    for c in cols:
        nets[f"COL{c}"] = code
        code += 1
    for sw in swlist:
        nets[f"NET-SW{sw.id}-D{sw.id}"] = code
        code += 1
    return nets


# ---------------------------------------------------------------------------
# Layers / setup
# ---------------------------------------------------------------------------


def _layers_section() -> str:
    return """\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(32 "B.Adhes" user "B.Adhesive")
\t\t(33 "F.Adhes" user "F.Adhesive")
\t\t(34 "B.Paste" user)
\t\t(35 "F.Paste" user)
\t\t(36 "B.SilkS" user)
\t\t(37 "F.SilkS" user)
\t\t(38 "B.Mask" user)
\t\t(39 "F.Mask" user)
\t\t(40 "Dwgs.User" user "User.Drawings")
\t\t(41 "Cmts.User" user "User.Comments")
\t\t(42 "Eco1.User" user "User.Eco1")
\t\t(43 "Eco2.User" user "User.Eco2")
\t\t(44 "Edge.Cuts" user)
\t\t(45 "Margin" user)
\t\t(46 "B.CrtYd" user)
\t\t(47 "F.CrtYd" user)
\t\t(48 "B.Fab" user)
\t\t(49 "F.Fab" user)
\t)"""


def _setup_section() -> str:
    return """\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t)"""


# ---------------------------------------------------------------------------
# Switch footprints (soldered + hotswap share most geometry)
# ---------------------------------------------------------------------------

# 1U keycap perimeter, drawn as 4 corner brackets so silkscreen doesn't run
# uninterrupted around the keycap edge (matches kbd-PCB convention).
_KEYCAP_BRACKETS: list[list[tuple[float, float]]] = [
    [(-9.525, -7.0), (-9.525, -9.525), (-7.0, -9.525)],
    [(7.0, -9.525), (9.525, -9.525), (9.525, -7.0)],
    [(9.525, 7.0), (9.525, 9.525), (7.0, 9.525)],
    [(-7.0, 9.525), (-9.525, 9.525), (-9.525, 7.0)],
]
_SWITCH_BODY = [(-7.0, -7.0), (7.0, -7.0), (7.0, 7.0), (-7.0, 7.0), (-7.0, -7.0)]
_KEYCAP_COURTYARD = [
    (-9.625, -9.625),
    (9.625, -9.625),
    (9.625, 9.625),
    (-9.625, 9.625),
    (-9.625, -9.625),
]
# Approximate Kailh socket bounding box (extends west and slightly south of
# the switch on B.Cu — modelled on MX_Alps_Hybrid B.CrtYd).
_SOCKET_COURTYARD = [
    (-8.5, -7.0),
    (6.5, -7.0),
    (6.5, -1.0),
    (-2.5, -1.0),
    (-2.5, 1.0),
    (-8.5, 1.0),
    (-8.5, -7.0),
]


def _switch_footprint(
    sw: SwitchDef, nets: dict[str, int], switch_type: SwitchType
) -> str:
    if switch_type == "soldered":
        return _switch_soldered(sw, nets)
    return _switch_hotswap(sw, nets)


def _switch_soldered(sw: SwitchDef, nets: dict[str, int]) -> str:
    fp_uuid = _u()
    ref = f"SW{sw.id}"
    col_net = nets[f"COL{sw.col}"]
    col_name = f"COL{sw.col}"
    link_net = nets[f"NET-SW{sw.id}-D{sw.id}"]
    link_name = f"NET-SW{sw.id}-D{sw.id}"
    rot = _kicad_angle(sw.rotation_deg)
    return (
        f'\t(footprint "Button_Switch_Keyboard:SW_Cherry_MX_1.00u_PCB"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {sw.cx_mm:.4f} {sw.cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "Cherry MX 1U keyswitch, PCB-mount, soldered")\n'
        f'\t\t(tags "Cherry MX Keyboard Keyswitch Switch PCB")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(ref, "SW_Push", "Button_Switch_Keyboard:SW_Cherry_MX_1.00u_PCB", rot)
        + _silk_keycap()
        + _fab_switch_body(rot)
        + _crtyd_keycap("F")
        + f'\t\t(pad "1" thru_hole circle (at -3.81 -2.54) (size 2.5 2.5) (drill 1.5)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (net {col_net} "{col_name}") (uuid "{_u()}"))\n'
        f'\t\t(pad "2" thru_hole circle (at 2.54 -5.08) (size 2.5 2.5) (drill 1.5)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (net {link_net} "{link_name}") (uuid "{_u()}"))\n'
        + _switch_npth()
        + "\t)"
    )


def _switch_hotswap(sw: SwitchDef, nets: dict[str, int]) -> str:
    fp_uuid = _u()
    ref = f"SW{sw.id}"
    col_net = nets[f"COL{sw.col}"]
    col_name = f"COL{sw.col}"
    link_net = nets[f"NET-SW{sw.id}-D{sw.id}"]
    link_name = f"NET-SW{sw.id}-D{sw.id}"
    rot = _kicad_angle(sw.rotation_deg)
    return (
        f'\t(footprint "Switch_Keyboard_Hotswap_Kailh:SW_Hotswap_Kailh_MX_1.00u"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {sw.cx_mm:.4f} {sw.cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "Cherry MX 1U keyswitch, Kailh CPG151101S11 hotswap socket on B.Cu")\n'
        f'\t\t(tags "Cherry MX Keyboard Hotswap Kailh")\n'
        f"\t\t(attr smd)\n"
        + _common_props(ref, "SW_Push", "Switch_Keyboard_Hotswap_Kailh:SW_Hotswap_Kailh_MX_1.00u", rot)
        + _silk_keycap()
        + _fab_switch_body(rot)
        + _crtyd_keycap("F")
        + _crtyd_socket("B")
        # Switch-pin clearance holes (3 mm — accommodates socket arm + switch pin).
        + f'\t\t(pad "" np_thru_hole circle (at -3.81 -2.54) (size 3 3) (drill 3)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        f'\t\t(pad "" np_thru_hole circle (at 2.54 -5.08) (size 3 3) (drill 3)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        + _switch_npth()
        # Kailh socket SMD pads on B.Cu (where the socket clips on).
        + f'\t\t(pad "1" smd rect (at -7.085 -2.54) (size 2.55 2.5)\n'
        f'\t\t\t(layers "B.Cu" "B.Paste" "B.Mask") (net {col_net} "{col_name}") (uuid "{_u()}"))\n'
        f'\t\t(pad "2" smd rect (at 5.842 -5.08) (size 2.55 2.5)\n'
        f'\t\t\t(layers "B.Cu" "B.Paste" "B.Mask") (net {link_net} "{link_name}") (uuid "{_u()}"))\n'
        + "\t)"
    )


def _switch_npth() -> str:
    """Center stem hole + two plastic peg holes — same on soldered and hotswap."""
    return (
        f'\t\t(pad "" np_thru_hole circle (at 0 0) (size 4 4) (drill 4)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        f'\t\t(pad "" np_thru_hole circle (at -5.08 0) (size 1.75 1.75) (drill 1.75)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        f'\t\t(pad "" np_thru_hole circle (at 5.08 0) (size 1.75 1.75) (drill 1.75)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
    )


def _common_props(
    ref: str,
    value: str,
    footprint: str,
    rot: float,
    side: str = "F",
    text_offset_y: float = 8.5,
) -> str:
    """Reference + Value + Footprint + Description properties.
    `side` is "F" for top-side parts or "B" for bottom-side parts; KiCad
    requires text properties to live on the same side as the footprint.
    `text_offset_y` is the distance from the part anchor to the Reference
    (above) and Value (below) text — tune per body height so the labels
    sit just outside the body."""
    silk = f"{side}.SilkS"
    fab = f"{side}.Fab"
    ref_y = -text_offset_y
    val_y = text_offset_y
    return (
        f'\t\t(property "Reference" "{ref}" (at 0 {ref_y} {rot:.3f}) (layer "{silk}") (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n"
        f'\t\t(property "Value" "{value}" (at 0 {val_y} {rot:.3f}) (layer "{fab}") (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n"
        f'\t\t(property "Footprint" "{footprint}" (at 0 0 0) (layer "{fab}") hide (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1.27 1.27))))\n"
        f'\t\t(property "Description" "Generated by keeb-layout-bot" (at 0 0 0) (layer "{fab}") hide (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1.27 1.27))))\n"
    )


def _silk_keycap() -> str:
    """Four corner brackets on F.SilkS that frame the 1U keycap."""
    out = []
    for bracket in _KEYCAP_BRACKETS:
        for a, b in zip(bracket, bracket[1:]):
            out.append(
                f"\t\t(fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
                f'(stroke (width 0.12) (type solid)) (layer "F.SilkS") '
                f'(uuid "{_u()}"))'
            )
    return "\n".join(out) + "\n"


def _fab_switch_body(rot: float) -> str:
    """14 × 14 mm switch body outline on F.Fab."""
    out = []
    for a, b in zip(_SWITCH_BODY, _SWITCH_BODY[1:]):
        out.append(
            f"\t\t(fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
            f'(stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "{_u()}"))'
        )
    out.append(
        f'\t\t(fp_text user "${{REFERENCE}}" (at 0 0 {rot:.3f}) (layer "F.Fab") '
        f'(uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))"
    )
    return "\n".join(out) + "\n"


def _crtyd_keycap(side: str) -> str:
    """1U keycap courtyard on F.CrtYd or B.CrtYd."""
    layer = f"{side}.CrtYd"
    out = []
    for a, b in zip(_KEYCAP_COURTYARD, _KEYCAP_COURTYARD[1:]):
        out.append(
            f"\t\t(fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
            f'(stroke (width 0.05) (type solid)) (layer "{layer}") '
            f'(uuid "{_u()}"))'
        )
    return "\n".join(out) + "\n"


def _crtyd_socket(side: str) -> str:
    """Kailh socket bounding-box courtyard on B.CrtYd."""
    layer = f"{side}.CrtYd"
    out = []
    for a, b in zip(_SOCKET_COURTYARD, _SOCKET_COURTYARD[1:]):
        out.append(
            f"\t\t(fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
            f'(stroke (width 0.05) (type solid)) (layer "{layer}") '
            f'(uuid "{_u()}"))'
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Diode / header / stabilizer / mounting hole footprints
# ---------------------------------------------------------------------------


def _diode_position(sw: SwitchDef) -> tuple[float, float]:
    """THT diode placed `DIODE_OFFSET_MM` along the switch's local +Y axis."""
    rot = math.radians(sw.rotation_deg)
    dx = -DIODE_OFFSET_MM * math.sin(rot)
    dy = DIODE_OFFSET_MM * math.cos(rot)
    return (sw.cx_mm + dx, sw.cy_mm + dy)


# SMD diode anchor in switch local coords: centered on the pad it connects
# to (switch pin 2 / hotswap socket pad 2). Hotswap shifts +6 mm in X to
# clear the larger Kailh socket pad on B.Cu.
SMD_DIODE_LOCAL_OFFSET = {
    "soldered": (2.54, -5.08),
    "hotswap": (8.54, -5.08),
}


def _smd_diode_position(sw: SwitchDef, switch_type: SwitchType) -> tuple[float, float]:
    """SMD diode global anchor: switch local pad-2 position rotated by the
    switch's own rotation and translated to the switch's center."""
    lx, ly = SMD_DIODE_LOCAL_OFFSET[switch_type]
    rot = math.radians(sw.rotation_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    return (
        sw.cx_mm + lx * cos_r - ly * sin_r,
        sw.cy_mm + lx * sin_r + ly * cos_r,
    )


def _diode_footprint(
    sw: SwitchDef,
    nets: dict[str, int],
    diode_type: DiodeType,
    switch_type: SwitchType,
) -> str:
    if diode_type == "tht":
        return _diode_footprint_tht(sw, nets)
    return _diode_footprint_smd(sw, nets, switch_type)


def _diode_footprint_tht(sw: SwitchDef, nets: dict[str, int]) -> str:
    fp_uuid = _u()
    ref = f"D{sw.id}"
    row_net = nets[f"ROW{sw.row}"]
    row_name = f"ROW{sw.row}"
    link_net = nets[f"NET-SW{sw.id}-D{sw.id}"]
    link_name = f"NET-SW{sw.id}-D{sw.id}"
    cx, cy = _diode_position(sw)
    rot = _kicad_angle(sw.rotation_deg)
    return (
        f'\t(footprint "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx:.4f} {cy:.4f} {rot:.3f})\n"
        f'\t\t(descr "1N4148 horizontal, 7.62mm pitch")\n'
        f'\t\t(tags "Diode")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(
            ref,
            "1N4148",
            "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal",
            rot,
            text_offset_y=1.8,
        )
        + f"\t\t(fp_line (start -1.5 -0.85) (end 1.5 -0.85) "
        f'(stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start 1.5 -0.85) (end 1.5 0.85) "
        f'(stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start 1.5 0.85) (end -1.5 0.85) "
        f'(stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start -1.5 0.85) (end -1.5 -0.85) "
        f'(stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start -1.0 -0.85) (end -1.0 0.85) "
        f'(stroke (width 0.2) (type solid)) (layer "F.SilkS") (uuid "{_u()}"))\n'
        f'\t\t(pad "1" thru_hole oval (at -3.81 0) (size 1.6 1.6) (drill 0.85)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (net {row_net} "{row_name}") (uuid "{_u()}"))\n'
        f'\t\t(pad "2" thru_hole oval (at 3.81 0) (size 1.6 1.6) (drill 0.85)\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (net {link_net} "{link_name}") (uuid "{_u()}"))\n'
        f"\t)"
    )


def _diode_footprint_smd(
    sw: SwitchDef, nets: dict[str, int], switch_type: SwitchType
) -> str:
    """SOD-123 SMD diode mounted on B.Cu, anchor centered on the switch
    pad it connects to (switch pin 2 for soldered; Kailh socket pad 2 for
    hotswap, with +6 mm X clearance from the larger SMD socket pad).
    Rotated 90° from the switch so its pads stack along the column axis."""
    fp_uuid = _u()
    ref = f"D{sw.id}"
    row_net = nets[f"ROW{sw.row}"]
    row_name = f"ROW{sw.row}"
    link_net = nets[f"NET-SW{sw.id}-D{sw.id}"]
    link_name = f"NET-SW{sw.id}-D{sw.id}"
    cx, cy = _smd_diode_position(sw, switch_type)
    rot = _kicad_angle((sw.rotation_deg + 90) % 360)
    return (
        f'\t(footprint "Diode_SMD:D_SOD-123"\n'
        f'\t\t(layer "B.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx:.4f} {cy:.4f} {rot:.3f})\n"
        f'\t\t(descr "1N4148 SMD, SOD-123 (B.Cu, under switch)")\n'
        f'\t\t(tags "Diode SMD SOD-123")\n'
        f"\t\t(attr smd)\n"
        + _common_props(
            ref,
            "1N4148",
            "Diode_SMD:D_SOD-123",
            rot,
            side="B",
            text_offset_y=1.8,
        )
        # Body outline on B.Fab (the diode is on the back of the board).
        + f"\t\t(fp_line (start -1.35 -0.8) (end 1.35 -0.8) "
        f'(stroke (width 0.1) (type solid)) (layer "B.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start 1.35 -0.8) (end 1.35 0.8) "
        f'(stroke (width 0.1) (type solid)) (layer "B.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start 1.35 0.8) (end -1.35 0.8) "
        f'(stroke (width 0.1) (type solid)) (layer "B.Fab") (uuid "{_u()}"))\n'
        f"\t\t(fp_line (start -1.35 0.8) (end -1.35 -0.8) "
        f'(stroke (width 0.1) (type solid)) (layer "B.Fab") (uuid "{_u()}"))\n'
        # Cathode bar on B.SilkS.
        f"\t\t(fp_line (start -0.8 -0.8) (end -0.8 0.8) "
        f'(stroke (width 0.2) (type solid)) (layer "B.SilkS") (uuid "{_u()}"))\n'
        # Pad 1 = K, pad 2 = A. Both on B.Cu.
        f'\t\t(pad "1" smd rect (at -1.65 0) (size 1.0 0.6)\n'
        f'\t\t\t(layers "B.Cu" "B.Paste" "B.Mask") (net {row_net} "{row_name}") (uuid "{_u()}"))\n'
        f'\t\t(pad "2" smd rect (at 1.65 0) (size 1.0 0.6)\n'
        f'\t\t\t(layers "B.Cu" "B.Paste" "B.Mask") (net {link_net} "{link_name}") (uuid "{_u()}"))\n'
        f"\t)"
    )


def _pro_micro_pin_to_net(
    pin: int, rows: list[int], cols: list[int], nets: dict[str, int]
) -> tuple[int, str] | None:
    """Map a Pro Micro pin number to its (net_code, net_name).

    Pin assignment matches the schematic: rows first, then cols, mapped
    onto PRO_MICRO_GPIO_PINS in order. Power (3, 4, 21, 23, 24) and RST
    (22) are left unconnected here — wire those manually if needed.
    """
    n_rows = len(rows)
    for i, p in enumerate(PRO_MICRO_GPIO_PINS):
        if p != pin:
            continue
        if i < n_rows:
            name = f"ROW{rows[i]}"
            return (nets[name], name)
        c_idx = i - n_rows
        if c_idx < len(cols):
            name = f"COL{cols[c_idx]}"
            return (nets[name], name)
        return None
    return None


def _pro_micro_footprint(
    cx_mm: float,
    cy_mm: float,
    rows: list[int],
    cols: list[int],
    nets: dict[str, int],
    rotation_deg: float = 0.0,
) -> str:
    """Inline Pro Micro footprint — 24 thru-hole pads in a 2 × 12 grid at
    2.54 mm pitch and 17.78 mm row spacing. Anchor at pin 1 (top-left in
    local frame, which is the USB end on the physical Pro Micro).
    Pins 1–12 down the left side; pins 13–24 up the right side (matching
    the physical board's pin numbering). `rotation_deg` is in SVG
    convention (CW positive); we negate to KiCad's CCW convention via
    `_kicad_angle` so the rendered footprint matches the plate."""
    fp_uuid = _u()
    pad_lines: list[str] = []
    for pin in range(1, 25):
        if pin <= 12:
            x = 0.0
            y = (pin - 1) * 2.54
        else:
            x = 17.78
            y = (24 - pin) * 2.54
        net_assignment = _pro_micro_pin_to_net(pin, rows, cols, nets)
        net_attr = ""
        if net_assignment is not None:
            net_code, net_name = net_assignment
            net_attr = f' (net {net_code} "{net_name}")'
        shape = "rect" if pin == 1 else "oval"
        pad_lines.append(
            f'\t\t(pad "{pin}" thru_hole {shape} (at {x:.4f} {y:.4f}) '
            f'(size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")'
            f'{net_attr} (uuid "{_u()}"))'
        )

    rot = _kicad_angle(rotation_deg)
    return (
        f'\t(footprint "{MCU_FOOTPRINT}"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx_mm:.4f} {cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "SparkFun Pro Micro - ATmega32U4 module, 24-pin DIP-style")\n'
        f'\t\t(tags "Pro Micro Arduino ATmega32U4")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(MCU_REF, "ProMicro", MCU_FOOTPRINT, rot)
        + "\n".join(pad_lines)
        + "\n\t)"
    )


def _header_footprint(
    cx_mm: float,
    cy_mm: float,
    rows: list[int],
    cols: list[int],
    nets: dict[str, int],
) -> str:
    n_pins = len(rows) + len(cols)
    fp_uuid = _u()
    ref = "J1"
    pin_lines: list[str] = []
    pin = 1
    for r in rows:
        net = nets[f"ROW{r}"]
        name = f"ROW{r}"
        shape = "rect" if pin == 1 else "oval"
        pin_lines.append(
            f'\t\t(pad "{pin}" thru_hole {shape} (at 0 {(pin - 1) * 2.54:.4f}) '
            f'(size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") '
            f'(net {net} "{name}") (uuid "{_u()}"))'
        )
        pin += 1
    for c in cols:
        net = nets[f"COL{c}"]
        name = f"COL{c}"
        pin_lines.append(
            f'\t\t(pad "{pin}" thru_hole oval (at 0 {(pin - 1) * 2.54:.4f}) '
            f'(size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") '
            f'(net {net} "{name}") (uuid "{_u()}"))'
        )
        pin += 1

    return (
        f'\t(footprint "Connector_PinHeader_2.54mm:PinHeader_1x{n_pins:02d}_P2.54mm_Vertical"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx_mm:.4f} {cy_mm:.4f} 0)\n"
        f'\t\t(descr "{n_pins}-pin header, 2.54mm pitch, vertical")\n'
        f'\t\t(tags "Connector Header 2.54mm")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(
            ref,
            f"Conn_01x{n_pins:02d}",
            f"Connector_PinHeader_2.54mm:PinHeader_1x{n_pins:02d}_P2.54mm_Vertical",
            0,
        )
        + "\n".join(pin_lines)
        + "\n\t)"
    )


# Canonical Cherry MX stabilizer geometry, per side, in switch-local coords
# (X = ±half_spacing, Y as below). Same pattern works for snap-in and screw-in
# PCB-mount stabs — the screw threads into the housing, not the PCB.
CHERRY_STAB_WIRE_OFFSET_Y_MM = 6.77
CHERRY_STAB_WIRE_HOLE_MM = 3.0
CHERRY_STAB_HOUSING_OFFSET_Y_MM = -8.24
CHERRY_STAB_HOUSING_HOLE_MM = 4.0
# Pair midpoints further than this from any switch are dropped (orphaned).
STAB_PAIRING_RADIUS_MM = 60.0
# Pair midpoint must coincide with the candidate switch's stem within this
# distance. Rotation-invariant — the midpoint of a real Cherry pair sits on
# the switch stem regardless of switch rotation.
STAB_PAIR_MIDPOINT_TOL_MM = 2.5
# The two cutouts in a pair must be equidistant from the switch within this
# slack. Also rotation-invariant.
STAB_PAIR_RADIAL_SYMMETRY_TOL_MM = 1.5
# Minimum separation between two paired cutouts — anything closer is the
# same cutout's bounding-box noise. Smallest real Cherry pair (2u) sits
# 23.876 mm apart, so 18 mm is a safe floor.
STAB_PAIR_MIN_SEPARATION_MM = 18.0


def _pair_stab_cutouts(
    stabs: list[StabilizerDef], switches: list[SwitchDef]
) -> list[tuple[StabilizerDef, ...]]:
    """Greedy mirror-symmetric pairing, rotation-invariant.

    Two cutouts pair if there's a switch S such that:
      - the cutouts' midpoint coincides with S's stem (within
        STAB_PAIR_MIDPOINT_TOL_MM)
      - the two cutouts are equidistant from S (within
        STAB_PAIR_RADIAL_SYMMETRY_TOL_MM)
      - they're separated by at least STAB_PAIR_MIN_SEPARATION_MM

    Both criteria are pure Euclidean distances → no dependence on the
    switch's rotation, so a stab pair around a 30° rotated thumb-cluster
    switch pairs the same way as an axis-aligned one.
    """
    if not stabs:
        return []
    used: set[int] = set()
    pairs: list[tuple[StabilizerDef, ...]] = []
    for i, s_a in enumerate(stabs):
        if i in used:
            continue
        best_j: int | None = None
        best_score = float("inf")
        for j, s_b in enumerate(stabs):
            if j == i or j in used:
                continue
            dx = s_a.cx_mm - s_b.cx_mm
            dy = s_a.cy_mm - s_b.cy_mm
            if (dx * dx + dy * dy) ** 0.5 < STAB_PAIR_MIN_SEPARATION_MM:
                continue
            mid_x = (s_a.cx_mm + s_b.cx_mm) / 2
            mid_y = (s_a.cy_mm + s_b.cy_mm) / 2
            sw = _nearest_switch(switches, mid_x, mid_y)
            if sw is None:
                continue
            mid_off = (
                (mid_x - sw.cx_mm) ** 2 + (mid_y - sw.cy_mm) ** 2
            ) ** 0.5
            if mid_off > STAB_PAIR_MIDPOINT_TOL_MM:
                continue
            d_a = (
                (s_a.cx_mm - sw.cx_mm) ** 2 + (s_a.cy_mm - sw.cy_mm) ** 2
            ) ** 0.5
            d_b = (
                (s_b.cx_mm - sw.cx_mm) ** 2 + (s_b.cy_mm - sw.cy_mm) ** 2
            ) ** 0.5
            if abs(d_a - d_b) > STAB_PAIR_RADIAL_SYMMETRY_TOL_MM:
                continue
            # Lower is better: tighter midpoint, more equal radii.
            score = mid_off + abs(d_a - d_b)
            if score < best_score:
                best_score = score
                best_j = j
        if best_j is not None:
            pairs.append((s_a, stabs[best_j]))
            used.add(i)
            used.add(best_j)
        else:
            pairs.append((s_a,))
            used.add(i)
    return pairs


def _nearest_switch(
    switches: list[SwitchDef], x: float, y: float
) -> SwitchDef | None:
    best: SwitchDef | None = None
    best_d2 = float("inf")
    for sw in switches:
        d2 = (sw.cx_mm - x) ** 2 + (sw.cy_mm - y) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = sw
    return best


def _pair_stabs_to_switches(
    switches: list[SwitchDef], stabs: list[StabilizerDef]
) -> list[tuple[SwitchDef, list[StabilizerDef]]]:
    """For each stab group (paired cutouts or orphan), anchor the assembly
    on the switch nearest the group's centroid. Pairing cutouts first avoids
    splitting a real pair across two switches when the row is sparse (e.g.
    a 6.25u spacebar whose right wire is closer to a switch in the row above
    than to the spacebar itself)."""
    if not stabs or not switches:
        return []
    grouped: dict[int, list[StabilizerDef]] = {}
    radius_sq = STAB_PAIRING_RADIUS_MM * STAB_PAIRING_RADIUS_MM
    for group in _pair_stab_cutouts(stabs, switches):
        mid_x = sum(s.cx_mm for s in group) / len(group)
        mid_y = sum(s.cy_mm for s in group) / len(group)
        best_sw = _nearest_switch(switches, mid_x, mid_y)
        if best_sw is None:
            continue
        d2 = (mid_x - best_sw.cx_mm) ** 2 + (mid_y - best_sw.cy_mm) ** 2
        if d2 <= radius_sq:
            grouped.setdefault(best_sw.id, []).extend(group)
    by_id = {sw.id: sw for sw in switches}
    return [(by_id[sw_id], stabs_for) for sw_id, stabs_for in grouped.items()]


def _stab_sides(switch: SwitchDef, stabs: list[StabilizerDef]) -> list[float]:
    """For each cutout, compute the signed X offset from switch stem in
    switch-local coords. Sides are sorted left-then-right."""
    rot = math.radians(switch.rotation_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    sides: list[float] = []
    for stab in stabs:
        dx = stab.cx_mm - switch.cx_mm
        dy = stab.cy_mm - switch.cy_mm
        # Inverse rotation: world → switch-local.
        local_x = dx * cos_r + dy * sin_r
        sides.append(local_x)
    return sorted(sides)


def _rotate_local_to_world(
    local_x: float, local_y: float, switch: SwitchDef
) -> tuple[float, float]:
    rot = math.radians(switch.rotation_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    return (
        switch.cx_mm + local_x * cos_r - local_y * sin_r,
        switch.cy_mm + local_x * sin_r + local_y * cos_r,
    )


def _stabilizer_pcb_mount(
    switch: SwitchDef, stabs: list[StabilizerDef]
) -> str:
    """Cherry MX PCB-mount stabilizer assembly anchored on the switch stem.
    Emits per side (one per detected cutout): a 3 mm wire-clearance NPTH and
    a 4 mm housing-post NPTH at the canonical Cherry offsets. Side X offsets
    come from the detected cutouts so any key size (2u, 6.25u, 7u, ...) works
    without a lookup table."""
    fp_uuid = _u()
    ref = f"ST{switch.id}"
    rot = _kicad_angle(switch.rotation_deg)
    sides = _stab_sides(switch, stabs)
    side_label = "+".join(f"{x:+.2f}" for x in sides)

    pads = []
    for side_x in sides:
        # Wire-clearance hole.
        pads.append(
            f'\t\t(pad "" np_thru_hole circle '
            f"(at {side_x:.4f} {CHERRY_STAB_WIRE_OFFSET_Y_MM:.4f}) "
            f"(size {CHERRY_STAB_WIRE_HOLE_MM} {CHERRY_STAB_WIRE_HOLE_MM}) "
            f"(drill {CHERRY_STAB_WIRE_HOLE_MM}) "
            f'(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        )
        # Housing-post hole.
        pads.append(
            f'\t\t(pad "" np_thru_hole circle '
            f"(at {side_x:.4f} {CHERRY_STAB_HOUSING_OFFSET_Y_MM:.4f}) "
            f"(size {CHERRY_STAB_HOUSING_HOLE_MM} {CHERRY_STAB_HOUSING_HOLE_MM}) "
            f"(drill {CHERRY_STAB_HOUSING_HOLE_MM}) "
            f'(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        )

    return (
        f'\t(footprint "keeb-layout-bot:Stabilizer_PCB_Mount"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {switch.cx_mm:.4f} {switch.cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "Cherry MX PCB-mount stabilizer for SW{switch.id} '
        f'(sides at X = {side_label} mm)")\n'
        f'\t\t(tags "Stabilizer PCB-Mount Cherry")\n'
        f"\t\t(attr exclude_from_pos_files exclude_from_bom)\n"
        + _common_props(
            ref,
            "Stab_PCB_Mount",
            "keeb-layout-bot:Stabilizer_PCB_Mount",
            rot,
            text_offset_y=12.0,
        )
        + "".join(pads)
        + "\t)"
    )


def _stabilizer_plate_mount(
    switch: SwitchDef, stabs: list[StabilizerDef]
) -> str:
    """Plate-mount stabilizer assembly: no drills (the stab clips into the
    plate). Emits a footprint-keepout zone on F.Cu spanning both stab sides
    so no other footprint can sit under the housing. Tracks, vias, pads, and
    copper pour remain allowed so signal routing can still pass through."""
    fp_uuid = _u()
    zone_uuid = _u()
    ref = f"ST{switch.id}"
    rot = _kicad_angle(switch.rotation_deg)
    sides = _stab_sides(switch, stabs)
    if not sides:
        return ""
    side_label = "+".join(f"{x:+.2f}" for x in sides)
    margin = 4.0
    left_local = min(sides) - margin
    right_local = max(sides) + margin
    # Span the wire and housing extents on the Y axis with a small pad.
    top_local = CHERRY_STAB_HOUSING_OFFSET_Y_MM - 2.0
    bot_local = CHERRY_STAB_WIRE_OFFSET_Y_MM + 2.0

    # KiCad zone polygons take world-frame coordinates (no per-footprint
    # rotation), so map the four local corners through the switch rotation.
    corners = [
        (left_local, top_local),
        (right_local, top_local),
        (right_local, bot_local),
        (left_local, bot_local),
    ]
    world_pts = [_rotate_local_to_world(x, y, switch) for x, y in corners]
    zone_pts = "".join(
        f"\t\t\t\t\t(xy {wx:.4f} {wy:.4f})\n" for wx, wy in world_pts
    )

    return (
        f'\t(footprint "keeb-layout-bot:Stabilizer_Plate_Mount"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {switch.cx_mm:.4f} {switch.cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "Plate-mount stabilizer for SW{switch.id} — F.Cu '
        f'footprint keepout, no drills (sides at X = {side_label} mm)")\n'
        f'\t\t(tags "Stabilizer Plate-Mount Cherry Keepout")\n'
        f"\t\t(attr exclude_from_pos_files exclude_from_bom)\n"
        + _common_props(
            ref,
            "Stab_Plate_Mount",
            "keeb-layout-bot:Stabilizer_Plate_Mount",
            rot,
            text_offset_y=12.0,
        )
        + f'\t\t(zone (net 0) (net_name "") (layer "F.Cu") (uuid "{zone_uuid}")\n'
        f'\t\t\t(name "Stab_Keepout_SW{switch.id}")\n'
        f"\t\t\t(hatch edge 0.5)\n"
        f"\t\t\t(connect_pads (clearance 0))\n"
        f"\t\t\t(min_thickness 0.25)\n"
        f"\t\t\t(filled_areas_thickness no)\n"
        f"\t\t\t(keepout\n"
        f"\t\t\t\t(tracks allowed)\n"
        f"\t\t\t\t(vias allowed)\n"
        f"\t\t\t\t(pads allowed)\n"
        f"\t\t\t\t(copperpour allowed)\n"
        f"\t\t\t\t(footprints not_allowed)\n"
        f"\t\t\t)\n"
        f"\t\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        f"\t\t\t(polygon\n"
        f"\t\t\t\t(pts\n"
        f"{zone_pts}"
        f"\t\t\t\t)\n"
        f"\t\t\t)\n"
        f"\t\t)\n"
        + "\t)"
    )


def _mounting_hole_footprint(hole: MountingHoleDef) -> str:
    fp_uuid = _u()
    drill = max(hole.diameter_mm, 1.5)
    pad = drill + 0.5
    ref = f"MH{hole.id}"
    # Mounting holes are pure mechanical features — hide Reference and Value
    # silk so the board doesn't get cluttered with "MH1 / MountingHole" labels
    # around every drill. KiCad still requires the properties to exist.
    return (
        f'\t(footprint "MountingHole:MountingHole_{drill:.1f}mm"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {hole.cx_mm:.4f} {hole.cy_mm:.4f} 0)\n"
        f'\t\t(descr "Mounting hole, {drill:.2f}mm diameter")\n'
        f'\t\t(tags "Mounting Hole")\n'
        f'\t\t(attr exclude_from_pos_files exclude_from_bom)\n'
        f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS") hide (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n"
        f'\t\t(property "Value" "MountingHole" (at 0 0 0) (layer "F.Fab") hide (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n"
        f'\t\t(property "Footprint" "MountingHole:MountingHole_{drill:.1f}mm" (at 0 0 0) (layer "F.Fab") hide (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1.27 1.27))))\n"
        f'\t\t(pad "" np_thru_hole circle (at 0 0) (size {pad:.2f} {pad:.2f}) (drill {drill:.2f})\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        f"\t)"
    )


# ---------------------------------------------------------------------------
# Edge cuts
# ---------------------------------------------------------------------------


_PATH_TOKEN = re.compile(r"([MLZ])\s*([-\d.]+)?\s*([-\d.]+)?", re.IGNORECASE)


def _edge_cuts(path_d: str, grow_mm: float = 0.0) -> list[str]:
    """Emit Edge.Cuts gr_line segments from the outline path. When
    `grow_mm > 0`, Minkowski-dilate the polygon via Shapely's mitered
    buffer so a rectangular plate stays rectangular."""
    points = _parse_path_points(path_d)
    if len(points) < 2:
        return []
    if grow_mm > 0:
        points = _grow_polygon_points(points, grow_mm)
        if len(points) < 2:
            return []
    lines: list[str] = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        lines.append(
            f"\t(gr_line (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) "
            f'(stroke (width 0.15) (type solid)) (layer "Edge.Cuts") '
            f'(uuid "{_u()}"))'
        )
    return lines


def _grow_polygon_points(
    points: list[tuple[float, float]], grow_mm: float
) -> list[tuple[float, float]]:
    """Dilate the outline polygon by `grow_mm` using Shapely's mitered
    buffer. Returns a closed loop of points (first == last) so the
    gr_line emission still produces a closed Edge.Cuts boundary."""
    from shapely.geometry import Polygon

    # Drop the closing duplicate (M..L..L..Z conventionally has the closing
    # point repeated) — Shapely's Polygon takes an open ring.
    ring = points[:-1] if points[0] == points[-1] else points
    if len(ring) < 3:
        return points
    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)
    grown = poly.buffer(grow_mm, join_style=2)  # mitered corners
    exterior = list(grown.exterior.coords)
    return [(round(x, 4), round(y, 4)) for x, y in exterior]


def _parse_path_points(path_d: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    start: tuple[float, float] | None = None
    for cmd, x, y in _PATH_TOKEN.findall(path_d):
        cmd_u = cmd.upper()
        if cmd_u in ("M", "L"):
            if x is None or y is None:
                continue
            pt = (float(x), float(y))
            points.append(pt)
            if cmd_u == "M":
                start = pt
        elif cmd_u == "Z" and start is not None and points and points[-1] != start:
            points.append(start)
    return points


def _u() -> str:
    return str(uuid.uuid4())


