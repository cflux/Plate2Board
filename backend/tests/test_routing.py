"""Unit tests for the auto-routing pipeline.

Three layers:
- `dsn` — does our DSN exporter emit the expected sections + nets for a
  minimal board? (No freerouting required; we just inspect text.)
- `ses` — does our SES parser + splicer round-trip canned freerouting
  output into kicad_pcb-compatible segment/via tokens?
- `client` — does the REST client correctly drive the freerouting job
  lifecycle? (Mocked httpx — no live sidecar.)
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re

import httpx
import pytest

from app.models.schemas import (
    McuPlacement,
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
)
from app.services.pcb import generate_pcb
from app.services.routing import client as routing_client
from app.services.routing.dsn import (
    DSN_MM_FACTOR,
    pad_world_positions,
    pcb_to_dsn,
)
from app.services.routing.ses import (
    Segment,
    Via,
    _atom,
    _find_child,
    _find_children,
    _parse_sexp,
    apply_ses_to_pcb,
    count_unattached_pads,
    parse_net_table,
    parse_ses,
)


# --- shared fixtures -------------------------------------------------------


def _result_two_keys() -> ParseResult:
    """Tiny 2-key, 1-row, 2-col board centered in a 30×20 outline. Just
    enough to exercise every footprint type and net category."""
    return ParseResult(
        svg_width_mm=30.0,
        svg_height_mm=20.0,
        pcb_outline=PcbOutline(
            width_mm=30.0,
            height_mm=20.0,
            path_d="M 0 0 L 30 0 L 30 20 L 0 20 Z",
        ),
        switches=[
            SwitchDef(id=1, cx_mm=8.0, cy_mm=10.0, row=0, col=0),
            SwitchDef(id=2, cx_mm=22.0, cy_mm=10.0, row=0, col=1),
        ],
        stabilizers=[],
        mounting_holes=[],
        unclassified=[],
        mcu_placement=McuPlacement(cx_mm=15.0, cy_mm=2.0, rotation_deg=0.0),
    )


# --- DSN export ------------------------------------------------------------


def test_dsn_minimal_board_has_required_sections() -> None:
    dsn = pcb_to_dsn(_result_two_keys())
    assert dsn.startswith('(pcb "keyboard"')
    # Major sections we depend on freerouting recognising.
    for section in ["(parser", "(resolution", "(structure", "(placement",
                    "(library", "(network"]:
        assert section in dsn, f"missing DSN section: {section}"
    # Two switches → two SW components; one MCU → U1 placed once.
    assert dsn.count('(place "SW1"') == 1
    assert dsn.count('(place "SW2"') == 1
    assert dsn.count('(place "U1"') == 1
    # Default + matrix netclasses.
    assert '(class "matrix"' in dsn
    # Each switch contributes COL/ROW/NET-SW*-D* membership.
    assert '"COL0"' in dsn and '"COL1"' in dsn
    assert '"ROW0"' in dsn
    # All parens balance.
    assert dsn.count("(") == dsn.count(")"), "unbalanced parens in DSN"


def test_dsn_skips_mcu_section_when_no_placement() -> None:
    parse = _result_two_keys().model_copy(update={"mcu_placement": None})
    dsn = pcb_to_dsn(parse)
    assert '"U1"' not in dsn, "MCU should be omitted when no placement"


def test_dsn_zero_switches_raises() -> None:
    parse = _result_two_keys().model_copy(update={"switches": []})
    with pytest.raises(ValueError):
        pcb_to_dsn(parse)


def test_dsn_emits_layer_direction_discipline() -> None:
    """Rows route on F.Cu (horizontal preference), columns on B.Cu
    (vertical). Scope ordering is load-bearing: autoroute_settings must
    come after the layer definitions (freerouting builds its layer table
    from them) but BEFORE any keepout — reading a keepout also builds the
    layer table, and freerouting only consumes autoroute_settings while
    that table is still unbuilt; emitted later, the scanner is left inside
    the scope and the rest of the DSN silently mis-parses to zero nets."""
    dsn = pcb_to_dsn(_result_two_keys())
    settings_at = dsn.index("(autoroute_settings")
    assert settings_at > dsn.index('(layer "B.Cu"')
    assert settings_at < dsn.index("(boundary")
    assert settings_at < dsn.index("(keepout")
    assert re.search(
        r"\(layer_rule F\.Cu\s+\(active on\)\s+"
        r"\(preferred_direction horizontal\)", dsn,
    ), "F.Cu must prefer horizontal (rows on top)"
    assert re.search(
        r"\(layer_rule B\.Cu\s+\(active on\)\s+"
        r"\(preferred_direction vertical\)", dsn,
    ), "B.Cu must prefer vertical (columns on bottom)"
    # Against-preferred cost must exceed preferred or the rules are inert.
    costs = re.findall(
        r"\(preferred_direction_trace_costs ([\d.]+)\)\s+"
        r"\(against_preferred_direction_trace_costs ([\d.]+)\)", dsn,
    )
    assert len(costs) == 2
    for preferred, against in costs:
        assert float(against) > float(preferred)


# --- DSN ↔ kicad_pcb geometric agreement ------------------------------------
#
# These tests interpret the emitted DSN exactly the way freerouting does
# (pin world = place + R(rot)·pin_local in the Y-up Specctra frame, then
# un-flip Y) and compare against pad positions recomputed from the generated
# kicad_pcb text using KiCad's own rotation convention
# (world = at + R_k(θ)·local, R_k = [[cos, sin], [−sin, cos]] in Y-down
# coords). The KiCad-side math is written out fresh here — NOT imported
# from the code under test — so a sign/convention bug anywhere in dsn.py
# shows up as a millimetre-scale mismatch at non-trivial angles. The DSN
# freerouting sees is self-consistent by construction, so this agreement
# check is the only unit-level signal for that class of bug.

# Deliberately awkward angles: include 0/90/270 (where sign errors can
# cancel) and odd ones (where they can't).
ODD_ANGLES = (0.0, 7.0, 30.0, 45.0, 90.0, 113.0, 270.0)


def _result_rotated() -> ParseResult:
    """Seven switches, one per ODD_ANGLES entry, plus a stab pair on the
    45° switch and one mounting hole — exercises every keepout source and
    every footprint type at arbitrary rotation."""
    switches = [
        SwitchDef(
            id=i + 1,
            cx_mm=25.0 + 24.0 * i,
            cy_mm=30.0 + 6.0 * (i % 2),
            rotation_deg=angle,
            row=i % 2,
            col=i // 2,
        )
        for i, angle in enumerate(ODD_ANGLES)
    ]
    # Stab cutout pair at ±11.94 mm along the 45° switch's local X axis
    # (id=4, center 97/36): world offset = standard 2D rotation of the
    # local offset by the SVG angle, same frame the parser produces.
    stab_sw = switches[3]
    rot = math.radians(stab_sw.rotation_deg)
    stabs = []
    for sid, local_x in ((1, -11.94), (2, 11.94)):
        stabs.append(StabilizerDef(
            id=sid,
            cx_mm=stab_sw.cx_mm + local_x * math.cos(rot),
            cy_mm=stab_sw.cy_mm + local_x * math.sin(rot),
            width_mm=6.65,
            height_mm=12.3,
        ))
    return ParseResult(
        svg_width_mm=200.0,
        svg_height_mm=60.0,
        pcb_outline=PcbOutline(
            width_mm=200.0,
            height_mm=60.0,
            path_d="M 0 0 L 200 0 L 200 60 L 0 60 Z",
        ),
        switches=switches,
        stabilizers=stabs,
        mounting_holes=[
            MountingHoleDef(id=1, cx_mm=10.0, cy_mm=10.0, diameter_mm=2.2),
        ],
        unclassified=[],
        mcu_placement=McuPlacement(cx_mm=100.0, cy_mm=6.0, rotation_deg=0.0),
    )


def _parse_dsn(dsn_text: str) -> list:
    """Parse the DSN, dropping the `(string_quote ")` parser line whose
    deliberately unpaired quote derails the generic sexp tokenizer."""
    return _parse_sexp(
        "\n".join(
            line for line in dsn_text.splitlines()
            if "string_quote" not in line
        )
    )


def _dsn_pad_worlds(dsn_text: str) -> dict[tuple[str, str], tuple[float, float]]:
    """(ref, pin_number) → pad world position in KiCad Y-down mm, computed
    by interpreting the DSN the way freerouting does."""
    root = _parse_dsn(dsn_text)
    library = _find_child(root, "library")
    images: dict[str, dict[str, tuple[float, float]]] = {}
    for image in _find_children(library, "image"):
        pins: dict[str, tuple[float, float]] = {}
        for pin in _find_children(image, "pin"):
            # (pin "PADSTACK" "NUMBER" x y)
            pins[_atom(pin[2])] = (float(_atom(pin[3])), float(_atom(pin[4])))
        images[_atom(image[1])] = pins
    out: dict[tuple[str, str], tuple[float, float]] = {}
    placement = _find_child(root, "placement")
    for comp in _find_children(placement, "component"):
        pins = images[_atom(comp[1])]
        for place in _find_children(comp, "place"):
            # (place "REF" x y side rotation)
            ref = _atom(place[1])
            px, py = float(_atom(place[2])), float(_atom(place[3]))
            side = _atom(place[4])
            # Back-side places make freerouting mirror the image — a
            # semantic this simulator (and pcb.py's WYSIWYG footprints)
            # doesn't model; everything must be placed front.
            assert side == "front", f"{ref} placed side={side}"
            rot = math.radians(float(_atom(place[5])))
            cos_r, sin_r = math.cos(rot), math.sin(rot)
            for number, (lx, ly) in pins.items():
                # freerouting: CCW-positive rotation in the Y-up frame.
                wx = px + lx * cos_r - ly * sin_r
                wy = py + lx * sin_r + ly * cos_r
                # DSN ticks → mm, and un-flip Y back to KiCad's frame.
                out[(ref, number)] = (wx / DSN_MM_FACTOR, -wy / DSN_MM_FACTOR)
    return out


def _kicad_pad_worlds(
    pcb_text: str,
) -> tuple[
    dict[tuple[str, str], tuple[tuple[float, float], str]],
    list[tuple[float, float]],
]:
    """Recompute pad world positions from the kicad_pcb text using KiCad's
    own rotation convention. Returns ``({(ref, number): ((x, y), net_name)},
    [NPTH centers])``."""
    pads: dict[tuple[str, str], tuple[tuple[float, float], str]] = {}
    npths: list[tuple[float, float]] = []
    root = _parse_sexp(pcb_text)
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
            # KiCad Y-down: world = at + R_k(θ)·local, R_k = [[c, s], [−s, c]].
            wx = fx + lx * cos_r + ly * sin_r
            wy = fy - lx * sin_r + ly * cos_r
            if _atom(pad[2]) == "np_thru_hole":
                npths.append((wx, wy))
            else:
                net = _find_child(pad, "net")
                net_name = _atom(net[2]) if net else ""
                pads[(ref, _atom(pad[1]))] = ((wx, wy), net_name)
    return pads, npths


@pytest.mark.parametrize(
    "switch_type,diode_type",
    [("soldered", "tht"), ("hotswap", "smd")],
)
def test_dsn_pads_match_kicad_pcb_at_odd_angles(
    switch_type: str, diode_type: str
) -> None:
    parse = _result_rotated()
    pcb_text = generate_pcb(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount",
    )
    dsn_text = pcb_to_dsn(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount",
    )
    dsn_pads = _dsn_pad_worlds(dsn_text)
    kicad_pads, _ = _kicad_pad_worlds(pcb_text)
    assert dsn_pads, "no pads parsed from DSN"
    for key, (dx, dy) in dsn_pads.items():
        assert key in kicad_pads, f"DSN pad {key} missing from kicad_pcb"
        (kx, ky), _net = kicad_pads[key]
        dist = math.hypot(dx - kx, dy - ky)
        assert dist < 1e-3, (
            f"pad {key} drifts {dist:.4f} mm: "
            f"freerouting sees ({dx:.4f}, {dy:.4f}), "
            f"KiCad places ({kx:.4f}, {ky:.4f})"
        )
    # Net topology agrees too: every pin the DSN assigns to a net maps to
    # a kicad_pcb pad carrying the same net name (catches pin swaps).
    network = _find_child(_parse_dsn(dsn_text), "network")
    checked = 0
    for net in _find_children(network, "net"):
        net_name = _atom(net[1])
        pins_node = _find_child(net, "pins")
        if pins_node is None:
            continue
        for pin_ref in pins_node[1:]:
            ref, _, number = _atom(pin_ref).rpartition("-")
            assert kicad_pads[(ref, number)][1] == net_name, (
                f"{ref}-{number}: DSN net {net_name} vs kicad "
                f"{kicad_pads[(ref, number)][1]}"
            )
            checked += 1
    assert checked > 0


@pytest.mark.parametrize("switch_type", ["soldered", "hotswap"])
def test_dsn_keepouts_cover_every_npth(switch_type: str) -> None:
    parse = _result_rotated()
    pcb_text = generate_pcb(
        parse, switch_type=switch_type, diode_type="tht",
        stabilizer_type="pcb_mount",
    )
    dsn_text = pcb_to_dsn(
        parse, switch_type=switch_type, diode_type="tht",
        stabilizer_type="pcb_mount",
    )
    _, npths = _kicad_pad_worlds(pcb_text)
    structure = _find_child(_parse_dsn(dsn_text), "structure")
    centroids: list[tuple[float, float]] = []
    for keepout in _find_children(structure, "keepout"):
        poly = _find_child(keepout, "polygon")
        coords = [float(_atom(v)) / DSN_MM_FACTOR for v in poly[3:]]
        xs, ys = coords[0::2], coords[1::2]
        xs, ys = xs[:-1], ys[:-1]  # drop the repeated closing vertex
        centroids.append((sum(xs) / len(xs), -(sum(ys) / len(ys))))
    assert npths, "fixture should produce NPTHs"
    # Keepouts also cover GND stitching vias (and boundary fences on
    # non-rectangular outlines), so the count is ≥ the NPTH count — the
    # invariant is that every NPTH has a keepout centered on it.
    assert len(centroids) >= len(npths)
    for nx, ny in npths:
        assert any(
            math.hypot(cx - nx, cy - ny) < 1e-3 for cx, cy in centroids
        ), f"NPTH at ({nx:.3f}, {ny:.3f}) has no keepout over it"


def test_dsn_place_rotation_is_kicad_angle_unnegated() -> None:
    """SVG rotation 30° → KiCad angle −30° → DSN place angle 330° (the
    Y-flip conjugation preserves the angle; see dsn.py docstring)."""
    parse = _result_two_keys()
    rotated = parse.switches[0].model_copy(update={"rotation_deg": 30.0})
    parse = parse.model_copy(update={"switches": [rotated, parse.switches[1]]})
    dsn = pcb_to_dsn(parse)
    assert re.search(r'\(place "SW1" \S+ \S+ front 330\.000\)', dsn)


def test_pad_world_positions_match_kicad_pcb() -> None:
    parse = _result_rotated()
    pcb_text = generate_pcb(
        parse, switch_type="hotswap", diode_type="smd",
        stabilizer_type="pcb_mount",
    )
    kicad_pads, _ = _kicad_pad_worlds(pcb_text)
    by_net = pad_world_positions(parse, switch_type="hotswap", diode_type="smd")
    for (ref, number), ((kx, ky), net_name) in kicad_pads.items():
        if not net_name:
            continue  # unconnected MCU pins carry no net
        if net_name == "GND":
            # GND is carried by the copper pours, not routed traces — it's
            # deliberately absent from the router's view of the board.
            continue
        entries = by_net.get(net_name, [])
        assert any(
            math.hypot(px - kx, py - ky) < 1e-3 for px, py, _r in entries
        ), f"{ref}-{number} ({net_name}) missing from pad_world_positions"


def _pad_copper_geometries(pcb_text: str):
    """``[(ref, number, net_name, layer_set, shapely_geom)]`` for every
    netted copper pad, in world coordinates. NPTHs (no net) are skipped."""
    from shapely.affinity import rotate as shp_rotate
    from shapely.affinity import translate as shp_translate
    from shapely.geometry import Point, box

    out = []
    root = _parse_sexp(pcb_text)
    for fp in _find_children(root, "footprint"):
        at = _find_child(fp, "at")
        fx, fy = float(_atom(at[1])), float(_atom(at[2]))
        theta = float(_atom(at[3])) if len(at) > 3 else 0.0
        cos_r = math.cos(math.radians(theta))
        sin_r = math.sin(math.radians(theta))
        ref = ""
        for prop in _find_children(fp, "property"):
            if _atom(prop[1]) == "Reference":
                ref = _atom(prop[2])
        for pad in _find_children(fp, "pad"):
            net = _find_child(pad, "net")
            if net is None:
                continue
            pad_at = _find_child(pad, "at")
            lx, ly = float(_atom(pad_at[1])), float(_atom(pad_at[2]))
            wx = fx + lx * cos_r + ly * sin_r
            wy = fy - lx * sin_r + ly * cos_r
            size = _find_child(pad, "size")
            w, h = float(_atom(size[1])), float(_atom(size[2]))
            if _atom(pad[3]) in ("circle", "oval"):
                geom = Point(wx, wy).buffer(max(w, h) / 2.0, quad_segs=16)
            else:  # rect — KiCad world = R_k(θ)·local, i.e. standard −θ.
                geom = box(-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)
                geom = shp_rotate(geom, -theta, origin=(0, 0))
                geom = shp_translate(geom, wx, wy)
            layers: set[str] = set()
            for tok in _find_child(pad, "layers")[1:]:
                t = _atom(tok)
                if t == "*.Cu":
                    layers |= {"F.Cu", "B.Cu"}
                elif t.endswith(".Cu"):
                    layers.add(t)
            out.append((ref, _atom(pad[1]), _atom(net[2]), layers, geom))
    return out


@pytest.mark.parametrize(
    "switch_type,diode_type",
    [
        ("soldered", "tht"),
        ("soldered", "smd"),
        ("hotswap", "tht"),
        ("hotswap", "smd"),
    ],
)
def test_no_cross_net_pad_clearance_violations(
    switch_type: str, diode_type: str
) -> None:
    """No two pads on different nets may sit closer than the 0.2 mm
    clearance rule on a shared copper layer — a violation here is a
    manufactured short (or an unroutable net for freerouting) baked into
    the footprint geometry itself. Caught a real one: the soldered+SMD
    diode anchor used to overlap the ROW pad with switch pin 2's
    through-hole copper by 0.10 mm."""
    parse = _result_rotated()
    pcb_text = generate_pcb(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount",
    )
    pads = _pad_copper_geometries(pcb_text)
    assert pads
    min_clearance = 0.2 - 1e-6
    violations = []
    for i in range(len(pads)):
        ref1, n1, net1, layers1, g1 = pads[i]
        for j in range(i + 1, len(pads)):
            ref2, n2, net2, layers2, g2 = pads[j]
            if net1 == net2 or not (layers1 & layers2):
                continue
            dist = g1.distance(g2)
            if dist < min_clearance:
                violations.append(
                    f"{ref1}-{n1} ({net1}) ↔ {ref2}-{n2} ({net2}): "
                    f"{dist:.3f} mm"
                )
    assert not violations, (
        "cross-net pad clearance violations:\n" + "\n".join(violations[:10])
    )


def test_count_unattached_pads() -> None:
    pads = {"COL0": [(10.0, 10.0, 1.25), (20.0, 10.0, 1.25)]}
    table = {"COL0": 1}

    def seg(x1: float, y1: float, x2: float, y2: float) -> Segment:
        return Segment(
            layer="F.Cu", width_mm=0.25, x1_mm=x1, y1_mm=y1,
            x2_mm=x2, y2_mm=y2, net_code=1, net_name="COL0",
        )

    # Wire spanning both pad centers → everything attached.
    assert count_unattached_pads([seg(10, 10, 20, 10)], [], pads, table) == 0
    # Wire drifted 2 mm off both pads → both flagged.
    assert count_unattached_pads([seg(10, 12, 20, 12)], [], pads, table) == 2
    # Net with no copper at all is an unrouted net, not drift — skipped.
    assert count_unattached_pads([], [], pads, table) == 0
    # A via sitting on a pad counts as attachment.
    via = Via(
        cx_mm=20.0, cy_mm=10.0, pad_diameter_mm=0.6, drill_diameter_mm=0.3,
        net_code=1, net_name="COL0",
    )
    assert count_unattached_pads([seg(10, 10, 15, 10)], [via], pads, table) == 0


# --- SES splice ------------------------------------------------------------


def _canned_pcb() -> str:
    """Minimal kicad_pcb-shaped wrapper with a known net table."""
    return (
        '(kicad_pcb (version 20240108)\n'
        '\t(net 0 "")\n'
        '\t(net 1 "COL0")\n'
        '\t(net 2 "ROW0")\n'
        '\t(net 3 "NET-SW1-D1")\n'
        ')\n'
    )


def test_parse_net_table_extracts_top_level_nets() -> None:
    table = parse_net_table(_canned_pcb())
    assert table == {"": 0, "COL0": 1, "ROW0": 2, "NET-SW1-D1": 3}


def test_ses_parses_wires_and_vias() -> None:
    ses = (
        '(session foo\n'
        '  (routes\n'
        '    (resolution mm 10)\n'
        '    (network_out\n'
        '      (net "COL0"\n'
        '        (wire (path "F.Cu" 3 100 200 150 200 200 250))\n'
        '      )\n'
        '      (net "ROW0"\n'
        '        (via "Via[0-1]_0.6:0.3_um" 110 -45 (type protect))\n'
        '      )\n'
        '    )\n'
        '  )\n'
        ')\n'
    )
    segments, vias = parse_ses(
        ses, {"COL0": 1, "ROW0": 2}, freerouting_quirk=False,
    )
    # Path was 3 points → 2 segments.
    assert len(segments) == 2
    # mm × 10 resolution → 100 → 10.0 mm. Y is negated on parse because
    # the DSN exporter Y-flips to convert KiCad Y-down → Specctra Y-up,
    # and the SES parser un-flips Y to restore KiCad coords.
    assert segments[0].x1_mm == pytest.approx(10.0)
    assert segments[0].y1_mm == pytest.approx(-20.0)
    assert segments[0].width_mm == pytest.approx(0.3)
    assert segments[0].net_code == 1
    # Via: x stays, y un-flipped.
    assert len(vias) == 1
    assert vias[0].net_code == 2
    assert vias[0].cx_mm == pytest.approx(11.0)
    assert vias[0].cy_mm == pytest.approx(4.5)  # SES -45 → un-flipped 4.5
    assert vias[0].pad_diameter_mm == pytest.approx(0.6)
    assert vias[0].drill_diameter_mm == pytest.approx(0.3)


def test_splice_inserts_segments_before_closing_paren() -> None:
    ses = (
        '(session foo\n'
        '  (routes\n'
        '    (resolution mm 10)\n'
        '    (network_out\n'
        '      (net "COL0"\n'
        '        (wire (path "F.Cu" 3 100 200 150 200))\n'
        '      )\n'
        '    )\n'
        '  )\n'
        ')\n'
    )
    pcb = _canned_pcb()
    spliced, stats = apply_ses_to_pcb(
        pcb, ses, total_connections=5, unrouted_connections=4,
        freerouting_quirk=False,
    )
    # Original (net …) rows are still there.
    assert '(net 1 "COL0")' in spliced
    # New segment with the right net code. Y is un-flipped on splice.
    assert "(segment (start 10.0000 -20.0000)" in spliced
    assert "(net 1)" in spliced
    # Stats reflect what was spliced + what was passed in.
    assert stats.routed_count == 1
    assert stats.via_count == 0
    assert stats.total_count == 5
    assert stats.unrouted_count == 4
    # Closing paren still the last meaningful token.
    assert spliced.rstrip().endswith(")")


def test_splice_unknown_net_is_skipped() -> None:
    ses = (
        '(session foo\n'
        '  (routes (resolution mm 10) (network_out\n'
        '    (net "MYSTERY" (wire (path "F.Cu" 3 1 2 3 4)))\n'
        '  ))\n'
        ')\n'
    )
    segs, vias = parse_ses(ses, {"COL0": 1}, freerouting_quirk=False)
    assert segs == [] and vias == []


# --- routing client (mocked) -----------------------------------------------


def _patch_client(transport: httpx.MockTransport):
    """Patch routing_client's httpx.AsyncClient so its inner `async with`
    block uses our MockTransport instead of a real network connection."""
    import unittest.mock as _mock
    real_client_cls = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    return _mock.patch.object(routing_client.httpx, "AsyncClient", fake_client)


def test_client_route_drives_full_lifecycle() -> None:
    """Patch httpx.AsyncClient with a transport that fakes every endpoint
    the client hits. Asserts the client returns the decoded SES + parsed
    stats from the final /output payload."""
    ses_text = (
        '(session foo (routes (resolution mm 10) (network_out)))\n'
    )
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sessions/create":
            return httpx.Response(200, json={"id": "sess-1"})
        if path == "/v1/jobs/enqueue":
            return httpx.Response(200, json={"id": "job-1"})
        if path == "/v1/jobs/job-1/input":
            body = json.loads(request.content)
            assert "data" in body and "filename" in body
            base64.b64decode(body["data"])
            return httpx.Response(200, json={})
        if path == "/v1/jobs/job-1/settings":
            return httpx.Response(200, json={})
        if path == "/v1/jobs/job-1/start":
            return httpx.Response(200, json={})
        if path == "/v1/jobs/job-1/output":
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return httpx.Response(204)
            return httpx.Response(200, json={
                "data": base64.b64encode(ses_text.encode()).decode(),
                "statistics": {
                    "routed_net_count": 7,
                    "unrouted_net_count": 1,
                    "via_count": 2,
                },
            })
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with _patch_client(transport):
        result = asyncio.run(routing_client.route(
            "(pcb fake)",
            poll_interval_s=0.01,
            timeout_s=2.0,
        ))

    assert result.stats.routed_net_count == 7
    assert result.stats.unrouted_net_count == 1
    assert result.stats.via_count == 2
    assert result.ses_text.startswith("(session")


def test_client_route_times_out_cleanly() -> None:
    """A sidecar that never returns 200 should trip the wall-clock cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sessions/create":
            return httpx.Response(200, json={"id": "sess-1"})
        if path == "/v1/jobs/enqueue":
            return httpx.Response(200, json={"id": "job-1"})
        if path in ("/v1/jobs/job-1/input", "/v1/jobs/job-1/settings",
                    "/v1/jobs/job-1/start", "/v1/jobs/job-1/cancel"):
            return httpx.Response(200, json={})
        if path == "/v1/jobs/job-1/output":
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with _patch_client(transport):
        with pytest.raises(routing_client.FreeroutingError, match="timed out"):
            asyncio.run(routing_client.route(
                "(pcb fake)",
                poll_interval_s=0.05,
                timeout_s=0.2,
            ))


# --- via-cost ladder runner (mocked client) ----------------------------------


def _runner_parse():
    from app.models.schemas import ParseResult, PcbOutline, SwitchDef

    return ParseResult(
        svg_width_mm=60.0,
        svg_height_mm=40.0,
        pcb_outline=PcbOutline(
            width_mm=60.0, height_mm=40.0,
            path_d="M 0 0 L 60 0 L 60 40 L 0 40 Z",
        ),
        switches=[SwitchDef(id=1, cx_mm=30.0, cy_mm=20.0, row=0, col=0)],
        stabilizers=[], mounting_holes=[], unclassified=[],
    )


def test_runner_stops_after_first_fully_routed_attempt(monkeypatch) -> None:
    from app.services.routing import client as rclient
    from app.services.routing import runner

    seen_via_costs: list[int] = []

    async def fake_route(dsn_text, *, progress_cb=None, timeout_s=None):
        import re as _re
        m = _re.search(r"\(via_costs (\d+)\)", dsn_text)
        seen_via_costs.append(int(m.group(1)))
        return rclient.RouteResult(
            ses_text="(session)",
            stats=rclient.RouterStats(routed_net_count=3, unrouted_net_count=0),
        )

    monkeypatch.setattr(rclient, "route", fake_route)
    result = asyncio.run(runner.route_board(_runner_parse()))
    assert result.stats.unrouted_net_count == 0
    assert seen_via_costs == [runner.VIA_COST_LADDER[0]]


def test_runner_retries_with_next_via_cost_and_keeps_best(monkeypatch) -> None:
    from app.services.routing import client as rclient
    from app.services.routing import runner

    seen_via_costs: list[int] = []

    async def fake_route(dsn_text, *, progress_cb=None, timeout_s=None):
        import re as _re
        m = _re.search(r"\(via_costs (\d+)\)", dsn_text)
        vc = int(m.group(1))
        seen_via_costs.append(vc)
        # First rung plateaus with 2 unrouted; second rung completes.
        unrouted = 2 if vc == runner.VIA_COST_LADDER[0] else 0
        return rclient.RouteResult(
            ses_text=f"(session via {vc})",
            stats=rclient.RouterStats(
                routed_net_count=10 - unrouted, unrouted_net_count=unrouted
            ),
        )

    monkeypatch.setattr(rclient, "route", fake_route)
    result = asyncio.run(runner.route_board(_runner_parse()))
    assert seen_via_costs == list(runner.VIA_COST_LADDER)
    assert result.stats.unrouted_net_count == 0
    assert "via 20" in result.ses_text


def test_runner_returns_best_when_no_attempt_completes(monkeypatch) -> None:
    from app.services.routing import client as rclient
    from app.services.routing import runner

    async def fake_route(dsn_text, *, progress_cb=None, timeout_s=None):
        import re as _re
        vc = int(_re.search(r"\(via_costs (\d+)\)", dsn_text).group(1))
        unrouted = 1 if vc == runner.VIA_COST_LADDER[-1] else 4
        return rclient.RouteResult(
            ses_text=f"(session via {vc})",
            stats=rclient.RouterStats(
                routed_net_count=10 - unrouted, unrouted_net_count=unrouted
            ),
        )

    monkeypatch.setattr(rclient, "route", fake_route)
    result = asyncio.run(runner.route_board(_runner_parse()))
    assert result.stats.unrouted_net_count == 1


def test_runner_survives_hard_failure_on_one_rung(monkeypatch) -> None:
    from app.services.routing import client as rclient
    from app.services.routing import runner

    calls = {"n": 0}

    async def fake_route(dsn_text, *, progress_cb=None, timeout_s=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rclient.FreeroutingError("sidecar hiccup")
        return rclient.RouteResult(
            ses_text="(session)",
            stats=rclient.RouterStats(routed_net_count=3, unrouted_net_count=0),
        )

    monkeypatch.setattr(rclient, "route", fake_route)
    result = asyncio.run(runner.route_board(_runner_parse()))
    assert result.stats.unrouted_net_count == 0
    assert calls["n"] == 2


def test_runner_raises_when_every_rung_hard_fails(monkeypatch) -> None:
    from app.services.routing import client as rclient
    from app.services.routing import runner

    async def fake_route(dsn_text, *, progress_cb=None, timeout_s=None):
        raise rclient.FreeroutingError("sidecar down")

    monkeypatch.setattr(rclient, "route", fake_route)
    with pytest.raises(rclient.FreeroutingError, match="sidecar down"):
        asyncio.run(runner.route_board(_runner_parse()))


# --- boundary fence for non-convex outlines ----------------------------------


def test_fence_boundary_rectangle_is_passthrough() -> None:
    from app.services.routing.dsn import _fence_boundary

    rect = [(0.0, 0.0), (200.0, 0.0), (200.0, 60.0), (0.0, 60.0), (0.0, 0.0)]
    boundary, fences = _fence_boundary(rect)
    assert boundary == rect
    assert fences == []


def test_fence_boundary_concave_outline_builds_fences() -> None:
    from shapely.geometry import Polygon, box

    from app.services.routing.dsn import _fence_boundary

    # Rectangle with a triangular notch in the bottom edge.
    outline = [
        (0.0, 0.0), (200.0, 0.0), (200.0, 60.0),
        (130.0, 60.0), (120.0, 45.0), (110.0, 60.0),
        (0.0, 60.0), (0.0, 0.0),
    ]
    boundary, fences = _fence_boundary(outline)
    # Boundary becomes the bounding rectangle.
    assert set(boundary) == {(0.0, 0.0), (200.0, 0.0), (200.0, 60.0), (0.0, 60.0)}
    assert fences, "expected fence keepouts for a concave outline"
    # Fences exactly cover bbox minus the outline (notch area = 150 mm²).
    poly = Polygon(outline[:-1])
    expected = box(0, 0, 200, 60).difference(poly)
    fence_union = None
    for f in fences:
        p = Polygon(f.points)
        assert p.is_valid
        fence_union = p if fence_union is None else fence_union.union(p)
    assert abs(fence_union.area - expected.area) < 1e-6
    assert fence_union.symmetric_difference(expected).area < 1e-6


def test_fence_boundary_alice_outline_splits_annulus() -> None:
    """An outline that touches its bbox only at isolated extremes produces
    an annulus difference — pieces must come back hole-free."""
    from shapely.geometry import Polygon

    from app.services.routing.dsn import _fence_boundary

    # Diamond: touches bbox at 4 midpoints; difference = 4 corner triangles
    # (or an annulus pinched at points, depending on shapely's split).
    outline = [(100.0, 0.0), (200.0, 50.0), (100.0, 100.0), (0.0, 50.0), (100.0, 0.0)]
    boundary, fences = _fence_boundary(outline)
    assert len(boundary) == 5
    assert fences
    total = sum(Polygon(f.points).area for f in fences)
    # bbox 200×100 minus diamond (area 10000) = 10000.
    assert abs(total - 10000.0) < 1e-3
    for f in fences:
        assert Polygon(f.points).is_valid


def test_dsn_emits_fence_keepouts_for_concave_outline() -> None:
    from app.models.schemas import ParseResult, PcbOutline, SwitchDef

    from app.services.routing.dsn import pcb_to_dsn

    parse = ParseResult(
        svg_width_mm=100.0, svg_height_mm=60.0,
        pcb_outline=PcbOutline(
            width_mm=100.0, height_mm=60.0,
            path_d="M 0 0 L 100 0 L 100 60 L 60 60 L 50 45 L 40 60 L 0 60 Z",
        ),
        switches=[SwitchDef(id=1, cx_mm=30.0, cy_mm=20.0, row=0, col=0)],
        stabilizers=[], mounting_holes=[], unclassified=[],
    )
    dsn = pcb_to_dsn(parse)
    # 3 switch NPTH keepouts + at least one fence keepout.
    assert dsn.count("(keepout") >= 4
    # The boundary must be the 4-corner bounding rectangle (5 closed pts
    # × 2 coords = 10 numbers).
    import re
    m = re.search(r"\(boundary \(path pcb 0 ([^)]*)\)\)", dsn)
    assert m and len(m.group(1).split()) == 10


# --- ground pour: GND exclusion + stitching via keepouts ---------------------


def test_dsn_excludes_gnd_from_network() -> None:
    parse = _result_rotated()
    dsn = pcb_to_dsn(parse, switch_type="soldered", diode_type="tht",
                     stabilizer_type="pcb_mount")
    assert '(net "GND"' not in dsn
    assert '"GND"' not in dsn


def test_dsn_keepouts_at_stitching_via_positions() -> None:
    from app.services.pcb import compute_stitching_vias
    from app.services.routing.dsn import _boundary_points, _prepare_parse

    parse = _result_rotated()
    prepared = _prepare_parse(parse)
    expected = compute_stitching_vias(
        list(prepared.switches), list(prepared.stabilizers),
        list(prepared.mounting_holes), prepared.mcu_placement,
        _boundary_points(prepared),
        switch_type="soldered", diode_type="tht", stabilizer_type="pcb_mount",
    )
    dsn = pcb_to_dsn(parse, switch_type="soldered", diode_type="tht",
                     stabilizer_type="pcb_mount")
    structure = _find_child(_parse_dsn(dsn), "structure")
    centroids = []
    for keepout in _find_children(structure, "keepout"):
        poly = _find_child(keepout, "polygon")
        coords = [float(_atom(v)) / DSN_MM_FACTOR for v in poly[3:]]
        xs, ys = coords[0::2][:-1], coords[1::2][:-1]
        centroids.append((sum(xs) / len(xs), -(sum(ys) / len(ys))))
    assert expected, "fixture should yield stitching vias"
    for x, y in expected:
        assert any(
            math.hypot(cx - x, cy - y) < 1e-3 for cx, cy in centroids
        ), f"no keepout over stitching via at ({x}, {y})"
    # With the pour disabled the via keepouts disappear.
    dsn_off = pcb_to_dsn(parse, switch_type="soldered", diode_type="tht",
                         stabilizer_type="pcb_mount", ground_pour=False)
    structure_off = _find_child(_parse_dsn(dsn_off), "structure")
    n_off = len(_find_children(structure_off, "keepout"))
    n_on = len(centroids)
    assert n_on - n_off == len(expected)


# --- per-key RGB in the DSN ---------------------------------------------------


def test_dsn_rgb_nets_and_keepouts() -> None:
    from app.services.pcb import _rgb_led_anchor
    from app.services.routing.dsn import _prepare_parse

    parse = _result_two_keys()
    dsn = pcb_to_dsn(parse, rgb=True)
    # VCC + chain nets present with LED/cap pins; GND stays pour-carried.
    assert '(net "VCC"' in dsn and "LED1-1" in dsn and "C1-1" in dsn
    assert '(net "RGB_DATA0"' in dsn and '(net "RGB_DATA1"' in dsn
    assert '(net "GND"' not in dsn
    assert '(class "power"' in dsn
    # The MCU feeds the chain + supplies VCC from RAW.
    assert re.search(r'\(net "RGB_DATA0"\s*\(pins [^)]*U1-\d+', dsn)
    assert re.search(r'\(net "VCC"\s*\(pins [^)]*U1-24', dsn)
    # One cutout keepout polygon per LED, centered on the LED anchor.
    prepared = _prepare_parse(parse)
    structure = _find_child(_parse_dsn(dsn), "structure")
    centroids = []
    for keepout in _find_children(structure, "keepout"):
        poly = _find_child(keepout, "polygon")
        coords = [float(_atom(v)) / DSN_MM_FACTOR for v in poly[3:]]
        xs, ys = coords[0::2][:-1], coords[1::2][:-1]
        centroids.append((sum(xs) / len(xs), -(sum(ys) / len(ys))))
    for sw in prepared.switches:
        ax, ay, _rot = _rgb_led_anchor(sw)
        assert any(
            math.hypot(cx - ax, cy - ay) < 1e-3 for cx, cy in centroids
        ), f"no cutout keepout at LED anchor of SW{sw.id}"


def test_dsn_rgb_routes_gnd_when_pour_off() -> None:
    parse = _result_two_keys()
    dsn = pcb_to_dsn(parse, rgb=True, ground_pour=False)
    m = re.search(r'\(net "GND"\s*\(pins ([^)]*)\)', dsn)
    assert m, "GND must be routed when the pour is off and RGB is on"
    pins = m.group(1).split()
    assert "U1-3" in pins and "U1-4" in pins and "U1-23" in pins
    assert "LED1-3" in pins and "C1-2" in pins
    # GND joins the power class alongside VCC.
    power_class = re.search(r'\(class "power"(.*?)\)\s*\)', dsn, re.DOTALL).group(1)
    assert '"GND"' in power_class and '"VCC"' in power_class
