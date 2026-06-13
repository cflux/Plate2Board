import re

import pytest

from app.models.schemas import (
    McuPlacement,
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
    UnclassifiedShape,
)
from app.services.pcb import generate_pcb as _real_generate_pcb
from app.services.svg_parser import parse_plate_svg


# All assertions in this file pin specific board coordinates (switch
# positions, stab offsets, mounting holes, etc.), so we opt out of the
# page-centering shift `generate_pcb` does by default. A dedicated test
# (`test_centering_*`) covers the centering behaviour.
def generate_pcb(*args, **kwargs):
    kwargs.setdefault("center_on_page", False)
    return _real_generate_pcb(*args, **kwargs)


def _result(
    switches: list[SwitchDef] | None = None,
    stabilizers: list[StabilizerDef] | None = None,
    mounting_holes: list[MountingHoleDef] | None = None,
    width: float = 100.0,
    # Tall default so the common `_sw(1, 50, 50)` fixture sits centered —
    # the pad edge-setback validation rejects components on the outline.
    height: float = 100.0,
) -> ParseResult:
    return ParseResult(
        svg_width_mm=width,
        svg_height_mm=height,
        pcb_outline=PcbOutline(
            width_mm=width,
            height_mm=height,
            path_d=f"M 0 0 L {width} 0 L {width} {height} L 0 {height} Z",
        ),
        switches=switches or [],
        stabilizers=stabilizers or [],
        mounting_holes=mounting_holes or [],
        unclassified=[],
    )


def _sw(id_: int, cx: float, cy: float, *, row: int = 0, col: int = 0, rotation: float = 0.0) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=cx, cy_mm=cy, row=row, col=col, rotation_deg=rotation)


def test_empty_pcb_has_valid_skeleton() -> None:
    out = generate_pcb(_result())
    assert out.startswith("(kicad_pcb")
    assert "(version 20240108)" in out
    assert out.count("(") == out.count(")")
    # Even without switches there are layers + setup + outline edge cuts.
    assert '"Edge.Cuts"' in out


def test_single_switch_placed_at_parsed_coords() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 19.05, 9.525, rotation=10.0)]))
    # Switch footprint at exactly the parsed center, rotated 10° CW visually
    # to match SVG. SVG positive-CW is emitted as KiCad negative-CCW so the
    # `(at)` angle is -10.
    assert re.search(
        r'\(footprint "keeb:SW_Cherry_MX_PCB_1\.00u".*?'
        r'\(at 19\.0500 9\.5250 -10\.000\)',
        out,
        re.DOTALL,
    )
    # Reference SW1 + diode D1 + Pro Micro U1 all present.
    assert '"SW1"' in out
    assert '"D1"' in out
    assert '"U1"' in out
    assert "keeb:Arduino_Pro_Micro" in out
    # Minimum nets: COL0, ROW0, NET-SW1-D1.
    assert '(net 1 "ROW0")' in out
    assert '(net 2 "COL0")' in out
    assert '(net 3 "NET-SW1-D1")' in out


def test_diode_offset_rotates_with_switch() -> None:
    """A switch at rotation 90° should have its diode placed in the +X
    direction (because (0, +5) rotated 90° → (-5, 0))."""
    sw = _sw(1, 50.0, 50.0, rotation=90.0)
    out = generate_pcb(_result(switches=[sw]))
    m = re.search(
        r'\(footprint "keeb:D_DO-35_SOD27_P7\.62mm_Horizontal"'
        r'\s*\(layer "F\.Cu"\)\s*\(uuid "[^"]+"\)\s*'
        r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)',
        out,
    )
    assert m, "diode footprint not emitted"
    dx = float(m.group(1)) - sw.cx_mm
    dy = float(m.group(2)) - sw.cy_mm
    # rotation 90°: pin direction = (-1, 0); diode goes that way (offset 5.5mm)
    assert dx < -5.0 and abs(dy) < 0.5, f"expected diode at +x offset, got dx={dx} dy={dy}"


def test_mounting_holes_become_npth() -> None:
    holes = [MountingHoleDef(id=1, cx_mm=5.0, cy_mm=5.0, diameter_mm=2.5)]
    out = generate_pcb(_result(switches=[_sw(1, 19.05, 9.525)], mounting_holes=holes))
    assert "MountingHole_2.5mm" in out
    # Detected diameter used as drill size on an unplated through-hole.
    assert re.search(r"np_thru_hole circle.*?\(drill 2\.50\)", out, re.DOTALL)


def test_mounting_hole_refdes_and_fab_text_hidden() -> None:
    """Mounting holes are mechanical-only — their Reference (`MH{n}`) and
    Value (`MountingHole`) properties exist for KiCad's bookkeeping but
    should be hidden so silk and fab aren't cluttered with a label per
    drill."""
    holes = [MountingHoleDef(id=1, cx_mm=5.0, cy_mm=5.0, diameter_mm=2.5)]
    out = generate_pcb(_result(switches=[_sw(1, 19.05, 9.525)], mounting_holes=holes))
    mh_block = re.search(
        r'\(footprint "keeb:MountingHole_2\.5mm".+?\n\t\)',
        out,
        re.DOTALL,
    )
    assert mh_block, "mounting hole footprint missing"
    block = mh_block.group(0)
    # Both Reference and Value must carry the `hide` flag.
    assert re.search(r'\(property "Reference" "MH1".+?hide', block)
    assert re.search(r'\(property "Value" "MountingHole".+?hide', block)


def _stab_pair_for_switch(
    sw_id: int, sw_cx: float, sw_cy: float, half_spacing: float
) -> list[StabilizerDef]:
    """Two stab cutouts flanking a switch at ±half_spacing along X."""
    return [
        StabilizerDef(
            id=sw_id * 10 + 1,
            cx_mm=sw_cx - half_spacing,
            cy_mm=sw_cy,
            width_mm=14.0,
            height_mm=7.0,
            rotation_deg=0.0,
        ),
        StabilizerDef(
            id=sw_id * 10 + 2,
            cx_mm=sw_cx + half_spacing,
            cy_mm=sw_cy,
            width_mm=14.0,
            height_mm=7.0,
            rotation_deg=0.0,
        ),
    ]


def test_pcb_mount_stab_emits_canonical_holes() -> None:
    """PCB-mount stab: anchored on switch stem, 2 NPTH per detected side
    (wire-clearance at +6.77, housing at -8.24) at the cutout's half_spacing
    in switch-local coords."""
    half_spacing = 11.938
    stabs = _stab_pair_for_switch(1, 50.0, 50.0, half_spacing)
    out = generate_pcb(
        _result(switches=[_sw(1, 50, 50)], stabilizers=stabs),
        stabilizer_type="pcb_mount",
    )

    assert '"keeb:Stabilizer_PCB_Mount"' in out
    assert "(at 50.0000 50.0000 0.000)" in out

    stab_block = re.search(
        r'\(footprint "keeb:Stabilizer_PCB_Mount".+?\n\t\)',
        out,
        re.DOTALL,
    )
    assert stab_block
    block = stab_block.group(0)

    # 4 NPTH holes: wire (3.0 mm) and housing (4.0 mm) on each side.
    nptHs = re.findall(
        r'\(pad "" np_thru_hole circle \(at ([-+\d.]+) ([-+\d.]+)\) '
        r"\(size ([\d.]+) \3\) \(drill \3\)",
        block,
    )
    assert len(nptHs) == 4
    by_diameter: dict[float, list[tuple[float, float]]] = {}
    for x, y, d in nptHs:
        by_diameter.setdefault(float(d), []).append((float(x), float(y)))
    assert sorted(by_diameter.keys()) == [3.0, 4.0]
    # Wire holes at ±11.938, +6.77.
    wire_pts = sorted(by_diameter[3.0])
    assert wire_pts == sorted(
        [(-half_spacing, 6.77), (half_spacing, 6.77)]
    )
    # Housing holes at ±11.938, -8.24.
    housing_pts = sorted(by_diameter[4.0])
    assert housing_pts == sorted(
        [(-half_spacing, -8.24), (half_spacing, -8.24)]
    )

    # No Edge.Cuts segments — the plate cutout is not mirrored.
    assert "Edge.Cuts" not in block


def test_plate_mount_stab_emits_footprint_keepout_zone() -> None:
    """Plate-mount stab: no drills, just an F.Cu footprint-keepout zone so
    no other component can sit under the stab housing. Tracks/vias/pads/
    copperpour remain allowed so routing can still pass through."""
    stabs = _stab_pair_for_switch(1, 50.0, 50.0, 11.938)
    out = generate_pcb(
        _result(switches=[_sw(1, 50, 50)], stabilizers=stabs),
        stabilizer_type="plate_mount",
    )

    assert '"keeb:Stabilizer_Plate_Mount"' in out
    stab_block = re.search(
        r'\(footprint "keeb:Stabilizer_Plate_Mount".+?\n\t\)',
        out,
        re.DOTALL,
    )
    assert stab_block
    block = stab_block.group(0)

    # No drills.
    assert "np_thru_hole" not in block
    assert "thru_hole" not in block

    # Keepout zone on F.Cu disallowing only footprints.
    assert '(layer "F.Cu")' in block
    assert "(keepout" in block
    assert "(footprints not_allowed)" in block
    assert "(tracks allowed)" in block
    assert "(vias allowed)" in block
    assert "(pads allowed)" in block
    assert "(copperpour allowed)" in block


def test_stab_pairing_picks_nearest_switch() -> None:
    """Two switches; stab cutouts flank the second one. The assembly should
    anchor on switch 2, not switch 1."""
    sws = [_sw(1, 20.0, 40.0), _sw(2, 80.0, 40.0)]
    stabs = _stab_pair_for_switch(2, 80.0, 40.0, 11.938)
    out = generate_pcb(_result(switches=sws, stabilizers=stabs))

    # PCB-mount footprint anchored at switch 2's stem (80, 40).
    assert "(at 80.0000 40.0000 0.000)" in out
    # Exactly one stab assembly footprint (only switch 2 has stabs). Count
    # the opening `(footprint "..."` token, not the name's appearance inside
    # the Footprint property of common props.
    stab_footprints = re.findall(
        r'^\t\(footprint "keeb:Stabilizer_PCB_Mount"',
        out,
        re.MULTILINE,
    )
    assert len(stab_footprints) == 1
    # Reference labels the owning switch.
    assert '"ST2"' in out
    assert '"ST1"' not in out


def test_stab_pair_stays_together_when_row_is_sparse() -> None:
    """6.25u spacebar pair: two stab cutouts 100 mm apart at the same Y,
    with the keyboard row above containing more switches than the spacebar
    row. The pair must end up on the spacebar (nearest to the *midpoint*),
    NOT split across two different rows."""
    # Spacebar at (121.4, 70). Row above has 5 switches spread across
    # the same X range. The right stab cutout's *individual* nearest
    # switch is in the row above; only the pair midpoint disambiguates.
    spacebar = SwitchDef(id=99, cx_mm=121.4, cy_mm=70.0, row=4, col=5)
    row_above = [
        SwitchDef(id=i, cx_mm=x, cy_mm=50.0, row=3, col=i)
        for i, x in enumerate([80.0, 100.0, 120.0, 140.0, 160.0])
    ]
    stabs = [
        StabilizerDef(
            id=1, cx_mm=71.4, cy_mm=68.2, width_mm=15, height_mm=7, rotation_deg=270
        ),
        StabilizerDef(
            id=2, cx_mm=171.4, cy_mm=68.2, width_mm=15, height_mm=7, rotation_deg=270
        ),
    ]
    out = generate_pcb(
        _result(switches=row_above + [spacebar], stabilizers=stabs,
                width=250.0, height=120.0)
    )

    # Exactly one stab assembly footprint, anchored on the spacebar.
    fps = re.findall(
        r'^\t\(footprint "keeb:Stabilizer_PCB_Mount"',
        out,
        re.MULTILINE,
    )
    assert len(fps) == 1
    # Anchored at the spacebar's stem (121.4, 70.0), not at a row-above switch.
    assert "(at 121.4000 70.0000 0.000)" in out
    # And the assembly contains four NPTH (both stab sides).
    stab_block = re.search(
        r'\(footprint "keeb:Stabilizer_PCB_Mount".+?\n\t\)',
        out,
        re.DOTALL,
    ).group(0)
    assert stab_block.count("np_thru_hole") == 4


def test_stab_pairing_handles_rotated_switch() -> None:
    """A switch rotated 30° (e.g. Dactyl thumb cluster) with two stab cutouts
    at switch-local (±11.938, 0) lands them at different world Y values.
    The rotation-invariant pairing (midpoint-on-stem + equidistant) must
    still pair them and emit a single assembly with 4 NPTH."""
    import math

    rot_deg = 30.0
    rot = math.radians(rot_deg)
    half = 11.938
    sw_cx, sw_cy = 100.0, 100.0
    # Local (±half, 0) rotated by 30° into world coords.
    left_cx = sw_cx + (-half) * math.cos(rot)
    left_cy = sw_cy + (-half) * math.sin(rot)
    right_cx = sw_cx + half * math.cos(rot)
    right_cy = sw_cy + half * math.sin(rot)
    # Sanity: world ΔY is ~11.9 mm — far past the old Y-tolerance bug.
    assert abs(right_cy - left_cy) > 11.0

    sw = SwitchDef(
        id=1, cx_mm=sw_cx, cy_mm=sw_cy, row=0, col=0, rotation_deg=rot_deg
    )
    stabs = [
        StabilizerDef(
            id=1, cx_mm=left_cx, cy_mm=left_cy, width_mm=14, height_mm=7,
            rotation_deg=rot_deg,
        ),
        StabilizerDef(
            id=2, cx_mm=right_cx, cy_mm=right_cy, width_mm=14, height_mm=7,
            rotation_deg=rot_deg,
        ),
    ]
    out = generate_pcb(
        _result(switches=[sw], stabilizers=stabs, width=200.0, height=200.0)
    )

    fps = re.findall(
        r'^\t\(footprint "keeb:Stabilizer_PCB_Mount"',
        out,
        re.MULTILINE,
    )
    assert len(fps) == 1
    stab_block = re.search(
        r'\(footprint "keeb:Stabilizer_PCB_Mount".+?\n\t\)',
        out,
        re.DOTALL,
    ).group(0)
    assert stab_block.count("np_thru_hole") == 4
    # Local pad offsets should be Cherry canonical (±11.938 X, ±wire/housing Y),
    # not the rotated world frame.
    pads = re.findall(
        r'np_thru_hole circle \(at\s+([-+\d.]+)\s+([-+\d.]+)\)', stab_block
    )
    xs = sorted({round(float(x), 3) for x, _ in pads})
    ys = sorted({round(float(y), 2) for _, y in pads})
    assert xs == [-half, half]
    assert ys == [-8.24, 6.77]


def test_svg_clockwise_rotation_emitted_as_negative_kicad_angle() -> None:
    """SVG rotates clockwise for positive degrees; KiCad rotates counter-
    clockwise. Every footprint we emit (switch, diode, stabilizer) for a
    rotated switch must use a negative angle so the rendered PCB visually
    matches the plate SVG."""
    sw = _sw(1, 50.0, 50.0, rotation=15.0)
    stabs = _stab_pair_for_switch(1, 50.0, 50.0, 11.938)
    out = generate_pcb(_result(switches=[sw], stabilizers=stabs))

    # All three footprint families carry -15.000 in their (at), not +15.000.
    for fp_name in (
        "keeb:SW_Cherry_MX_PCB_1.00u",
        "keeb:D_DO-35_SOD27_P7.62mm_Horizontal",
        "keeb:Stabilizer_PCB_Mount",
    ):
        m = re.search(
            rf'\(footprint "{re.escape(fp_name)}".*?'
            r"\(at [-\d.]+ [-\d.]+ (-?\d+\.\d+)\)",
            out,
            re.DOTALL,
        )
        assert m, f"{fp_name} footprint missing"
        assert m.group(1) == "-15.000", (
            f"{fp_name} angle {m.group(1)} — expected -15.000"
        )


def test_unknown_stabilizer_type_raises() -> None:
    with pytest.raises(ValueError, match="stabilizer_type"):
        generate_pcb(_result(switches=[_sw(1, 0, 0)]), stabilizer_type="custom")


def test_outline_becomes_edge_cuts() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 25)]))
    # Bounding-box outline → 4 gr_line segments on Edge.Cuts.
    edge_lines = re.findall(r'gr_line.*?layer "Edge\.Cuts"', out)
    assert len(edge_lines) == 4


def test_mcu_placement_default_matches_legacy_position() -> None:
    """When `mcu_placement` is None on the parse, the generator falls back
    to the legacy formula: off the right edge, vertically centered."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    parse.mcu_placement = None
    out = generate_pcb(parse)
    expected_x = 100.0 + 12.0  # svg_width + HEADER_GAP_MM
    expected_y = (50.0 - 11 * 2.54) / 2
    assert f"(at {expected_x:.4f} {expected_y:.4f} 0.000)" in out


def test_mcu_placement_override_carries_through() -> None:
    """User-supplied mcu_placement controls anchor and rotation. SVG-CW
    rotation (90°) emits as KiCad CCW negative (-90.000)."""
    parse = _result(switches=[_sw(1, 50, 60)])
    # Rotated 90° the module spans x ≈ (cx − 27.94)..cx — keep it inboard.
    parse.mcu_placement = McuPlacement(cx_mm=40.0, cy_mm=12.0, rotation_deg=90.0)
    out = generate_pcb(parse)
    # Pro Micro footprint anchored at (40, 12) with KiCad angle -90 (SVG CW = KiCad CCW negative).
    assert re.search(
        r'\(footprint "keeb:Arduino_Pro_Micro".*?\(at 40\.0000 12\.0000 -90\.000\)',
        out,
        re.DOTALL,
    )


def test_outline_shrink_insets_edge_cuts() -> None:
    """A 100 × 50 mm plate with a 5 mm PCB inset should produce an
    Edge.Cuts ring at (5, 5)..(95, 45) — a 90 × 40 rectangle with
    mitered corners (no arc segments)."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    parse.outline_shrink_mm = 5.0
    out = generate_pcb(parse)
    edge_lines = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\).+?Edge\.Cuts',
        out,
    )
    assert len(edge_lines) == 4, f"expected 4 mitered edges, got {len(edge_lines)}"
    xs = {round(float(c), 2) for seg in edge_lines for c in (seg[0], seg[2])}
    ys = {round(float(c), 2) for seg in edge_lines for c in (seg[1], seg[3])}
    assert xs == {5.0, 95.0}, f"x extremes wrong: {xs}"
    assert ys == {5.0, 45.0}, f"y extremes wrong: {ys}"


def test_edited_outline_replaces_parsed_outline() -> None:
    """When `edited_outline_path_d` is set with no growth, the PCB
    Edge.Cuts uses that polygon verbatim — even concave notches survive."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    parse.edited_outline_path_d = (
        "M -1 -1 L 101 -1 L 101 51 L 60 51 L 60 40 L 40 40 L 40 51 L -1 51 Z"
    )
    parse.outline_shrink_mm = 0.0
    out = generate_pcb(parse)
    edges = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\)',
        out,
    )
    coords = {(round(float(s), 1), round(float(t), 1)) for seg in edges for s, t in [(seg[0], seg[1]), (seg[2], seg[3])]}
    # Notch coordinates must be present verbatim.
    assert (60.0, 40.0) in coords
    assert (40.0, 40.0) in coords


def test_edited_outline_plus_shrink_insets_the_edited_polygon() -> None:
    """outline_shrink_mm should still work on top of an edited polygon —
    insetting it rather than the parsed outline. Confirms shrink is not
    silently ignored after edits."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    # 100×50 mm rect slightly past the parse's viewport so the 3 mm inset
    # keeps the lone switch's pads comfortably inside.
    parse.edited_outline_path_d = "M -2 -2 L 102 -2 L 102 52 L -2 52 Z"
    parse.outline_shrink_mm = 3.0
    out = generate_pcb(parse)
    edges = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\)',
        out,
    )
    xs = {round(float(c), 1) for seg in edges for c in (seg[0], seg[2])}
    ys = {round(float(c), 1) for seg in edges for c in (seg[1], seg[3])}
    # Edited rect (-2..102 × -2..52) inset 3 mm → (1..99 × 1..49).
    assert xs == {1.0, 99.0}
    assert ys == {1.0, 49.0}


def test_outline_shrink_zero_is_passthrough() -> None:
    """outline_shrink_mm == 0 must take the no-buffer path so generation
    stays byte-identical (regression guard against an accidental Shapely
    round-trip of the outline)."""
    parse_a = _result(switches=[_sw(1, 50, 25)])
    parse_b = _result(switches=[_sw(1, 50, 25)])
    parse_b.outline_shrink_mm = 0.0
    out_a = generate_pcb(parse_a)
    out_b = generate_pcb(parse_b)
    # Strip UUIDs (they're randomized per call) before comparing.
    strip_uuid = re.compile(r'"uuid"\s+"[^"]+"|uuid "[^"]+"')
    assert strip_uuid.sub("", out_a) == strip_uuid.sub("", out_b)


def test_kbplate_full_pcb_has_every_switch_and_diode(example_plate_svg: str) -> None:
    parse = parse_plate_svg(example_plate_svg)
    out = generate_pcb(parse)

    n = len(parse.switches)
    # 2 footprints per switch (SW + D) + 1 header + N stabilizers + N holes
    expected_min_footprints = 2 * n + 1
    fp_count = out.count("(footprint ")
    assert fp_count >= expected_min_footprints

    # Every switch reference present.
    for sw in parse.switches:
        assert f'"SW{sw.id}"' in out
        assert f'"D{sw.id}"' in out

    # Pro Micro module footprint (24-pin, 2 × 12 thru-hole).
    assert "keeb:Arduino_Pro_Micro" in out
    assert '"U1"' in out


def test_complex_example_pcb_oversized_for_pro_micro(complex_example_svg: str) -> None:
    """The Dactyl fixture has more row+col pins than the 18 Pro Micro GPIO
    pins under any matrix strategy, so PCB generation should refuse with a
    clear error rather than emit an unwireable board."""
    parse = parse_plate_svg(complex_example_svg, matrix_strategy="stagger_aware")
    with pytest.raises(ValueError, match="Pro Micro"):
        generate_pcb(parse)


def test_hotswap_emits_smd_socket_pads_on_b_cu() -> None:
    """Hotswap variant has B.Cu SMD pads where the Kailh socket clips on,
    plus enlarged 3 mm NPTH at the switch pin positions for socket-arm clearance."""
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]), switch_type="hotswap")
    assert '"keeb:SW_Hotswap_Kailh_MX_1.00u"' in out
    # SMD pad 1 on B.Cu at (-7.085, -2.54).
    assert re.search(
        r'\(pad "1" smd rect \(at -7\.085 -2\.54 [-\d.]+\) \(size 2\.55 2\.5\)\s*'
        r'\(layers "B\.Cu" "B\.Paste" "B\.Mask"\)',
        out,
    )
    # SMD pad 2 on B.Cu at (5.842, -5.08).
    assert re.search(
        r'\(pad "2" smd rect \(at 5\.842 -5\.08 [-\d.]+\)',
        out,
    )
    # NPTH at switch pin positions (3 mm drill, larger than soldered's 1.5 mm).
    assert re.search(
        r'np_thru_hole circle \(at -3\.81 -2\.54\) \(size 3 3\) \(drill 3\)',
        out,
    )


def test_soldered_does_not_have_smd_socket_pads() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]), switch_type="soldered")
    # No SMD pads on B.Cu in the soldered variant.
    assert "B.Cu" "B.Paste" not in out  # constant-folded sanity check
    assert '(layers "B.Cu" "B.Paste" "B.Mask")' not in out


def test_unknown_switch_type_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown switch_type"):
        generate_pcb(_result(switches=[_sw(1, 0, 0)]), switch_type="bluetooth")


def test_smd_diode_emits_sod123_on_b_cu() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]), diode_type="smd")
    assert '"keeb:D_SOD-123"' in out
    # Pads on B.Cu, no thru-hole drill.
    assert re.search(
        r'\(pad "1" smd rect \(at -1\.65 0 [-\d.]+\) \(size 1\.0 0\.6\)\s*'
        r'\(layers "B\.Cu" "B\.Paste" "B\.Mask"\)',
        out,
    )
    # No THT diode present.
    assert "keeb:D_DO-35" not in out


def test_tht_diode_remains_default() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]))
    assert "keeb:D_DO-35_SOD27_P7.62mm_Horizontal" in out
    assert "keeb:D_SOD-123" not in out


def test_unknown_diode_type_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown diode_type"):
        generate_pcb(_result(switches=[_sw(1, 0, 0)]), diode_type="schottky")


def test_pcb_emits_no_track_segments() -> None:
    """We don't ship a built-in router — every connection is left as
    ratsnest so Freerouting (or KiCad's interactive router) can handle it
    correctly. Verify no `(segment ...)` tracks ever appear in output."""
    sws = [
        _sw(1, 30.0, 30.0, row=0, col=0),
        _sw(2, 49.05, 30.0, row=0, col=1),
        _sw(3, 30.0, 49.05, row=1, col=0),
    ]
    for switch_type in ("soldered", "hotswap"):
        for diode_type in ("tht", "smd"):
            out = generate_pcb(
                _result(switches=sws),
                switch_type=switch_type,
                diode_type=diode_type,
            )
            assert "(segment " not in out, (
                f"unexpected segment in {switch_type}/{diode_type} output"
            )


def test_proper_footprint_includes_silkscreen_and_courtyard() -> None:
    """Upgrade from minimal footprints: every switch should carry F.SilkS
    keycap brackets and an F.Courtyard outline."""
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]))
    # Keycap silkscreen brackets — the 4 corner brackets give 8 line segments.
    silk_lines = re.findall(r'\(fp_line.+?\(layer "F\.SilkS"\)', out)
    assert len(silk_lines) >= 8, f"expected ≥8 F.SilkS lines, got {len(silk_lines)}"
    # Courtyard rectangle (4 sides).
    crt_lines = re.findall(r'\(fp_line.+?\(layer "F\.CrtYd"\)', out)
    assert len(crt_lines) == 4
    # F.Fab body outline (4 sides + reference text).
    fab_lines = re.findall(r'\(fp_line.+?\(layer "F\.Fab"\)', out)
    assert len(fab_lines) >= 4


# ---------------------------------------------------------------------------
# Page centering (default behavior — opt-in by NOT passing center_on_page)
# ---------------------------------------------------------------------------


def test_centering_shifts_switch_to_page_center_on_a4() -> None:
    """A 100×50 board with a switch at its top-left corner should land
    centered on A4 (297×210, landscape): the board's bbox center maps
    onto the page center."""
    sws = [_sw(1, 50.0, 25.0)]
    out = _real_generate_pcb(_result(switches=sws, width=100.0, height=50.0))
    # The board's center maps onto the A4 page center (148.5, 105), so a
    # switch at the board center lands exactly there.
    m = re.search(
        r'\(footprint "keeb:SW_Cherry_MX_PCB_1\.00u".*?'
        r'\(at ([-\d.]+) ([-\d.]+)',
        out, re.DOTALL,
    )
    assert m is not None, "no soldered switch footprint found"
    x, y = float(m.group(1)), float(m.group(2))
    assert abs(x - 148.5) < 0.01, f"expected switch x≈148.5, got {x}"
    assert abs(y - 105.0) < 0.01, f"expected switch y≈105.0, got {y}"


def test_centering_picks_a3_when_board_exceeds_a4() -> None:
    """A 300×200 board doesn't fit A4 (must clear 297×210 minus 2×20mm
    margin = 257×170) but fits A3 (380×257 inside its margin). Verify
    the paper token reflects that pick."""
    sws = [_sw(1, 150, 100)]
    parse = _result(switches=sws, width=300.0, height=200.0)
    out = _real_generate_pcb(parse)
    assert '(paper "A3")' in out


def test_centering_keeps_edge_cuts_at_translated_polygon() -> None:
    """Edge.Cuts gr_line endpoints should be shifted by the same offset
    applied to footprints — board outline and switches stay relative."""
    sws = [_sw(1, 50.0, 25.0)]
    out = _real_generate_pcb(_result(switches=sws, width=100.0, height=50.0))
    # Switch lands at A4 center (148.5, 105). Polygon was 0..100, 0..50 →
    # after centering, corners are (98.5,80), (198.5,80), (198.5,130), (98.5,130).
    assert "(start 98.5000 80.0000)" in out
    assert "(end 198.5000 80.0000)" in out


# ---------------------------------------------------------------------------
# Diode placement conflict avoidance
# ---------------------------------------------------------------------------

import math  # noqa: E402

from app.services.pcb import (  # noqa: E402
    _diode_pads_world,
    _npth_obstacles,
    resolve_diode_placements,
)


def _placements(parse, **kwargs):
    defaults = dict(
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount"
    )
    defaults.update(kwargs)
    return resolve_diode_placements(
        list(parse.switches),
        list(parse.stabilizers),
        list(parse.mounting_holes),
        parse.mcu_placement,
        **defaults,
    )


def _assert_pads_clear_npths(parse, placements, *, switch_type, diode_type,
                             stabilizer_type="pcb_mount") -> None:
    from app.services.pcb import _DIODE_PAD_RADIUS
    import math as _math

    npths = _npth_obstacles(
        list(parse.switches), list(parse.stabilizers),
        list(parse.mounting_holes), switch_type, stabilizer_type,
    )
    pad_r = _DIODE_PAD_RADIUS[diode_type]
    for sw in parse.switches:
        p = placements[sw.id]
        for px, py, _key in _diode_pads_world(
            p.cx_mm, p.cy_mm, p.svg_rotation_deg, sw, diode_type
        ):
            for ox, oy, orad in npths:
                gap = _math.hypot(ox - px, oy - py) - orad - pad_r
                assert gap >= 0.29, (
                    f"D{sw.id} pad at ({px:.2f},{py:.2f}) only {gap:.2f} mm "
                    f"from NPTH at ({ox:.2f},{oy:.2f})"
                )


def test_diode_placement_defaults_unchanged_without_conflicts() -> None:
    """A lone switch has nothing to conflict with — the resolver must keep
    the historical default anchor exactly (hotswap SMD: +8.54, -5.08 local,
    rotated 90° from the switch)."""
    sw = _sw(1, 50.0, 50.0)
    placements = _placements(
        _result(switches=[sw]), switch_type="hotswap", diode_type="smd"
    )
    p = placements[1]
    assert p.cx_mm == pytest.approx(58.54)
    assert p.cy_mm == pytest.approx(44.92)
    assert p.svg_rotation_deg == 90.0


def test_smd_diode_moves_off_neighbor_stab_housing_hole() -> None:
    """Production repro: with 19.05 mm pitch and a pcb-mount stab on the
    right-hand key, the left key's hotswap SMD diode pad lands on the stab's
    4 mm housing-post hole. The resolver must relocate that diode; every
    diode pad must clear every NPTH afterwards."""
    sw1 = _sw(1, 30.0, 30.0, row=0, col=0)
    sw2 = _sw(2, 49.05, 30.0, row=0, col=1)
    parse = _result(
        switches=[sw1, sw2],
        stabilizers=_stab_pair_for_switch(2, 49.05, 30.0, 11.94),
    )
    placements = _placements(parse, switch_type="hotswap", diode_type="smd")
    # The default anchor for SW1's diode would be (38.54, 24.92) — pad 1 at
    # (38.54, 23.27) sits 2.08 mm from the stab housing hole at
    # (37.11, 21.76), inside drill + pad + clearance. Must have moved.
    moved = math.hypot(placements[1].cx_mm - 38.54, placements[1].cy_mm - 24.92)
    assert moved > 0.5, "SW1's diode should have been relocated"
    # SW2's own diode hangs to its right, away from the stab — unchanged.
    assert placements[2].cx_mm == pytest.approx(57.59)
    assert placements[2].cy_mm == pytest.approx(24.92)
    _assert_pads_clear_npths(
        parse, placements, switch_type="hotswap", diode_type="smd"
    )


def test_tht_diode_moves_off_mounting_hole() -> None:
    """A mounting hole drilled where the THT diode's ROW pad would land
    forces the resolver to a different anchor."""
    sw = _sw(1, 30.0, 30.0)
    parse = _result(
        switches=[sw],
        mounting_holes=[MountingHoleDef(id=1, cx_mm=27.0, cy_mm=35.5, diameter_mm=4.0)],
    )
    placements = _placements(parse, switch_type="soldered", diode_type="tht")
    moved = math.hypot(placements[1].cx_mm - 30.0, placements[1].cy_mm - 35.5)
    assert moved > 0.5, "diode should have been relocated off the hole"
    _assert_pads_clear_npths(
        parse, placements, switch_type="soldered", diode_type="tht"
    )


def test_generated_pcb_uses_resolved_diode_position() -> None:
    """The kicad_pcb must emit the conflict-resolved diode anchor, not the
    static default."""
    sw1 = _sw(1, 30.0, 30.0, row=0, col=0)
    sw2 = _sw(2, 49.05, 30.0, row=0, col=1)
    parse = _result(
        switches=[sw1, sw2],
        stabilizers=_stab_pair_for_switch(2, 49.05, 30.0, 11.94),
    )
    placements = _placements(parse, switch_type="hotswap", diode_type="smd")
    out = generate_pcb(parse, switch_type="hotswap", diode_type="smd")
    p1 = placements[1]
    assert f"(at {p1.cx_mm:.4f} {p1.cy_mm:.4f}" in out
    # And the default anchor of SW1's diode must NOT appear.
    assert "(at 38.5400 24.9200" not in out


def test_dsn_diodes_match_pcb_diodes_after_resolution() -> None:
    """pcb_to_dsn must place diodes exactly where generate_pcb does, even
    when the resolver moved them (same prepared parse → same resolution)."""
    from app.services.routing.dsn import pad_world_positions

    sw1 = _sw(1, 30.0, 30.0, row=0, col=0)
    sw2 = _sw(2, 49.05, 30.0, row=0, col=1)
    parse = _result(
        switches=[sw1, sw2],
        stabilizers=_stab_pair_for_switch(2, 49.05, 30.0, 11.94),
    )
    pcb_text = _real_generate_pcb(parse, switch_type="hotswap", diode_type="smd")
    pads = pad_world_positions(parse, switch_type="hotswap", diode_type="smd")
    # Every ROW-net diode pad position reported by the DSN layer must
    # appear as a pad `(at …)`-derived world position in the pcb text.
    diode_at = re.findall(
        r'\(footprint "keeb:D_SOD-123"\s*\(layer "B\.Cu"\)\s*\(uuid "[^"]+"\)\s*'
        r"\(at ([-\d.]+) ([-\d.]+)",
        pcb_text,
    )
    assert len(diode_at) == 2
    anchors = {(round(float(x), 3), round(float(y), 3)) for x, y in diode_at}
    for px, py, _r in pads["ROW0"]:
        # Each diode ROW pad must be 1.65 mm from one of the pcb anchors.
        import math as _math
        assert any(
            abs(_math.hypot(px - ax, py - ay) - 1.65) < 1e-6
            for ax, ay in anchors
        ), f"DSN diode pad ({px}, {py}) doesn't match any pcb anchor"


def test_mcu_silkscreen_centered_between_pin_rows() -> None:
    """The Pro Micro is anchored at pin 1 (a corner), so its Reference/Value
    text must stack around the body center (8.89, 13.97) — text hanging off
    the anchor lands outside the footprint and usually off the board."""
    out = generate_pcb(
        _result(switches=[_sw(1, 50.0, 50.0)]),
    )
    mcu = re.search(r'\(footprint "keeb:Arduino_Pro_Micro".+?\n\t\)', out, re.DOTALL)
    assert mcu, "Pro Micro footprint missing"
    block = mcu.group(0)
    assert re.search(r'\(property "Reference" "U1" \(at 8\.89 12\.47 ', block)
    assert re.search(r'\(property "Value" "ProMicro" \(at 8\.89 15\.47 ', block)


# ---------------------------------------------------------------------------
# Ground pour + stitching vias
# ---------------------------------------------------------------------------

from app.services.pcb import (  # noqa: E402
    GND_NET_NAME,
    MCU_GND_PINS,
    STITCH_EDGE_INSET_MM,
    STITCH_NPTH_CLEARANCE_MM,
    STITCH_PAD_CLEARANCE_MM,
    STITCH_VIA_SIZE_MM,
    _fixed_pad_obstacles,
    compute_stitching_vias,
)


def _pour_result() -> ParseResult:
    """3×3 grid with an MCU and a mounting hole — big enough for vias."""
    sws = [
        _sw(r * 3 + c + 1, 30.0 + c * 19.05, 30.0 + r * 19.05, row=r, col=c)
        for r in range(3)
        for c in range(3)
    ]
    res = _result(switches=sws, width=100.0, height=100.0,
                  mounting_holes=[MountingHoleDef(id=1, cx_mm=8.0, cy_mm=8.0, diameter_mm=2.2)])
    return res.model_copy(update={
        "mcu_placement": McuPlacement(cx_mm=75.0, cy_mm=8.0, rotation_deg=0.0),
    })


def test_gnd_net_appended_last_and_only_with_pour() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]))
    # 1 row + 1 col + 1 link → GND gets code 4, appended last.
    assert '(net 4 "GND")' in out
    assert '(net 1 "ROW0")' in out  # existing codes untouched
    off = generate_pcb(_result(switches=[_sw(1, 50, 50)]), ground_pour=False)
    assert '"GND"' not in off
    assert "(zone" not in off
    assert "(via" not in off
    empty = generate_pcb(_result())
    assert '"GND"' not in empty


def test_mcu_gnd_pins_carry_gnd_net() -> None:
    out = generate_pcb(_pour_result())
    mcu = re.search(r'\(footprint "keeb:Arduino_Pro_Micro".+?\n\t\)', out, re.DOTALL)
    assert mcu
    block = mcu.group(0)
    for pin in MCU_GND_PINS:
        assert re.search(
            rf'\(pad "{pin}" thru_hole \w+ \(at [^)]*\)[^(]*\(size [^)]*\) '
            rf'\(drill [^)]*\) \(layers[^)]*\) \(net \d+ "GND"\)',
            block,
        ), f"MCU pin {pin} should carry GND"
    # Power (21, 24) and RST (22) stay unconnected.
    for pin in (21, 22, 24):
        m = re.search(rf'\(pad "{pin}" thru_hole [^\n]*', block)
        assert m and "(net" not in m.group(0)


def test_gnd_zones_on_both_layers_unfilled() -> None:
    out = generate_pcb(_pour_result())
    zones = re.findall(r'\(zone\n.*?\n\t\)', out, re.DOTALL)
    gnd_zones = [z for z in zones if '(net_name "GND")' in z]
    layers = sorted(re.search(r'\(layer "([^"]+)"\)', z).group(1) for z in gnd_zones)
    assert layers == ["B.Cu", "F.Cu"]
    for z in gnd_zones:
        assert "(fill yes" in z
        assert "(filled_polygon" not in z
        assert "(keepout" not in z
    # Polygon is inset from the 0..100 outline.
    xs = [float(m) for m in re.findall(r"\(xy ([-\d.]+)", gnd_zones[0])]
    assert min(xs) >= 0.29 and max(xs) <= 100.0 - 0.29


def test_stitching_vias_inside_outline_and_clear_of_obstacles() -> None:
    from shapely.geometry import Point, Polygon

    parse = _pour_result()
    boundary = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    vias = compute_stitching_vias(
        list(parse.switches), [], list(parse.mounting_holes),
        parse.mcu_placement, boundary,
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount",
    )
    assert vias, "expected stitching vias on a 100×100 board"
    poly = Polygon(boundary)
    via_r = STITCH_VIA_SIZE_MM / 2
    from app.services.pcb import _npth_obstacles
    npths = _npth_obstacles(list(parse.switches), [], list(parse.mounting_holes),
                            "soldered", "pcb_mount")
    pads = _fixed_pad_obstacles(list(parse.switches), parse.mcu_placement, "soldered")
    for x, y in vias:
        assert poly.buffer(-STITCH_EDGE_INSET_MM + 1e-6).contains(Point(x, y))
        for ox, oy, orad in npths:
            assert math.hypot(ox - x, oy - y) >= orad + via_r + STITCH_NPTH_CLEARANCE_MM - 1e-6
        for ox, oy, orad, _k in pads:
            assert math.hypot(ox - x, oy - y) >= orad + via_r + STITCH_PAD_CLEARANCE_MM - 1e-6
    # And each position appears as a GND via in the pcb text.
    out = generate_pcb(parse, ground_pour=True)
    gnd_code = int(re.search(r'\(net (\d+) "GND"\)', out).group(1))
    for x, y in vias:
        assert f"(via (at {x:.4f} {y:.4f}) (size 0.6000) (drill 0.3000)" in out
    assert out.count(f"(net {gnd_code}) (uuid") == len(vias)


# ---------------------------------------------------------------------------
# Per-key RGB (SK6812 MINI-E)
# ---------------------------------------------------------------------------

from app.services.pcb import (  # noqa: E402
    RGB_LED_PAD_LOCAL,
    VCC_NET_NAME,
    _rgb_led_anchor,
    _rgb_obstacles,
)


def _rgb_result() -> ParseResult:
    sws = [
        _sw(r * 3 + c + 1, 30.0 + c * 19.05, 30.0 + r * 19.05, row=r, col=c)
        for r in range(2)
        for c in range(3)
    ]
    res = _result(switches=sws, width=100.0, height=80.0)
    return res.model_copy(update={
        "mcu_placement": McuPlacement(cx_mm=75.0, cy_mm=5.0, rotation_deg=0.0),
    })


def test_rgb_off_emits_no_led_parts() -> None:
    out = generate_pcb(_rgb_result())
    assert "LED_SK6812MINI-E" not in out
    assert "keeb:C_0603" not in out
    assert '"VCC"' not in out
    assert '"RGB_DATA0"' not in out


def test_rgb_emits_led_and_cap_per_switch_with_cutout() -> None:
    out = generate_pcb(_rgb_result(), rgb=True)
    assert out.count('(footprint "keeb:LED_SK6812MINI-E"') == 6
    assert out.count('(footprint "keeb:C_0603"') == 6
    led_block = re.search(
        r'\(footprint "keeb:LED_SK6812MINI-E".+?\n\t\)', out, re.DOTALL
    ).group(0)
    # The cutout is a milled board slot on Edge.Cuts (4 lines), NOT a
    # copper NPTH pad — no copper square under the LED.
    assert "np_thru_hole" not in led_block
    assert led_block.count('(layer "Edge.Cuts")') == 4
    assert '"B.SilkS"' in led_block
    assert '(layers "B.Cu" "B.Paste" "B.Mask")' in led_block
    assert out.count("(") == out.count(")")


def test_rgb_net_codes_appended_after_gnd() -> None:
    out = generate_pcb(_rgb_result(), rgb=True)
    # 2 rows + 3 cols + 6 links = 11, GND=12, VCC=13, RGB_DATA0..5=14..19.
    assert '(net 12 "GND")' in out
    assert '(net 13 "VCC")' in out
    assert '(net 14 "RGB_DATA0")' in out
    assert '(net 19 "RGB_DATA5")' in out


def test_rgb_chain_is_continuous_and_last_dout_open() -> None:
    out = generate_pcb(_rgb_result(), rgb=True)
    led_blocks = re.findall(
        r'\(footprint "keeb:LED_SK6812MINI-E".+?\n\t\)', out, re.DOTALL
    )
    by_ref = {}
    for b in led_blocks:
        ref = re.search(r'\(property "Reference" "(LED\d+)"', b).group(1)
        pads = dict(re.findall(
            r'\(pad "(\d)" smd rect \(at [^)]*\) \(size [^)]*\)\s*'
            r'\(layers "B\.Cu"[^)]*\)(?: \(net \d+ "([^"]*)"\))?',
            b,
        ))
        by_ref[ref] = pads
    # Chain follows the serpentine order (row 0 left→right, row 1
    # right→left): LED1→LED2→LED3→LED6→LED5→LED4.
    from app.services.pcb import rgb_chain_indices
    parse = _rgb_result()
    indices = rgb_chain_indices(list(parse.switches))
    order = sorted(indices, key=indices.get)
    assert by_ref[f"LED{order[0]}"]["4"] == "RGB_DATA0"
    for a, b in zip(order, order[1:]):
        assert by_ref[f"LED{a}"]["2"] == by_ref[f"LED{b}"]["4"], f"chain broken at LED{a}"
    # Last DOUT unconnected.
    assert by_ref[f"LED{order[-1]}"].get("2", "") == ""
    # VDD/GND on every LED.
    for ref, pads in by_ref.items():
        assert pads["1"] == "VCC"
        assert pads["3"] == "GND"


def test_rgb_chain_ignores_electrical_matrix() -> None:
    """The daisy-chain order is purely geometric — re-gridding the matrix
    (row/col) on the SAME physical layout must not change the chain. This is
    the regression for matrix edits producing long DIN->DOUT hops."""
    from app.services.pcb import rgb_chain_indices

    sane = [
        _sw(r * 3 + c + 1, 30.0 + c * 19.05, 30.0 + r * 19.05, row=r, col=c)
        for r in range(2)
        for c in range(3)
    ]
    # Same positions/ids, but the matrix is scrambled (all one row, reversed
    # columns) the way a hand-edited grid might be.
    scrambled = [
        SwitchDef(id=s.id, cx_mm=s.cx_mm, cy_mm=s.cy_mm, row=0, col=99 - s.id)
        for s in sane
    ]
    assert rgb_chain_indices(sane) == rgb_chain_indices(scrambled)


def test_rgb_chain_crosses_split_gap_once() -> None:
    """On a layout with two clusters separated by a wide void, the chain
    should cross the gap exactly once — the worst hop equals that single
    crossing, not several long hops from a tangled seed."""
    import math
    from app.services.pcb import rgb_chain_indices

    # Two 3x2 clusters, a 60 mm void between their nearest columns.
    sws = []
    nid = 1
    for base_x in (0.0, 60.0 + 2 * 19.05):
        for r in range(3):
            for c in range(2):
                sws.append(_sw(nid, base_x + c * 19.05, r * 19.05, row=r, col=c))
                nid += 1
    chain = rgb_chain_indices(sws)
    by_idx = sorted(sws, key=lambda s: chain[s.id])
    hops = [
        math.hypot(by_idx[k + 1].cx_mm - by_idx[k].cx_mm,
                   by_idx[k + 1].cy_mm - by_idx[k].cy_mm)
        for k in range(len(by_idx) - 1)
    ]
    long_hops = [h for h in hops if h > 40.0]
    assert len(long_hops) == 1, f"expected one gap crossing, got {long_hops}"


def test_rgb_mcu_pins() -> None:
    out = generate_pcb(_rgb_result(), rgb=True)
    mcu = re.search(r'\(footprint "keeb:Arduino_Pro_Micro".+?\n\t\)', out, re.DOTALL).group(0)
    # RAW (24) → VCC; next free GPIO after 2 rows + 3 cols (index 5 → pin 10) → RGB_DATA0.
    assert re.search(r'\(pad "24" [^\n]*\(net \d+ "VCC"\)', mcu)
    assert re.search(r'\(pad "10" [^\n]*\(net \d+ "RGB_DATA0"\)', mcu)


def test_rgb_tht_diode_relocates_off_led() -> None:
    """The THT diode default anchor (0, +5.5) overlaps the LED cutout at
    (0, +4.7) — with RGB on, the resolver must move every diode and the
    result must clear the LED obstacles."""
    parse = _rgb_result()
    placements = _placements(parse, switch_type="soldered", diode_type="tht", rgb=True)
    rgb_npths, rgb_pads = _rgb_obstacles(list(parse.switches))
    from app.services.pcb import _DIODE_PAD_RADIUS, _diode_pads_world
    for sw in parse.switches:
        p = placements[sw.id]
        default = math.hypot(p.cx_mm - sw.cx_mm, p.cy_mm - (sw.cy_mm + 5.5))
        assert default > 0.5, f"D{sw.id} should have moved off the LED"
        for px, py, _key in _diode_pads_world(p.cx_mm, p.cy_mm, p.svg_rotation_deg, sw, "tht"):
            for ox, oy, orad in rgb_npths:
                assert math.hypot(ox - px, oy - py) >= orad + _DIODE_PAD_RADIUS["tht"] + 0.29


def test_rgb_stitching_vias_avoid_leds() -> None:
    parse = _rgb_result()
    boundary = [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0)]
    vias = compute_stitching_vias(
        list(parse.switches), [], [], parse.mcu_placement, boundary,
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount",
        rgb=True,
    )
    rgb_npths, rgb_pads = _rgb_obstacles(list(parse.switches))
    for x, y in vias:
        for ox, oy, orad in rgb_npths:
            assert math.hypot(ox - x, oy - y) >= orad + 0.3 + STITCH_NPTH_CLEARANCE_MM - 1e-6
        for ox, oy, orad, _k in rgb_pads:
            assert math.hypot(ox - x, oy - y) >= orad + 0.3 + STITCH_PAD_CLEARANCE_MM - 1e-6


def test_rgb_gpio_budget_enforced() -> None:
    # 5 rows × 13 cols = 18 pins — full budget; +1 for RGB must raise.
    sws = [
        _sw(r * 13 + c + 1, 15.0 + c * 19.05, 15.0 + r * 19.05, row=r, col=c)
        for r in range(5)
        for c in range(13)
    ]
    parse = _result(switches=sws, width=280.0, height=120.0)
    generate_pcb(parse)  # fits without rgb
    with pytest.raises(ValueError, match="RGB"):
        generate_pcb(parse, rgb=True)


# ---------------------------------------------------------------------------
# Outline shrink degeneracy + pad edge-setback validation
# ---------------------------------------------------------------------------

from app.services.pcb import (  # noqa: E402
    PAD_EDGE_SETBACK_MM,
    validate_pad_setback,
)


def test_shrink_that_removes_outline_raises() -> None:
    parse = _result(switches=[_sw(1, 50, 50)])
    parse.outline_shrink_mm = 60.0  # 100×100 board — 60 mm eats it all
    with pytest.raises(ValueError, match="removes the entire PCB outline"):
        generate_pcb(parse)


def test_shrink_that_splits_outline_raises() -> None:
    # Dumbbell: two 40-wide lobes joined by a 4 mm-tall neck. A 3 mm
    # shrink severs the neck into two islands.
    parse = _result(switches=[_sw(1, 20, 20), _sw(2, 80, 20, col=1)],
                    width=100.0, height=40.0)
    parse.edited_outline_path_d = (
        "M 0 0 L 40 0 L 40 18 L 60 18 L 60 0 L 100 0 "
        "L 100 40 L 60 40 L 60 22 L 40 22 L 40 40 L 0 40 Z"
    )
    parse.outline_shrink_mm = 3.0
    with pytest.raises(ValueError, match="splits the PCB outline"):
        generate_pcb(parse)


def test_pad_setback_rejects_pad_near_edge() -> None:
    """Shrinking until a switch pad sits closer than 0.5 mm to the PCB
    edge must fail with the offending ref named."""
    # Switch at (50, 11): soldered pad 2 at (52.54, 5.92), r 1.25 — bottom
    # pad copper edge is 4.67 mm from y=0. A 4 mm shrink leaves 0.67 - ok;
    # 4.5 leaves 0.17 — violation.
    parse = _result(switches=[_sw(1, 50, 11)], width=100.0, height=100.0)
    parse.outline_shrink_mm = 4.5
    with pytest.raises(ValueError, match="SW1"):
        generate_pcb(parse)
    parse.outline_shrink_mm = 3.0
    generate_pcb(parse)  # comfortably clear — no raise


def test_pad_setback_rejects_mcu_on_edge_at_zero_shrink() -> None:
    """The setback check runs even without any shrink — an MCU dragged
    onto the plate edge fails with a clear message."""
    parse = _result(switches=[_sw(1, 50, 50)])
    parse.mcu_placement = McuPlacement(cx_mm=-2.0, cy_mm=30.0, rotation_deg=0.0)
    with pytest.raises(ValueError, match="U1"):
        generate_pcb(parse)


def test_validate_pad_setback_reports_distances() -> None:
    boundary = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    sw = _sw(1, 50, 50)
    ok = validate_pad_setback(
        [sw], [], [], None, boundary,
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount",
    )
    assert ok == []
    # Same switch against a boundary whose edge passes right next to it.
    tight = [(0.0, 0.0), (52.0, 0.0), (52.0, 100.0), (0.0, 100.0)]
    bad = validate_pad_setback(
        [sw], [], [], None, tight,
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount",
    )
    assert bad and any("SW1" in v or "D1" in v for v in bad)


def test_shrink_applies_to_zones_vias_and_dsn_boundary() -> None:
    from app.services.routing.dsn import _boundary_points

    parse = _result(switches=[_sw(1, 50, 50)])
    parse.outline_shrink_mm = 5.0
    # DSN boundary = outline inset by 5.
    pts = _boundary_points(parse)
    xs = {round(x, 1) for x, _y in pts}
    ys = {round(y, 1) for _x, y in pts}
    assert xs == {5.0, 95.0} and ys == {5.0, 95.0}
    # GND zone polygon insets a further 0.3 from the shrunk outline.
    out = generate_pcb(parse)
    zone = re.search(r'\(zone\n.*?\n\t\)', out, re.DOTALL).group(0)
    zxs = [float(m) for m in re.findall(r"\(xy ([-\d.]+)", zone)]
    assert min(zxs) >= 5.3 - 1e-6 and max(zxs) <= 94.7 + 1e-6
    # Stitching vias stay inside the shrunk outline.
    for m in re.finditer(r"\(via \(at ([-\d.]+) ([-\d.]+)\) \(size 0\.6000\)", out):
        x, y = float(m.group(1)), float(m.group(2))
        assert 5.0 < x < 95.0 and 5.0 < y < 95.0


# ---------------------------------------------------------------------------
# MCU form factors (XIAO / Pico)
# ---------------------------------------------------------------------------

from app.services.mcu import get_mcu_profile  # noqa: E402


def _mcu_result() -> ParseResult:
    # 2×3 = 5 pins, fits every MCU even with RGB. Centered on a roomy
    # board so MCU + switch pads clear the 0.5 mm edge setback.
    sws = [
        _sw(r * 3 + c + 1, 40.0 + c * 19.05, 40.0 + r * 19.05, row=r, col=c)
        for r in range(2)
        for c in range(3)
    ]
    res = _result(switches=sws, width=160.0, height=160.0)
    return res.model_copy(update={
        "mcu_placement": McuPlacement(cx_mm=70.0, cy_mm=90.0, rotation_deg=0.0),
    })


@pytest.mark.parametrize(
    "mcu_type,footprint,last_pin,last_xy",
    [
        ("xiao", "keeb:XIAO", 14, (15.24, 0.0)),
        ("xiao_smd", "keeb:XIAO_SMD", 14, (15.24 + 0.4625, 0.0)),
        ("pico", "keeb:RaspberryPi_Pico", 40, (17.78, 0.0)),
    ],
)
def test_mcu_footprint_emitted(mcu_type, footprint, last_pin, last_xy) -> None:
    out = generate_pcb(_mcu_result(), mcu_type=mcu_type)
    assert f'(footprint "{footprint}"' in out
    mcu_block = re.search(
        rf'\(footprint "{re.escape(footprint)}".+?\n\t\)', out, re.DOTALL
    ).group(0)
    # The MCU anchor sits at the placement; pads are local, so the last
    # pin's LOCAL position relative to the anchor must match the profile.
    prof = get_mcu_profile(mcu_type)
    assert prof.pins[last_pin] == pytest.approx(last_xy)
    # Pad count matches the profile.
    assert mcu_block.count('(pad "') == len(prof.pins)
    assert out.count("(") == out.count(")")


def test_xiao_smd_pads_are_smd_no_drill() -> None:
    out = generate_pcb(_mcu_result(), mcu_type="xiao_smd")
    block = re.search(
        r'\(footprint "keeb:XIAO_SMD".+?\n\t\)', out, re.DOTALL
    ).group(0)
    assert "thru_hole" not in block
    assert "(drill" not in block
    assert block.count("smd rect") == 14
    assert '(layers "F.Cu" "F.Paste" "F.Mask")' in block


def test_xiao_th_pads_have_drill() -> None:
    out = generate_pcb(_mcu_result(), mcu_type="xiao")
    block = re.search(
        r'\(footprint "keeb:XIAO".+?\n\t\)', out, re.DOTALL
    ).group(0)
    assert block.count("thru_hole") == 14
    assert "(drill 0.889)" in block


def test_mcu_net_mapping_per_profile() -> None:
    # Pico GND pins all carry GND with the pour; VBUS carries VCC with RGB.
    out = generate_pcb(_mcu_result(), mcu_type="pico", rgb=True)
    block = re.search(
        r'\(footprint "keeb:RaspberryPi_Pico".+?\n\t\)', out, re.DOTALL
    ).group(0)
    prof = get_mcu_profile("pico")
    for g in prof.gnd_pins:
        assert re.search(rf'\(pad "{g}" [^\n]*"GND"\)', block), f"pin {g} GND"
    assert re.search(rf'\(pad "{prof.power_5v_pin}" [^\n]*"VCC"\)', block)
    # First gpio pin past the 5-key matrix drives the RGB chain.
    data_pin = prof.gpio_pins[2 + 3]
    assert re.search(rf'\(pad "{data_pin}" [^\n]*"RGB_DATA0"\)', block)


def test_pico_lifts_gpio_ceiling_for_rgb() -> None:
    # 5 rows × 13 cols = 18 GPIO + 1 RGB = 19: rejected by Pro Micro (18),
    # accepted by the Pico (26). Big board so pads clear the edge.
    sws = [
        _sw(r * 13 + c + 1, 25.0 + c * 19.05, 25.0 + r * 19.05, row=r, col=c)
        for r in range(5)
        for c in range(13)
    ]
    parse = _result(switches=sws, width=320.0, height=160.0).model_copy(
        update={"mcu_placement": McuPlacement(cx_mm=160.0, cy_mm=8.0, rotation_deg=0.0)}
    )
    with pytest.raises(ValueError, match="Pro Micro"):
        generate_pcb(parse, rgb=True)  # default pro_micro
    # Pico accepts it (no GPIO-budget error; geometry permitting).
    out = generate_pcb(parse, mcu_type="pico", rgb=True)
    assert '(footprint "keeb:RaspberryPi_Pico"' in out


# ---------------------------------------------------------------------------
# Rotated-switch RGB layout fixes (v0.12.1)
# ---------------------------------------------------------------------------


def _rotated_rgb_parse(rot_deg: float = 10.0) -> ParseResult:
    sw = _sw(1, 60.0, 60.0, rotation=rot_deg)
    return _result(switches=[sw], width=120.0, height=120.0).model_copy(
        update={"mcu_placement": McuPlacement(cx_mm=20.0, cy_mm=20.0, rotation_deg=0.0)}
    )


def test_rect_smd_pads_carry_footprint_rotation() -> None:
    """LED + cap rect SMD pads must bake the footprint orientation into
    their own (at … angle) so the rectangle rotates with the body (KiCad
    treats board pad angles as absolute, not composed)."""
    out = generate_pcb(_rotated_rgb_parse(10.0), diode_type="tht", rgb=True)
    for fp, expect_rot in (
        ("keeb:LED_SK6812MINI-E", "-190.000"),  # switch 10° + LED 180°
        ("keeb:C_0603", "-10.000"),
    ):
        block = re.search(rf'\(footprint "{re.escape(fp)}".+?\n\t\)', out, re.DOTALL).group(0)
        pads = re.findall(r'\(pad "\d" smd rect \(at [-\d.]+ [-\d.]+ ([-\d.]+)\)', block)
        assert pads, f"{fp}: no rect SMD pads found"
        assert all(p == expect_rot for p in pads), f"{fp}: pad angles {pads} != {expect_rot}"


def test_smd_diode_pads_carry_rotation() -> None:
    out = generate_pcb(_rotated_rgb_parse(10.0), diode_type="smd", rgb=False)
    block = re.search(r'\(footprint "keeb:D_SOD-123".+?\n\t\)', out, re.DOTALL).group(0)
    # SMD diode is switch_rot + 90 → -100 kicad.
    angles = re.findall(r'\(pad "\d" smd rect \(at [-\d.]+ [-\d.]+ ([-\d.]+)\)', block)
    assert angles == ["-100.000", "-100.000"], angles


def test_led_cutout_is_edge_cuts_not_copper() -> None:
    out = generate_pcb(_rotated_rgb_parse(10.0), diode_type="tht", rgb=True)
    block = re.search(
        r'\(footprint "keeb:LED_SK6812MINI-E".+?\n\t\)', out, re.DOTALL
    ).group(0)
    assert "np_thru_hole" not in block  # no copper-flooded cutout pad
    assert block.count('(layer "Edge.Cuts")') == 4  # milled slot rectangle


def test_tht_diode_clears_switch_pin_with_rgb_on_rotated_switch() -> None:
    """The reported bug: RGB pushes the THT diode north onto the switch
    pins, and same-net pads were allowed to overlap. The diode's link pad
    must now physically clear the switch's link pin."""
    parse = _rotated_rgb_parse(10.0)
    out = generate_pcb(parse, diode_type="tht", rgb=True)

    def pad_world(cx, cy, rotdeg, lx, ly):
        r = math.radians(rotdeg)
        c, s = math.cos(r), math.sin(r)
        return (cx + lx * c + ly * s, cy - lx * s + ly * c)

    def anchor(fp):
        b = re.search(rf'\(footprint "{re.escape(fp)}".+?\n\t\)', out, re.DOTALL).group(0)
        m = re.search(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', b)
        return b, float(m.group(1)), float(m.group(2)), float(m.group(3))

    swb, scx, scy, srot = anchor("keeb:SW_Cherry_MX_PCB_1.00u")
    slink = pad_world(scx, scy, srot, 2.54, -5.08)  # switch pin 2 (link)
    db, dcx, dcy, drot = anchor("keeb:D_DO-35_SOD27_P7.62mm_Horizontal")
    dlink = None
    for m in re.finditer(
        r'\(pad "\d" thru_hole oval \(at ([-\d.]+) [-\d.]+\).*?\(net \d+ "([^"]+)"\)',
        db, re.DOTALL,
    ):
        if "NET-SW" in m.group(2):
            dlink = pad_world(dcx, dcy, drot, float(m.group(1)), 0.0)
    dist = math.hypot(dlink[0] - slink[0], dlink[1] - slink[1])
    # THT pad radii 0.8 + 1.25 + 0.2 clearance.
    assert dist >= 0.8 + 1.25 + 0.2 - 1e-6, f"diode link pad only {dist:.2f} mm from switch pin"


# ---------------------------------------------------------------------------
# v0.12.2: THT diode north placement + VCC split-plane pour
# ---------------------------------------------------------------------------

from app.services.pcb import VCC_NET_NAME, compute_vcc_vias  # noqa: E402


def test_tht_diode_moves_north_of_pins_with_rgb() -> None:
    """RGB takes the south slot; the THT diode must sit well north of the
    switch pin band (in the inter-row gap), not across it."""
    parse = _rotated_rgb_parse(10.0)
    out = generate_pcb(parse, diode_type="tht", rgb=True)
    db = re.search(r'\(footprint "keeb:D_DO-35.+?\n\t\)', out, re.DOTALL).group(0)
    m = re.search(r'\(at ([-\d.]+) ([-\d.]+) [-\d.]+\)', db)
    dcx, dcy = float(m.group(1)), float(m.group(2))
    sw = parse.switches[0]
    # Diode anchor is north of the switch by more than the body half (7 mm).
    # (Switch at 60,60; north = smaller y.)
    assert dcy <= sw.cy_mm - 7.0, f"diode at y={dcy}, expected well north of {sw.cy_mm}"


def test_rgb_split_planes_gnd_bottom_vcc_top() -> None:
    out = generate_pcb(_rgb_result(), rgb=True)
    zones = re.findall(
        r'\(zone\n\t\t\(net \d+\)\n\t\t\(net_name "([^"]+)"\)\n\t\t\(layer "([^"]+)"\)',
        out,
    )
    assert ("GND", "B.Cu") in zones
    assert ("VCC", "F.Cu") in zones
    assert ("GND", "F.Cu") not in zones  # GND is single-layer when split
    assert ("VCC", "B.Cu") not in zones
    assert len(zones) == 2


def test_rgb_vcc_vias_one_per_vcc_pad_no_gnd_stitching() -> None:
    parse = _rgb_result()
    out = generate_pcb(parse, rgb=True)
    vcc = int(re.search(r'\(net (\d+) "VCC"\)', out).group(1))
    via_count = len(re.findall(rf'\(via \(at [-\d.]+ [-\d.]+\)[^\n]*\(net {vcc}\)', out))
    # 6 LEDs + 6 caps = 12 VCC pads → 12 vias.
    assert via_count == len(compute_vcc_vias(list(parse.switches))) == 12
    # No GND stitching vias in the split-plane case (GND is single-layer).
    gnd = int(re.search(r'\(net (\d+) "GND"\)', out).group(1))
    assert not re.search(rf'\(via \(at [-\d.]+ [-\d.]+\)[^\n]*\(net {gnd}\)', out)


def test_non_rgb_ground_pour_still_both_layers_with_stitching() -> None:
    # Regression guard: without RGB the GND pour stays on both layers and
    # keeps its stitching vias.
    parse = _rgb_result()
    out = generate_pcb(parse, rgb=False)  # ground_pour defaults on
    zones = re.findall(r'\(net_name "GND"\)\n\t\t\(layer "([^"]+)"\)', out)
    assert sorted(zones) == ["B.Cu", "F.Cu"]
    assert '"VCC"' not in out
    gnd = int(re.search(r'\(net (\d+) "GND"\)', out).group(1))
    assert re.search(rf'\(via \(at [-\d.]+ [-\d.]+\)[^\n]*\(net {gnd}\)', out)
