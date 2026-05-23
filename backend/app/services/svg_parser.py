from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass

from lxml import etree
from shapely.geometry import MultiPoint, Point, Polygon
from svgpathtools import parse_path

from ..models.schemas import (
    McuPlacement,
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SvgParseError,
    SwitchDef,
    UnclassifiedShape,
)
from .matrix import assign_matrix

SWITCH_DIM_MM = 14.0
SWITCH_TOL_MM = 1.0
OUTLINE_TOL_MM = 1.0
MIN_SHAPE_DIM_MM = 0.5
STAB_LONG_MIN_MM = 12.0
STAB_LONG_MAX_MM = 18.0
STAB_SHORT_MIN_MM = 4.0
STAB_SHORT_MAX_MM = 12.0
MOUNT_HOLE_DIAM_MIN_MM = 1.5  # M1.6 with tolerance
MOUNT_HOLE_DIAM_MAX_MM = 4.5  # M3.5 with tolerance
PATH_SAMPLE_COUNT = 80
STAB_PAIR_MAX_DIST_MM = 75.0
STAB_PAIR_MIDPOINT_TOL_MM = 2.0
PIN_PARALLEL_DOT_THRESHOLD = math.cos(math.radians(45.0))
SQUARE_RATIO_TOL = 0.05  # how close long/short ratio must be to 1.0 to count as square
OUTLINE_SNAP_TOL_MM = 0.05  # endpoint-matching tolerance when stitching open lines
OUTLINE_SWITCH_COVERAGE = 0.95  # reconstructed outline must enclose this fraction of switches

# (a, b, c, d, e, f): the 2x3 affine matrix
# [a c e]
# [b d f]
# Maps (x, y) -> (a*x + c*y + e, b*x + d*y + f)
Matrix = tuple[float, float, float, float, float, float]
IDENTITY_MATRIX: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass
class _Mabb:
    cx: float
    cy: float
    long_mm: float
    short_mm: float
    long_angle_deg: float


@dataclass
class _Subpath:
    index: int
    d: str
    axis_xmin: float
    axis_ymin: float
    axis_xmax: float
    axis_ymax: float
    mabb: _Mabb
    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    is_closed: bool

    @property
    def axis_width(self) -> float:
        return self.axis_xmax - self.axis_xmin

    @property
    def axis_height(self) -> float:
        return self.axis_ymax - self.axis_ymin


def parse_plate_svg(
    svg_text: str,
    matrix_strategy: str = "auto",
    svg_unit_override: str | None = None,
) -> ParseResult:
    try:
        root = etree.fromstring(svg_text.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        raise SvgParseError(f"SVG is not well-formed XML: {exc}") from exc

    svg_w_units, svg_h_units = _read_viewbox(root)

    subpaths = _collect_subpaths(root)
    if not subpaths:
        raise SvgParseError("SVG has no path sub-paths")

    if svg_unit_override and svg_unit_override != "auto":
        if svg_unit_override not in _UNIT_TO_MM:
            raise SvgParseError(
                f"unknown svg_unit_override: {svg_unit_override!r}"
            )
        mm_per_unit = _UNIT_TO_MM[svg_unit_override]
        detected_unit = svg_unit_override
    else:
        mm_per_unit, detected_unit = _detect_mm_scale(root, svg_w_units, subpaths)
    if mm_per_unit != 1.0:
        for sub in subpaths:
            _scale_subpath(sub, mm_per_unit)

    svg_w_mm = svg_w_units * mm_per_unit
    svg_h_mm = svg_h_units * mm_per_unit

    outline_idx = _find_outline(subpaths, svg_w_mm, svg_h_mm)

    switches: list[SwitchDef] = []
    stabilizers: list[StabilizerDef] = []
    mounting_holes: list[MountingHoleDef] = []
    unclassified: list[UnclassifiedShape] = []
    next_id = 1

    for i, sub in enumerate(subpaths):
        if outline_idx is not None and i == outline_idx:
            continue
        if sub.mabb.long_mm < MIN_SHAPE_DIM_MM or sub.mabb.short_mm < MIN_SHAPE_DIM_MM:
            continue
        if _is_switch(sub.mabb):
            angle = sub.mabb.long_angle_deg % 90.0
            if angle > 45.0:
                angle -= 90.0
            switches.append(
                SwitchDef(
                    id=next_id,
                    cx_mm=round(sub.mabb.cx, 4),
                    cy_mm=round(sub.mabb.cy, 4),
                    rotation_deg=round((angle + 360.0) % 360.0, 3),
                )
            )
        elif _is_mounting_hole(sub.mabb):
            diameter = (sub.mabb.long_mm + sub.mabb.short_mm) / 2.0
            mounting_holes.append(
                MountingHoleDef(
                    id=next_id,
                    cx_mm=round(sub.mabb.cx, 4),
                    cy_mm=round(sub.mabb.cy, 4),
                    diameter_mm=round(diameter, 4),
                )
            )
        elif _is_stabilizer(sub.mabb):
            stabilizers.append(
                StabilizerDef(
                    id=next_id,
                    cx_mm=round(sub.mabb.cx, 4),
                    cy_mm=round(sub.mabb.cy, 4),
                    width_mm=round(sub.mabb.long_mm, 4),
                    height_mm=round(sub.mabb.short_mm, 4),
                    rotation_deg=round(sub.mabb.long_angle_deg, 3),
                )
            )
        else:
            unclassified.append(
                UnclassifiedShape(
                    id=next_id,
                    cx_mm=round(sub.mabb.cx, 4),
                    cy_mm=round(sub.mabb.cy, 4),
                    width_mm=round(sub.mabb.long_mm, 4),
                    height_mm=round(sub.mabb.short_mm, 4),
                    rotation_deg=round(sub.mabb.long_angle_deg, 3),
                )
            )
        next_id += 1

    pcb_outline = _build_outline(
        subpaths, outline_idx, svg_w_mm, svg_h_mm, switches
    )

    _orient_switches_against_stabs(switches, stabilizers)
    _orient_stabs_against_switches(switches, stabilizers)
    chosen_strategy = assign_matrix(switches, strategy=matrix_strategy)

    # Default MCU placement: near the plate's top-right corner, fully
    # inside the plate so it's visible in the preview viewBox (which is
    # plate-anchored). Pin 1 sits so the Pro Micro body (18 × 33 mm with
    # offsets -0.11, -1.5 from pin 1) lands ~5 mm inside the top and right
    # edges. The user can drag/rotate from there.
    PRO_MICRO_BODY_W_MM = 18.0
    PRO_MICRO_BODY_X_OFF = -0.11
    PRO_MICRO_BODY_Y_OFF = -1.5
    EDGE_MARGIN_MM = 5.0
    # cx + BODY_X_OFF + BODY_W = svg_w_mm - EDGE_MARGIN_MM
    mcu_default_cx = svg_w_mm - EDGE_MARGIN_MM - PRO_MICRO_BODY_X_OFF - PRO_MICRO_BODY_W_MM
    # cy + BODY_Y_OFF = EDGE_MARGIN_MM
    mcu_default_cy = EDGE_MARGIN_MM - PRO_MICRO_BODY_Y_OFF
    mcu_default = McuPlacement(
        cx_mm=round(max(PRO_MICRO_BODY_W_MM / 2, mcu_default_cx), 4),
        cy_mm=round(mcu_default_cy, 4),
        rotation_deg=0.0,
    )

    return ParseResult(
        svg_width_mm=round(svg_w_mm, 4),
        svg_height_mm=round(svg_h_mm, 4),
        pcb_outline=pcb_outline,
        switches=switches,
        stabilizers=stabilizers,
        mounting_holes=mounting_holes,
        unclassified=unclassified,
        mcu_placement=mcu_default,
        matrix_strategy=chosen_strategy,
        detected_svg_unit=detected_unit,
        mm_per_unit=round(mm_per_unit, 6),
    )


# ---------------------------------------------------------------------------
# SVG tree walking + transforms
# ---------------------------------------------------------------------------


def _read_viewbox(root: etree._Element) -> tuple[float, float]:
    vb = root.get("viewBox")
    if not vb:
        raise SvgParseError("SVG is missing a viewBox attribute")
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        raise SvgParseError(f"Unexpected viewBox value: {vb!r}")
    try:
        _, _, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise SvgParseError(f"viewBox is not numeric: {exc}") from exc
    return w, h


def _collect_subpaths(root: etree._Element) -> list[_Subpath]:
    out: list[_Subpath] = []
    for path_elem, matrix in _walk_paths(root, IDENTITY_MATRIX):
        d = path_elem.get("d")
        if not d:
            continue
        try:
            path = parse_path(d)
        except Exception:
            continue
        for sub in path.continuous_subpaths():
            try:
                pts = _sample_transformed_points(sub, matrix, PATH_SAMPLE_COUNT)
            except Exception:
                continue
            if len(pts) < 2:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            xmin, xmax = min(xs), max(xs)
            ymin, ymax = min(ys), max(ys)
            try:
                mabb = _compute_mabb_from_points(pts)
            except Exception:
                continue
            start_pt = pts[0]
            end_pt = pts[-1]
            is_closed = (
                math.hypot(start_pt[0] - end_pt[0], start_pt[1] - end_pt[1])
                < 1e-6
            )
            out.append(
                _Subpath(
                    index=len(out),
                    d=sub.d(),
                    axis_xmin=xmin,
                    axis_ymin=ymin,
                    axis_xmax=xmax,
                    axis_ymax=ymax,
                    mabb=mabb,
                    start_pt=start_pt,
                    end_pt=end_pt,
                    is_closed=is_closed,
                )
            )
    return out


def _walk_paths(elem: etree._Element, parent: Matrix):
    matrix = _compose(parent, _parse_transform(elem.get("transform")))
    tag = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else None
    if tag == "path":
        yield elem, matrix
    for child in elem:
        if not isinstance(child.tag, str):
            continue
        yield from _walk_paths(child, matrix)


def _parse_transform(attr: str | None) -> Matrix:
    if not attr:
        return IDENTITY_MATRIX
    matrix: Matrix = IDENTITY_MATRIX
    for m in re.finditer(r"(\w+)\s*\(([^)]*)\)", attr):
        name = m.group(1)
        try:
            args = [float(x) for x in re.split(r"[\s,]+", m.group(2).strip()) if x]
        except ValueError:
            continue
        matrix = _compose(matrix, _transform_to_matrix(name, args))
    return matrix


def _transform_to_matrix(name: str, args: list[float]) -> Matrix:
    if name == "matrix" and len(args) == 6:
        return (args[0], args[1], args[2], args[3], args[4], args[5])
    if name == "translate":
        tx = args[0] if args else 0.0
        ty = args[1] if len(args) > 1 else 0.0
        return (1.0, 0.0, 0.0, 1.0, tx, ty)
    if name == "scale":
        sx = args[0] if args else 1.0
        sy = args[1] if len(args) > 1 else sx
        return (sx, 0.0, 0.0, sy, 0.0, 0.0)
    if name == "rotate":
        if not args:
            return IDENTITY_MATRIX
        a = math.radians(args[0])
        c, s = math.cos(a), math.sin(a)
        rot: Matrix = (c, s, -s, c, 0.0, 0.0)
        if len(args) >= 3:
            cx, cy = args[1], args[2]
            t1: Matrix = (1.0, 0.0, 0.0, 1.0, cx, cy)
            t2: Matrix = (1.0, 0.0, 0.0, 1.0, -cx, -cy)
            return _compose(_compose(t1, rot), t2)
        return rot
    return IDENTITY_MATRIX


def _compose(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _sample_transformed_points(
    sub, matrix: Matrix, n: int
) -> list[tuple[float, float]]:
    a, b, c, d, e, f = matrix
    pts: list[tuple[float, float]] = []
    for i in range(n + 1):
        z = sub.point(i / n)
        x, y = float(z.real), float(z.imag)
        pts.append((a * x + c * y + e, b * x + d * y + f))
    return pts


# ---------------------------------------------------------------------------
# Scale detection
# ---------------------------------------------------------------------------


def _detect_mm_scale(
    root: etree._Element, viewbox_w: float, subpaths: list[_Subpath]
) -> tuple[float, str]:
    """Return `(mm_per_unit, unit_label)`. The label is the explicit SVG
    unit when the `width` attribute carries one (mm/cm/in/pt/pc), or
    `"inferred"` when the scale was derived from the switch-cutout
    heuristic, or `"unitless"` when neither path produced a scale and we
    fell back to 1.0."""
    width_attr = root.get("width")
    width_mm = _parse_length_mm(width_attr)
    if width_mm is not None and viewbox_w > 0:
        ratio = width_mm / viewbox_w
        if 0.01 < ratio < 100.0:
            unit_match = _LENGTH_RE.match(width_attr or "")
            unit = (unit_match.group(2).lower() if unit_match else "") or "mm"
            return ratio, unit

    sizes: list[float] = []
    for sub in subpaths:
        if sub.mabb.long_mm < 1e-6:
            continue
        ratio = sub.mabb.short_mm / sub.mabb.long_mm
        if 1.0 - SQUARE_RATIO_TOL <= ratio <= 1.0 + SQUARE_RATIO_TOL:
            sizes.append(sub.mabb.long_mm)

    if not sizes:
        return 1.0, "unitless"

    typical = statistics.median(sizes)
    if typical < 1e-6:
        return 1.0, "unitless"
    return SWITCH_DIM_MM / typical, "inferred"


# Conversion factors for the unit-override form field.
_UNIT_TO_MM: dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
}


_LENGTH_RE = re.compile(r"^\s*([+-]?\d*\.?\d+)\s*([a-zA-Z%]*)\s*$")


def _parse_length_mm(value: str | None) -> float | None:
    if not value:
        return None
    m = _LENGTH_RE.match(value)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower()
    if unit == "mm":
        return n
    if unit == "cm":
        return n * 10.0
    if unit == "in":
        return n * 25.4
    if unit == "pt":
        return n * 25.4 / 72.0
    if unit == "pc":
        return n * 25.4 / 6.0
    # px / unitless / % don't have a fixed mm conversion without DPI
    return None


def _scale_subpath(sub: _Subpath, scale: float) -> None:
    sub.axis_xmin *= scale
    sub.axis_ymin *= scale
    sub.axis_xmax *= scale
    sub.axis_ymax *= scale
    sub.mabb = _Mabb(
        cx=sub.mabb.cx * scale,
        cy=sub.mabb.cy * scale,
        long_mm=sub.mabb.long_mm * scale,
        short_mm=sub.mabb.short_mm * scale,
        long_angle_deg=sub.mabb.long_angle_deg,
    )
    sub.start_pt = (sub.start_pt[0] * scale, sub.start_pt[1] * scale)
    sub.end_pt = (sub.end_pt[0] * scale, sub.end_pt[1] * scale)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _compute_mabb_from_points(pts: list[tuple[float, float]]) -> _Mabb:
    rect = MultiPoint(pts).minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    e1_dx = coords[1][0] - coords[0][0]
    e1_dy = coords[1][1] - coords[0][1]
    e1_len = math.hypot(e1_dx, e1_dy)

    e2_dx = coords[2][0] - coords[1][0]
    e2_dy = coords[2][1] - coords[1][1]
    e2_len = math.hypot(e2_dx, e2_dy)

    if e1_len >= e2_len:
        long_dx, long_dy, long_len = e1_dx, e1_dy, e1_len
        short_len = e2_len
    else:
        long_dx, long_dy, long_len = e2_dx, e2_dy, e2_len
        short_len = e1_len

    long_angle = math.degrees(math.atan2(long_dy, long_dx)) % 180.0
    centroid = rect.centroid
    return _Mabb(
        cx=float(centroid.x),
        cy=float(centroid.y),
        long_mm=long_len,
        short_mm=short_len,
        long_angle_deg=long_angle,
    )


def _find_outline(
    subpaths: list[_Subpath], svg_w_mm: float, svg_h_mm: float
) -> int | None:
    for i, sub in enumerate(subpaths):
        if (
            abs(sub.axis_width - svg_w_mm) <= OUTLINE_TOL_MM
            and abs(sub.axis_height - svg_h_mm) <= OUTLINE_TOL_MM
        ):
            return i
    return None


def _build_outline(
    subpaths: list[_Subpath],
    outline_idx: int | None,
    svg_w_mm: float,
    svg_h_mm: float,
    switches: list[SwitchDef],
) -> PcbOutline:
    if outline_idx is not None:
        sub = subpaths[outline_idx]
        return PcbOutline(
            width_mm=round(sub.axis_width, 4),
            height_mm=round(sub.axis_height, 4),
            path_d=_rect_path(sub.axis_xmin, sub.axis_ymin, sub.axis_xmax, sub.axis_ymax),
        )

    open_segments = [
        (s.start_pt, s.end_pt) for s in subpaths if not s.is_closed
    ]
    polygon = _reconstruct_outline_from_lines(open_segments, switches)
    if polygon is not None:
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return PcbOutline(
            width_mm=round(max(xs) - min(xs), 4),
            height_mm=round(max(ys) - min(ys), 4),
            path_d=_polygon_to_path_d(polygon),
        )

    return PcbOutline(
        width_mm=round(svg_w_mm, 4),
        height_mm=round(svg_h_mm, 4),
        path_d=_rect_path(0.0, 0.0, svg_w_mm, svg_h_mm),
    )


def _polygon_to_path_d(pts: list[tuple[float, float]]) -> str:
    parts = [f"M {pts[0][0]:.4f} {pts[0][1]:.4f}"]
    for x, y in pts[1:]:
        parts.append(f"L {x:.4f} {y:.4f}")
    parts.append("Z")
    return " ".join(parts)


def _reconstruct_outline_from_lines(
    line_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    switches: list[SwitchDef],
) -> list[tuple[float, float]] | None:
    if len(line_segments) < 3 or not switches:
        return None

    def snap(pt: tuple[float, float]) -> tuple[float, float]:
        return (
            round(pt[0] / OUTLINE_SNAP_TOL_MM) * OUTLINE_SNAP_TOL_MM,
            round(pt[1] / OUTLINE_SNAP_TOL_MM) * OUTLINE_SNAP_TOL_MM,
        )

    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for s, e in line_segments:
        ss, se = snap(s), snap(e)
        if ss == se:
            continue
        segs.append((ss, se))
    if len(segs) < 3:
        return None

    adj: dict[tuple[float, float], list[tuple[int, tuple[float, float]]]] = {}
    for i, (s, e) in enumerate(segs):
        adj.setdefault(s, []).append((i, e))
        adj.setdefault(e, []).append((i, s))

    seg_seen: set[int] = set()
    cycles: list[list[tuple[float, float]]] = []
    for seed in range(len(segs)):
        if seed in seg_seen:
            continue
        # Flood-fill the connected component of segments containing `seed`.
        comp: set[int] = set()
        stack = [seed]
        while stack:
            idx = stack.pop()
            if idx in comp:
                continue
            comp.add(idx)
            for endpoint in segs[idx]:
                for nbr_idx, _ in adj.get(endpoint, []):
                    if nbr_idx not in comp:
                        stack.append(nbr_idx)
        seg_seen |= comp

        # Walk the component starting from a degree-1 endpoint if one exists
        # (open chain), otherwise from any endpoint (closed cycle).
        comp_pts: set[tuple[float, float]] = set()
        for idx in comp:
            comp_pts.add(segs[idx][0])
            comp_pts.add(segs[idx][1])
        dangling = [p for p in comp_pts if len(adj[p]) == 1]
        start = dangling[0] if dangling else next(iter(comp_pts))

        cycle: list[tuple[float, float]] = []
        used: set[int] = set()
        current = start
        while True:
            cycle.append(current)
            options = [
                (i, other)
                for i, other in adj[current]
                if i in comp and i not in used
            ]
            if not options:
                break
            sid, nxt = options[0]
            used.add(sid)
            current = nxt

        if len(cycle) >= 4:
            cycles.append(cycle)

    if not cycles:
        return None

    sw_pts = [Point(sw.cx_mm, sw.cy_mm) for sw in switches]
    best: tuple[int, float, list[tuple[float, float]]] | None = None
    for cyc in cycles:
        try:
            poly = Polygon(cyc)
        except Exception:
            continue
        if not poly.is_valid or poly.area <= 0:
            continue
        contained = sum(1 for p in sw_pts if poly.contains(p) or poly.touches(p))
        if best is None or contained > best[0] or (
            contained == best[0] and poly.area > best[1]
        ):
            best = (contained, poly.area, cyc)

    if best is None:
        return None
    if best[0] < math.ceil(len(sw_pts) * OUTLINE_SWITCH_COVERAGE):
        return None
    return best[2]


def _rect_path(x0: float, y0: float, x1: float, y1: float) -> str:
    return (
        f"M {x0:.4f} {y0:.4f} "
        f"L {x1:.4f} {y0:.4f} "
        f"L {x1:.4f} {y1:.4f} "
        f"L {x0:.4f} {y1:.4f} Z"
    )


def _is_switch(mabb: _Mabb) -> bool:
    return (
        abs(mabb.long_mm - SWITCH_DIM_MM) <= SWITCH_TOL_MM
        and abs(mabb.short_mm - SWITCH_DIM_MM) <= SWITCH_TOL_MM
    )


def _is_stabilizer(mabb: _Mabb) -> bool:
    if _is_switch(mabb):
        return False
    return (
        STAB_LONG_MIN_MM <= mabb.long_mm <= STAB_LONG_MAX_MM
        and STAB_SHORT_MIN_MM <= mabb.short_mm <= STAB_SHORT_MAX_MM
    )


def _is_mounting_hole(mabb: _Mabb) -> bool:
    if mabb.long_mm < 1e-6:
        return False
    ratio = mabb.short_mm / mabb.long_mm
    if ratio < 1.0 - SQUARE_RATIO_TOL:
        return False
    return MOUNT_HOLE_DIAM_MIN_MM <= mabb.long_mm <= MOUNT_HOLE_DIAM_MAX_MM


# ---------------------------------------------------------------------------
# Orientation heuristics (unchanged)
# ---------------------------------------------------------------------------


def _orient_switches_against_stabs(
    switches: list[SwitchDef], stabilizers: list[StabilizerDef]
) -> None:
    if len(stabilizers) < 2:
        return
    for sw in switches:
        pair = _find_flanking_stab_pair(sw, stabilizers)
        if pair is None:
            continue
        s1, s2 = pair
        axis_x = s2.cx_mm - s1.cx_mm
        axis_y = s2.cy_mm - s1.cy_mm
        axis_len = math.hypot(axis_x, axis_y)
        if axis_len < 1e-6:
            continue
        rot_rad = math.radians(sw.rotation_deg)
        pin_x = -math.sin(rot_rad)
        pin_y = math.cos(rot_rad)
        cos_angle = (pin_x * axis_x + pin_y * axis_y) / axis_len
        if abs(cos_angle) > PIN_PARALLEL_DOT_THRESHOLD:
            sw.rotation_deg = round((sw.rotation_deg + 90.0) % 360.0, 3)


def _orient_stabs_against_switches(
    switches: list[SwitchDef], stabilizers: list[StabilizerDef]
) -> None:
    if not switches or len(stabilizers) < 2:
        return
    for stab in stabilizers:
        flanking_sw = _find_flanking_switch_for_stab(stab, stabilizers, switches)
        if flanking_sw is None:
            continue
        dx = flanking_sw.cx_mm - stab.cx_mm
        dy = flanking_sw.cy_mm - stab.cy_mm
        axis_rad = math.radians(stab.rotation_deg)
        proj = dx * math.cos(axis_rad) + dy * math.sin(axis_rad)
        if proj < 0:
            stab.rotation_deg = round((stab.rotation_deg + 180.0) % 360.0, 3)


def _find_flanking_switch_for_stab(
    stab: StabilizerDef,
    stabilizers: list[StabilizerDef],
    switches: list[SwitchDef],
) -> SwitchDef | None:
    for other in stabilizers:
        if other is stab:
            continue
        mx = (stab.cx_mm + other.cx_mm) / 2
        my = (stab.cy_mm + other.cy_mm) / 2
        for sw in switches:
            if math.hypot(mx - sw.cx_mm, my - sw.cy_mm) <= STAB_PAIR_MIDPOINT_TOL_MM:
                return sw
    return None


def _find_flanking_stab_pair(
    sw: SwitchDef, stabilizers: list[StabilizerDef]
) -> tuple[StabilizerDef, StabilizerDef] | None:
    nearby = [
        s
        for s in stabilizers
        if math.hypot(s.cx_mm - sw.cx_mm, s.cy_mm - sw.cy_mm) <= STAB_PAIR_MAX_DIST_MM
    ]
    for i in range(len(nearby)):
        for j in range(i + 1, len(nearby)):
            s1, s2 = nearby[i], nearby[j]
            mx = (s1.cx_mm + s2.cx_mm) / 2
            my = (s1.cy_mm + s2.cy_mm) / 2
            if math.hypot(mx - sw.cx_mm, my - sw.cy_mm) <= STAB_PAIR_MIDPOINT_TOL_MM:
                return s1, s2
    return None
