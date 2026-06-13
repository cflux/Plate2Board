"""Unit tests for the post-route island-doctor (reconnect_islands).

Hand-crafted minimal kicad_pcb strings — no freerouting. Each board is a
big rectangular GND pour fragmented by a foreign trace; we assert which
heal mechanism fires (cross-layer via / same-layer-or-bridge jumper /
warning) by diffing the via/segment counts and reading the warnings.
"""

from __future__ import annotations

import re

from app.services.routing.islands import (
    reconnect_islands, _pads, _cutouts, _parse_sexp,
)


# --- minimal kicad_pcb builders -------------------------------------------

def _zone(net, name, layer, x0, y0, x1, y1):
    pts = " ".join(
        f"(xy {x} {y})" for x, y in
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    )
    return (f'  (zone (net {net}) (net_name "{name}") (layer "{layer}")\n'
            f'    (polygon (pts {pts})))')


def _fp(ref, x, y, pads, rot=None):
    at = f"(at {x} {y})" if rot is None else f"(at {x} {y} {rot})"
    body = [f'  (footprint "lib:{ref}" (layer "F.Cu") (uuid "u-{ref}") {at}',
            f'    (property "Reference" "{ref}" (at 0 0))']
    for p in pads:
        body.append("    " + p)
    body.append("  )")
    return "\n".join(body)


def _pad(num, lx, ly, layers, net=None, name=None, ptype="smd rect"):
    net_clause = f' (net {net} "{name}")' if net is not None else ""
    return (f'(pad "{num}" {ptype} (at {lx} {ly}) (size 1 1) '
            f'(layers {layers}){net_clause})')


def _npth(lx, ly, dia=2.0):
    return (f'(pad "" np_thru_hole circle (at {lx} {ly}) (size {dia} {dia}) '
            f'(drill {dia}) (layers "*.Cu"))')


def _seg(x1, y1, x2, y2, layer, net, width=1.0):
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) '
            f'(layer "{layer}") (net {net}) (uuid "s"))')


def _board(*parts):
    return "(kicad_pcb\n" + "\n".join(parts) + "\n)\n"


def _via_count(t):
    return len(re.findall(r"\(via ", t))


def _seg_count(t):
    return t.count("(segment")


# --- tests -----------------------------------------------------------------

def test_cross_layer_via_heal():
    """GND on both layers: a B.Cu pad fenced off on B but with the F.Cu GND
    plane intact under it heals with a single cross-layer via."""
    board = _board(
        _zone(1, "GND", "F.Cu", 0, 0, 60, 40),
        _zone(1, "GND", "B.Cu", 0, 0, 60, 40),
        _fp("U1", 10, 20, [_pad(1, 0, 0, '"*.Cu"', net=1, name="GND")]),
        _fp("D1", 50, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
        _seg(30, -5, 30, 45, "B.Cu", 2),  # foreign wall, fully cuts B
    )
    out, warnings = reconnect_islands(board)
    assert warnings == []
    assert _via_count(out) - _via_count(board) == 1
    assert _seg_count(out) == _seg_count(board)
    # Idempotent: the via merges the island, a second pass adds nothing.
    out2, w2 = reconnect_islands(out)
    assert w2 == []
    assert _via_count(out2) == _via_count(out)
    assert _seg_count(out2) == _seg_count(out)


def test_bridge_over_other_layer():
    """RGB-style split: GND on B.Cu only, fenced on B, but F.Cu is clear —
    heals with a via-pair bridge (2 vias + 1 trace over F.Cu)."""
    board = _board(
        _zone(1, "GND", "B.Cu", 0, 0, 60, 40),
        _fp("U1", 10, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
        _fp("D1", 45, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
        _seg(30, -5, 30, 45, "B.Cu", 2),  # foreign wall on B only
    )
    out, warnings = reconnect_islands(board)
    assert warnings == []
    assert _via_count(out) - _via_count(board) == 2
    assert _seg_count(out) - _seg_count(board) == 1
    assert '(layer "F.Cu")' in out  # the bridge trace runs over F.Cu


def test_fully_fenced_warns():
    """Both layers walled at the same place: nothing can heal → one
    warning, no copper added."""
    board = _board(
        _zone(1, "GND", "B.Cu", 0, 0, 60, 40),
        _fp("U1", 10, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
        _fp("D1", 45, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
        _seg(30, -5, 30, 45, "B.Cu", 2),
        _seg(30, -5, 30, 45, "F.Cu", 2),  # the bridge layer is walled too
    )
    out, warnings = reconnect_islands(board)
    assert len(warnings) == 1
    assert "GND" in warnings[0]
    assert _via_count(out) == _via_count(board)
    assert _seg_count(out) == _seg_count(board)


def test_no_zones_is_noop():
    board = _board(
        _fp("D1", 50, 20, [_pad(1, 0, 0, '"B.Cu"', net=1, name="GND")]),
    )
    out, warnings = reconnect_islands(board)
    assert warnings == []
    assert out == board


def test_pad_rotation_world_position():
    """A pad at local (2,0) on a footprint rotated 90° lands at (fx, fy-2)
    in KiCad's Y-down frame."""
    board = _board(
        _fp("D1", 10, 20, [_pad(1, 2, 0, '"B.Cu"', net=1, name="GND")], rot=90),
    )
    root = _parse_sexp(board)
    pads = _pads(root)
    assert len(pads) == 1
    code, layers, x, y, r, is_npth, ref = pads[0]
    assert ref == "D1" and code == 1 and not is_npth
    assert abs(x - 10.0) < 1e-6 and abs(y - 18.0) < 1e-6


def test_npth_is_obstacle_not_copper():
    """np_thru_hole pads are obstacles (cutouts), flagged is_npth, never
    treated as net copper."""
    from shapely.geometry import Point
    board = _board(
        _fp("H1", 30, 20, [_npth(0, 0, dia=3.0)]),
    )
    root = _parse_sexp(board)
    pads = _pads(root)
    assert len(pads) == 1 and pads[0][5] is True  # is_npth
    cut = _cutouts(root)
    assert cut is not None and cut.contains(Point(30, 20))
