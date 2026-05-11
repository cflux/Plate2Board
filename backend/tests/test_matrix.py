import pytest

from app.models.schemas import SwitchDef
from app.services.matrix import assign_matrix
from app.services.svg_parser import parse_plate_svg


def _sw(id_: int, cx: float, cy: float) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=cx, cy_mm=cy)


def test_simple_two_row_grid_assigns_rows_top_to_bottom() -> None:
    switches = [
        _sw(1, 0.0, 0.0),
        _sw(2, 19.05, 0.0),
        _sw(3, 0.0, 19.05),
        _sw(4, 19.05, 19.05),
    ]
    assign_matrix(switches)
    by_id = {s.id: (s.row, s.col) for s in switches}
    assert by_id[1] == (0, 0)
    assert by_id[2] == (0, 1)
    assert by_id[3] == (1, 0)
    assert by_id[4] == (1, 1)


def test_unsorted_input_still_groups_correctly() -> None:
    switches = [
        _sw(1, 19.05, 19.05),
        _sw(2, 0.0, 0.0),
        _sw(3, 19.05, 0.0),
        _sw(4, 0.0, 19.05),
    ]
    assign_matrix(switches)
    by_id = {s.id: (s.row, s.col) for s in switches}
    assert by_id[2] == (0, 0)
    assert by_id[3] == (0, 1)
    assert by_id[4] == (1, 0)
    assert by_id[1] == (1, 1)


def test_staggered_within_tolerance_stays_in_same_row() -> None:
    # 1mm of stagger within a row — common in real layouts.
    switches = [
        _sw(1, 0.0, 0.0),
        _sw(2, 19.05, 1.0),
        _sw(3, 38.10, 0.5),
    ]
    assign_matrix(switches)
    rows = {s.row for s in switches}
    assert rows == {0}


def test_kbplate_example_has_four_rows(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)
    rows = {s.row for s in result.switches}
    assert rows == {0, 1, 2, 3}, f"expected 4 rows, got {sorted(rows)}"

    # Each row has unique column indices.
    for r in rows:
        cols = [s.col for s in result.switches if s.row == r]
        assert len(cols) == len(set(cols)), f"row {r} has duplicate cols: {cols}"

    # Switches should be ordered left-to-right within each row.
    for r in rows:
        in_row = sorted(
            (s for s in result.switches if s.row == r),
            key=lambda s: s.col,
        )
        for prev, curr in zip(in_row, in_row[1:]):
            assert curr.cx_mm > prev.cx_mm, (
                f"row {r}: col {prev.col} (cx={prev.cx_mm}) "
                f"not < col {curr.col} (cx={curr.cx_mm})"
            )


def test_complex_example_assigns_every_switch(complex_example_svg: str) -> None:
    """Heavily column-staggered Dactyl layouts confound naive Y-grouping —
    every switch still gets a (row, col), but the auto-assignment is rough
    and the user is expected to drag-reassign in the UI."""
    result = parse_plate_svg(complex_example_svg)
    assert all(s.row >= 0 and s.col >= 0 for s in result.switches)

    # Within each row, columns must still be unique (no two switches share a slot).
    by_row: dict[int, list[int]] = {}
    for sw in result.switches:
        by_row.setdefault(sw.row, []).append(sw.col)
    for r, cols in by_row.items():
        assert len(cols) == len(set(cols)), f"row {r} has duplicate cols: {cols}"


def test_empty_switch_list_is_noop() -> None:
    switches: list[SwitchDef] = []
    assign_matrix(switches)
    assert switches == []


def test_column_first_simple_2x2_grid() -> None:
    # Same 2x2 grid as the row-first test, but with column_first the axes flip:
    # leftmost column (cx=0) → col 0, with the top switch (cy=0) → row 0.
    switches = [
        _sw(1, 0.0, 0.0),
        _sw(2, 19.05, 0.0),
        _sw(3, 0.0, 19.05),
        _sw(4, 19.05, 19.05),
    ]
    assign_matrix(switches, strategy="column_first")
    by_id = {s.id: (s.row, s.col) for s in switches}
    assert by_id[1] == (0, 0)
    assert by_id[3] == (1, 0)
    assert by_id[2] == (0, 1)
    assert by_id[4] == (1, 1)


def test_column_first_complex_example_assigns_correctly(
    complex_example_svg: str,
) -> None:
    """The complex_example.svg is a split-Dactyl with a wide gap between
    halves, so column-first cleaves into 2 column-clusters (one per hand).
    Within each cluster, rows must still be numbered 0..N-1 from the
    topmost switch downward — that's the algorithm contract."""
    result = parse_plate_svg(complex_example_svg, matrix_strategy="column_first")
    by_col: dict[int, list[SwitchDef]] = {}
    for sw in result.switches:
        by_col.setdefault(sw.col, []).append(sw)

    # At minimum we should split the two hands.
    assert len(by_col) >= 2

    for c, col_switches in by_col.items():
        col_switches.sort(key=lambda s: s.row)
        rows_in_col = [s.row for s in col_switches]
        assert rows_in_col == list(range(len(col_switches))), (
            f"col {c} rows are not 0..N: {rows_in_col}"
        )
        for prev, curr in zip(col_switches, col_switches[1:]):
            assert curr.cy_mm > prev.cy_mm


def test_assign_matrix_unknown_strategy_raises() -> None:
    switches = [_sw(1, 0.0, 0.0)]
    with pytest.raises(ValueError, match="unknown matrix strategy"):
        assign_matrix(switches, strategy="diagonal")


def test_stagger_aware_axis_aligned_2x2_grid() -> None:
    # Same axis-aligned grid as the column-first test — stagger-aware should
    # produce the same result for rotation=0 layouts.
    switches = [
        _sw(1, 0.0, 0.0),
        _sw(2, 19.05, 0.0),
        _sw(3, 0.0, 19.05),
        _sw(4, 19.05, 19.05),
    ]
    assign_matrix(switches, strategy="stagger_aware")
    by_id = {s.id: (s.row, s.col) for s in switches}
    assert by_id[1] == (0, 0)
    assert by_id[3] == (1, 0)
    assert by_id[2] == (0, 1)
    assert by_id[4] == (1, 1)


def test_stagger_aware_complex_example_recovers_finger_columns(
    complex_example_svg: str,
) -> None:
    """The Dactyl complex_example has 12 finger columns + 2 thumb keys (58 total).
    Stagger-aware should recover the 12 finger columns by chaining through
    each switch's own rotation. The 2 thumb keys don't chain to anything and
    become their own single-switch columns — that's correct per design and
    the user can drag them in the matrix grid."""
    result = parse_plate_svg(complex_example_svg, matrix_strategy="stagger_aware")
    by_col: dict[int, list[SwitchDef]] = {}
    for sw in result.switches:
        by_col.setdefault(sw.col, []).append(sw)

    multi_key_cols = [c for c, sw in by_col.items() if len(sw) >= 2]
    # The 12 finger columns should all chain into multi-key columns. The 2
    # thumb-cluster orphans become single-switch columns (13/14 either way).
    assert len(multi_key_cols) == 12, (
        f"expected exactly 12 multi-key (finger) columns, got "
        f"{len(multi_key_cols)} (sizes: {sorted(len(sw) for sw in by_col.values())})"
    )

    # No column should exceed 5 keys (typical Dactyl column depth).
    max_col_size = max(len(sw) for sw in by_col.values())
    assert max_col_size <= 5

    # Within every column, rows are 0..N-1 ordered by cy.
    for c, col_switches in by_col.items():
        col_switches.sort(key=lambda s: s.row)
        rows_in_col = [s.row for s in col_switches]
        assert rows_in_col == list(range(len(col_switches))), (
            f"col {c} rows are not 0..N: {rows_in_col}"
        )
        for prev, curr in zip(col_switches, col_switches[1:]):
            assert curr.cy_mm > prev.cy_mm


def test_stagger_aware_kbplate_chains_each_column(example_plate_svg: str) -> None:
    """Axis-aligned kbplate: stagger-aware should chain each cx-aligned
    stack into its own column. Each column has switches with strictly
    increasing cy and the row indices form a 0..N-1 sequence."""
    result = parse_plate_svg(example_plate_svg, matrix_strategy="stagger_aware")
    by_col: dict[int, list[SwitchDef]] = {}
    for sw in result.switches:
        by_col.setdefault(sw.col, []).append(sw)

    for c, col_switches in by_col.items():
        col_switches.sort(key=lambda s: s.row)
        rows_in_col = [s.row for s in col_switches]
        assert rows_in_col == list(range(len(col_switches))), (
            f"col {c} rows are not 0..N: {rows_in_col}"
        )
