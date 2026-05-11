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
DiodeType = Literal["tht", "smd"]
DIODE_TYPES: tuple[DiodeType, ...] = ("tht", "smd")


def generate_pcb(
    parse: ParseResult,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
) -> str:
    if switch_type not in SWITCH_TYPES:
        raise ValueError(
            f"unknown switch_type: {switch_type!r} (expected one of {SWITCH_TYPES})"
        )
    if diode_type not in DIODE_TYPES:
        raise ValueError(
            f"unknown diode_type: {diode_type!r} (expected one of {DIODE_TYPES})"
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
        # pin 1 (top-left of the module). Place it off the right edge of
        # the plate, vertically centered.
        header_x = parse.svg_width_mm + HEADER_GAP_MM
        header_y = (parse.svg_height_mm - 11 * 2.54) / 2
        out.append(_pro_micro_footprint(header_x, header_y, rows, cols, nets))

    for stab in parse.stabilizers:
        out.append(_stabilizer_footprint(stab))

    for hole in parse.mounting_holes:
        out.append(_mounting_hole_footprint(hole))

    out.extend(_edge_cuts(parse.pcb_outline.path_d))

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
    rot = sw.rotation_deg
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
    rot = sw.rotation_deg
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
    ref: str, value: str, footprint: str, rot: float, side: str = "F"
) -> str:
    """Reference + Value + Footprint + Description properties.
    `side` is "F" for top-side parts or "B" for bottom-side parts; KiCad
    requires text properties to live on the same side as the footprint."""
    silk = f"{side}.SilkS"
    fab = f"{side}.Fab"
    return (
        f'\t\t(property "Reference" "{ref}" (at 0 -8.5 {rot:.3f}) (layer "{silk}") (uuid "{_u()}")\n'
        f"\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n"
        f'\t\t(property "Value" "{value}" (at 0 8.5 {rot:.3f}) (layer "{fab}") (uuid "{_u()}")\n'
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
    rot = sw.rotation_deg
    return (
        f'\t(footprint "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx:.4f} {cy:.4f} {rot:.3f})\n"
        f'\t\t(descr "1N4148 horizontal, 7.62mm pitch")\n'
        f'\t\t(tags "Diode")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(ref, "1N4148", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal", rot)
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
    rot = (sw.rotation_deg + 90) % 360
    return (
        f'\t(footprint "Diode_SMD:D_SOD-123"\n'
        f'\t\t(layer "B.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx:.4f} {cy:.4f} {rot:.3f})\n"
        f'\t\t(descr "1N4148 SMD, SOD-123 (B.Cu, under switch)")\n'
        f'\t\t(tags "Diode SMD SOD-123")\n'
        f"\t\t(attr smd)\n"
        + _common_props(ref, "1N4148", "Diode_SMD:D_SOD-123", rot, side="B")
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
) -> str:
    """Inline Pro Micro footprint — 24 thru-hole pads in a 2 × 12 grid at
    2.54 mm pitch and 17.78 mm row spacing. Anchor at pin 1 (top-left).
    Pins 1–12 down the left side; pins 13–24 up the right side (matching
    the physical board's pin numbering)."""
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

    return (
        f'\t(footprint "{MCU_FOOTPRINT}"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {cx_mm:.4f} {cy_mm:.4f} 0)\n"
        f'\t\t(descr "SparkFun Pro Micro - ATmega32U4 module, 24-pin DIP-style")\n'
        f'\t\t(tags "Pro Micro Arduino ATmega32U4")\n'
        f"\t\t(attr through_hole)\n"
        + _common_props(MCU_REF, "ProMicro", MCU_FOOTPRINT, 0)
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


def _stabilizer_footprint(stab: StabilizerDef) -> str:
    """Emit the stabilizer cutout exactly as the plate SVG describes it.

    No fixed-geometry pads — we trust the plate designer placed each cutout
    where they want it. The footprint contains a single Edge.Cuts rectangle
    sized to the detected (width_mm × height_mm) at (cx_mm, cy_mm) with the
    detected rotation, so KiCad mills the same hole the plate has.
    """
    fp_uuid = _u()
    ref = f"ST{stab.id}"
    rot = stab.rotation_deg
    half_long = stab.width_mm / 2
    half_short = stab.height_mm / 2
    corners = [
        (-half_long, -half_short),
        (half_long, -half_short),
        (half_long, half_short),
        (-half_long, half_short),
    ]
    closed = list(zip(corners, corners[1:] + corners[:1]))
    edge_lines = "".join(
        f"\t\t(fp_line (start {a[0]:.4f} {a[1]:.4f}) (end {b[0]:.4f} {b[1]:.4f}) "
        f'(stroke (width 0.15) (type solid)) (layer "Edge.Cuts") (uuid "{_u()}"))\n'
        for a, b in closed
    )
    return (
        f'\t(footprint "keeb-layout-bot:Stabilizer_Cutout"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {stab.cx_mm:.4f} {stab.cy_mm:.4f} {rot:.3f})\n"
        f'\t\t(descr "Stabilizer cutout from plate SVG: {stab.width_mm:.2f} × {stab.height_mm:.2f} mm")\n'
        f'\t\t(tags "Stabilizer Cutout Plate")\n'
        f"\t\t(attr exclude_from_pos_files exclude_from_bom)\n"
        + _common_props(
            ref,
            f"Cutout_{stab.width_mm:.1f}x{stab.height_mm:.1f}",
            "keeb-layout-bot:Stabilizer_Cutout",
            rot,
        )
        + edge_lines
        + "\t)"
    )


def _mounting_hole_footprint(hole: MountingHoleDef) -> str:
    fp_uuid = _u()
    drill = max(hole.diameter_mm, 1.5)
    pad = drill + 0.5
    ref = f"MH{hole.id}"
    return (
        f'\t(footprint "MountingHole:MountingHole_{drill:.1f}mm"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{fp_uuid}")\n'
        f"\t\t(at {hole.cx_mm:.4f} {hole.cy_mm:.4f} 0)\n"
        f'\t\t(descr "Mounting hole, {drill:.2f}mm diameter")\n'
        f'\t\t(tags "Mounting Hole")\n'
        + _common_props(ref, "MountingHole", f"MountingHole:MountingHole_{drill:.1f}mm", 0)
        + f'\t\t(pad "" np_thru_hole circle (at 0 0) (size {pad:.2f} {pad:.2f}) (drill {drill:.2f})\n'
        f'\t\t\t(layers "*.Cu" "*.Mask") (uuid "{_u()}"))\n'
        f"\t)"
    )


# ---------------------------------------------------------------------------
# Edge cuts
# ---------------------------------------------------------------------------


_PATH_TOKEN = re.compile(r"([MLZ])\s*([-\d.]+)?\s*([-\d.]+)?", re.IGNORECASE)


def _edge_cuts(path_d: str) -> list[str]:
    points = _parse_path_points(path_d)
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


