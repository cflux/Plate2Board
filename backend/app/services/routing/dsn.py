"""Specctra DSN exporter for the keeb-layout-bot PCB.

Freerouting consumes Specctra DSN to discover the board outline, pad
positions, and netlist; it returns a Specctra SES with routed wires + vias
which we splice back into the kicad_pcb.

We don't round-trip via KiCad — we own the kicad_pcb emitter so we can
re-derive pad world positions directly from the same `ParseResult` +
constants that `pcb.py` uses, keeping DSN and kicad_pcb perfectly aligned.

Coordinate convention
---------------------
Specctra is a Y-up (math) coordinate system; KiCad is Y-down (screen).
freerouting uses the input DSN coordinates literally — it doesn't know we
have a Y-down source — so any rotation we hand it is applied as
CCW-positive in Y-up space. Match KiCad's own DSN exporter: Y-flip every
coordinate on emit (placement, image pin locals, boundary, keepouts) and
emit the placement rotation *unchanged*. The angle survives the flip
because conjugating KiCad's Y-down rotation matrix by the Y-flip yields
the standard CCW matrix with the same angle: F·R_k(θ)·F = R_m(θ). (An
earlier revision negated the rotation here, which mirror-rotated every
footprint in freerouting's view — self-consistently, so routing
"succeeded" but the spliced wires missed the real pads on any rotated
layout.) The SES parser re-flips Y on every wire / via so the splice
into kicad_pcb lands on the original Y-down coordinates.

All components are placed side=front, even those whose copper lives on
B.Cu (hotswap sockets, SMD diodes) — the padstack layer list pins the
copper to the right layer, and side=back would make freerouting mirror
the image (KiCad's exporter compensates with a 180−θ angle because real
KiCad files store back-side footprints pre-mirrored; our generator emits
WYSIWYG coords, so no mirror must be applied).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ...models.schemas import ParseResult, SwitchDef
from ..pcb import (
    CHERRY_STAB_HOUSING_HOLE_MM,
    CHERRY_STAB_HOUSING_OFFSET_Y_MM,
    CHERRY_STAB_WIRE_HOLE_MM,
    CHERRY_STAB_WIRE_OFFSET_Y_MM,
    DiodeType,
    MCU_REF,
    PRO_MICRO_GPIO_PINS,
    SMD_DIODE_LOCAL_OFFSET,
    StabilizerType,
    SwitchType,
    _diode_position,
    _enumerate_nets,
    _kicad_angle,
    _pair_stabs_to_switches,
    _parse_path_points,
    _rotate_local_to_world,
    _smd_diode_position,
    _stab_sides,
    center_parse_on_page,
)

# Resolution = um * 10  → coords emitted as integer 0.1 µm units (KiCad's
# default Specctra precision). Freerouting routes these correctly and
# quickly. NOTE: freerouting normalises internally to a finer scale and
# the SES it emits encodes coordinates at 10× the integer values it would
# need for the declared `um 10` resolution — the SES parser compensates
# (see `SES_FREEROUTING_SCALE_QUIRK`).
DSN_RESOLUTION_UNIT = "um"
DSN_RESOLUTION = 10
# mm × this_factor → integer DSN coordinate.
DSN_MM_FACTOR = 10_000  # 1 mm = 10,000 ticks of (0.1 µm)

# Default routing rules (mirrored from the Matrix netclass in project.py).
DEFAULT_CLEARANCE_MM = 0.2
DEFAULT_TRACK_WIDTH_MM = 0.25
MATRIX_TRACK_WIDTH_MM = 0.30

# Standard via for the 2-layer Matrix class: 0.6 mm pad, 0.3 mm drill.
VIA_PAD_DIAMETER_MM = 0.6
VIA_DRILL_DIAMETER_MM = 0.3

# Layer naming matches kicad_pcb so SES wires reference the same layer
# tokens we already emit there.
LAYER_F_CU = "F.Cu"
LAYER_B_CU = "B.Cu"
LAYERS_SIGNAL = (LAYER_F_CU, LAYER_B_CU)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Padstack:
    """One padstack per (shape, size, layers) tuple. Referenced from images."""
    name: str
    # ``circle``/``rect``/``oval`` — we map oval to circle since freerouting
    # doesn't have a native oval primitive and our ovals are square (1.7×1.7
    # or 1.6×1.6, so a circle of equal diameter is a strict superset).
    kind: str
    # Width / height in mm. For circles, width == height == diameter.
    w_mm: float
    h_mm: float
    # Layers the copper appears on: through-hole pads on both signal layers,
    # SMD pads on one. Drill is implied by kind=="thru" — we don't emit drill
    # geometry to DSN because freerouting only routes signal copper.
    layers: tuple[str, ...] = LAYERS_SIGNAL


@dataclass
class ImagePin:
    """Pin within a component-local image: drives both pad geometry (via
    padstack ref) and net topology (via the pin number).
    """
    number: str
    padstack: Padstack
    local_x: float
    local_y: float


@dataclass
class Image:
    """Footprint template. One per distinct footprint type we emit."""
    name: str
    pins: list[ImagePin] = field(default_factory=list)


@dataclass
class Component:
    """One per footprint instance. Net assignments per pin live in `nets`
    so the same image can serve every soldered-switch instance (each with
    its own ROW/COL/LINK assignments)."""
    image_name: str
    ref: str
    place_x: float
    place_y: float
    rotation_deg: float
    side: str = "front"
    # Maps pin number → net name (omitted pins are unconnected, e.g. MCU
    # power pins or NPTHs).
    nets: dict[str, str] = field(default_factory=dict)


@dataclass
class KeepoutCircle:
    """Per-board keepout for NPTHs the router must avoid. Freerouting honours
    these on signal layers."""
    diameter_mm: float
    cx_mm: float
    cy_mm: float


# ---------------------------------------------------------------------------
# Padstacks + Images
# ---------------------------------------------------------------------------

# Padstack defs reused across all images. Names use the canonical
# "Round_<diameter>" / "Smd_<W>x<H>" pattern so DSN debugging is easy.
PS_SW_TH = Padstack("Round_2p5_TH", "circle", 2.5, 2.5)
PS_SW_HOTSWAP = Padstack("Smd_2p55x2p5_B", "rect", 2.55, 2.5, layers=(LAYER_B_CU,))
PS_DIODE_TH = Padstack("Round_1p6_TH", "circle", 1.6, 1.6)
PS_DIODE_SMD = Padstack("Smd_1p0x0p6_B", "rect", 1.0, 0.6, layers=(LAYER_B_CU,))
PS_PROMICRO_TH = Padstack("Round_1p7_TH", "circle", 1.7, 1.7)

ALL_PADSTACKS: tuple[Padstack, ...] = (
    PS_SW_TH,
    PS_SW_HOTSWAP,
    PS_DIODE_TH,
    PS_DIODE_SMD,
    PS_PROMICRO_TH,
)


def _switch_soldered_image() -> Image:
    """Pads match `_switch_soldered` in pcb.py exactly."""
    return Image(
        name="sw_cherry_mx_soldered",
        pins=[
            ImagePin("1", PS_SW_TH, -3.81, -2.54),
            ImagePin("2", PS_SW_TH, 2.54, -5.08),
        ],
    )


def _switch_hotswap_image() -> Image:
    return Image(
        name="sw_cherry_mx_hotswap",
        pins=[
            ImagePin("1", PS_SW_HOTSWAP, -7.085, -2.54),
            ImagePin("2", PS_SW_HOTSWAP, 5.842, -5.08),
        ],
    )


def _diode_tht_image() -> Image:
    return Image(
        name="d_diode_tht",
        pins=[
            ImagePin("1", PS_DIODE_TH, -3.81, 0.0),
            ImagePin("2", PS_DIODE_TH, 3.81, 0.0),
        ],
    )


def _diode_smd_image() -> Image:
    return Image(
        name="d_diode_smd",
        pins=[
            ImagePin("1", PS_DIODE_SMD, -1.65, 0.0),
            ImagePin("2", PS_DIODE_SMD, 1.65, 0.0),
        ],
    )


def _pro_micro_image() -> Image:
    """24-pin 2×12 thru-hole grid, mirrors `_pro_micro_footprint` in pcb.py."""
    pins: list[ImagePin] = []
    for pin in range(1, 25):
        if pin <= 12:
            x = 0.0
            y = (pin - 1) * 2.54
        else:
            x = 17.78
            y = (24 - pin) * 2.54
        pins.append(ImagePin(str(pin), PS_PROMICRO_TH, x, y))
    return Image(name="u_pro_micro", pins=pins)


# Map image name → builder so the assembler only emits images it actually uses.
IMAGE_BUILDERS = {
    "sw_cherry_mx_soldered": _switch_soldered_image,
    "sw_cherry_mx_hotswap": _switch_hotswap_image,
    "d_diode_tht": _diode_tht_image,
    "d_diode_smd": _diode_smd_image,
    "u_pro_micro": _pro_micro_image,
}


def _switch_image_name(switch_type: SwitchType) -> str:
    return "sw_cherry_mx_soldered" if switch_type == "soldered" else "sw_cherry_mx_hotswap"


def _diode_image_name(diode_type: DiodeType) -> str:
    return "d_diode_tht" if diode_type == "tht" else "d_diode_smd"


# ---------------------------------------------------------------------------
# Component + pad collection
# ---------------------------------------------------------------------------


def _switch_components(
    parse: ParseResult,
    nets: dict[str, int],
    switch_type: SwitchType,
) -> list[Component]:
    image = _switch_image_name(switch_type)
    out: list[Component] = []
    for sw in parse.switches:
        out.append(
            Component(
                image_name=image,
                ref=f"SW{sw.id}",
                place_x=sw.cx_mm,
                place_y=sw.cy_mm,
                rotation_deg=_kicad_angle(sw.rotation_deg),
                nets={
                    "1": f"COL{sw.col}",
                    "2": f"NET-SW{sw.id}-D{sw.id}",
                },
            )
        )
    return out


def _diode_components(
    parse: ParseResult,
    nets: dict[str, int],
    diode_type: DiodeType,
    switch_type: SwitchType,
) -> list[Component]:
    image = _diode_image_name(diode_type)
    out: list[Component] = []
    for sw in parse.switches:
        if diode_type == "tht":
            cx, cy = _diode_position(sw)
            rot = _kicad_angle(sw.rotation_deg)
        else:
            cx, cy = _smd_diode_position(sw, switch_type)
            rot = _kicad_angle((sw.rotation_deg + 90) % 360)
        out.append(
            Component(
                image_name=image,
                ref=f"D{sw.id}",
                place_x=cx,
                place_y=cy,
                rotation_deg=rot,
                # SMD diode copper is on B.Cu via its padstack's layer list;
                # the place stays side=front because pcb.py emits the B.Cu
                # footprint WYSIWYG (no mirrored frame), while a DSN back-
                # side place would make freerouting mirror the pin locations
                # across the Y axis — swapping pads 1 and 2.
                side="front",
                nets={
                    "1": f"ROW{sw.row}",
                    "2": f"NET-SW{sw.id}-D{sw.id}",
                },
            )
        )
    return out


def _mcu_components(parse: ParseResult, nets: dict[str, int]) -> list[Component]:
    if parse.mcu_placement is None:
        return []
    m = parse.mcu_placement
    rows = sorted({s.row for s in parse.switches})
    cols = sorted({s.col for s in parse.switches})
    nets_per_pin: dict[str, str] = {}
    n_rows = len(rows)
    for i, pin in enumerate(PRO_MICRO_GPIO_PINS):
        if i < n_rows:
            nets_per_pin[str(pin)] = f"ROW{rows[i]}"
        else:
            c_idx = i - n_rows
            if c_idx < len(cols):
                nets_per_pin[str(pin)] = f"COL{cols[c_idx]}"
    return [
        Component(
            image_name="u_pro_micro",
            ref=MCU_REF,
            place_x=m.cx_mm,
            place_y=m.cy_mm,
            rotation_deg=_kicad_angle(m.rotation_deg),
            nets=nets_per_pin,
        )
    ]


# ---------------------------------------------------------------------------
# Keepouts (NPTH holes the router must avoid)
# ---------------------------------------------------------------------------


# Switch NPTHs in footprint-local mm: center stem (4 mm) + two peg holes
# (1.75 mm at ±5.08). Mirrors pcb._switch_npth.
SWITCH_NPTH_LOCAL = (
    (0.0, 0.0, 4.0),
    (-5.08, 0.0, 1.75),
    (5.08, 0.0, 1.75),
)
# Hotswap adds two 3 mm switch-pin clearance holes at the THT pin
# positions (mirrors pcb._switch_hotswap).
HOTSWAP_PIN_NPTH_LOCAL = (
    (-3.81, -2.54, 3.0),
    (2.54, -5.08, 3.0),
)


def _switch_npth_keepouts(
    parse: ParseResult, switch_type: SwitchType
) -> list[KeepoutCircle]:
    """All NPTHs of every switch footprint, rotated to world coords with
    `_rotate_local_to_world` — the same convention pcb.py uses, so each
    keepout sits exactly over the hole KiCad drills."""
    locals_ = SWITCH_NPTH_LOCAL
    if switch_type == "hotswap":
        locals_ = locals_ + HOTSWAP_PIN_NPTH_LOCAL
    out: list[KeepoutCircle] = []
    for sw in parse.switches:
        for lx, ly, d in locals_:
            wx, wy = _rotate_local_to_world(lx, ly, sw)
            out.append(KeepoutCircle(d, wx, wy))
    return out


def _stabilizer_keepouts(
    parse: ParseResult,
    stab_type: StabilizerType,
) -> list[KeepoutCircle]:
    """PCB-mount stabs add 4 NPTHs per side; plate-mount stabs don't drill."""
    if stab_type != "pcb_mount" or not parse.switches or not parse.stabilizers:
        return []
    out: list[KeepoutCircle] = []
    pairs = _pair_stabs_to_switches(parse.switches, parse.stabilizers)
    by_sw: dict[int, list] = {}
    for sw, stabs in pairs:
        by_sw.setdefault(sw.id, []).extend(stabs)
    for sw in parse.switches:
        stabs = by_sw.get(sw.id, [])
        if not stabs:
            continue
        sides = _stab_sides(sw, stabs)
        for side_x in sides:
            for ly, d in [
                (CHERRY_STAB_WIRE_OFFSET_Y_MM, CHERRY_STAB_WIRE_HOLE_MM),
                (CHERRY_STAB_HOUSING_OFFSET_Y_MM, CHERRY_STAB_HOUSING_HOLE_MM),
            ]:
                wx, wy = _rotate_local_to_world(side_x, ly, sw)
                out.append(KeepoutCircle(d, wx, wy))
    return out


def _mounting_hole_keepouts(parse: ParseResult) -> list[KeepoutCircle]:
    return [
        KeepoutCircle(max(h.diameter_mm, 1.5), h.cx_mm, h.cy_mm)
        for h in parse.mounting_holes
    ]


# ---------------------------------------------------------------------------
# Boundary polygon (mirrors `_edge_cuts`)
# ---------------------------------------------------------------------------


def _boundary_points(parse: ParseResult) -> list[tuple[float, float]]:
    """Return the closed polygon of the board edge: edited outline if present
    else parsed, then dilated by outline_grow_mm. Mirrors what pcb._edge_cuts
    emits so DSN and Edge.Cuts agree to the millimetre."""
    base_path = parse.edited_outline_path_d or parse.pcb_outline.path_d
    pts = _parse_path_points(base_path)
    if parse.outline_grow_mm > 0 and len(pts) >= 3:
        # Re-use the same Shapely buffer pcb._grow_polygon_points uses so
        # the boundary tracks Edge.Cuts exactly.
        from ..pcb import _grow_polygon_points  # local import avoids cycle
        pts = _grow_polygon_points(pts, parse.outline_grow_mm)
    return pts


# ---------------------------------------------------------------------------
# Net collection
# ---------------------------------------------------------------------------


def _collect_pins_per_net(components: list[Component]) -> dict[str, list[str]]:
    """`{ "COL0": ["SW1-1", "SW2-1", ...], ... }`"""
    out: dict[str, list[str]] = {}
    for comp in components:
        for pin_num, net in comp.nets.items():
            out.setdefault(net, []).append(f"{comp.ref}-{pin_num}")
    return out


# ---------------------------------------------------------------------------
# DSN emitter
# ---------------------------------------------------------------------------


def _fmt_coord(mm: float) -> str:
    """Specctra coordinates in our resolution unit. We emit `um 10` so each
    integer is 0.1 µm = 1/10000 mm — multiply mm by 10000."""
    return f"{mm * DSN_MM_FACTOR:.0f}"


def _fmt_y(mm: float) -> str:
    """Specctra is Y-up; KiCad is Y-down. Every Y coordinate emitted into
    the DSN flips sign so freerouting sees the board in its native frame.
    The SES parser un-flips Y to map wires back into kicad_pcb coords."""
    return f"{-mm * DSN_MM_FACTOR:.0f}"


def _fmt_mm(mm: float) -> str:
    return f"{mm:.4f}"


# Number of segments used to approximate each NPTH keepout circle. 16 is
# enough that the polygon's inscribed-vs-circumscribed difference stays
# below freerouting's clearance tolerance for a 1.75–4 mm hole.
KEEPOUT_POLY_SEGMENTS = 16


def _emit_parser() -> list[str]:
    return [
        "  (parser",
        '    (string_quote ")',
        "    (space_in_quoted_tokens on)",
        '    (host_cad "KiCad\'s Pcbnew")',
        '    (host_version "9.0.0")',
        "  )",
    ]


def _emit_structure(
    boundary: list[tuple[float, float]],
    keepouts: list[KeepoutCircle],
) -> list[str]:
    out = ["  (structure"]
    out.append(f'    (layer "{LAYER_F_CU}" (type signal) (property (index 0)))')
    out.append(f'    (layer "{LAYER_B_CU}" (type signal) (property (index 1)))')
    # Freerouting accepts exactly one bounding_shape per boundary. Use a
    # closed polyline so non-rectangular plate outlines (edited or grown)
    # are honoured by the router. The polygon must close back on itself
    # (last point == first point), which our caller's boundary already
    # implicitly does — we re-append the first vertex to be safe.
    closed = list(boundary)
    if closed[0] != closed[-1]:
        closed.append(closed[0])
    coord_pairs = " ".join(
        f"{_fmt_coord(x)} {_fmt_y(y)}" for x, y in closed
    )
    out.append(f'    (boundary (path pcb 0 {coord_pairs}))')
    # NPTH keepouts as polygons (one polygon per NPTH). Earlier we tried
    # `(circle signal D X Y)` but freerouting 2.2.4 rejects that shape as
    # "degenerate" and routes through the holes. Polygons match what
    # KiCad's own Specctra exporter emits.
    for ko in keepouts:
        out.append(_emit_keepout_polygon(ko))
    out.append(
        f'    (via "Via[0-1]_{VIA_PAD_DIAMETER_MM:.1f}:{VIA_DRILL_DIAMETER_MM:.1f}_um")'
    )
    out.append("    (rule")
    out.append(f"      (width {_fmt_coord(DEFAULT_TRACK_WIDTH_MM)})")
    out.append(f"      (clearance {_fmt_coord(DEFAULT_CLEARANCE_MM)})")
    out.append("    )")
    out.append("  )")
    return out


def _emit_keepout_polygon(ko: KeepoutCircle) -> str:
    """Approximate `ko` as an N-gon polygon keepout on both signal layers.
    Slight oversize (~0.05 mm) so the inscribed polygon still fully
    covers the underlying drill hole."""
    r = ko.diameter_mm / 2.0 + 0.05
    pts: list[tuple[float, float]] = []
    for i in range(KEEPOUT_POLY_SEGMENTS):
        a = 2 * math.pi * i / KEEPOUT_POLY_SEGMENTS
        pts.append((ko.cx_mm + r * math.cos(a), ko.cy_mm + r * math.sin(a)))
    pts.append(pts[0])
    coord_pairs = " ".join(
        f"{_fmt_coord(x)} {_fmt_y(y)}" for x, y in pts
    )
    return f'    (keepout "" (polygon signal 0 {coord_pairs}))'


def _emit_placement(components: list[Component]) -> list[str]:
    out = ["  (placement"]
    by_image: dict[str, list[Component]] = {}
    for c in components:
        by_image.setdefault(c.image_name, []).append(c)
    for image_name, comps in by_image.items():
        out.append(f'    (component "{image_name}"')
        for c in comps:
            # Y-flip placement Y but keep the KiCad rotation angle as-is:
            # F·R_k(θ)·F = R_m(θ), so the Y-flipped frame uses the same θ
            # (see module docstring). KiCad's own Specctra exporter does
            # exactly this for front-side footprints. Normalised to
            # [0, 360) for freerouting's 90°-multiple fast path.
            out.append(
                f'      (place "{c.ref}" '
                f"{_fmt_coord(c.place_x)} {_fmt_y(c.place_y)} "
                f"{c.side} {c.rotation_deg % 360.0:.3f})"
            )
        out.append("    )")
    out.append("  )")
    return out


def _emit_library(used_images: set[str]) -> list[str]:
    out = ["  (library"]
    for image_name in sorted(used_images):
        image = IMAGE_BUILDERS[image_name]()
        out.append(f'    (image "{image_name}"')
        for pin in image.pins:
            # Pin local coords are also Y-flipped so they compose
            # correctly with the Y-flipped placement (see module docstring).
            out.append(
                f'      (pin "{pin.padstack.name}" "{pin.number}" '
                f"{_fmt_coord(pin.local_x)} {_fmt_y(pin.local_y)})"
            )
        out.append("    )")
    # Padstacks (one per unique padstack referenced by any used image).
    used_padstacks: set[Padstack] = set()
    for image_name in used_images:
        for pin in IMAGE_BUILDERS[image_name]().pins:
            used_padstacks.add(pin.padstack)
    for ps in sorted(used_padstacks, key=lambda p: p.name):
        out.append(f'    (padstack "{ps.name}"')
        for layer in ps.layers:
            if ps.kind == "circle":
                out.append(
                    f'      (shape (circle "{layer}" {_fmt_coord(ps.w_mm)}))'
                )
            else:  # rect (SMD)
                half_w, half_h = ps.w_mm / 2.0, ps.h_mm / 2.0
                out.append(
                    f'      (shape (rect "{layer}" '
                    f"{_fmt_coord(-half_w)} {_fmt_coord(-half_h)} "
                    f"{_fmt_coord(half_w)} {_fmt_coord(half_h)}))"
                )
        out.append("      (attach off)")
        out.append("    )")
    # Standard via padstack.
    out.append(
        f'    (padstack "Via[0-1]_{VIA_PAD_DIAMETER_MM:.1f}:{VIA_DRILL_DIAMETER_MM:.1f}_um"'
    )
    for layer in LAYERS_SIGNAL:
        out.append(
            f'      (shape (circle "{layer}" {_fmt_coord(VIA_PAD_DIAMETER_MM)}))'
        )
    out.append("      (attach off)")
    out.append("    )")
    out.append("  )")
    return out


def _emit_network(
    pins_per_net: dict[str, list[str]],
) -> list[str]:
    out = ["  (network"]
    for net_name in sorted(pins_per_net):
        pins = pins_per_net[net_name]
        out.append(f'    (net "{net_name}"')
        out.append(f"      (pins {' '.join(pins)}))")
    # Two classes mirror project.py's netclasses: default = 0.25 mm,
    # matrix (ROW*/COL*/NET-SW*-D*) = 0.30 mm. Freerouting will pick
    # the wider width for matrix traces accordingly.
    matrix_nets = sorted(
        n for n in pins_per_net
        if n.startswith("ROW") or n.startswith("COL") or n.startswith("NET-SW")
    )
    default_nets = sorted(n for n in pins_per_net if n not in matrix_nets)
    if default_nets:
        out.append('    (class "default"')
        for n in default_nets:
            out.append(f'      "{n}"')
        out.append(f'      (circuit (use_via "Via[0-1]_{VIA_PAD_DIAMETER_MM:.1f}:{VIA_DRILL_DIAMETER_MM:.1f}_um"))')
        out.append(f"      (rule (width {_fmt_coord(DEFAULT_TRACK_WIDTH_MM)}) "
                   f"(clearance {_fmt_coord(DEFAULT_CLEARANCE_MM)}))")
        out.append("    )")
    if matrix_nets:
        out.append('    (class "matrix"')
        for n in matrix_nets:
            out.append(f'      "{n}"')
        out.append(f'      (circuit (use_via "Via[0-1]_{VIA_PAD_DIAMETER_MM:.1f}:{VIA_DRILL_DIAMETER_MM:.1f}_um"))')
        out.append(f"      (rule (width {_fmt_coord(MATRIX_TRACK_WIDTH_MM)}) "
                   f"(clearance {_fmt_coord(DEFAULT_CLEARANCE_MM)}))")
        out.append("    )")
    out.append("  )")
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _prepare_parse(parse: ParseResult) -> ParseResult:
    """Apply the same centering shift + matrix renumbering `generate_pcb`
    performs, so everything derived here lines up coordinate-for-coordinate
    and refdes-for-refdes with the emitted kicad_pcb."""
    _paper, parse = center_parse_on_page(parse)
    from ..matrix import renumber_switches  # local import avoids cycle
    return parse.model_copy(
        update={"switches": renumber_switches(parse.switches)}
    )


def _build_components(
    parse: ParseResult,
    switch_type: SwitchType,
    diode_type: DiodeType,
) -> list[Component]:
    nets = _enumerate_nets(parse.switches)
    components: list[Component] = []
    components.extend(_switch_components(parse, nets, switch_type))
    components.extend(_diode_components(parse, nets, diode_type, switch_type))
    components.extend(_mcu_components(parse, nets))
    return components


def pad_world_positions(
    parse: ParseResult,
    *,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
) -> dict[str, list[tuple[float, float, float]]]:
    """Net name → ``[(x_mm, y_mm, radius_mm)]`` of every connected pad, in
    KiCad Y-down coordinates after the same centering/renumbering
    `generate_pcb` applies. ``radius_mm`` is the pad's max half-extent.

    Used by the post-route validator to check that routed wires actually
    land on pads — the DSN we feed freerouting is self-consistent by
    construction, so a coordinate-convention bug here doesn't fail routing,
    it silently misplaces every trace. This recomputes pad positions with
    KiCad's own rotation convention (world = at + R_k(θ)·local,
    R_k = [[cos, sin], [−sin, cos]] in Y-down coords) as an independent
    cross-check at splice time.
    """
    parse = _prepare_parse(parse)
    components = _build_components(parse, switch_type, diode_type)
    images = {
        name: IMAGE_BUILDERS[name]()
        for name in {c.image_name for c in components}
    }
    out: dict[str, list[tuple[float, float, float]]] = {}
    for comp in components:
        rot = math.radians(comp.rotation_deg)
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        for pin in images[comp.image_name].pins:
            net = comp.nets.get(pin.number)
            if net is None:
                continue
            lx, ly = pin.local_x, pin.local_y
            wx = comp.place_x + lx * cos_r + ly * sin_r
            wy = comp.place_y - lx * sin_r + ly * cos_r
            radius = max(pin.padstack.w_mm, pin.padstack.h_mm) / 2.0
            out.setdefault(net, []).append((wx, wy, radius))
    return out


def pcb_to_dsn(
    parse: ParseResult,
    *,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
    stabilizer_type: StabilizerType = "pcb_mount",
    design_name: str = "keyboard",
) -> str:
    """Build a Specctra DSN file describing the board to freerouting.

    Mirrors `pcb.generate_pcb`'s view of the layout (same nets, same pad
    positions, same boundary). Returns the DSN text — caller writes it to a
    file or POSTs to the freerouting REST API.
    """
    if not parse.switches:
        raise ValueError("cannot export DSN from a board with zero switches")

    parse = _prepare_parse(parse)
    components = _build_components(parse, switch_type, diode_type)

    used_images = {c.image_name for c in components}
    pins_per_net = _collect_pins_per_net(components)

    boundary = _boundary_points(parse)
    if len(boundary) < 3:
        raise ValueError("board outline has fewer than 3 vertices")

    keepouts: list[KeepoutCircle] = []
    keepouts.extend(_switch_npth_keepouts(parse, switch_type))
    keepouts.extend(_stabilizer_keepouts(parse, stabilizer_type))
    keepouts.extend(_mounting_hole_keepouts(parse))

    lines: list[str] = []
    lines.append(f'(pcb "{design_name}"')
    lines.extend(_emit_parser())
    lines.append(f"  (resolution {DSN_RESOLUTION_UNIT} {DSN_RESOLUTION})")
    lines.append(f"  (unit {DSN_RESOLUTION_UNIT})")
    lines.extend(_emit_structure(boundary, keepouts))
    lines.extend(_emit_placement(components))
    lines.extend(_emit_library(used_images))
    lines.extend(_emit_network(pins_per_net))
    lines.append("  (wiring)")
    lines.append(")")
    return "\n".join(lines) + "\n"
