"""Splice Freerouting's SES output back into the kicad_pcb text.

The SES file is a Specctra session: it tells us which wires + vias to add
on which layers and net. We translate each one into a kicad_pcb
``(segment …)`` or ``(via …)`` token, look up the net code from the pcb's
own ``(net N "NAME")`` table, and insert the tokens just before the closing
``)`` of the ``(kicad_pcb …)`` sexp.

We don't round-trip via KiCad — same reason as `dsn.py`: we own both the
input (kicad_pcb we emitted) and the output (SES from freerouting), so we
can keep the splice surgical without reformatting the rest of the file.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- minimal S-expression tokenizer ----------------------------------------
#
# Specctra files use S-expressions with quoted strings ("...") and
# whitespace-separated atoms. This is enough for parsing both SES and the
# net-table portion of kicad_pcb; we don't try to be a general parser.

_TOKEN_RE = re.compile(r'''
    \s* (?:
        (?P<open>\() |
        (?P<close>\)) |
        "(?P<qstr>(?:[^"\\]|\\.)*)" |
        (?P<atom>[^\s()"]+)
    )
''', re.VERBOSE)


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Return ``[(kind, value), …]`` tokens. Kind ∈ {open, close, str, atom}."""
    out: list[tuple[str, str]] = []
    pos = 0
    end = len(text)
    while pos < end:
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            if text[pos:].strip() == "":
                break
            raise ValueError(
                f"unexpected token at position {pos}: {text[pos:pos + 40]!r}"
            )
        pos = m.end()
        if m.group("open"):
            out.append(("open", "("))
        elif m.group("close"):
            out.append(("close", ")"))
        elif m.group("qstr") is not None:
            out.append(("str", m.group("qstr")))
        else:
            out.append(("atom", m.group("atom")))
    return out


def _parse_sexp(text: str) -> list:
    """Parse a single top-level sexp into a nested list. Quoted strings are
    tagged via a ``("str", value)`` tuple so the caller can distinguish them
    from bare atoms when it matters.
    """
    tokens = _tokenize(text)
    node, idx = _parse_at(tokens, 0)
    return node


def _parse_at(tokens: list[tuple[str, str]], i: int):
    if i >= len(tokens) or tokens[i][0] != "open":
        raise ValueError(
            f"expected '(' at token {i}, got {tokens[i] if i < len(tokens) else 'EOF'}"
        )
    i += 1
    items: list = []
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "close":
            return items, i + 1
        if kind == "open":
            child, i = _parse_at(tokens, i)
            items.append(child)
            continue
        if kind == "str":
            items.append(("str", val))
        else:
            items.append(val)
        i += 1
    raise ValueError("unexpected EOF in sexp")


# --- domain ----------------------------------------------------------------


@dataclass
class Segment:
    layer: str
    width_mm: float
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    net_code: int
    net_name: str


@dataclass
class Via:
    cx_mm: float
    cy_mm: float
    pad_diameter_mm: float
    drill_diameter_mm: float
    net_code: int
    net_name: str


# --- net table from kicad_pcb ----------------------------------------------

# `(net 12 "ROW0")` at top level of (kicad_pcb …). Pads also contain
# `(net …)` clauses but those live INSIDE (footprint …) blocks, never at
# the top level — so a simple non-nested regex over the top-level region
# is safe. We use the same lightweight regex pcb.py / the kicad source use
# to walk net rows.
_NET_ROW_RE = re.compile(r'^\s*\(net\s+(\d+)\s+"([^"]*)"\s*\)\s*$', re.MULTILINE)


def parse_net_table(pcb_text: str) -> dict[str, int]:
    """Return ``{net_name: net_code}`` parsed from the pcb's ``(net N "NAME")``
    rows. Only the top-level rows are considered (pad-level ``(net N "NAME")``
    declarations are inside ``(footprint …)`` blocks and indent differently
    — typically by 2+ tabs — so the line-anchored regex skips them naturally
    given how pcb.py formats output)."""
    out: dict[str, int] = {}
    for m in _NET_ROW_RE.finditer(pcb_text):
        out[m.group(2)] = int(m.group(1))
    return out


# --- SES parsing -----------------------------------------------------------


def _atom(node) -> str:
    """Coerce a node to a bare atom string, accepting ('str', s) and raw."""
    if isinstance(node, tuple) and node[0] == "str":
        return node[1]
    return node


def _find_children(node: list, name: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and _atom(c[0]) == name]


def _find_child(node: list, name: str) -> list | None:
    for c in node:
        if isinstance(c, list) and c and _atom(c[0]) == name:
            return c
    return None


_UNIT_IN_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "um": 0.001,
    "inch": 25.4,
    "mil": 0.0254,
}

# Freerouting normalises every input DSN to its internal scale and echoes
# back coordinates at THAT scale, regardless of what `(resolution …)` it
# also emits in the SES. Empirically (freerouting 2.2.4), a DSN declared
# at `um 10` produces SES coords 10× larger than the SES's own resolution
# token would imply. We compensate in the SES parser so the spliced
# kicad_pcb traces land on real-world coords.
SES_FREEROUTING_SCALE_QUIRK = 10.0


def _resolution_divisor(ses_node: list, quirk: float = 1.0) -> float:
    """Return the divisor that converts SES integer coords → mm. For a
    ``(resolution UNIT SCALE)`` token, each integer tick is ``UNIT/SCALE``
    of the unit's metric length, so ``mm = ticks * UNIT_IN_MM / SCALE``,
    i.e. ``divisor = SCALE / UNIT_IN_MM``. The result is multiplied by
    `quirk` to compensate for freerouting's internal up-scaling — pass
    `quirk=SES_FREEROUTING_SCALE_QUIRK` for real freerouting output, or
    leave at 1.0 for hand-crafted test SES files."""
    # resolution lives inside (routes …); fall back to looking at the
    # session top-level if not found there.
    routes = _find_child(ses_node, "routes")
    candidates = [routes, ses_node] if routes else [ses_node]
    for cand in candidates:
        if cand is None:
            continue
        res = _find_child(cand, "resolution")
        if res:
            unit = _atom(res[1]).lower()
            scale = float(_atom(res[2]))
            unit_mm = _UNIT_IN_MM.get(unit)
            if unit_mm is None:
                raise ValueError(f"unsupported SES resolution unit: {unit!r}")
            return (scale / unit_mm) * quirk
    # Default matches what dsn.py emits: (um 10) → divisor 10000 (× quirk).
    return 10_000.0 * quirk


def parse_ses(
    ses_text: str,
    net_table: dict[str, int],
    *,
    freerouting_quirk: bool = True,
) -> tuple[list[Segment], list[Via]]:
    """Parse routed wires + vias out of an SES file.

    `net_table` is the same `{name: code}` dict the kicad_pcb was emitted
    with (use `parse_net_table` on the pcb text). Wires/vias referencing
    nets that aren't in the table are skipped — freerouting sometimes
    invents synthetic nets for its internal bookkeeping that we don't need
    to splice back in.

    `freerouting_quirk` (default True) applies the 10× compensation for
    freerouting's internal up-scaling of input DSN coords. Tests with
    hand-crafted SES files should pass False.
    """
    tree = _parse_sexp(ses_text)
    if not isinstance(tree, list) or not tree or _atom(tree[0]) != "session":
        raise ValueError("SES root is not (session …)")
    quirk = SES_FREEROUTING_SCALE_QUIRK if freerouting_quirk else 1.0
    divisor = _resolution_divisor(tree, quirk=quirk)
    routes = _find_child(tree, "routes")
    if routes is None:
        return [], []
    network_out = _find_child(routes, "network_out")
    if network_out is None:
        return [], []

    segments: list[Segment] = []
    vias: list[Via] = []

    for net_node in _find_children(network_out, "net"):
        if len(net_node) < 2:
            continue
        net_name = _atom(net_node[1])
        net_code = net_table.get(net_name)
        if net_code is None:
            # Skip unknown nets (synthetic / freerouting bookkeeping).
            continue
        for wire in _find_children(net_node, "wire"):
            path = _find_child(wire, "path")
            if path is None or len(path) < 5:
                continue
            # (path "F.Cu" 250 x1 y1 x2 y2 …)
            layer = _atom(path[1])
            try:
                width = float(_atom(path[2])) / divisor
                coords = [float(_atom(t)) / divisor for t in path[3:]]
            except (TypeError, ValueError):
                continue
            for i in range(0, len(coords) - 2, 2):
                # Negate Y to undo the Y-flip we applied on DSN emit (see
                # `dsn.py` module docstring) so coords land in KiCad's
                # Y-down convention again.
                segments.append(Segment(
                    layer=layer,
                    width_mm=width,
                    x1_mm=coords[i],
                    y1_mm=-coords[i + 1],
                    x2_mm=coords[i + 2],
                    y2_mm=-coords[i + 3],
                    net_code=net_code,
                    net_name=net_name,
                ))
        for via in _find_children(net_node, "via"):
            # (via "padstack_name" x y …)
            if len(via) < 4:
                continue
            try:
                cx = float(_atom(via[2])) / divisor
                cy = float(_atom(via[3])) / divisor
            except (TypeError, ValueError):
                continue
            # Pull pad / drill diameter out of the padstack name when it
            # follows the canonical "Name_W:D_um" / "Name_W:D" pattern dsn.py
            # emits. Falls back to the project.py defaults (0.6 / 0.3).
            ps_name = _atom(via[1])
            pad_d, drill_d = _via_dims_from_padstack(ps_name)
            vias.append(Via(
                cx_mm=cx,
                cy_mm=-cy,  # un-flip Y, same as segment endpoints
                pad_diameter_mm=pad_d,
                drill_diameter_mm=drill_d,
                net_code=net_code,
                net_name=net_name,
            ))
    return segments, vias


_VIA_DIMS_RE = re.compile(r"_([\d.]+):([\d.]+)")


def _via_dims_from_padstack(name: str) -> tuple[float, float]:
    """Default via dims match the Matrix netclass in project.py (0.6 / 0.3)."""
    m = _VIA_DIMS_RE.search(name)
    if not m:
        return (0.6, 0.3)
    return (float(m.group(1)), float(m.group(2)))


# --- splice back into kicad_pcb --------------------------------------------


def _u() -> str:
    return str(uuid.uuid4())


def _render_segment(s: Segment) -> str:
    return (
        f'\t(segment (start {s.x1_mm:.4f} {s.y1_mm:.4f}) '
        f'(end {s.x2_mm:.4f} {s.y2_mm:.4f}) '
        f'(width {s.width_mm:.4f}) (layer "{s.layer}") '
        f'(net {s.net_code}) (uuid "{_u()}"))'
    )


def _render_via(v: Via) -> str:
    return (
        f'\t(via (at {v.cx_mm:.4f} {v.cy_mm:.4f}) '
        f'(size {v.pad_diameter_mm:.4f}) (drill {v.drill_diameter_mm:.4f}) '
        f'(layers "F.Cu" "B.Cu") '
        f'(net {v.net_code}) (uuid "{_u()}"))'
    )


def splice_routes(
    pcb_text: str,
    segments: list[Segment],
    vias: list[Via],
) -> str:
    """Insert ``(segment …)`` / ``(via …)`` tokens immediately before the
    final closing ``)`` of the kicad_pcb. Returns the new pcb text.

    If there are no routes (freerouting failed to find anything), the
    original text is returned unchanged.
    """
    if not segments and not vias:
        return pcb_text
    tokens = [_render_via(v) for v in vias] + [_render_segment(s) for s in segments]
    block = "\n" + "\n".join(tokens) + "\n"
    # The pcb sexp ends with a final ')' possibly followed by whitespace.
    # Insert our block immediately before that closing paren.
    idx = pcb_text.rstrip().rfind(")")
    if idx < 0:
        raise ValueError("kicad_pcb text has no closing paren")
    return pcb_text[:idx] + block + pcb_text[idx:]


# --- post-route validation ---------------------------------------------------

# A wire counts as attached to a pad if an endpoint (or via) lands within
# the pad's half-extent plus this slop. Generous enough for endpoints that
# terminate on the pad edge rather than dead-center, tight enough to flag
# the millimetre-scale drift a coordinate-convention bug produces.
PAD_ATTACH_SLOP_MM = 0.2


def find_unattached_pads(
    segments: list[Segment],
    vias: list[Via],
    pad_positions: dict[str, list[tuple[float, float, float]]],
    net_table: dict[str, int],
) -> list[tuple[str, float, float, float]]:
    """Pads whose net has routed copper but where no wire endpoint or via
    lands on the pad itself, as ``(net_name, x_mm, y_mm, nearest_mm)``.

    This is the tripwire for convention bugs between dsn.py and pcb.py: the
    DSN is self-consistent by construction, so freerouting happily reports
    success even when its view of the board is rotated/mirrored relative to
    the kicad_pcb — the only observable symptom is wires that don't touch
    pads after the splice. Nets with no copper at all are excluded (those
    are honest routing failures, already counted in `unrouted_count`).

    `pad_positions` maps net name → ``[(x_mm, y_mm, radius_mm)]`` in KiCad
    Y-down coords — see `dsn.pad_world_positions`.
    """
    points_by_code: dict[int, list[tuple[float, float]]] = {}
    for s in segments:
        pts = points_by_code.setdefault(s.net_code, [])
        pts.append((s.x1_mm, s.y1_mm))
        pts.append((s.x2_mm, s.y2_mm))
    for v in vias:
        points_by_code.setdefault(v.net_code, []).append((v.cx_mm, v.cy_mm))

    unattached: list[tuple[str, float, float, float]] = []
    for net_name, pads in pad_positions.items():
        code = net_table.get(net_name)
        points = points_by_code.get(code) if code is not None else None
        if not points:
            continue
        for px, py, radius in pads:
            nearest = min(
                math.hypot(x - px, y - py) for x, y in points
            )
            if nearest > radius + PAD_ATTACH_SLOP_MM:
                unattached.append((net_name, px, py, nearest))
    return unattached


def count_unattached_pads(
    segments: list[Segment],
    vias: list[Via],
    pad_positions: dict[str, list[tuple[float, float, float]]],
    net_table: dict[str, int],
) -> int:
    """Count of `find_unattached_pads` — kept for callers that only need
    the headline number."""
    return len(find_unattached_pads(segments, vias, pad_positions, net_table))


# --- public top-level helper ------------------------------------------------


@dataclass
class RouteStats:
    """Stats reported alongside the spliced pcb so the API / UI can render a
    'K of N routed' summary. `total_count` reflects ratsnest connections that
    needed routing (one per pin-to-pin link), and `routed_count` is the
    fraction freerouting closed."""
    routed_count: int
    via_count: int
    # `total_count` is supplied by the caller — it isn't computable from SES
    # alone (the SES doesn't enumerate unfinished rats). The freerouting REST
    # API reports it directly.
    total_count: int = 0
    unrouted_count: int = 0
    # Pads with routed copper on their net but no wire actually touching
    # them — see `count_unattached_pads`. 0 when the splice is geometrically
    # sound; only populated when the caller supplies `pad_positions`.
    unattached_pad_count: int = 0


def apply_ses_to_pcb(
    pcb_text: str,
    ses_text: str,
    *,
    total_connections: int = 0,
    unrouted_connections: int = 0,
    freerouting_quirk: bool = True,
    pad_positions: dict[str, list[tuple[float, float, float]]] | None = None,
) -> tuple[str, RouteStats]:
    """High-level: parse SES, splice into pcb, return (new_pcb, stats).

    `total_connections` / `unrouted_connections` come from the freerouting
    job-status response (the SES alone doesn't carry them). If they're 0
    the UI will just show "K wires, V vias" instead of "K of N routed".

    `pad_positions` (from `dsn.pad_world_positions`) enables the post-route
    geometry check — see `count_unattached_pads`.

    `freerouting_quirk` propagates to `parse_ses` (default True). Set
    False only for hand-crafted SES test fixtures.
    """
    net_table = parse_net_table(pcb_text)
    segments, vias = parse_ses(
        ses_text, net_table, freerouting_quirk=freerouting_quirk
    )
    routed_pcb = splice_routes(pcb_text, segments, vias)
    unattached = 0
    if pad_positions is not None:
        missing = find_unattached_pads(segments, vias, pad_positions, net_table)
        unattached = len(missing)
        if missing:
            detail = "; ".join(
                f"{net} pad at ({x:.2f}, {y:.2f}) — nearest wire {d:.2f} mm"
                for net, x, y, d in missing[:10]
            )
            logger.warning(
                "post-route check: %d pad(s) have routed copper on their "
                "net but no wire touching them: %s",
                unattached, detail,
            )
    stats = RouteStats(
        routed_count=len(segments),
        via_count=len(vias),
        total_count=total_connections,
        unrouted_count=unrouted_connections,
        unattached_pad_count=unattached,
    )
    return routed_pcb, stats
