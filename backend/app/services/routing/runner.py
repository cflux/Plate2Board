"""Route a board through freerouting, retrying across a via-cost ladder.

Freerouting 2.2.4 stops at the first auto-router pass that makes no
progress, so a single run can plateau with a few nets unrouted — and which
nets strand is deterministic per via cost but flips between via costs
(measured: a 62-switch hotswap/smd board routes fully at via_costs=20 but
strands 2 nets at 50, while the small e2e fixture routes fully at 50 but
strands 1 net at 20). Re-routing the same DSN is useless (same plateau),
but changing the via cost explores a genuinely different solution space.

`route_board` tries each rung until a run completes fully, and otherwise
returns the attempt with the fewest unrouted nets. Extra rungs only cost
time when the previous rung failed to complete.
"""

from __future__ import annotations

import logging
from typing import Optional

from ...models.schemas import ParseResult
from ..pcb import DiodeType, StabilizerType, SwitchType
from . import client
from .client import ProgressCB, RouteResult
from .dsn import pcb_to_dsn

logger = logging.getLogger(__name__)

# First rung is freerouting's default via cost; later rungs progressively
# favor vias to let congested single-layer areas escape to the other side.
VIA_COST_LADDER: tuple[int, ...] = (50, 20)


async def route_board(
    parse: ParseResult,
    *,
    switch_type: SwitchType = "soldered",
    diode_type: DiodeType = "tht",
    stabilizer_type: StabilizerType = "pcb_mount",
    ground_pour: bool = True,
    rgb: bool = False,
    progress_cb: Optional[ProgressCB] = None,
    timeout_s: float | None = None,
) -> RouteResult:
    """Route `parse` and return the best result across the via-cost ladder.

    Raises `client.FreeroutingError` only if every attempt hard-fails; a
    hard failure after a successful attempt returns the earlier result.
    """
    best: RouteResult | None = None
    last_error: client.FreeroutingError | None = None
    for attempt, via_costs in enumerate(VIA_COST_LADDER, start=1):
        dsn_text = pcb_to_dsn(
            parse,
            switch_type=switch_type,
            diode_type=diode_type,
            stabilizer_type=stabilizer_type,
            via_costs=via_costs,
            ground_pour=ground_pour,
            rgb=rgb,
        )
        try:
            result = await client.route(
                dsn_text, progress_cb=progress_cb, timeout_s=timeout_s
            )
        except client.FreeroutingError as exc:
            logger.warning(
                "routing attempt %d (via_costs=%d) failed: %s",
                attempt, via_costs, exc,
            )
            last_error = exc
            continue
        if best is None or (
            result.stats.unrouted_net_count < best.stats.unrouted_net_count
        ):
            best = result
        if best.stats.unrouted_net_count == 0:
            break
        logger.info(
            "routing attempt %d (via_costs=%d) left %d net(s) unrouted%s",
            attempt, via_costs, result.stats.unrouted_net_count,
            " — retrying with the next via cost"
            if attempt < len(VIA_COST_LADDER) else "",
        )
    if best is None:
        raise last_error or client.FreeroutingError("routing produced no result")
    return best
