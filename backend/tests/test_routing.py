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

import httpx
import pytest

from app.models.schemas import (
    McuPlacement,
    ParseResult,
    PcbOutline,
    SwitchDef,
)
from app.services.routing import client as routing_client
from app.services.routing.dsn import pcb_to_dsn
from app.services.routing.ses import (
    apply_ses_to_pcb,
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
    # mm × 10 resolution → 100 → 10.0 mm
    assert segments[0].x1_mm == pytest.approx(10.0)
    assert segments[0].width_mm == pytest.approx(0.3)
    assert segments[0].net_code == 1
    # Via reads pad/drill from canonical padstack name.
    assert len(vias) == 1
    assert vias[0].net_code == 2
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
    # New segment with the right net code.
    assert "(segment (start 10.0000 20.0000)" in spliced
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
