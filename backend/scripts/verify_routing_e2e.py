"""End-to-end routing verification against a live freerouting sidecar.

Generates a 7-switch board whose switches sit at deliberately awkward
angles (7°, 30°, 45°, 113°, …), routes it through freerouting, splices
the SES back into the kicad_pcb, and then re-checks the routed board with
independently written math:

1. freerouting must report 0 unrouted nets;
2. the splice-time validator must report 0 unattached pads;
3. every netted pad in the spliced kicad_pcb must have a same-net wire
   endpoint or via landing on it — pad world positions are recomputed
   here from each footprint's stored ``(at cx cy rot)`` using KiCad's
   rotation convention (world = at + R_k(θ)·local,
   R_k = [[cos, sin], [−sin, cos]] in Y-down coords), deliberately NOT
   imported from dsn.py so a convention bug there cannot vouch for
   itself;
4. no trace may cross any NPTH (switch stems/pegs, hotswap pin holes,
   stab holes, mounting holes);
5. layer discipline: F.Cu copper must be more horizontal than B.Cu
   (rows on top, columns on bottom — driven by the DSN
   autoroute_settings layer rules).

Run inside the compose network (the sidecar publishes no host port):

    docker run --rm --network keeblayoutbot_default \\
        -e FREEROUTING_URL=http://freerouting:37864 \\
        -v "$PWD/backend:/src" -w /src keeblayoutbot-backend:latest \\
        python scripts/verify_routing_e2e.py

Exits 0 if every check passes for all four build configs, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys

sys.path.insert(0, ".")

from app.models.schemas import (  # noqa: E402
    McuPlacement,
    MountingHoleDef,
    ParseResult,
    PcbOutline,
    StabilizerDef,
    SwitchDef,
)
from app.services.pcb import generate_pcb  # noqa: E402
from app.services.routing import runner as routing_runner  # noqa: E402
from app.services.routing.dsn import pad_world_positions  # noqa: E402
from app.services.routing.ses import (  # noqa: E402
    _atom,
    _find_child,
    _find_children,
    _parse_sexp,
    apply_ses_to_pcb,
)
from app.services.routing.islands import (  # noqa: E402
    reconnect_islands,
    _connectivity,
    _cutouts,
    _fill_regions,
    _foreign_union,
    _pads,
    _segments,
    _vias,
    _zone_polys,
)


def _stranded_pour_pads(pcb_text: str) -> int:
    """Total GND/VCC pour pads not in their net's anchor component, using
    the island-doctor's own connectivity model (so bridge heals count)."""
    root = _parse_sexp(pcb_text)
    zones = _zone_polys(root)
    zones.pop("__names__", None)
    if not zones:
        return 0
    pads = _pads(root)
    segs = _segments(pcb_text)
    vias = _vias(pcb_text)
    cutouts = _cutouts(root)
    pour_layers: dict[int, list[str]] = {}
    for (nc, layer) in zones:
        pour_layers.setdefault(nc, []).append(layer)
    stranded = 0
    for net_code, layers in pour_layers.items():
        foreign = {lyr: _foreign_union(net_code, lyr, pads, segs, vias)
                   for lyr in ("F.Cu", "B.Cu")}
        regions = {lyr: _fill_regions(zones[(net_code, lyr)], foreign[lyr], cutouts)
                   for lyr in layers}
        uf, net_pads, anchor = _connectivity(
            net_code, layers, regions, pads, segs, vias)
        stranded += sum(1 for i, x, y, r, plyrs in net_pads
                        if uf.find(("pad", i)) != anchor)
    return stranded

ODD_ANGLES = (0.0, 7.0, 30.0, 45.0, 90.0, 113.0, 270.0)

# A wire endpoint / via must land within the pad's half-extent plus this.
PAD_SLOP_MM = 0.2


def build_parse(mcu_type: str = "pro_micro", rgb: bool = False) -> ParseResult:
    # The Pico is 51 mm long — far too tall for the 60 mm fixture sized
    # for a Pro Micro. For big MCUs, drop onto a taller rectangular board
    # with the module parked below the switch field (the concave-outline
    # fence path is already covered by the Pro Micro variants).
    # The Pico is physically huge, and RGB packs LEDs + caps + the data
    # chain onto B.Cu — both want the roomier rectangular board so the
    # synthetic 7-switch arc's long data hops stay routable (real boards
    # space keys on a grid; the concave-fence path is covered by the
    # non-RGB variants on the notched board).
    big_board = mcu_type == "pico" or rgb
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
    stab_sw = switches[3]
    rot = math.radians(stab_sw.rotation_deg)
    stabs = [
        StabilizerDef(
            id=sid,
            cx_mm=stab_sw.cx_mm + local_x * math.cos(rot),
            cy_mm=stab_sw.cy_mm + local_x * math.sin(rot),
            width_mm=6.65,
            height_mm=12.3,
        )
        for sid, local_x in ((1, -11.94), (2, 11.94))
    ]
    if big_board:
        # Plain 200 × 120 board; Pico parked below the switches, well
        # clear of the 0.5 mm edge setback.
        return ParseResult(
            svg_width_mm=200.0,
            svg_height_mm=120.0,
            pcb_outline=PcbOutline(
                width_mm=200.0, height_mm=120.0,
                path_d="M 0 0 L 200 0 L 200 120 L 0 120 Z",
            ),
            switches=switches,
            stabilizers=stabs,
            mounting_holes=[
                MountingHoleDef(id=1, cx_mm=10.0, cy_mm=10.0, diameter_mm=2.2),
            ],
            unclassified=[],
            mcu_placement=McuPlacement(cx_mm=90.0, cy_mm=62.0, rotation_deg=0.0),
            outline_shrink_mm=1.0,
        )
    return ParseResult(
        svg_width_mm=200.0,
        svg_height_mm=60.0,
        # Deliberately NON-CONVEX: a multi-vertex concave chain in the
        # bottom edge. Freerouting mis-decomposes concave boundaries
        # (nets near the dents go unrouted), so this exercises the
        # bbox-boundary + keepout-fence path in dsn._fence_boundary —
        # a plain rectangle here would leave that path untested.
        pcb_outline=PcbOutline(
            width_mm=200.0,
            height_mm=60.0,
            path_d="M 0 0 L 200 0 L 200 60 L 130 60 L 120 50 "
                   "L 110 58 L 100 60 L 0 60 Z",
        ),
        switches=switches,
        stabilizers=stabs,
        mounting_holes=[
            MountingHoleDef(id=1, cx_mm=10.0, cy_mm=10.0, diameter_mm=2.2),
        ],
        unclassified=[],
        mcu_placement=McuPlacement(cx_mm=100.0, cy_mm=6.0, rotation_deg=0.0),
        # Exercise the PCB inset end-to-end: Edge.Cuts, DSN boundary,
        # fences, zones, and stitching vias all follow the shrunk outline.
        outline_shrink_mm=1.0,
    )


def collect_board_geometry(pcb_text: str):
    """From the spliced kicad_pcb: netted pads (with fresh KiCad rotation
    math), NPTH holes, segments, and vias."""
    pads = []   # (ref, number, net_code, x, y, radius)
    npths = []  # (x, y, drill_radius)
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
            wx = fx + lx * cos_r + ly * sin_r
            wy = fy - lx * sin_r + ly * cos_r
            if _atom(pad[2]) == "np_thru_hole":
                drill = _find_child(pad, "drill")
                if _atom(drill[1]) == "oval":
                    # Slot (e.g. the RGB LED cutout) — use the inscribed
                    # circle: the circumscribed one would false-positive
                    # on legal traces near the slot's corners (the exact
                    # rect keepout is unit-tested in test_routing.py).
                    r = min(
                        float(_atom(drill[2])), float(_atom(drill[3]))
                    ) / 2.0
                else:
                    r = float(_atom(drill[1])) / 2.0
                npths.append((wx, wy, r))
                continue
            net = _find_child(pad, "net")
            if net is None:
                continue
            size = _find_child(pad, "size")
            radius = max(float(_atom(size[1])), float(_atom(size[2]))) / 2.0
            pads.append((ref, _atom(pad[1]), int(_atom(net[1])), wx, wy, radius))

    segments = [
        tuple(float(g) for g in m.groups()[:4])
        + (int(m.group(7)), float(m.group(5)), m.group(6))
        for m in re.finditer(
            r"\(segment \(start ([-\d.]+) ([-\d.]+)\) "
            r"\(end ([-\d.]+) ([-\d.]+)\) \(width ([-\d.]+)\) "
            r'\(layer "([^"]+)"\)'
            r".*?\(net (\d+)\)",
            pcb_text,
        )
    ]  # (x1, y1, x2, y2, net_code, width, layer)
    vias = [
        (float(m.group(1)), float(m.group(2)), int(m.group(3)))
        for m in re.finditer(
            r"\(via \(at ([-\d.]+) ([-\d.]+)\).*?\(net (\d+)\)", pcb_text
        )
    ]  # (x, y, net_code)
    return pads, npths, segments, vias


def point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def verify(
    switch_type: str, diode_type: str, rgb: bool = False,
    mcu_type: str = "pro_micro",
) -> bool:
    mcu_tag = "" if mcu_type == "pro_micro" else f"/{mcu_type}"
    label = f"[{switch_type}/{diode_type}{'/rgb' if rgb else ''}{mcu_tag}]"
    parse = build_parse(mcu_type, rgb)
    pcb_text = generate_pcb(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount", rgb=rgb, mcu_type=mcu_type,
    )
    print(f"{label} routing via freerouting…", flush=True)
    # Routes through the same via-cost-ladder runner production uses.
    # Short per-attempt cap: this fixture routes in seconds, so a hang here
    # means a regression (e.g. freerouting mis-parsed the DSN) — fail fast
    # rather than waiting out the production timeout.
    result = asyncio.run(routing_runner.route_board(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount", rgb=rgb, mcu_type=mcu_type,
        timeout_s=240.0,
    ))
    routed_pcb, stats = apply_ses_to_pcb(
        pcb_text,
        result.ses_text,
        total_connections=result.stats.total_net_count,
        unrouted_connections=result.stats.unrouted_net_count,
        pad_positions=pad_world_positions(
            parse, switch_type=switch_type, diode_type=diode_type,
            rgb=rgb, mcu_type=mcu_type,
        ),
    )
    ok = True
    print(
        f"{label} freerouting: {result.stats.routed_net_count} routed, "
        f"{result.stats.unrouted_net_count} unrouted; splice: "
        f"{stats.routed_count} segments, {stats.via_count} vias"
    )
    if result.stats.unrouted_net_count:
        print(f"{label} FAIL: freerouting left nets unrouted")
        ok = False
    if stats.unattached_pad_count:
        print(f"{label} FAIL: {stats.unattached_pad_count} unattached pad(s)")
        ok = False

    from app.services.routing.ses import parse_net_table
    net_table = parse_net_table(pcb_text)
    gnd_code = net_table.get("GND")
    # VCC is pour-carried too on RGB boards (F.Cu plane, split from the
    # B.Cu GND pour) — its pads are fed by vias, not routed traces.
    vcc_code = net_table.get("VCC") if rgb else None
    pour_codes = {c for c in (gnd_code, vcc_code) if c is not None}

    pads, npths, segments, vias = collect_board_geometry(routed_pcb)
    nets_with_copper = {s[4] for s in segments} | {v[2] for v in vias}
    missed = []
    for ref, number, code, px, py, radius in pads:
        if code in pour_codes:
            # GND/VCC are carried by the copper pours (filled in KiCad),
            # not routed traces — their vias put them in nets_with_copper
            # but no wire ever lands on a pour pad by design.
            continue
        if code not in nets_with_copper:
            missed.append((ref, number, "net has no copper at all"))
            continue
        tol = radius + PAD_SLOP_MM
        hit = any(
            s[4] == code and (
                math.hypot(s[0] - px, s[1] - py) <= tol
                or math.hypot(s[2] - px, s[3] - py) <= tol
            )
            for s in segments
        ) or any(
            v[2] == code and math.hypot(v[0] - px, v[1] - py) <= tol
            for v in vias
        )
        if not hit:
            missed.append((ref, number, f"no wire within {tol:.2f} mm"))
    if missed:
        print(f"{label} FAIL: {len(missed)} pad(s) without copper landing on them:")
        for ref, number, why in missed[:10]:
            print(f"    {ref}-{number}: {why}")
        ok = False
    else:
        print(f"{label} all {len(pads)} pads have wires landing on them")

    crossings = 0
    for hx, hy, hr in npths:
        for x1, y1, x2, y2, _code, width, _layer in segments:
            if point_segment_distance(hx, hy, x1, y1, x2, y2) < hr + width / 2.0:
                crossings += 1
    if crossings:
        print(f"{label} FAIL: {crossings} trace(s) cross NPTH holes")
        ok = False
    else:
        print(f"{label} no traces cross any of the {len(npths)} NPTHs")

    # Pour vias (GND stitching when non-RGB; per-key VCC vias when RGB)
    # are invisible to freerouting (their net isn't in the DSN network) —
    # the via-position keepouts must keep routed copper of every other net
    # clear of them. 0.19 ≈ the 0.2 netclass clearance minus float slack.
    pour_vias = [(v[0], v[1]) for v in vias if v[2] in pour_codes]
    via_hits = 0
    for vx, vy in pour_vias:
        for x1, y1, x2, y2, code, width, _layer in segments:
            if code in pour_codes:
                continue
            if point_segment_distance(vx, vy, x1, y1, x2, y2) < 0.3 + width / 2.0 + 0.19:
                via_hits += 1
        for ox, oy, ocode in vias:
            if ocode not in pour_codes and math.hypot(ox - vx, oy - vy) < 0.6 + 0.19:
                via_hits += 1
    if via_hits:
        print(f"{label} FAIL: routed copper within clearance of "
              f"{via_hits} pour-via spot(s)")
        ok = False
    else:
        print(f"{label} routed copper clears all {len(pour_vias)} pour vias")

    # Layer discipline: with the DSN autoroute_settings layer rules, F.Cu
    # should carry mostly horizontal copper (rows) and B.Cu mostly
    # vertical (columns). Compare the horizontal share of copper length
    # per layer — the relative ordering is the robust signal.
    horiz = {"F.Cu": 0.0, "B.Cu": 0.0}
    total = {"F.Cu": 0.0, "B.Cu": 0.0}
    for x1, y1, x2, y2, _code, _width, layer in segments:
        if layer not in total:
            continue
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        length = math.hypot(dx, dy)
        total[layer] += length
        if dx >= dy:
            horiz[layer] += length
    share = {
        layer: (horiz[layer] / total[layer] if total[layer] else 0.0)
        for layer in total
    }
    print(
        f"{label} horizontal copper share: "
        f"F.Cu {share['F.Cu']:.0%} ({total['F.Cu']:.0f} mm), "
        f"B.Cu {share['B.Cu']:.0%} ({total['B.Cu']:.0f} mm)"
    )
    # Only meaningful when both layers carry real copper: in the
    # hotswap/smd config every pad is on B.Cu, so F.Cu sees a few mm of
    # crossover jumps and its share is statistical noise. The 5-point
    # margin tolerates near-ties (routing shifts slightly run to run,
    # e.g. around stitching-via keepouts) — the signal we care about is
    # a clear inversion, which is what an inert layer_rule produces.
    if (
        min(total.values()) >= 75.0
        and share["F.Cu"] < share["B.Cu"] - 0.05
    ):
        print(f"{label} FAIL: no row/column layer discipline "
              f"(F.Cu should be more horizontal than B.Cu)")
        ok = False

    # Island doctor: reconnect GND/VCC pour fragments the traces fenced off.
    # It must never raise, must be additive (stranded count can't go up),
    # and must be idempotent (a second pass adds nothing).
    before = _stranded_pour_pads(routed_pcb)
    try:
        healed_pcb, island_warnings = reconnect_islands(routed_pcb)
    except Exception as exc:  # pragma: no cover - the pass swallows internally
        print(f"{label} FAIL: island doctor raised: {exc}")
        return False
    after = _stranded_pour_pads(healed_pcb)
    healed_again, _ = reconnect_islands(healed_pcb)
    added_vias = healed_again.count("(via ") - healed_pcb.count("(via ")
    added_segs = healed_again.count("(segment") - healed_pcb.count("(segment")
    print(f"{label} island doctor: stranded pour pads {before}→{after}, "
          f"{len(island_warnings)} warned; idempotent adds "
          f"{added_vias} vias / {added_segs} segs")
    if after > before:
        print(f"{label} FAIL: island doctor increased stranded pads "
              f"({before}→{after})")
        ok = False
    if added_vias or added_segs:
        print(f"{label} FAIL: island doctor not idempotent "
              f"(+{added_vias} vias, +{added_segs} segs on re-run)")
        ok = False
    return ok


def main() -> int:
    all_ok = True
    for switch_type, diode_type, rgb, mcu_type in (
        ("soldered", "tht", False, "pro_micro"),
        ("soldered", "smd", False, "pro_micro"),
        ("hotswap", "tht", False, "pro_micro"),
        ("hotswap", "smd", False, "pro_micro"),
        # RGB adds LED/cap pads + cutout keepouts + VCC/data nets on the
        # already-busy B.Cu — route the two extreme variants.
        ("soldered", "tht", True, "pro_micro"),
        ("hotswap", "smd", True, "pro_micro"),
        # MCU form factors: the castellated-SMD XIAO (F.Cu-only MCU pads)
        # and the Pico (40-pin, RGB).
        ("soldered", "tht", False, "xiao_smd"),
        ("hotswap", "smd", True, "pico"),
    ):
        try:
            all_ok &= verify(switch_type, diode_type, rgb, mcu_type)
        except Exception as exc:  # noqa: BLE001 — report and fail
            tag = "" if mcu_type == "pro_micro" else f"/{mcu_type}"
            print(f"[{switch_type}/{diode_type}{'/rgb' if rgb else ''}{tag}] ERROR: {exc}")
            all_ok = False
    print("E2E RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
