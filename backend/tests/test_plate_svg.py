import re

from app.models.schemas import (
    McuPlacement,
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
)
from app.services.plate_svg import generate_plate_svg
from app.services.svg_parser import parse_plate_svg


def _result(
    switches: list[SwitchDef] | None = None,
    stabilizers: list[StabilizerDef] | None = None,
    mounting_holes: list[MountingHoleDef] | None = None,
    outline_shrink_mm: float = 0.0,
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
        outline_shrink_mm=outline_shrink_mm,
        mcu_placement=McuPlacement(cx_mm=0, cy_mm=0, rotation_deg=0.0),
    )


def _sw(id_: int, cx: float, cy: float, rotation: float = 0.0) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=cx, cy_mm=cy, rotation_deg=rotation)


def test_generate_plate_svg_emits_well_formed_svg() -> None:
    out = generate_plate_svg(_result(switches=[_sw(1, 50, 25)]))
    assert out.startswith('<?xml version="1.0"')
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in out
    assert out.rstrip().endswith("</svg>")
    # Outline polygon present.
    assert re.search(r'<path d="M [^"]+"', out)
    # Switch cutout at (50, 25) — 14×14 mm centered, so x=43 y=18.
    assert re.search(
        r'<rect x="43\.0000" y="18\.0000" width="14(?:\.0+)?" height="14(?:\.0+)?"',
        out,
    )


def test_generate_plate_svg_ignores_shrink() -> None:
    """The plate IS the reference outline: `outline_shrink_mm` insets the
    PCB only, so the plate export must be identical with or without it."""
    plain = generate_plate_svg(_result(switches=[_sw(1, 50, 25)]))
    shrunk = generate_plate_svg(
        _result(switches=[_sw(1, 50, 25)], outline_shrink_mm=5.0)
    )
    assert plain == shrunk


def test_generate_plate_svg_no_grow_matches_original_bounds() -> None:
    out = generate_plate_svg(_result(switches=[_sw(1, 50, 25)]))
    # ViewBox starts at (≈-2, ≈-2) due to SVG_MARGIN_MM only.
    m = re.search(r'viewBox="([-\d.]+) ([-\d.]+)', out)
    assert m
    assert abs(float(m.group(1)) + 2.0) < 0.01
    assert abs(float(m.group(2)) + 2.0) < 0.01


def test_edited_outline_with_zero_shrink_uses_edited_polygon() -> None:
    parse = _result(switches=[_sw(1, 50, 25)], outline_shrink_mm=0.0)
    parse.edited_outline_path_d = "M 5 5 L 95 5 L 95 45 L 5 45 Z"
    out = generate_plate_svg(parse)
    # Outline path starts with the edited polygon's first point.
    assert re.search(r'<path d="M\s+5\.0000\s+5\.0000', out)


def test_edited_outline_plate_ignores_shrink() -> None:
    """Shrink never touches the plate export, even with an edited outline:
    the plate path keeps the edited polygon verbatim."""
    parse = _result(switches=[_sw(1, 50, 25)], outline_shrink_mm=4.0)
    parse.edited_outline_path_d = "M 10 10 L 90 10 L 90 40 L 10 40 Z"
    out = generate_plate_svg(parse)
    # Outline path's first vertex is the edited polygon's own (10, 10).
    assert re.search(r'<path d="M\s+10\.0000\s+10\.0000', out)


def test_unit_override_rescales(example_plate_svg: str) -> None:
    """Forcing the override to `in` (inches) re-scales the parse: a plate
    that's natively 228.6 mm becomes 228.6 * 25.4 = 5806.44 mm."""
    parse_in = parse_plate_svg(example_plate_svg, svg_unit_override="in")
    assert abs(parse_in.svg_width_mm - 228.6 * 25.4) < 0.1
    assert parse_in.detected_svg_unit == "in"
    parse_auto = parse_plate_svg(example_plate_svg, svg_unit_override="auto")
    assert abs(parse_auto.svg_width_mm - 228.6) < 0.1


def test_generate_plate_svg_kbplate_round_trip(example_plate_svg: str) -> None:
    """End-to-end on the real kbplate fixture — the PCB inset must not
    leak into the plate export."""
    parse = parse_plate_svg(example_plate_svg)
    parse.outline_shrink_mm = 2.0
    out = generate_plate_svg(parse)

    # Every switch ends up with a cutout rect — count them.
    n_sw_rects = len(re.findall(r'<rect [^>]*width="14(?:\.0+)?"', out))
    assert n_sw_rects == len(parse.switches)
    # Identical to the un-shrunk export.
    parse.outline_shrink_mm = 0.0
    assert out == generate_plate_svg(parse)
