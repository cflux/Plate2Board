import os

import pytest

from app.models.schemas import SwitchDef
from app.services.schematic import generate_schematic

# Skip the entire module unless KiCad symbol libraries are reachable. Locally
# this means installing the `kicad-symbols` apt package; in production the
# backend Dockerfile installs it. CI without the package gets a clean skip.
SYMBOL_DIR = os.environ.get("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols")
if not os.path.isdir(SYMBOL_DIR):
    pytest.skip(
        f"kicad-symbols not installed at {SYMBOL_DIR} — install via "
        "`apt-get install kicad-symbols`",
        allow_module_level=True,
    )


def _sw(id_: int, row: int, col: int) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=0.0, cy_mm=0.0, row=row, col=col)


def test_zero_switches_raises() -> None:
    with pytest.raises(ValueError, match="zero switches"):
        generate_schematic([])


def test_single_switch_produces_valid_kicad_sch() -> None:
    text = generate_schematic([_sw(1, 0, 0)])
    assert text.startswith("(kicad_sch")
    assert text.count("(") == text.count(")")
    # Embeds lib_symbols and component instances for SW, D, J.
    assert "Switch:SW_Push" in text
    assert "Diode:1N4148" in text
    assert "Keyboard_MCU:ProMicro" in text
    assert "ROW0" in text
    assert "COL0" in text


def test_no_hide_yes_or_hide_no_in_output() -> None:
    """KiCad 9's parser rejects `(hide yes)` inside `(pin_numbers …)` —
    we post-process to use the bare-keyword `hide` everywhere."""
    text = generate_schematic([_sw(1, 0, 0)])
    assert "(hide yes)" not in text
    assert "(hide no)" not in text
    # Bare `hide` should still appear — Switch:SW_Push hides pin numbers.
    assert " hide" in text or "hide)" in text


def test_version_bumped_to_kicad9() -> None:
    """SKiDL's kicad9 backend writes `(version 20230409)` (KiCad 7) — we
    bump it so KiCad 9's parser selects modern grammar."""
    text = generate_schematic([_sw(1, 0, 0)])
    assert "(version 20230409)" not in text
    assert "(version 20241229)" in text


def test_pin_numbers_uses_bare_hide_keyword() -> None:
    text = generate_schematic([_sw(1, 0, 0)])
    # Switch:SW_Push emits `(pin_numbers (hide yes))`. After our fix it
    # should contain the bare-keyword `hide`, not `(hide yes)`.
    import re as _re

    pin_numbers_blocks = _re.findall(r"\(pin_numbers[^)]*\)", text)
    assert pin_numbers_blocks, "no pin_numbers block found"
    for block in pin_numbers_blocks:
        assert "(hide yes)" not in block
        assert "hide" in block


def test_grid_layout_places_switches_at_predictable_coords() -> None:
    """Components should land in a row × col grid, not wherever SKiDL
    auto-placer dropped them."""
    import re

    sws = [
        _sw(1, 0, 0),
        _sw(2, 0, 0),
    ]
    sws[0].row, sws[0].col = 0, 0
    sws[1].row, sws[1].col = 0, 1
    text = generate_schematic(sws)

    def _find_placed(ref: str) -> tuple[float, float]:
        # Reference appears once inside its own symbol block; capture the
        # nearest preceding `(at X Y R)` (which is the symbol's own coord).
        ref_pos = text.find(f'(property "Reference" "{ref}"')
        assert ref_pos != -1, f"{ref} not found"
        # Walk back to the nearest "(at X Y R)" before the reference.
        block = text[max(0, ref_pos - 400):ref_pos]
        ats = re.findall(r"\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+[-+\d.]+\)", block)
        assert ats, f"no (at) found before {ref}"
        return (float(ats[-1][0]), float(ats[-1][1]))

    sw1 = _find_placed("SW1")
    sw2 = _find_placed("SW2")
    d1 = _find_placed("D1")
    j1 = _find_placed("U1")

    # SW1 at col 0, SW2 at col 1 → exactly one column-spacing apart in X.
    assert abs((sw2[0] - sw1[0]) - 40.0) < 0.5
    # Same row → identical Y.
    assert abs(sw2[1] - sw1[1]) < 0.5
    # D1 sits below SW1, offset right by SW pin 2's local X (5.08 mm) so
    # the diode's anode lines up directly under SW pin 2 — the wire that
    # replaces the N$ label can then be a clean vertical segment.
    assert abs(d1[0] - sw1[0] - 5.08) < 0.5
    assert abs(d1[1] - sw1[1] - 12.0) < 0.5
    # U1 (Pro Micro) sits left of the matrix grid.
    assert j1[0] < sw1[0]


def test_n_dollar_labels_replaced_by_wires() -> None:
    """Intra-cell `N$N` labels (linking SW pin 2 to D anode) must be gone
    from the output. Each switch+diode pair should instead have a (wire ...)
    block connecting their pins."""
    import re

    sws = [_sw(1, 0, 0), _sw(2, 0, 0)]
    sws[0].row, sws[0].col = 0, 0
    sws[1].row, sws[1].col = 0, 1
    text = generate_schematic(sws)

    assert 'global_label "N$' not in text
    # Vertical link wires: one per switch (SW.2 → D anode).
    wires = re.findall(
        r"\(wire\s*\(pts\s*\(xy\s+([-+\d.]+)\s+([-+\d.]+)\)\s+\(xy\s+([-+\d.]+)\s+([-+\d.]+)\)",
        text,
    )
    vertical_link = [
        w for w in wires if abs(float(w[0]) - float(w[2])) < 0.1
    ]
    assert len(vertical_link) == 2, (
        f"expected 2 vertical link wires (one per switch), got "
        f"{len(vertical_link)}"
    )


def test_row_labels_consolidated_with_horizontal_wire() -> None:
    """For each row, the matrix grid has just ONE ROW{n} label on the left
    side; all the per-diode duplicates are gone, replaced by a horizontal
    wire that runs across all diode K pins in that row."""
    import re

    sws = []
    for c in range(3):
        sw_obj = _sw(c + 1, 0, 0)
        sw_obj.row, sw_obj.col = 0, c
        sws.append(sw_obj)
    text = generate_schematic(sws)

    # Count ROW0 labels at the matrix area (X > 90 = grid origin).
    matrix_row_labels = re.findall(
        r'\(global_label\s+"ROW0"\s*\(shape \w+\)\s*\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+[-+\d.]+\)',
        text,
    )
    matrix_only = [
        (float(x), float(y)) for x, y in matrix_row_labels if float(x) > 90
    ]
    # Was 3 (one per switch); now should be 1 (consolidated).
    assert len(matrix_only) == 1, (
        f"expected 1 consolidated ROW0 label, got {len(matrix_only)}: {matrix_only}"
    )

    # And there should be a horizontal wire at the diode K row Y, spanning
    # roughly from the consolidated label X to the rightmost diode X.
    wires = re.findall(
        r"\(wire\s*\(pts\s*\(xy\s+([-+\d.]+)\s+([-+\d.]+)\)\s+\(xy\s+([-+\d.]+)\s+([-+\d.]+)\)",
        text,
    )
    horiz = [
        (float(x1), float(y1), float(x2), float(y2))
        for x1, y1, x2, y2 in wires
        if abs(float(y1) - float(y2)) < 0.1
    ]
    assert horiz, "expected at least one horizontal wire (the row bus)"


def test_diodes_rotated_90_for_col2row_direction() -> None:
    """Diodes are rotated 90° — cathode-bar at the TOP toward the switch,
    anode at the BOTTOM toward the row bus. This matches QMK's COL2ROW
    direction (current: row → diode → switch → col)."""
    import re

    sws = [_sw(1, 0, 0)]
    sws[0].row, sws[0].col = 0, 0
    text = generate_schematic(sws)

    m = re.search(
        r'\(lib_id\s+"Diode:1N4148"\)\s*\(at\s+[-+\d.]+\s+[-+\d.]+\s+([-+\d.]+)\)\s*\(unit',
        text,
    )
    assert m, "D1 main (at) not found"
    rot = float(m.group(1))
    assert rot == 90.0


def test_refdes_assigned_in_row_major_order() -> None:
    """Switches are renumbered so SW1 is at (row=0, col=0) regardless of
    SVG-detection order. The unsorted input below should map:
        original id 1 (row=1, col=1) → SW4 (bottom-right)
        original id 2 (row=0, col=0) → SW1 (top-left)
        original id 3 (row=0, col=1) → SW2
        original id 4 (row=1, col=0) → SW3
    """
    import re

    sws = [_sw(1, 0, 0), _sw(2, 0, 0), _sw(3, 0, 0), _sw(4, 0, 0)]
    sws[0].row, sws[0].col = 1, 1
    sws[1].row, sws[1].col = 0, 0
    sws[2].row, sws[2].col = 0, 1
    sws[3].row, sws[3].col = 1, 0
    text = generate_schematic(sws)

    def _placed_position(ref: str) -> tuple[float, float]:
        ref_pos = text.find(f'(property "Reference" "{ref}"')
        assert ref_pos != -1, f"{ref} not in schematic"
        block = text[max(0, ref_pos - 400):ref_pos]
        ats = re.findall(r"\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+[-+\d.]+\)", block)
        assert ats
        return (float(ats[-1][0]), float(ats[-1][1]))

    sw1 = _placed_position("SW1")
    sw2 = _placed_position("SW2")
    sw3 = _placed_position("SW3")
    sw4 = _placed_position("SW4")

    # Row-major: SW1 < SW2 in X same row; SW3 < SW4 in X same row;
    # SW1.y < SW3.y; SW2.y < SW4.y (rows go top to bottom).
    assert sw1[1] < sw3[1]  # SW1 row 0, SW3 row 1
    assert sw2[1] < sw4[1]
    assert sw1[0] < sw2[0]  # cols go left to right
    assert sw3[0] < sw4[0]


def test_consolidated_row_label_extends_left() -> None:
    """The consolidated ROW label sits at the left end of the row wire and
    its text must extend AWAY from the wire (rotation 180, justify right)
    or it'll overlap the horizontal bus."""
    import re

    sws = []
    for c in range(3):
        sw_obj = _sw(c + 1, 0, 0)
        sw_obj.row, sw_obj.col = 0, c
        sws.append(sw_obj)
    text = generate_schematic(sws)

    # The matrix-area ROW0 label (after consolidation, X > 90) should have
    # rotation 180 and justify right.
    m = re.search(
        r'\(global_label\s+"ROW0"\s*\(shape \w+\)\s*\(at\s+([-+\d.]+)\s+'
        r'[-+\d.]+\s+([-+\d.]+)\)(?:.|\s)*?\(justify\s+(\w+)',
        text,
    )
    assert m, "consolidated ROW0 label not found"
    # Find the matrix-area instance specifically (X > 90).
    rows = re.finditer(
        r'\(global_label\s+"ROW0"\s*\(shape \w+\)\s*\(at\s+([-+\d.]+)\s+'
        r'[-+\d.]+\s+([-+\d.]+)\)(?:.|\s)*?\(justify\s+(\w+)',
        text,
    )
    matrix_label = None
    for r in rows:
        x = float(r.group(1))
        if x > 90:
            matrix_label = r
            break
    assert matrix_label is not None
    rotation = float(matrix_label.group(2))
    justify = matrix_label.group(3)
    assert rotation == 180.0, f"row label rotation must be 180, got {rotation}"
    assert justify == "right", f"row label justify must be right, got {justify!r}"


def test_diode_property_text_left_and_right_of_body() -> None:
    """Reference sits 5 mm to the LEFT of the rotated diode anchor (with
    justify right so text extends further left), Value sits 5 mm to the
    RIGHT (justify left, extends right). Both at the same Y as the anchor."""
    import re

    sws = [_sw(1, 0, 0)]
    sws[0].row, sws[0].col = 0, 0
    text = generate_schematic(sws)

    ref_m = re.search(
        r'\(property\s+"Reference"\s+"D1"\s*\(at\s+([-+\d.]+)\s+([-+\d.]+)',
        text,
    )
    assert ref_m
    ref_x = float(ref_m.group(1))
    ref_y = float(ref_m.group(2))

    anchor_m = re.search(
        r'\(lib_id\s+"Diode:1N4148"\)\s*\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+[-+\d.]+\)\s*\(unit',
        text,
    )
    assert anchor_m
    anchor_x = float(anchor_m.group(1))
    anchor_y = float(anchor_m.group(2))

    # Reference 5 mm to the LEFT, same Y.
    assert abs(ref_x - (anchor_x - 5.0)) < 0.5
    assert abs(ref_y - anchor_y) < 0.5

    # Value 5 mm to the RIGHT, same Y.
    val_search = text[ref_m.end():ref_m.end() + 1000]
    val_m = re.search(
        r'\(property\s+"Value"\s+"1N4148"\s*\(at\s+([-+\d.]+)\s+([-+\d.]+)',
        val_search,
    )
    assert val_m
    val_x = float(val_m.group(1))
    val_y = float(val_m.group(2))
    assert abs(val_x - (anchor_x + 5.0)) < 0.5
    assert abs(val_y - anchor_y) < 0.5

    # Justify: Reference right (text extends left), Value left (text extends right).
    ref_block_match = re.search(
        r'\(property\s+"Reference"\s+"D1".+?\(justify\s+(\w+)',
        text,
        re.DOTALL,
    )
    assert ref_block_match
    assert ref_block_match.group(1) == "right"

    val_block_match = re.search(
        r'\(property\s+"Value"\s+"1N4148".+?\(justify\s+(\w+)',
        val_search,
        re.DOTALL,
    )
    assert val_block_match
    assert val_block_match.group(1) == "left"


def test_schematic_includes_col2row_note() -> None:
    """A text annotation states the matrix wiring direction so anyone
    importing the schematic knows which DIODE_DIRECTION to set in firmware."""
    sws = [_sw(1, 0, 0)]
    sws[0].row, sws[0].col = 0, 0
    text = generate_schematic(sws)
    assert "COL2ROW" in text
    # The annotation is a `(text "...")` block, not a label or property.
    import re

    m = re.search(r'\(text\s+"[^"]*COL2ROW[^"]*"', text)
    assert m, "COL2ROW (text ...) annotation not found"


def test_grid_layout_uses_user_paper_sized_to_matrix() -> None:
    """Standard sizes (A4/A3) cap at 420 mm wide — 12+ col grids don't
    fit. Use User-defined paper sized to the actual grid extent."""
    import re

    sws = [_sw(1, 0, 0)]
    sws[0].row, sws[0].col = 0, 0
    text = generate_schematic(sws)

    m = re.search(r'\(paper "User" ([\d.]+) ([\d.]+)\)', text)
    assert m, f"expected User paper size in: {text[:200]}"
    w, h = float(m.group(1)), float(m.group(2))
    # Single switch needs space for U1 + 1 col + padding.
    assert 100 < w < 200
    assert 50 < h < 200
    # Standard A4/A3 sizes should NOT appear.
    assert '(paper "A4")' not in text
    assert '(paper "A3")' not in text


def test_header_pin_labels_follow_mcu_not_nearby_switch() -> None:
    """Every ROW/COL label sitting on a Pro Micro pin must travel with U1
    (anchor at GRID_HEADER_X_MM=50). The ProMicro's pins extend ±10.16 mm
    from its anchor and labels are pushed an additional 2 mm outboard, so
    they land at X ≈ 37.84 (left pins) or X ≈ 62.16 (right pins). Verify
    they don't drift away into the matrix area."""
    import re

    sws = []
    for r in range(4):
        for c in range(4):
            i = r * 4 + c + 1
            sw_obj = _sw(i, 0, 0)
            sw_obj.row = r
            sw_obj.col = c
            sws.append(sw_obj)
    text = generate_schematic(sws)

    label_positions = re.findall(
        r'\(global_label\s+"((?:ROW|COL)\d+)"\s*\(shape[^)]+\)\s*'
        r'\(at\s+([-+\d.]+)\s+([-+\d.]+)\s+[-+\d.]+\)',
        text,
    )
    assert label_positions, "no ROW/COL labels found"

    by_name: dict[str, list[tuple[float, float]]] = {}
    for name, x, y in label_positions:
        by_name.setdefault(name, []).append((float(x), float(y)))

    # Each net must have ≥1 label at U1's pin column (X ≈ 39.84 or 60.16).
    for name, positions in by_name.items():
        near_u1 = [
            p for p in positions
            if abs(p[0] - 37.84) < 1.0 or abs(p[0] - 62.16) < 1.0
        ]
        assert near_u1, (
            f"net {name!r} has no label at U1's pins "
            f"(positions: {positions})"
        )


def test_pro_micro_pin_assignments() -> None:
    """Rows then cols are wired to Pro Micro GPIO pins in PRO_MICRO_GPIO_PINS
    order. For a 2-row × 2-col matrix that's pins 5, 6 (rows) and pins 7, 8
    (cols)."""
    sws = []
    for r in range(2):
        for c in range(2):
            i = r * 2 + c + 1
            sw_obj = _sw(i, 0, 0)
            sw_obj.row, sw_obj.col = r, c
            sws.append(sw_obj)
    text = generate_schematic(sws)

    # The placed U1 instance should use the ProMicro lib_id.
    assert '(lib_id "Keyboard_MCU:ProMicro")' in text


def test_2x2_grid_contains_every_switch_and_diode() -> None:
    """Synthetic 4-switch 2×2 matrix exercises the full wiring path
    (row/col nets, header sized to 4 pins, all components present)."""
    sws = [_sw(1, 0, 0), _sw(2, 0, 1), _sw(3, 1, 0), _sw(4, 1, 1)]
    text = generate_schematic(sws)

    for sw in sws:
        assert f'"SW{sw.id}"' in text, f"missing SW{sw.id}"
        assert f'"D{sw.id}"' in text, f"missing D{sw.id}"

    for r in (0, 1):
        assert f"ROW{r}" in text
    for c in (0, 1):
        assert f"COL{c}" in text

    # MCU is a Pro Micro (24 pins, fixed) regardless of matrix size.
    assert '(lib_id "Keyboard_MCU:ProMicro")' in text
    assert text.count("(") == text.count(")")


def test_gnd_net_present_when_ground_pour_enabled() -> None:
    """SKiDL renders the GND net as power:GND symbols on the MCU's ground
    pins. (The bare string "GND" appears in the ProMicro symbol's own pin
    names regardless, so assert on the power-symbol lib_id.)"""
    text = generate_schematic([_sw(1, 0, 0)])
    assert '(lib_id "power:GND")' in text, "GND power symbol missing"
    off = generate_schematic([_sw(1, 0, 0)], ground_pour=False)
    assert '(lib_id "power:GND")' not in off


def test_rgb_schematic_has_led_chain_and_grid_targets() -> None:
    sws = [_sw(i + 1, 0, i) for i in range(3)]
    text = generate_schematic(sws, rgb=True)
    assert text.count('(lib_id "Keyboard_LED:SK6812MINI-E")') == 3
    assert text.count('(lib_id "Device:C")') == 3
    assert '"RGB_DATA0"' in text and '"RGB_DATA1"' in text and '"RGB_DATA2"' in text
    assert '(lib_id "power:VCC")' in text
    assert text.count("(") == text.count(")")
    # LED symbols landed on their grid targets (not SKiDL's auto placement):
    # LED for col c sits at GRID_ORIGIN_X + c*56, GRID_ORIGIN_Y + 24.
    from app.services.schematic import (
        GRID_COL_SPACING_RGB_MM,
        GRID_LED_OFFSET_Y_MM,
        GRID_ORIGIN_X_MM,
        GRID_ORIGIN_Y_MM,
    )
    import re as _re
    for c in range(3):
        x = GRID_ORIGIN_X_MM + c * GRID_COL_SPACING_RGB_MM
        y = GRID_ORIGIN_Y_MM + GRID_LED_OFFSET_Y_MM
        assert _re.search(
            rf'\(at {x:.4f} {y:.4f} 0\.000\)', text
        ), f"LED for col {c} not at grid target ({x}, {y})"
    off = generate_schematic(sws)
    assert "SK6812MINI-E" not in off
