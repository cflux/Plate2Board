"""Routing-completion check on a realistic full-size board.

Builds a 62-switch staggered keyboard (5 rows × 13 cols + a stabilized
bottom-row key), routes it through the freerouting sidecar via the same
via-cost-ladder runner production uses, and reports every pad whose net
has copper but no wire endpoint/via landing on the pad — including the
nearest wire point (distinguishes 'freerouting never routed it' from 'our
splice lost/misplaced it').

Born as the reproduction for two production bugs (June 2026): a hotswap
SMD diode pad overlapping a neighboring key's stab housing hole, and
freerouting's first-no-progress-pass plateau stranding reachable pads.

Run inside the compose network:

    docker run --rm --network keeblayoutbot_default \
        -e FREEROUTING_URL=http://freerouting:37864 \
        -v "$PWD/backend:/src" -w /src keeblayoutbot-backend:latest \
        python scripts/repro_unattached.py [switch_type diode_type]

Artifacts (SES, routed PCB) are written to /src/_repro_out/.
"""

from __future__ import annotations

import asyncio
import math
import os
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
    apply_ses_to_pcb,
    parse_net_table,
    parse_ses,
)

U = 19.05  # 1u key pitch in mm

# Row x-offsets of a standard ANSI-ish stagger, in units.
ROW_STAGGER_U = [0.0, 0.5, 0.75, 1.25, 0.0]
ROW_KEYS = [13, 13, 13, 13, 10]  # 5 rows + 13 cols = 18 = Pro Micro GPIO budget


def build_parse() -> ParseResult:
    switches: list[SwitchDef] = []
    sid = 1
    top = 42.0  # leave room above row 0 for a non-overlapping MCU
    for row in range(5):
        n = ROW_KEYS[row]
        for k in range(n):
            switches.append(
                SwitchDef(
                    id=sid,
                    cx_mm=15.0 + (ROW_STAGGER_U[row] + k) * U,
                    cy_mm=top + row * U,
                    rotation_deg=0.0,
                    row=row,
                    col=k,
                )
            )
            sid += 1
    width = 15.0 * 2 + 14 * U
    height = top + 15.0 + 4 * U
    # 2u-ish stabilized key: give the last switch of the bottom row stabs.
    stab_sw = switches[-1]
    stabs = [
        StabilizerDef(
            id=i + 1,
            cx_mm=stab_sw.cx_mm + dx,
            cy_mm=stab_sw.cy_mm,
            width_mm=6.65,
            height_mm=12.3,
        )
        for i, dx in enumerate((-11.94, 11.94))
    ]
    return ParseResult(
        svg_width_mm=width,
        svg_height_mm=height,
        pcb_outline=PcbOutline(
            width_mm=width,
            height_mm=height,
            path_d=f"M 0 0 L {width} 0 L {width} {height} L 0 {height} Z",
        ),
        switches=switches,
        stabilizers=stabs,
        mounting_holes=[
            MountingHoleDef(id=i + 1, cx_mm=x, cy_mm=y, diameter_mm=2.2)
            for i, (x, y) in enumerate(
                [(8, 8), (width - 8, 8), (8, height - 8), (width - 8, height - 8)]
            )
        ],
        unclassified=[],
        mcu_placement=McuPlacement(
            cx_mm=width / 2, cy_mm=8.0, rotation_deg=0.0
        ),  # pins span y 8..36, clear of row 0 keepouts at y≥42
    )


def main() -> int:
    switch_type = sys.argv[1] if len(sys.argv) > 1 else "soldered"
    diode_type = sys.argv[2] if len(sys.argv) > 2 else "tht"
    label = f"[{switch_type}/{diode_type}]"
    parse = build_parse()
    print(f"{label} {len(parse.switches)} switches")

    pcb_text = generate_pcb(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount",
    )

    print(f"{label} routing via freerouting…", flush=True)
    result = asyncio.run(routing_runner.route_board(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount", timeout_s=600.0,
    ))

    os.makedirs("_repro_out", exist_ok=True)
    tag = f"{switch_type}_{diode_type}"
    with open(f"_repro_out/{tag}.ses", "w") as f:
        f.write(result.ses_text)

    pad_positions = pad_world_positions(
        parse, switch_type=switch_type, diode_type=diode_type,
        stabilizer_type="pcb_mount",
    )
    routed_pcb, stats = apply_ses_to_pcb(
        pcb_text,
        result.ses_text,
        total_connections=result.stats.total_net_count,
        unrouted_connections=result.stats.unrouted_net_count,
        pad_positions=pad_positions,
    )
    with open(f"_repro_out/{tag}.kicad_pcb", "w") as f:
        f.write(routed_pcb)

    print(
        f"{label} freerouting: {result.stats.routed_net_count} routed, "
        f"{result.stats.unrouted_net_count} unrouted; splice: "
        f"{stats.routed_count} segments, {stats.via_count} vias; "
        f"unattached pads: {stats.unattached_pad_count}"
    )

    # Re-run the attach check with full detail.
    net_table = parse_net_table(pcb_text)
    segments, vias = parse_ses(result.ses_text, net_table)
    points_by_code: dict[int, list[tuple[float, float]]] = {}
    for s in segments:
        points_by_code.setdefault(s.net_code, []).extend(
            [(s.x1_mm, s.y1_mm), (s.x2_mm, s.y2_mm)]
        )
    for v in vias:
        points_by_code.setdefault(v.net_code, []).append((v.cx_mm, v.cy_mm))

    bad = 0
    for net_name, pads in sorted(pad_positions.items()):
        code = net_table.get(net_name)
        points = points_by_code.get(code) if code is not None else None
        if not points:
            continue
        for px, py, radius in pads:
            dmin = min(math.hypot(x - px, y - py) for x, y in points)
            if dmin > radius + 0.2:
                bad += 1
                print(
                    f"  UNATTACHED {net_name} pad at ({px:.2f}, {py:.2f}) "
                    f"r={radius:.2f} — nearest wire point {dmin:.2f} mm away"
                )
    if not bad and result.stats.unrouted_net_count == 0:
        print(f"{label} PASS: fully routed, all pads attached")
        return 0
    print(f"{label} FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
