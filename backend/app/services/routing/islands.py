"""Post-route island doctor: reconnect copper-pour islands.

GND/VCC ship as unfilled KiCad zones (pour-carried — never routed by
freerouting; the user fills them with B). Once signal traces are spliced
in, a pour fragments into disconnected regions: a same-net pad sitting in
an island that doesn't reach the main plane is effectively unconnected.

`reconnect_islands` approximates each pour's fill (shapely), finds
stranded same-net copper, and reconnects it — a cross-layer via where the
net also pours on the other layer (the non-RGB GND-both-layers case), else
a short same-layer jumper through a clear gap (the RGB split-plane case,
where the other layer is the *other* net), else a warning. It is purely
additive: it only splices vias/segments and never rewrites existing
copper, and any geometry failure returns the board untouched.
"""

from __future__ import annotations

import logging
import math
import re

from ..pcb import (
    MCU_REF,
    STITCH_VIA_DRILL_MM,
    STITCH_VIA_SIZE_MM,
)
from .dsn import POWER_TRACK_WIDTH_MM
from .ses import (
    Segment,
    Via,
    _atom,
    _find_child,
    _find_children,
    _parse_sexp,
    splice_routes,
)

logger = logging.getLogger(__name__)

# Copper-to-pour clearance used when carving the approximate fill. The
# netclass rule is 0.2 mm; we add half the zone min-thickness so a neck
# KiCad wouldn't actually flood reads as a cut. Over-fragmenting is safe
# (worst case a redundant via); under-fragmenting could yield a shorting
# jumper, so bias high.
CLEARANCE_MM = 0.2 + 0.125
# A pad/via counts as touching a fill region within the zone thermal gap.
TOUCH_TOL_MM = 0.5 + 0.05
JUMPER_WIDTH_MM = POWER_TRACK_WIDTH_MM
MAX_JUMPER_LEN_MM = 20.0
MIN_REGION_AREA_MM2 = 0.05
# KiCad won't flood copper thinner than the zone min_thickness (0.25 mm in
# _pour_zone); half that is the opening radius that severs sub-min_thickness
# necks so the approximate fill fragments the same way the real fill does.
_FILL_OPEN_MM = 0.25 / 2
MAX_PASSES = 3
VIA_R = STITCH_VIA_SIZE_MM / 2.0
# A bridge/jumper targets a point inside the anchor fill region; that
# region's boundary follows the (already 0.3 mm edge-inset) pour outline, so
# a target right on the boundary puts a via's copper at the board edge. Pull
# the target this far inside the boundary so the via clears the edge (and any
# foreign copper the region was carved around).
TARGET_INSET_MM = VIA_R + CLEARANCE_MM
# When the nearest island→plane crossing is blocked on the bridge layer, try
# this many points sampled around the island boundary — a clear crossing
# usually sits a few mm along the fence from the nearest one.
_BRIDGE_SAMPLES = 24


# ---------------------------------------------------------------------------
# Parsing the shipped routed kicad_pcb
# ---------------------------------------------------------------------------


def _zone_polys(root) -> dict[tuple[int, str], object]:
    """{(net_code, layer): unioned shapely polygon} for every pour zone."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    by_key: dict[tuple[int, str], list] = {}
    names: dict[int, str] = {}
    for z in _find_children(root, "zone"):
        net = _find_child(z, "net")
        layer = _find_child(z, "layer")
        nm = _find_child(z, "net_name")
        poly = _find_child(z, "polygon")
        if net is None or layer is None or poly is None:
            continue
        code = int(_atom(net[1]))
        lyr = _atom(layer[1])
        if nm is not None:
            names[code] = _atom(nm[1])
        pts = _find_child(poly, "pts")
        coords = [
            (float(_atom(xy[1])), float(_atom(xy[2])))
            for xy in _find_children(pts, "xy")
        ]
        if len(coords) >= 3:
            by_key.setdefault((code, lyr), []).append(Polygon(coords))
    out: dict[tuple[int, str], object] = {}
    for key, polys in by_key.items():
        g = unary_union([p.buffer(0) for p in polys])
        out[key] = g
    out["__names__"] = names  # type: ignore[index]
    return out


def _pads(root):
    """Yield (net_code|None, layers:set, x, y, radius, is_npth, ref)."""
    out = []
    for fp in _find_children(root, "footprint"):
        at = _find_child(fp, "at")
        fx, fy = float(_atom(at[1])), float(_atom(at[2]))
        theta = math.radians(float(_atom(at[3])) if len(at) > 3 else 0.0)
        cos_r, sin_r = math.cos(theta), math.sin(theta)
        ref = ""
        for prop in _find_children(fp, "property"):
            if _atom(prop[1]) == "Reference":
                ref = _atom(prop[2])
        for pad in _find_children(fp, "pad"):
            pad_at = _find_child(pad, "at")
            lx, ly = float(_atom(pad_at[1])), float(_atom(pad_at[2]))
            wx = fx + lx * cos_r + ly * sin_r
            wy = fy - lx * sin_r + ly * cos_r
            is_npth = _atom(pad[2]) == "np_thru_hole"
            lyr_node = _find_child(pad, "layers")
            layers = set()
            if lyr_node is not None:
                for t in lyr_node[1:]:
                    a = _atom(t)
                    if a in ("*.Cu",):
                        layers.update({"F.Cu", "B.Cu"})
                    elif a.endswith(".Cu"):
                        layers.add(a)
            size = _find_child(pad, "size")
            radius = (
                max(float(_atom(size[1])), float(_atom(size[2]))) / 2.0
                if size else 0.5
            )
            net = _find_child(pad, "net")
            code = int(_atom(net[1])) if net is not None else None
            out.append((code, layers, wx, wy, radius, is_npth, ref))
    return out


_SEG_RE = re.compile(
    r"\(segment \(start ([-\d.]+) ([-\d.]+)\) \(end ([-\d.]+) ([-\d.]+)\) "
    r'\(width ([-\d.]+)\) \(layer "([^"]+)"\).*?\(net (\d+)\)'
)
_VIA_RE = re.compile(r"\(via \(at ([-\d.]+) ([-\d.]+)\).*?\(net (\d+)\)")


def _segments(pcb_text: str):
    return [
        (float(m[1]), float(m[2]), float(m[3]), float(m[4]),
         float(m[5]), m[6], int(m[7]))
        for m in _SEG_RE.finditer(pcb_text)
    ]  # x1,y1,x2,y2,width,layer,net


def _vias(pcb_text: str):
    return [
        (float(m[1]), float(m[2]), int(m[3])) for m in _VIA_RE.finditer(pcb_text)
    ]  # x,y,net (through-via: both layers)


def _cutouts(root):
    """Union of interior board cutouts: footprint Edge.Cuts rings (LED
    slots) + NPTH drills (buffered)."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import polygonize, unary_union

    geoms = []
    for fp in _find_children(root, "footprint"):
        at = _find_child(fp, "at")
        fx, fy = float(_atom(at[1])), float(_atom(at[2]))
        theta = math.radians(float(_atom(at[3])) if len(at) > 3 else 0.0)
        cos_r, sin_r = math.cos(theta), math.sin(theta)

        def world(lx, ly):
            return (fx + lx * cos_r + ly * sin_r, fy - lx * sin_r + ly * cos_r)

        edge_lines = []
        for ln in _find_children(fp, "fp_line"):
            lyr = _find_child(ln, "layer")
            if lyr is None or _atom(lyr[1]) != "Edge.Cuts":
                continue
            s = _find_child(ln, "start")
            e = _find_child(ln, "end")
            edge_lines.append(LineString([
                world(float(_atom(s[1])), float(_atom(s[2]))),
                world(float(_atom(e[1])), float(_atom(e[2]))),
            ]))
        if edge_lines:
            for poly in polygonize(unary_union(edge_lines)):
                geoms.append(poly)
        # NPTH drills as obstacle discs.
        for pad in _find_children(fp, "pad"):
            if _atom(pad[2]) != "np_thru_hole":
                continue
            pa = _find_child(pad, "at")
            lx, ly = float(_atom(pa[1])), float(_atom(pa[2]))
            size = _find_child(pad, "size")
            r = max(float(_atom(size[1])), float(_atom(size[2]))) / 2.0 if size else 1.0
            wx, wy = world(lx, ly)
            geoms.append(Point(wx, wy).buffer(r + CLEARANCE_MM))
    return unary_union(geoms) if geoms else None


# ---------------------------------------------------------------------------
# Fill approximation + connectivity
# ---------------------------------------------------------------------------


def _foreign_union(net_code, layer, pads, segments, vias):
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    geoms = []
    for x1, y1, x2, y2, w, lyr, code in segments:
        if lyr == layer and code != net_code:
            geoms.append(LineString([(x1, y1), (x2, y2)]).buffer(w / 2 + CLEARANCE_MM))
    for x, y, code in vias:
        if code != net_code:
            geoms.append(Point(x, y).buffer(VIA_R + CLEARANCE_MM))
    for code, layers, x, y, r, is_npth, _ref in pads:
        if is_npth:
            continue  # NPTH handled in cutouts
        if layer in layers and code != net_code:
            geoms.append(Point(x, y).buffer(r + CLEARANCE_MM))
    return unary_union(geoms) if geoms else None


def _fill_regions(zone_poly, foreign, cutouts):
    g = zone_poly
    if foreign is not None:
        g = g.difference(foreign)
    if cutouts is not None:
        g = g.difference(cutouts)
    g = g.buffer(0)
    # Sever necks thinner than KiCad's zone min_thickness: it won't flood a
    # hairline, so two blobs joined only by one are really separate islands.
    # A morphological opening (erode then dilate by half the min thickness)
    # drops sub-min_thickness connections without shrinking the regions, so
    # our fragmentation matches the real fill (we'd otherwise believe copper
    # flows where it doesn't and skip a needed stitch).
    opened = g.buffer(-_FILL_OPEN_MM, join_style=2).buffer(_FILL_OPEN_MM, join_style=2)
    g = opened.buffer(0)
    regions = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    return [r for r in regions if not r.is_empty and r.area >= MIN_REGION_AREA_MM2]


class _UF:
    def __init__(self):
        self.p: dict = {}

    def add(self, x):
        self.p.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconnect_islands(pcb_text: str) -> tuple[str, list[str]]:
    """Reconnect stranded GND/VCC pour copper. Returns (new_pcb, warnings).
    Purely additive; never raises (geometry errors → input unchanged)."""
    try:
        return _reconnect(pcb_text)
    except Exception:  # noqa: BLE001 — never break a routed job
        logger.exception("island reconnection failed")
        return pcb_text, ["copper-pour island check failed; verify GND/VCC fills in KiCad"]


# Two same-net connectors (pad / via / trace endpoint) within this distance
# are the same electrical node. A via dropped at a pad and a bridge trace
# ending on that via must read as joined, so this exceeds typical placement
# slop but stays under the nearest pad-to-pad pitch.
_COINCIDENT_MM = 0.3


def _connectivity(net_code, layers, regions, pads, segs, vias):
    """Build a union-find over ALL same-net copper — fill regions, pads,
    vias, and trace endpoints — so a heal that runs over a non-pour layer
    (a via-pair bridge whose trace lives where the net doesn't pour) reads
    as a real connection on the next parse. Returns (uf, net_pads, anchor).

    This is the single source of truth for "is this pad stranded": the heal
    loop and the e2e verifier both call it, so they can't disagree about
    what the added copper actually connected.
    """
    from shapely.geometry import Point

    uf = _UF()
    net_pads = [
        (i, x, y, r, plyrs)
        for i, (code, plyrs, x, y, r, is_npth, _ref) in enumerate(pads)
        if code == net_code and not is_npth
    ]
    net_vias = [(i, x, y) for i, (x, y, code) in enumerate(vias) if code == net_code]
    net_segs = [
        (i, s) for i, s in enumerate(segs) if s[6] == net_code
    ]  # s = (x1,y1,x2,y2,w,layer,code)

    for layer in layers:
        for ri in range(len(regions.get(layer, []))):
            uf.add(("r", layer, ri))

    # pad ↔ region on each layer the pad sits on
    for i, x, y, r, plyrs in net_pads:
        node = ("pad", i)
        uf.add(node)
        for layer in layers:
            if layer not in plyrs:
                continue
            for ri, reg in enumerate(regions[layer]):
                if reg.distance(Point(x, y)) <= r + TOUCH_TOL_MM:
                    uf.union(node, ("r", layer, ri))

    # via ↔ region on every pour layer it lands in (a through-via bridges layers)
    for i, x, y in net_vias:
        node = ("via", i)
        uf.add(node)
        for layer in layers:
            for ri, reg in enumerate(regions[layer]):
                if reg.distance(Point(x, y)) <= VIA_R + TOUCH_TOL_MM:
                    uf.union(node, ("r", layer, ri))

    # trace endpoints: the segment joins its own ends, and each end joins any
    # region on the trace's layer it lands on (only matters where the net
    # pours on the trace layer; on a non-pour layer the end joins via the
    # coincidence pass below).
    for i, (x1, y1, x2, y2, w, lyr, _code) in net_segs:
        ea, eb = ("seg", i, 0), ("seg", i, 1)
        uf.add(ea)
        uf.add(eb)
        uf.union(ea, eb)
        if lyr in regions:
            for ex, ey, en in ((x1, y1, ea), (x2, y2, eb)):
                for ri, reg in enumerate(regions[lyr]):
                    if reg.distance(Point(ex, ey)) <= w / 2 + TOUCH_TOL_MM:
                        uf.union(en, ("r", lyr, ri))

    # Coincident same-net connectors are one node — this is what stitches a
    # bridge's vias to its trace endpoints (and to the stranded pad).
    conns = [(x, y, ("pad", i)) for i, x, y, r, p in net_pads]
    conns += [(x, y, ("via", i)) for i, x, y in net_vias]
    for i, (x1, y1, x2, y2, w, lyr, _code) in net_segs:
        conns.append((x1, y1, ("seg", i, 0)))
        conns.append((x2, y2, ("seg", i, 1)))
    tol2 = _COINCIDENT_MM * _COINCIDENT_MM
    for a in range(len(conns)):
        xa, ya, na = conns[a]
        for b in range(a + 1, len(conns)):
            xb, yb, nb = conns[b]
            if (xa - xb) ** 2 + (ya - yb) ** 2 <= tol2:
                uf.union(na, nb)

    # Anchor = component holding a U1 pad of this net (else largest region).
    anchor = None
    for i, x, y, r, plyrs in net_pads:
        if pads[i][6] == MCU_REF:
            anchor = uf.find(("pad", i))
            break
    if anchor is None:
        best = None
        for layer in regions:
            for ri, reg in enumerate(regions[layer]):
                if best is None or reg.area > best[0]:
                    best = (reg.area, ("r", layer, ri))
        anchor = uf.find(best[1]) if best else None
    return uf, net_pads, anchor


def _reconnect(pcb_text: str) -> tuple[str, list[str]]:
    from shapely.geometry import LineString, Point

    root = _parse_sexp(pcb_text)
    zmap = _zone_polys(root)
    names = zmap.pop("__names__")  # type: ignore[arg-type]
    if not zmap:
        return pcb_text, []  # no pours (unrouted path) → nothing to do

    pads = _pads(root)
    cutouts = _cutouts(root)
    base_segs = _segments(pcb_text)
    base_vias = _vias(pcb_text)
    pour_nets = sorted({code for code, _layer in zmap})
    pour_layers: dict[int, list[str]] = {}
    for code, layer in zmap:
        pour_layers.setdefault(code, []).append(layer)

    added_segs: list[Segment] = []
    added_vias: list[Via] = []
    warnings: list[str] = []
    healed_pads: set[int] = set()

    for _pass in range(MAX_PASSES):
        segs = base_segs + [
            (s.x1_mm, s.y1_mm, s.x2_mm, s.y2_mm, s.width_mm, s.layer, s.net_code)
            for s in added_segs
        ]
        vias = base_vias + [(v.cx_mm, v.cy_mm, v.net_code) for v in added_vias]
        new_count = 0

        for net_code in pour_nets:
            net_name = names.get(net_code, str(net_code))
            layers = pour_layers[net_code]
            # Foreign-copper union for BOTH physical layers (the bridge
            # heal runs a GND trace over the *other* layer), and fill
            # regions for the pour layers.
            foreign_by_layer: dict[str, object] = {
                lyr: _foreign_union(net_code, lyr, pads, segs, vias)
                for lyr in ("F.Cu", "B.Cu")
            }
            regions: dict[str, list] = {
                layer: _fill_regions(zmap[(net_code, layer)],
                                     foreign_by_layer[layer], cutouts)
                for layer in layers
            }

            uf, net_pads, anchor = _connectivity(
                net_code, layers, regions, pads, segs, vias)
            if anchor is None:
                continue

            # Heal stranded pads.
            for i, x, y, r, plyrs in net_pads:
                if i in healed_pads or uf.find(("pad", i)) == anchor:
                    continue
                healed = _heal(
                    x, y, plyrs, layers, regions, uf, anchor,
                    pad_radius=r, foreign_by_layer=foreign_by_layer,
                    cutouts=cutouts, pad_component=uf.find(("pad", i)),
                )
                if healed is None:
                    # Don't warn here — a pad we can't heal this pass may be
                    # picked up next pass once a neighbour's bridge lands.
                    # Warnings are derived from the FINAL connectivity below.
                    continue
                kind, payload = healed
                if kind == "via":
                    vx, vy = payload
                    added_vias.append(Via(vx, vy, STITCH_VIA_SIZE_MM,
                                          STITCH_VIA_DRILL_MM, net_code, net_name))
                elif kind == "jumper":
                    path, layer = payload
                    for k in range(len(path) - 1):
                        (lx1, ly1), (lx2, ly2) = path[k], path[k + 1]
                        added_segs.append(Segment(layer, JUMPER_WIDTH_MM,
                                                  lx1, ly1, lx2, ly2, net_code, net_name))
                else:  # bridge: a via at each end (each crosses from the pour
                    # layer to the trace layer) joined by a trace on `other`.
                    path, other = payload
                    px, py = path[0]
                    qx, qy = path[-1]
                    added_vias.append(Via(px, py, STITCH_VIA_SIZE_MM,
                                          STITCH_VIA_DRILL_MM, net_code, net_name))
                    added_vias.append(Via(qx, qy, STITCH_VIA_SIZE_MM,
                                          STITCH_VIA_DRILL_MM, net_code, net_name))
                    for k in range(len(path) - 1):
                        (lx1, ly1), (lx2, ly2) = path[k], path[k + 1]
                        added_segs.append(Segment(other, JUMPER_WIDTH_MM,
                                                  lx1, ly1, lx2, ly2, net_code, net_name))
                new_count += 1
                healed_pads.add(i)
                uf.union(("pad", i), anchor)

        if new_count == 0:
            break

    new_pcb = splice_routes(pcb_text, added_segs, added_vias)

    # Warnings reflect the FINAL board: recompute connectivity with every
    # added via/jumper in place and warn only for pads that are *still*
    # stranded. This keeps the user-facing "finish by hand" tally accurate
    # (a pad healed late by a neighbour's bridge must not still warn).
    segs = base_segs + [
        (s.x1_mm, s.y1_mm, s.x2_mm, s.y2_mm, s.width_mm, s.layer, s.net_code)
        for s in added_segs
    ]
    vias = base_vias + [(v.cx_mm, v.cy_mm, v.net_code) for v in added_vias]
    for net_code in pour_nets:
        net_name = names.get(net_code, str(net_code))
        layers = pour_layers[net_code]
        foreign_by_layer = {
            lyr: _foreign_union(net_code, lyr, pads, segs, vias)
            for lyr in ("F.Cu", "B.Cu")
        }
        regions = {
            layer: _fill_regions(zmap[(net_code, layer)],
                                 foreign_by_layer[layer], cutouts)
            for layer in layers
        }
        uf, net_pads, anchor = _connectivity(
            net_code, layers, regions, pads, segs, vias)
        if anchor is None:
            continue
        for i, x, y, r, plyrs in net_pads:
            if uf.find(("pad", i)) != anchor:
                warnings.append(
                    f"{net_name} pad near ({x:.1f}, {y:.1f}) could not be "
                    f"auto-connected — add a via/jumper in KiCad"
                )

    # Collapse duplicate warnings but return the full list — the count is
    # the user-facing tally, so it must not be pre-capped (the caller
    # truncates the *logged* sample to 10).
    seen, deduped = set(), []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    return new_pcb, deduped


def _find_path(start, target, layer, *, foreign_by_layer, cutouts):
    """A clear poly-line from `start` to `target` on `layer`, or None.

    Tries a straight shot first, then two L-shaped routes (one corner each)
    that thread around a single obstacle. Returns the list of points
    (length ≥ 2). The total length cap applies to the whole poly-line."""
    from shapely.geometry import LineString

    half = JUMPER_WIDTH_MM / 2 + CLEARANCE_MM
    foreign = foreign_by_layer.get(layer)

    def _seg_clear(p1, p2):
        corridor = LineString([p1, p2]).buffer(half)
        if foreign is not None and corridor.intersects(foreign):
            return False
        if cutouts is not None and corridor.intersects(cutouts):
            return False
        return True

    def _path_len(pts):
        return sum(
            ((pts[k + 1][0] - pts[k][0]) ** 2 + (pts[k + 1][1] - pts[k][1]) ** 2) ** 0.5
            for k in range(len(pts) - 1)
        )

    sx, sy = start
    tx, ty = target
    candidates = (
        [start, target],
        [start, (tx, sy), target],
        [start, (sx, ty), target],
    )
    for pts in candidates:
        if _path_len(pts) > MAX_JUMPER_LEN_MM:
            continue
        if all(_seg_clear(pts[k], pts[k + 1]) for k in range(len(pts) - 1)):
            return pts
    return None


def _heal(x, y, pad_layers, pour_layers, regions, uf, anchor, *,
          pad_radius, foreign_by_layer, cutouts, pad_component):
    """Return ("via", (x,y)) | ("jumper", (points, layer))
    | ("bridge", (points, other_layer)) | None.

    Heals the pad's stranded ISLAND on a pour layer to the anchor plane on the
    same pour layer. Working region→region (not from the pad point) matters:
    a VCC pad physically sits on B.Cu but VCC only pours on F.Cu (it reaches
    the plane via a via-in-pad), so the break to repair is on F.Cu, and the
    pad itself is boxed in by its LED's other B.Cu pads. We bridge between a
    clear point in the island and the plane, straddling the fence trace, over
    the other (clearer) layer."""
    from shapely.ops import nearest_points
    from shapely.geometry import Point

    pt = Point(x, y)
    # (a) cross-layer via: the pad's copper already reaches an anchor region
    # on a *second* pour layer (net pours on ≥2 layers — non-RGB GND on both).
    if len(pour_layers) >= 2:
        for layer in pour_layers:
            for ri, reg in enumerate(regions.get(layer, [])):
                if uf.find(("r", layer, ri)) == anchor and \
                        reg.distance(pt) <= pad_radius + TOUCH_TOL_MM:
                    return ("via", (x, y))

    from shapely.ops import unary_union

    def _inset(reg):
        inner = reg.buffer(-TARGET_INSET_MM)
        return reg if inner.is_empty else inner

    def _candidates(src_geom, anc_geom):
        """(dist, s, t) crossing candidates from the source island to the
        plane: the globally-nearest pair plus points sampled around the
        source boundary — the nearest gap is often blocked on the bridge
        layer while a spot a few mm along it is clear."""
        out = []
        s0, t0 = nearest_points(src_geom, anc_geom)
        out.append((s0.distance(t0), (s0.x, s0.y), (t0.x, t0.y)))
        boundary = src_geom.boundary
        if not boundary.is_empty and boundary.length > 0:
            n = _BRIDGE_SAMPLES
            for k in range(n):
                sp = boundary.interpolate(k / n, normalized=True)
                tp = nearest_points(anc_geom, sp)[0]
                out.append((sp.distance(tp), (sp.x, sp.y), (tp.x, tp.y)))
        return sorted(c for c in out if c[0] <= MAX_JUMPER_LEN_MM)

    for al in regions:  # each pour layer that has fill regions
        # Source = the pad's own stranded island(s) on this layer; anchor =
        # the plane. Insetting keeps the via copper off the region boundary
        # (and so off the board edge). Fall back to the pad point when the pad
        # touches no region on this layer (e.g. a pad just outside the pour).
        src = [_inset(reg) for ri, reg in enumerate(regions[al])
               if uf.find(("r", al, ri)) == pad_component]
        anc = [_inset(reg) for ri, reg in enumerate(regions[al])
               if uf.find(("r", al, ri)) == anchor]
        if not anc:
            continue
        anc_geom = unary_union(anc)
        src_geom = unary_union(src) if src else pt
        other = "F.Cu" if al == "B.Cu" else "B.Cu"
        for _d, s, t in _candidates(src_geom, anc_geom):
            # (b) same-layer jumper: a clear thermal gap (not a fence) on `al`.
            # Rare between two fill regions, but free of a via when it fits.
            path = _find_path(s, t, al, foreign_by_layer=foreign_by_layer,
                              cutouts=cutouts)
            if path is not None:
                return ("jumper", (path, al))
            # (c) via-pair bridge: trace on the OTHER layer over the fence, a
            # via at each end crossing back into the pour layer `al`.
            path = _find_path(s, t, other, foreign_by_layer=foreign_by_layer,
                              cutouts=cutouts)
            if path is not None:
                return ("bridge", (path, other))
    return None
