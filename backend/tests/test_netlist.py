import re

import pytest

from app.models.schemas import SwitchDef
from app.services.netlist import generate_netlist
from app.services.svg_parser import parse_plate_svg


def _sw(id_: int, row: int, col: int) -> SwitchDef:
    return SwitchDef(id=id_, cx_mm=0.0, cy_mm=0.0, row=row, col=col)


def test_empty_input_emits_valid_skeleton() -> None:
    out = generate_netlist([])
    assert out.startswith('(export (version "E")')
    assert out.rstrip().endswith(")")
    assert "(components\n  )" in out
    assert "(nets\n  )" in out


def test_single_switch_creates_switch_diode_mcu() -> None:
    out = generate_netlist([_sw(1, 0, 0)])
    assert '(comp (ref "SW1")' in out
    assert '(comp (ref "D1")' in out
    assert '(comp (ref "U1")' in out
    # MCU is a Pro Micro module.
    assert "ProMicro" in out
    assert "Module:Arduino_Pro_Micro" in out
    # COL2ROW: SW.1 → COL, D pin 1 (cathode) → ROW, SW.2 ↔ D.2 (anode).
    assert re.search(
        r'\(net \(code "\d+"\) \(name "COL0"\)\s+\(node \(ref "SW1"\) \(pin "1"\)\)',
        out,
    )
    assert re.search(
        r'\(net \(code "\d+"\) \(name "ROW0"\)\s+\(node \(ref "D1"\) \(pin "1"\)\)',
        out,
    )
    # Inter-component link
    assert "NET-SW1-D1" in out


def test_kbplate_netlist_has_one_net_per_row_and_col(example_plate_svg: str) -> None:
    result = parse_plate_svg(example_plate_svg)
    out = generate_netlist(result.switches)

    rows = sorted({s.row for s in result.switches})
    cols = sorted({s.col for s in result.switches})

    for r in rows:
        assert f'(name "ROW{r}")' in out
    for c in cols:
        assert f'(name "COL{c}")' in out

    # Each switch contributes one SW{id} and one D{id} component.
    assert out.count("(comp (ref \"SW") == len(result.switches)
    assert out.count("(comp (ref \"D") == len(result.switches)

    # MCU is a fixed 24-pin Pro Micro regardless of matrix size.
    assert '(comp (ref "U1")' in out
    assert "ProMicro" in out
    # Each ROW and COL net should connect to a unique Pro Micro pin.
    for r in rows:
        m = re.search(
            rf'\(net \(code "\d+"\) \(name "ROW{r}"\).+?\(node \(ref "U1"\) \(pin "(\d+)"\)\)',
            out,
            re.DOTALL,
        )
        assert m, f"ROW{r} not connected to U1"
    for c in cols:
        m = re.search(
            rf'\(net \(code "\d+"\) \(name "COL{c}"\).+?\(node \(ref "U1"\) \(pin "(\d+)"\)\)',
            out,
            re.DOTALL,
        )
        assert m, f"COL{c} not connected to U1"


def test_collisions_in_row_col_ok() -> None:
    """Multiple switches in the same row/col share that net (the matrix's whole point)."""
    sws = [_sw(1, 0, 0), _sw(2, 0, 1), _sw(3, 1, 0), _sw(4, 1, 1)]
    out = generate_netlist(sws)
    # ROW0 should reference D1 and D2 (cathode pins of both row-0 switches' diodes).
    row0 = _extract_net(out, "ROW0")
    assert ('"D1"', '"1"') in row0
    assert ('"D2"', '"1"') in row0
    # COL0 should reference SW1 and SW3.
    col0 = _extract_net(out, "COL0")
    assert ('"SW1"', '"1"') in col0
    assert ('"SW3"', '"1"') in col0


def _extract_net(text: str, name: str) -> set[tuple[str, str]]:
    m = re.search(
        rf'\(net \(code "\d+"\) \(name "{name}"\)(.*?)\)\s*\n\s*(?:\(net|\)\s+\)\s*\n\))',
        text,
        re.DOTALL,
    )
    assert m, f"net {name!r} not found in netlist"
    nodes = set()
    for ref, pin in re.findall(r'\(node \(ref ("[^"]+")\) \(pin ("[^"]+")\)\)', m.group(1)):
        nodes.add((ref, pin))
    return nodes


@pytest.mark.parametrize("strategy", ["row_first", "column_first", "stagger_aware"])
def test_complex_example_oversized_for_pro_micro(
    complex_example_svg: str, strategy: str
) -> None:
    """The Dactyl fixture has more row+col pins than the 18 GPIO available
    on a Pro Micro under any matrix-detection strategy. Generation should
    raise a clear ValueError rather than silently dropping connections."""
    result = parse_plate_svg(complex_example_svg, matrix_strategy=strategy)
    with pytest.raises(ValueError, match="Pro Micro"):
        generate_netlist(result.switches)
