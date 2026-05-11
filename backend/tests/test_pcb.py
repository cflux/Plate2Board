import re

import pytest

from app.models.schemas import (
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
    UnclassifiedShape,
    MountingHoleDef,
)
from app.services.pcb import generate_pcb
from app.services.svg_parser import parse_plate_svg


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
    # Switch footprint at exactly the parsed center, rotated 10°.
    assert re.search(
        r'\(footprint "Button_Switch_Keyboard:SW_Cherry_MX_1\.00u_PCB".*?'
        r'\(at 19\.0500 9\.5250 10\.000\)',
        out,
        re.DOTALL,
    )
    # Reference SW1 + diode D1 + Pro Micro U1 all present.
    assert '"SW1"' in out
    assert '"D1"' in out
    assert '"U1"' in out
    assert "Module:Arduino_Pro_Micro" in out
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
        r'\(footprint "Diode_THT:D_DO-35_SOD27_P7\.62mm_Horizontal"'
        r'\s*\(layer "F\.Cu"\)\s*\(uuid "[^"]+"\)\s*'
        r'\(at ([-\d.]+) ([-\d.]+) ([\d.]+)\)',
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


def test_stabilizer_cutout_mirrors_svg_geometry_exactly() -> None:
    """Each stabilizer cutout in the SVG → one Edge.Cuts rectangle on the
    PCB at the detected (cx, cy, width, height, rotation). No fixed-geometry
    2U/6.25U/etc. footprint with internal pad spacing — we trust the plate."""
    stab = StabilizerDef(
        id=1, cx_mm=30.0, cy_mm=20.0, width_mm=14.0, height_mm=7.0, rotation_deg=0.0
    )
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)], stabilizers=[stab]))

    # New per-cutout footprint name; old hardcoded 2U name must be gone.
    assert '"keeb-layout-bot:Stabilizer_Cutout"' in out
    assert "Mounting_Keyboard_Stabilizer:Stabilizer_Cherry_MX_2.00u" not in out
    assert "(at 30.0000 20.0000 0.000)" in out

    stab_block = re.search(
        r'\(footprint "keeb-layout-bot:Stabilizer_Cutout".+?\n\t\)',
        out,
        re.DOTALL,
    )
    assert stab_block, "stabilizer block missing"
    block = stab_block.group(0)

    # No electrical pads, no synthetic NPTHs.
    assert not re.search(r'\(pad "[12]" ', block)
    assert "(at -11.938" not in block
    assert "(at 11.938" not in block

    # Exactly 4 Edge.Cuts segments forming the rectangle.
    edge_lines = re.findall(r'\(fp_line.+?\(layer "Edge\.Cuts"\)', block)
    assert len(edge_lines) == 4

    # Corners in local (footprint) frame: (±7, ±3.5) for a 14 × 7 mm cutout.
    coords = re.findall(
        r"\(start (-?\d+\.\d+) (-?\d+\.\d+)\) \(end (-?\d+\.\d+) (-?\d+\.\d+)\)",
        block,
    )
    seen_xs = {round(float(c[0]), 1) for c in coords} | {
        round(float(c[2]), 1) for c in coords
    }
    seen_ys = {round(float(c[1]), 1) for c in coords} | {
        round(float(c[3]), 1) for c in coords
    }
    assert seen_xs == {-7.0, 7.0}
    assert seen_ys == {-3.5, 3.5}


def test_outline_becomes_edge_cuts() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 25)]))
    # Bounding-box outline → 4 gr_line segments on Edge.Cuts.
    edge_lines = re.findall(r'gr_line.*?layer "Edge\.Cuts"', out)
    assert len(edge_lines) == 4


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
    assert "Module:Arduino_Pro_Micro" in out
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
    assert '"Switch_Keyboard_Hotswap_Kailh:SW_Hotswap_Kailh_MX_1.00u"' in out
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
    assert '"Diode_SMD:D_SOD-123"' in out
    # Pads on B.Cu, no thru-hole drill.
    assert re.search(
        r'\(pad "1" smd rect \(at -1\.65 0\) \(size 1\.0 0\.6\)\s*'
        r'\(layers "B\.Cu" "B\.Paste" "B\.Mask"\)',
        out,
    )
    # No THT diode present.
    assert "Diode_THT:D_DO-35" not in out


def test_tht_diode_remains_default() -> None:
    out = generate_pcb(_result(switches=[_sw(1, 50, 50)]))
    assert "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal" in out
    assert "Diode_SMD:D_SOD-123" not in out


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
