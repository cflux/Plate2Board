"""Generate a clean keyboard plate SVG from a parsed ParseResult.

Output is laser-cut-ready: every feature is a black-stroked, fill-less
outline so a typical CO2 / fiber cutter interprets each shape as a cut.
The plate outline uses ``outline_grow_mm`` (Shapely mitered buffer) so
the exported plate matches the PCB Edge.Cuts the generator emits.
"""

from __future__ import annotations

from ..models.schemas import (
    ParseResult,
    StabilizerDef,
    SwitchDef,
)
from .pcb import _grow_polygon_points, _parse_path_points
from .svg_parser import _polygon_to_path_d

SWITCH_CUTOUT_MM = 14.0
SVG_MARGIN_MM = 2.0
STROKE_WIDTH_MM = 0.1


def generate_plate_svg(parse: ParseResult) -> str:
    """Emit a single-layer SVG with the (optionally grown) outline plus
    every cutout. Coordinates stay in mm; the SVG viewBox matches the
    grown outline bbox plus a small margin so the file is print-ready."""
    # Use the user-edited polygon (if any) as the base shape; otherwise the
    # parsed outline. Either way, `outline_grow_mm` dilates the result so
    # the grow control keeps working after edits.
    base_path = parse.edited_outline_path_d or parse.pcb_outline.path_d
    outline_points = _parse_path_points(base_path)
    if not outline_points:
        # Fall back to a rectangle matching the SVG viewBox.
        w, h = parse.svg_width_mm, parse.svg_height_mm
        outline_points = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h), (0.0, 0.0)]
    if parse.outline_grow_mm > 0:
        outline_points = _grow_polygon_points(
            outline_points, parse.outline_grow_mm
        )

    xs = [p[0] for p in outline_points]
    ys = [p[1] for p in outline_points]
    xmin = min(xs) - SVG_MARGIN_MM
    ymin = min(ys) - SVG_MARGIN_MM
    xmax = max(xs) + SVG_MARGIN_MM
    ymax = max(ys) + SVG_MARGIN_MM
    w = xmax - xmin
    h = ymax - ymin

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{w:.4f}mm" height="{h:.4f}mm" '
        f'viewBox="{xmin:.4f} {ymin:.4f} {w:.4f} {h:.4f}">'
    )
    parts.append(
        f'  <g fill="none" stroke="black" stroke-width="{STROKE_WIDTH_MM}">'
    )

    # Plate outline (grown if outline_grow_mm > 0).
    outline_d = _polygon_to_path_d(list(outline_points))
    parts.append(f'    <path d="{outline_d}" />')

    # Switch cutouts: 14 × 14 mm centered on each switch, rotated by the
    # switch's own SVG rotation.
    for sw in parse.switches:
        parts.append("    " + _switch_cutout_svg(sw))

    # Stabilizer cutouts: detected width × height at detected position+rotation.
    for stab in parse.stabilizers:
        parts.append("    " + _stab_cutout_svg(stab))

    # Mounting holes: simple circles at the detected diameter.
    for mh in parse.mounting_holes:
        parts.append(
            f'    <circle cx="{mh.cx_mm:.4f}" cy="{mh.cy_mm:.4f}" '
            f'r="{mh.diameter_mm / 2:.4f}" />'
        )

    parts.append("  </g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _switch_cutout_svg(sw: SwitchDef) -> str:
    half = SWITCH_CUTOUT_MM / 2
    return (
        f'<rect x="{sw.cx_mm - half:.4f}" y="{sw.cy_mm - half:.4f}" '
        f'width="{SWITCH_CUTOUT_MM}" height="{SWITCH_CUTOUT_MM}" '
        f'transform="rotate({sw.rotation_deg:.3f} '
        f'{sw.cx_mm:.4f} {sw.cy_mm:.4f})" />'
    )


def _stab_cutout_svg(stab: StabilizerDef) -> str:
    return (
        f'<rect x="{stab.cx_mm - stab.width_mm / 2:.4f}" '
        f'y="{stab.cy_mm - stab.height_mm / 2:.4f}" '
        f'width="{stab.width_mm:.4f}" height="{stab.height_mm:.4f}" '
        f'transform="rotate({stab.rotation_deg:.3f} '
        f'{stab.cx_mm:.4f} {stab.cy_mm:.4f})" />'
    )
