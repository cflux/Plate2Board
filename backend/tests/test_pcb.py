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
    height: float = 50.0,
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
    sws = [_sw(1, 0.0, 0.0), _sw(2, 100.0, 0.0)]
    stabs = _stab_pair_for_switch(2, 100.0, 0.0, 11.938)
    out = generate_pcb(_result(switches=sws, stabilizers=stabs))

    # PCB-mount footprint anchored at switch 2's stem (100, 0).
    assert "(at 100.0000 0.0000 0.000)" in out
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
        _result(switches=row_above + [spacebar], stabilizers=stabs)
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
    out = generate_pcb(_result(switches=[sw], stabilizers=stabs))

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
    parse = _result(switches=[_sw(1, 50, 25)])
    parse.mcu_placement = McuPlacement(cx_mm=20.0, cy_mm=10.0, rotation_deg=90.0)
    out = generate_pcb(parse)
    # Pro Micro footprint anchored at (20, 10) with KiCad angle -90 (SVG CW = KiCad CCW negative).
    assert re.search(
        r'\(footprint "keeb:Arduino_Pro_Micro".*?\(at 20\.0000 10\.0000 -90\.000\)',
        out,
        re.DOTALL,
    )


def test_outline_grow_dilates_edge_cuts() -> None:
    """A 100 × 50 mm rectangular plate dilated by 5 mm should produce an
    Edge.Cuts ring at (-5, -5)..(105, 55) — a 110 × 60 rectangle with
    mitered corners (no arc segments)."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    parse.outline_grow_mm = 5.0
    out = generate_pcb(parse)
    edge_lines = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\).+?Edge\.Cuts',
        out,
    )
    assert len(edge_lines) == 4, f"expected 4 mitered edges, got {len(edge_lines)}"
    xs = {round(float(c), 2) for seg in edge_lines for c in (seg[0], seg[2])}
    ys = {round(float(c), 2) for seg in edge_lines for c in (seg[1], seg[3])}
    assert xs == {-5.0, 105.0}, f"x extremes wrong: {xs}"
    assert ys == {-5.0, 55.0}, f"y extremes wrong: {ys}"


def test_edited_outline_replaces_parsed_outline() -> None:
    """When `edited_outline_path_d` is set with no growth, the PCB
    Edge.Cuts uses that polygon verbatim — even concave notches survive."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    parse.edited_outline_path_d = (
        "M -1 -1 L 101 -1 L 101 51 L 60 51 L 60 40 L 40 40 L 40 51 L -1 51 Z"
    )
    parse.outline_grow_mm = 0.0
    out = generate_pcb(parse)
    edges = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\)',
        out,
    )
    coords = {(round(float(s), 1), round(float(t), 1)) for seg in edges for s, t in [(seg[0], seg[1]), (seg[2], seg[3])]}
    # Notch coordinates must be present verbatim.
    assert (60.0, 40.0) in coords
    assert (40.0, 40.0) in coords


def test_edited_outline_plus_grow_dilates_the_edited_polygon() -> None:
    """outline_grow_mm should still work on top of an edited polygon —
    dilating it rather than the parsed outline. Confirms grow is not
    silently ignored after edits."""
    parse = _result(switches=[_sw(1, 50, 25)], width=100.0, height=50.0)
    # 80×40 mm rect centered in the parse's 100×50 viewport.
    parse.edited_outline_path_d = "M 10 5 L 90 5 L 90 45 L 10 45 Z"
    parse.outline_grow_mm = 3.0
    out = generate_pcb(parse)
    edges = re.findall(
        r'gr_line \(start ([-+\d.]+) ([-+\d.]+)\) \(end ([-+\d.]+) ([-+\d.]+)\)',
        out,
    )
    xs = {round(float(c), 1) for seg in edges for c in (seg[0], seg[2])}
    ys = {round(float(c), 1) for seg in edges for c in (seg[1], seg[3])}
    # Edited rect (10..90 × 5..45) dilated 3 mm → (7..93 × 2..48).
    assert xs == {7.0, 93.0}
    assert ys == {2.0, 48.0}


def test_outline_grow_zero_is_passthrough() -> None:
    """outline_grow_mm == 0 must take the no-buffer path so generation
    stays byte-identical to the pre-feature behavior (regression guard
    against accidental Shapely round-trip)."""
    parse_a = _result(switches=[_sw(1, 50, 25)])
    parse_b = _result(switches=[_sw(1, 50, 25)])
    parse_b.outline_grow_mm = 0.0
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
        r'\(pad "1" smd rect \(at -7\.085 -2\.54\) \(size 2\.55 2\.5\)\s*'
        r'\(layers "B\.Cu" "B\.Paste" "B\.Mask"\)',
        out,
    )
    # SMD pad 2 on B.Cu at (5.842, -5.08).
    assert re.search(
        r'\(pad "2" smd rect \(at 5\.842 -5\.08\)',
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
        r'\(pad "1" smd rect \(at -1\.65 0\) \(size 1\.0 0\.6\)\s*'
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
        _sw(1, 0.0, 0.0, row=0, col=0),
        _sw(2, 19.05, 0.0, row=0, col=1),
        _sw(3, 0.0, 19.05, row=1, col=0),
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
    centered on A4 (297×210, landscape). The switch at (0, 0) of the
    parsed board should move by half the page minus half the board."""
    sws = [_sw(1, 0.0, 0.0)]
    out = _real_generate_pcb(_result(switches=sws, width=100.0, height=50.0))
    # A4 (page center 148.5, 105) minus board half (50, 25) → switch lands at
    # (98.5, 80) in page coords.
    m = re.search(
        r'\(footprint "keeb:SW_Cherry_MX_PCB_1\.00u".*?'
        r'\(at ([-\d.]+) ([-\d.]+)',
        out, re.DOTALL,
    )
    assert m is not None, "no soldered switch footprint found"
    x, y = float(m.group(1)), float(m.group(2))
    assert abs(x - 98.5) < 0.01, f"expected switch x≈98.5, got {x}"
    assert abs(y - 80.0) < 0.01, f"expected switch y≈80.0, got {y}"


def test_centering_picks_a3_when_board_exceeds_a4() -> None:
    """A 300×200 board doesn't fit A4 (must clear 297×210 minus 2×20mm
    margin = 257×170) but fits A3 (380×257 inside its margin). Verify
    the paper token reflects that pick."""
    sws = [_sw(1, 0, 0)]
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
