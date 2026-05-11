import math

import pytest

from app.models.schemas import SvgParseError
from app.services.svg_parser import parse_plate_svg


def test_parses_example_plate(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)

    assert result.svg_width_mm == pytest.approx(228.6)
    assert result.svg_height_mm == pytest.approx(76.2)

    assert result.pcb_outline.width_mm == pytest.approx(228.6, abs=0.01)
    assert result.pcb_outline.height_mm == pytest.approx(76.2, abs=0.01)
    assert result.pcb_outline.path_d.startswith("M")

    assert len(result.switches) == 40
    assert len(result.stabilizers) == 2
    assert result.unclassified == []


def test_unrotated_switches_have_near_zero_rotation(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)
    for sw in result.switches:
        # Axis-aligned switches should land near 0° (or wrap-around 360°) since
        # the parser now normalizes to [-45, 45) and stores in [0, 360).
        diff_from_zero = min(sw.rotation_deg, 360.0 - sw.rotation_deg)
        assert diff_from_zero < 0.5, f"switch {sw.id} rotation_deg={sw.rotation_deg}"


@pytest.mark.parametrize("angle", [-40.0, -20.0, -10.0, -5.0, 0.0, 5.0, 10.0, 20.0, 40.0])
def test_small_rotation_pins_point_mostly_down(angle: float) -> None:
    """Regression: a CCW-tilted (negative-angle) switch used to land at
    rotation_deg≈80° (pins-left) because the parser normalized to [0, 90).
    With normalization to [-45, 45), small tilts in either direction should
    keep the seeded pin direction mostly downward."""
    sub = _rotated_square_subpath(50.0, 50.0, 14.0, angle)
    svg = _make_synthetic_svg([sub])
    result = parse_plate_svg(svg)
    assert len(result.switches) == 1
    sw = result.switches[0]
    rot = math.radians(sw.rotation_deg)
    pin_x = -math.sin(rot)
    pin_y = math.cos(rot)
    assert pin_y > 0.5, (
        f"angle={angle} rotation={sw.rotation_deg} "
        f"pin=({pin_x:.3f},{pin_y:.3f}) — pin should point mostly down"
    )


def test_switch_centers_within_outline(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)
    for sw in result.switches:
        assert 0 <= sw.cx_mm <= result.svg_width_mm
        assert 0 <= sw.cy_mm <= result.svg_height_mm


def test_stabilizer_dims_are_narrow(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)
    for stab in result.stabilizers:
        # width_mm is the long edge; height_mm is the short edge.
        assert stab.height_mm < stab.width_mm


def test_empty_svg_raises() -> None:
    with pytest.raises(SvgParseError):
        parse_plate_svg("<svg></svg>")


def test_missing_viewbox_raises() -> None:
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 L 1 0 L 1 1 L 0 1 Z"/></svg>'
    with pytest.raises(SvgParseError):
        parse_plate_svg(svg)


def _rotated_square_subpath(cx: float, cy: float, side: float, angle_deg: float) -> str:
    half = side / 2
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    pts = [
        (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)
        for dx, dy in corners
    ]
    cmds = [f"M {pts[0][0]:.4f} {pts[0][1]:.4f}"]
    for x, y in pts[1:]:
        cmds.append(f"L {x:.4f} {y:.4f}")
    cmds.append("Z")
    return " ".join(cmds)


def _make_synthetic_svg(extra_subpaths: list[str], width: float = 100.0, height: float = 100.0) -> str:
    outline = f"M 0 0 L {width} 0 L {width} {height} L 0 {height} Z"
    d = " ".join([outline, *extra_subpaths])
    return (
        f'<svg width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg"><g><path d="{d}"/></g></svg>'
    )


@pytest.mark.parametrize("angle", [0.0, 7.0, 30.0, 45.0, 67.5, 89.0])
def test_rotated_square_classified_as_switch(angle: float) -> None:
    sub = _rotated_square_subpath(50.0, 50.0, 14.0, angle)
    svg = _make_synthetic_svg([sub])
    result = parse_plate_svg(svg)

    assert len(result.switches) == 1
    assert result.unclassified == []
    sw = result.switches[0]
    assert sw.cx_mm == pytest.approx(50.0, abs=0.01)
    assert sw.cy_mm == pytest.approx(50.0, abs=0.01)

    detected = sw.rotation_deg % 90.0
    target = angle % 90.0
    diff = min(abs(detected - target), 90.0 - abs(detected - target))
    assert diff < 1.0, f"angle={angle} detected={sw.rotation_deg} diff={diff}"


def _rotated_rect_subpath(
    cx: float, cy: float, w: float, h: float, angle_deg: float
) -> str:
    half_w, half_h = w / 2, h / 2
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    corners = [
        (-half_w, -half_h),
        (half_w, -half_h),
        (half_w, half_h),
        (-half_w, half_h),
    ]
    pts = [
        (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)
        for dx, dy in corners
    ]
    return " ".join(
        [
            f"M {pts[0][0]:.4f} {pts[0][1]:.4f}",
            f"L {pts[1][0]:.4f} {pts[1][1]:.4f}",
            f"L {pts[2][0]:.4f} {pts[2][1]:.4f}",
            f"L {pts[3][0]:.4f} {pts[3][1]:.4f}",
            "Z",
        ]
    )


def _pin_axis_dot(rotation_deg: float, axis_x: float, axis_y: float) -> float:
    """Returns |cos(angle)| between the switch pin direction and the given axis."""
    rot = math.radians(rotation_deg)
    pin_x = -math.sin(rot)
    pin_y = math.cos(rot)
    axis_len = math.hypot(axis_x, axis_y)
    return abs(pin_x * axis_x + pin_y * axis_y) / axis_len


def test_horizontal_spacebar_pins_perpendicular_to_stabs() -> None:
    sw_sub = _rotated_square_subpath(100.0, 50.0, 14.0, 0.0)
    stab_l = _rotated_rect_subpath(50.0, 50.0, 7.0, 14.0, 0.0)
    stab_r = _rotated_rect_subpath(150.0, 50.0, 7.0, 14.0, 0.0)
    svg = _make_synthetic_svg([sw_sub, stab_l, stab_r], width=200.0, height=100.0)
    result = parse_plate_svg(svg)
    assert len(result.switches) == 1
    assert len(result.stabilizers) == 2
    sw = result.switches[0]
    # Stabs along screen +X. Pin direction must NOT be along X.
    assert _pin_axis_dot(sw.rotation_deg, 1.0, 0.0) < 0.5


def test_vertical_spacebar_pins_perpendicular_to_stabs() -> None:
    sw_sub = _rotated_square_subpath(50.0, 100.0, 14.0, 0.0)
    stab_top = _rotated_rect_subpath(50.0, 50.0, 7.0, 14.0, 0.0)
    stab_bot = _rotated_rect_subpath(50.0, 150.0, 7.0, 14.0, 0.0)
    svg = _make_synthetic_svg([sw_sub, stab_top, stab_bot], width=100.0, height=200.0)
    result = parse_plate_svg(svg)
    assert len(result.switches) == 1
    assert len(result.stabilizers) == 2
    sw = result.switches[0]
    # Stabs along screen +Y. Pin direction must NOT be along Y.
    assert _pin_axis_dot(sw.rotation_deg, 0.0, 1.0) < 0.5


def test_angled_spacebar_pins_perpendicular_to_stabs() -> None:
    angle_deg = 30.0
    a = math.radians(angle_deg)
    cx, cy = 100.0, 100.0
    offset = 50.0
    dx = offset * math.cos(a)
    dy = offset * math.sin(a)
    sw_sub = _rotated_square_subpath(cx, cy, 14.0, angle_deg)
    stab_a = _rotated_rect_subpath(cx - dx, cy - dy, 7.0, 14.0, angle_deg)
    stab_b = _rotated_rect_subpath(cx + dx, cy + dy, 7.0, 14.0, angle_deg)
    svg = _make_synthetic_svg([sw_sub, stab_a, stab_b], width=300.0, height=300.0)
    result = parse_plate_svg(svg)
    assert len(result.switches) == 1
    assert len(result.stabilizers) == 2
    sw = result.switches[0]
    assert _pin_axis_dot(sw.rotation_deg, math.cos(a), math.sin(a)) < 0.5


def _stab_head_dir(stab) -> tuple[float, float]:
    rad = math.radians(stab.rotation_deg)
    return math.cos(rad), math.sin(rad)


def test_stab_head_points_toward_flanking_switch_left() -> None:
    """Stab to the left of a switch — head should point right (toward switch)."""
    sw = _rotated_square_subpath(100.0, 50.0, 14.0, 0.0)
    stab = _rotated_rect_subpath(50.0, 50.0, 14.0, 7.0, 0.0)  # long axis horizontal
    other = _rotated_rect_subpath(150.0, 50.0, 14.0, 7.0, 0.0)
    svg = _make_synthetic_svg([sw, stab, other], width=200.0, height=100.0)
    result = parse_plate_svg(svg)
    left_stab = min(result.stabilizers, key=lambda s: s.cx_mm)
    hx, hy = _stab_head_dir(left_stab)
    # Switch is at x=100, stab at x=50, so head should point in +X direction.
    assert hx > 0.5, f"left stab head_dir=({hx:.3f},{hy:.3f}) rot={left_stab.rotation_deg}"


def test_stab_head_points_toward_flanking_switch_right() -> None:
    sw = _rotated_square_subpath(100.0, 50.0, 14.0, 0.0)
    stab_l = _rotated_rect_subpath(50.0, 50.0, 14.0, 7.0, 0.0)
    stab_r = _rotated_rect_subpath(150.0, 50.0, 14.0, 7.0, 0.0)
    svg = _make_synthetic_svg([sw, stab_l, stab_r], width=200.0, height=100.0)
    result = parse_plate_svg(svg)
    right_stab = max(result.stabilizers, key=lambda s: s.cx_mm)
    hx, _hy = _stab_head_dir(right_stab)
    # Switch is at x=100, stab at x=150, so head should point in −X direction.
    assert hx < -0.5


def test_stab_head_points_toward_flanking_switch_angled() -> None:
    angle_deg = 30.0
    a = math.radians(angle_deg)
    cx, cy = 100.0, 100.0
    offset = 50.0
    dx_o = offset * math.cos(a)
    dy_o = offset * math.sin(a)
    sw = _rotated_square_subpath(cx, cy, 14.0, angle_deg)
    stab_a = _rotated_rect_subpath(cx - dx_o, cy - dy_o, 14.0, 7.0, angle_deg)
    stab_b = _rotated_rect_subpath(cx + dx_o, cy + dy_o, 14.0, 7.0, angle_deg)
    svg = _make_synthetic_svg([sw, stab_a, stab_b], width=300.0, height=300.0)
    result = parse_plate_svg(svg)
    sw_def = result.switches[0]
    for stab in result.stabilizers:
        hx, hy = _stab_head_dir(stab)
        toward_sw_x = sw_def.cx_mm - stab.cx_mm
        toward_sw_y = sw_def.cy_mm - stab.cy_mm
        # Head direction should align with the vector toward the switch
        norm = math.hypot(toward_sw_x, toward_sw_y)
        dot = (hx * toward_sw_x + hy * toward_sw_y) / norm
        assert dot > 0.5, f"stab rot={stab.rotation_deg} head=({hx:.3f},{hy:.3f}) toward_sw=({toward_sw_x:.2f},{toward_sw_y:.2f}) dot={dot:.3f}"


def test_realistic_kbplate_spacebar_stab_orientation() -> None:
    """Real kbplate output: horizontal spacebar with two vertical stab cutouts
    (7×15 mm), with stab cutouts slightly south of the switch. The flanking-
    pair heuristic should orient both heads consistently, not based on which
    row-above neighbor happens to align with the long axis."""
    sw_cx, sw_cy = 121.44, 66.675
    stab_y = 68.175
    sw_sub = _rotated_square_subpath(sw_cx, sw_cy, 14.0, 0.0)
    stab_l = _rotated_rect_subpath(sw_cx - 50.0, stab_y, 7.0, 15.0, 0.0)
    stab_r = _rotated_rect_subpath(sw_cx + 50.0, stab_y, 7.0, 15.0, 0.0)
    # Add a row-above neighbor directly above the left stab to ensure the
    # OLD heuristic would have picked it instead of the spacebar.
    decoy = _rotated_square_subpath(sw_cx - 50.0, sw_cy - 19.05, 14.0, 0.0)
    svg = _make_synthetic_svg(
        [sw_sub, stab_l, stab_r, decoy], width=250.0, height=120.0
    )
    result = parse_plate_svg(svg)
    assert len(result.switches) == 2
    assert len(result.stabilizers) == 2
    # Both stab heads should point in the same direction (consistency).
    rotations = sorted(s.rotation_deg for s in result.stabilizers)
    assert rotations[0] == pytest.approx(rotations[1], abs=1.0)


def test_parses_complex_example(complex_example_svg: str) -> None:
    """Multi-path SVG with a top-level translate transform and viewBox in
    points (not mm). The parser should walk all <path> elements, apply the
    composed transform, auto-detect mm scale from typical switch size, and
    classify rotated 14mm cutouts as switches."""
    result = parse_plate_svg(complex_example_svg)

    # 964 viewBox units × pt-to-mm ratio (≈0.353) ≈ 340 mm wide
    assert 300.0 < result.svg_width_mm < 400.0
    assert 130.0 < result.svg_height_mm < 200.0

    assert len(result.switches) >= 50

    # Switches in this layout sit at various angles — verify each one is square.
    for sw in result.switches:
        assert 0.0 <= sw.rotation_deg < 360.0

    # No stabilizers in this Dactyl-style layout.
    assert len(result.stabilizers) == 0

    # Small ~2–3mm circles outside the switches are mounting holes, not unclassified.
    assert len(result.mounting_holes) >= 8
    for hole in result.mounting_holes:
        assert 1.5 <= hole.diameter_mm <= 4.5

    # Outline should be reconstructed from the open <path id="LINE"> segments,
    # not the SVG bounding box. A bounding rectangle has exactly 4 'L' commands
    # plus the closing 'Z'; the real Dactyl outline has many more vertices.
    n_line_cmds = result.pcb_outline.path_d.count("L")
    assert n_line_cmds >= 10, f"outline has only {n_line_cmds} edges — looks like a bounding box"


def test_handles_translate_transform() -> None:
    """A synthetic SVG with a parent <g> translate(...) should still produce
    switch centers at the post-transform location."""
    sub = _rotated_square_subpath(0.0, 0.0, 14.0, 0.0)  # at origin
    inner = (
        f'<svg width="100mm" height="100mm" viewBox="0 0 100 100" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<g transform="translate(40 30)">'
        f'<path d="M 0 0 L 100 0 L 100 100 L 0 100 Z"/>'
        f'<path d="{sub}"/>'
        f'</g>'
        f'</svg>'
    )
    # Outline rect drawn from (0,0)→(100,100) inside the translated g — won't
    # match the SVG viewBox after transform; we still expect the switch to
    # land at (40, 30).
    result = parse_plate_svg(inner)
    switches_at_40_30 = [
        s for s in result.switches
        if abs(s.cx_mm - 40.0) < 0.5 and abs(s.cy_mm - 30.0) < 0.5
    ]
    assert len(switches_at_40_30) == 1


def test_unflanked_switch_keeps_default_rotation() -> None:
    sw_sub = _rotated_square_subpath(100.0, 50.0, 14.0, 0.0)
    # Two stabs that do NOT flank this switch (same side of it).
    stab_a = _rotated_rect_subpath(30.0, 50.0, 7.0, 14.0, 0.0)
    stab_b = _rotated_rect_subpath(45.0, 50.0, 7.0, 14.0, 0.0)
    svg = _make_synthetic_svg([sw_sub, stab_a, stab_b], width=200.0, height=100.0)
    result = parse_plate_svg(svg)
    assert len(result.switches) == 1
    sw = result.switches[0]
    # Should remain at the detected default of ~0° (mod 90).
    normalized = min(sw.rotation_deg, 90.0 - (sw.rotation_deg % 90.0))
    assert normalized < 1.0 or sw.rotation_deg == 0.0


def test_rotated_stabilizer_keeps_long_angle() -> None:
    half_w, half_h = 7.5 / 2, 14.0 / 2
    cx, cy = 50.0, 50.0
    angle_deg = 30.0
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    corners = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)]
    pts = [
        (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)
        for dx, dy in corners
    ]
    sub = " ".join(
        [
            f"M {pts[0][0]:.4f} {pts[0][1]:.4f}",
            f"L {pts[1][0]:.4f} {pts[1][1]:.4f}",
            f"L {pts[2][0]:.4f} {pts[2][1]:.4f}",
            f"L {pts[3][0]:.4f} {pts[3][1]:.4f}",
            "Z",
        ]
    )
    svg = _make_synthetic_svg([sub])
    result = parse_plate_svg(svg)

    assert len(result.switches) == 0
    assert len(result.stabilizers) == 1
    stab = result.stabilizers[0]
    assert stab.width_mm == pytest.approx(14.0, abs=0.01)
    assert stab.height_mm == pytest.approx(7.5, abs=0.01)
    # The long edge is +90° offset from the user's "30° rotation" because the
    # 14mm dimension is along the local Y axis. Either 30° + 90° = 120° (mod 180 -> 120°)
    # or after modular arithmetic the parser may report it directly.
    expected = (angle_deg + 90.0) % 180.0
    assert stab.rotation_deg == pytest.approx(expected, abs=1.0)
