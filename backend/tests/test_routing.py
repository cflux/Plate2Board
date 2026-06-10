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
    assert len(centroids) == len(npths)
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
        entries = by_net.get(net_name, [])
        assert any(
            math.hypot(px - kx, py - ky) < 1e-3 for px, py, _r in entries
        ), f"{ref}-{number} ({net_name}) missing from pad_world_positions"


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
