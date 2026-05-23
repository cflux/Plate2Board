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

import re
import uuid
from dataclasses import dataclass

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
                segments.append(Segment(
                    layer=layer,
                    width_mm=width,
                    x1_mm=coords[i],
                    y1_mm=coords[i + 1],
                    x2_mm=coords[i + 2],
                    y2_mm=coords[i + 3],
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
                cy_mm=cy,
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


def apply_ses_to_pcb(
    pcb_text: str,
    ses_text: str,
    *,
    total_connections: int = 0,
    unrouted_connections: int = 0,
    freerouting_quirk: bool = True,
) -> tuple[str, RouteStats]:
    """High-level: parse SES, splice into pcb, return (new_pcb, stats).

    `total_connections` / `unrouted_connections` come from the freerouting
    job-status response (the SES alone doesn't carry them). If they're 0
    the UI will just show "K wires, V vias" instead of "K of N routed".

    `freerouting_quirk` propagates to `parse_ses` (default True). Set
    False only for hand-crafted SES test fixtures.
    """
    net_table = parse_net_table(pcb_text)
    segments, vias = parse_ses(
        ses_text, net_table, freerouting_quirk=freerouting_quirk
    )
    routed_pcb = splice_routes(pcb_text, segments, vias)
    stats = RouteStats(
        routed_count=len(segments),
        via_count=len(vias),
        total_count=total_connections,
        unrouted_count=unrouted_connections,
    )
    return routed_pcb, stats
