"""Generate a KiCad legacy netlist (S-expression) from a row/column matrix.

Each switch becomes:
    SW{id}                 — Cherry MX switch (Switch_Keyboard:SW_Push)
    D{id}                  — 1N4148 anti-ghost diode (Diode:1N4148)

Wiring per switch (COL2ROW per QMK):
    SW{id}.1  ←→  COL{col}
    SW{id}.2  ←→  D{id}.2  (anode)
    D{id}.1   ←→  ROW{row} (cathode)

The matrix is wired to a SparkFun Pro Micro module (U1) — rows mapped to
PRO_MICRO_GPIO_PINS in order, then cols.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models.schemas import SwitchDef
from .matrix import renumber_switches

SW_VALUE = "SW_Push"
SW_FOOTPRINT = "Button_Switch_Keyboard:SW_Cherry_MX_PCB_1.00u"
DIODE_VALUE = "1N4148"
DIODE_FOOTPRINT = "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"
MCU_REF = "U1"
MCU_VALUE = "ProMicro"
MCU_FOOTPRINT = "Module:Arduino_Pro_Micro"
PRO_MICRO_GPIO_PINS = [
    5, 6, 7, 8, 9, 10, 11, 12,
    13, 14, 15, 16, 17, 18, 19, 20,
    1, 2,
]


def generate_netlist(
    switches: Iterable[SwitchDef], *, ground_pour: bool = True
) -> str:
    # Renumber to row-major order so the netlist's SW1..SWN sequence matches
    # the schematic's grid layout (top-left to bottom-right).
    swlist = sorted(renumber_switches(list(switches)), key=lambda s: s.id)
    if not swlist:
        return _format_netlist([], {})

    rows = sorted({s.row for s in swlist})
    cols = sorted({s.col for s in swlist})

    components: list[tuple[str, str, str]] = []
    nets: dict[str, list[tuple[str, int]]] = {f"ROW{r}": [] for r in rows}
    for c in cols:
        nets[f"COL{c}"] = []

    for sw in swlist:
        sw_ref = f"SW{sw.id}"
        d_ref = f"D{sw.id}"
        components.append((sw_ref, SW_VALUE, SW_FOOTPRINT))
        components.append((d_ref, DIODE_VALUE, DIODE_FOOTPRINT))

        # COL2ROW direction: COL → SW → diode anode (D.2) → diode cathode (D.1) → ROW.
        # KiCad's Diode:1N4148 has pin 1 = K (cathode), pin 2 = A (anode).
        nets[f"COL{sw.col}"].append((sw_ref, 1))
        nets[f"ROW{sw.row}"].append((d_ref, 1))
        nets[f"NET-SW{sw.id}-D{sw.id}"] = [(sw_ref, 2), (d_ref, 2)]

    if len(rows) + len(cols) > len(PRO_MICRO_GPIO_PINS):
        raise ValueError(
            f"matrix has {len(rows) + len(cols)} row+col pins, but Pro Micro "
            f"only has {len(PRO_MICRO_GPIO_PINS)} GPIO pins available"
        )
    components.append((MCU_REF, MCU_VALUE, MCU_FOOTPRINT))

    # Map rows then cols onto PRO_MICRO_GPIO_PINS (D2..D9, D10..A3, then TX/RX).
    pin_iter = iter(PRO_MICRO_GPIO_PINS)
    for r in rows:
        nets[f"ROW{r}"].append((MCU_REF, next(pin_iter)))
    for c in cols:
        nets[f"COL{c}"].append((MCU_REF, next(pin_iter)))

    # GND last so every existing net keeps its code. Pins 3/4/23 are the
    # Pro Micro's ground pins; the PCB carries this net via copper pours.
    if ground_pour:
        nets["GND"] = [(MCU_REF, p) for p in (3, 4, 23)]

    return _format_netlist(components, nets)


def _format_netlist(
    components: list[tuple[str, str, str]],
    nets: dict[str, list[tuple[str, int]]],
) -> str:
    lines = ['(export (version "E")']
    lines.append("  (design")
    lines.append('    (source "keeb-layout-bot")')
    lines.append('    (tool "keeb-layout-bot 0.1.0")')
    lines.append("  )")

    lines.append("  (components")
    for ref, value, footprint in components:
        lines.append(
            f'    (comp (ref "{ref}") (value "{value}") (footprint "{footprint}"))'
        )
    lines.append("  )")

    lines.append("  (nets")
    for code, (name, nodes) in enumerate(nets.items(), start=1):
        lines.append(f'    (net (code "{code}") (name "{name}")')
        for ref, p in nodes:
            lines.append(f'      (node (ref "{ref}") (pin "{p}"))')
        lines.append("    )")
    lines.append("  )")

    lines.append(")")
    return "\n".join(lines) + "\n"
