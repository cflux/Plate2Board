from __future__ import annotations

import math

from ..models.schemas import SwitchDef

ROW_TOLERANCE_MM = 9.0
COL_TOLERANCE_MM = 9.5
# Stagger-aware: pairwise adjacency criteria for "same column, next row(s)".
KEY_SPACING_MM = 19.05  # standard 1U key spacing
SPACING_TOL_MM = 4.0  # allowed deviation from k * KEY_SPACING_MM
ADJ_PERP_MAX_MM = 6.0  # max perpendicular drift over a 1U gap
MAX_ROW_GAP = 2  # accept 1U or 2U gaps; lets a column bridge a missing row

_VALID_STRATEGIES = ("row_first", "column_first", "stagger_aware")
AUTO_STRATEGY = "auto"


def row_major_refdes(switches: list[SwitchDef]) -> dict[int, int]:
    """Map each switch's `.id` to a 1-based refdes index in row-major order.

    Top-left of the matrix (smallest row, smallest col) gets refdes 1.
    Used by `renumber_switches` and any caller that wants to know what
    refdes a given input switch will end up with."""
    ordered = sorted(switches, key=lambda s: (s.row, s.col, s.id))
    return {sw.id: i + 1 for i, sw in enumerate(ordered)}


def renumber_switches(switches: list[SwitchDef]) -> list[SwitchDef]:
    """Return a new list of switches with `.id` reassigned in row-major
    order so the schematic/PCB/netlist refdes (`SW{id}`/`D{id}`) match
    the grid layout: top-left = SW1, bottom-right = SWN."""
    refdes = row_major_refdes(switches)
    return [
        SwitchDef(
            id=refdes[sw.id],
            cx_mm=sw.cx_mm,
            cy_mm=sw.cy_mm,
            rotation_deg=sw.rotation_deg,
            row=sw.row,
            col=sw.col,
        )
        for sw in switches
    ]


def assign_matrix(switches: list[SwitchDef], strategy: str = "row_first") -> str:
    """Assign row/col indices in-place using one of three strategies.
    Returns the actually-applied strategy name (`"auto"` resolves to one of
    the three real strategies).

    - ``row_first``: group by ``cy_mm`` into rows (Y gap > ROW_TOLERANCE_MM
      starts a new row), sort each row by ``cx_mm``. Wins on axis-aligned
      layouts; collapses heavily column-staggered ones.
    - ``column_first``: group by ``cx_mm`` into columns, sort each column by
      ``cy_mm``. Topmost switch in each column = row 0 regardless of its
      absolute Y vs. other columns. Wins on simple staggered layouts; on
      split keyboards it cleaves into 2 hand-clusters.
    - ``stagger_aware``: build a pairwise adjacency graph using each switch's
      own rotation as its column axis; connected components are columns. Wins
      on heavily column-staggered layouts (Dactyl) where simple X-clustering
      fails. Switches that don't chain (e.g. lone thumb keys) become their
      own single-key columns; the user can drag them in the matrix grid.
    - ``auto``: try all three strategies on a snapshot and pick the one whose
      resulting matrix is most square (smallest ``|rows − cols|``). Ties
      broken by declaration order in ``_VALID_STRATEGIES``.
    """
    if strategy == AUTO_STRATEGY:
        strategy = pick_best_strategy(switches)
    if strategy == "row_first":
        _assign_row_first(switches)
    elif strategy == "column_first":
        _assign_column_first(switches)
    elif strategy == "stagger_aware":
        _assign_stagger_aware(switches)
    else:
        raise ValueError(
            f"unknown matrix strategy: {strategy!r} "
            f"(expected one of {_VALID_STRATEGIES + (AUTO_STRATEGY,)})"
        )
    return strategy


def pick_best_strategy(switches: list[SwitchDef]) -> str:
    """Snapshot the switches, run each real strategy on a deep copy, and
    return the one whose matrix is most square AND most densely populated.

    Score = `|rows - cols| + EMPTY_CELL_PENALTY * (rows * cols - N)`.
    The balance term encodes the user's primary intent ("most even rows
    and cols"); the empty-cell term breaks ties toward strategies that
    fit the keys into a tighter grid. Without the penalty an axis-aligned
    keyboard like kbplate would prefer a sparse 6 × 11 over a tidy
    4 × 12 because |6−11| < |4−12| — visually unhelpful. Declaration
    order in `_VALID_STRATEGIES` is the final tie-break."""
    if not switches:
        return _VALID_STRATEGIES[0]
    n = len(switches)
    best_name = _VALID_STRATEGIES[0]
    best_score = float("inf")
    for name in _VALID_STRATEGIES:
        trial = [sw.model_copy(deep=True) for sw in switches]
        if name == "row_first":
            _assign_row_first(trial)
        elif name == "column_first":
            _assign_column_first(trial)
        else:
            _assign_stagger_aware(trial)
        n_rows = len({sw.row for sw in trial})
        n_cols = len({sw.col for sw in trial})
        balance = abs(n_rows - n_cols)
        empty_cells = n_rows * n_cols - n
        score = balance + 0.5 * empty_cells
        if score < best_score:
            best_score = score
            best_name = name
    return best_name


def _assign_row_first(switches: list[SwitchDef]) -> None:
    if not switches:
        return

    by_cy = sorted(switches, key=lambda s: s.cy_mm)
    rows: list[list[SwitchDef]] = [[by_cy[0]]]
    for sw in by_cy[1:]:
        if sw.cy_mm - rows[-1][-1].cy_mm <= ROW_TOLERANCE_MM:
            rows[-1].append(sw)
        else:
            rows.append([sw])

    for row_idx, row in enumerate(rows):
        row.sort(key=lambda s: s.cx_mm)
        for col_idx, sw in enumerate(row):
            sw.row = row_idx
            sw.col = col_idx


def _assign_column_first(switches: list[SwitchDef]) -> None:
    if not switches:
        return

    by_cx = sorted(switches, key=lambda s: s.cx_mm)
    cols: list[list[SwitchDef]] = [[by_cx[0]]]
    for sw in by_cx[1:]:
        if sw.cx_mm - cols[-1][-1].cx_mm <= COL_TOLERANCE_MM:
            cols[-1].append(sw)
        else:
            cols.append([sw])

    for col_idx, col in enumerate(cols):
        col.sort(key=lambda s: s.cy_mm)
        for row_idx, sw in enumerate(col):
            sw.row = row_idx
            sw.col = col_idx


def _assign_stagger_aware(switches: list[SwitchDef]) -> None:
    if not switches:
        return

    n = len(switches)
    adj: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _column_adjacent(switches[i], switches[j]) or _column_adjacent(
                switches[j], switches[i]
            ):
                adj[i].add(j)
                adj[j].add(i)

    visited = [False] * n
    columns: list[list[SwitchDef]] = []
    for start in range(n):
        if visited[start]:
            continue
        comp: list[SwitchDef] = []
        stack = [start]
        while stack:
            v = stack.pop()
            if visited[v]:
                continue
            visited[v] = True
            comp.append(switches[v])
            stack.extend(adj[v])
        columns.append(comp)

    # Order columns left-to-right by mean cx.
    columns.sort(key=lambda col: sum(s.cx_mm for s in col) / len(col))

    for col_idx, col in enumerate(columns):
        col.sort(key=lambda s: s.cy_mm)
        for row_idx, sw in enumerate(col):
            sw.row = row_idx
            sw.col = col_idx


def _column_adjacent(a: SwitchDef, b: SwitchDef) -> bool:
    """True if `b` is in `a`'s column, 1U or 2U away along the column axis.

    Uses `a`'s rotation as the column axis: pin direction = column axis.
    The 2U fallback bridges columns with a single missing row (common on
    60% layouts where the leftmost row-2 / row-3 key is wider than 1U
    and absent from most logical columns).
    """
    rot = math.radians(a.rotation_deg)
    axis_x, axis_y = -math.sin(rot), math.cos(rot)
    perp_x, perp_y = math.cos(rot), math.sin(rot)
    dx = b.cx_mm - a.cx_mm
    dy = b.cy_mm - a.cy_mm
    along = abs(dx * axis_x + dy * axis_y)
    perp = abs(dx * perp_x + dy * perp_y)
    if perp > ADJ_PERP_MAX_MM:
        return False
    for k in range(1, MAX_ROW_GAP + 1):
        target = k * KEY_SPACING_MM
        if abs(along - target) <= SPACING_TOL_MM:
            return True
    return False
