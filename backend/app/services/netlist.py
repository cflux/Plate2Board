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
LED_VALUE = "SK6812MINI-E"
LED_FOOTPRINT = "keeb:LED_SK6812MINI-E"
CAP_VALUE = "100nF"
CAP_FOOTPRINT = "keeb:C_0603"
PRO_MICRO_GPIO_PINS = [
    5, 6, 7, 8, 9, 10, 11, 12,
    13, 14, 15, 16, 17, 18, 19, 20,
    1, 2,
]


def generate_netlist(
    switches: Iterable[SwitchDef],
    *,
    ground_pour: bool = True,
    rgb: bool = False,
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

    pins_needed = len(rows) + len(cols) + (1 if rgb else 0)
    if pins_needed > len(PRO_MICRO_GPIO_PINS):
        raise ValueError(
            f"matrix needs {pins_needed} GPIO pins"
            f"{' (incl. 1 for the RGB chain)' if rgb else ''}, but Pro Micro "
            f"only has {len(PRO_MICRO_GPIO_PINS)} available"
        )
    components.append((MCU_REF, MCU_VALUE, MCU_FOOTPRINT))

    # Map rows then cols onto PRO_MICRO_GPIO_PINS (D2..D9, D10..A3, then TX/RX).
    pin_iter = iter(PRO_MICRO_GPIO_PINS)
    for r in rows:
        nets[f"ROW{r}"].append((MCU_REF, next(pin_iter)))
    for c in cols:
        nets[f"COL{c}"].append((MCU_REF, next(pin_iter)))

    # GND/VCC/RGB_DATA* last (in this order) so every existing net keeps
    # its code. Pins 3/4/23 are the Pro Micro's ground pins; the PCB
    # carries GND via copper pours when the pour is on, traces otherwise.
    if ground_pour or rgb:
        nets["GND"] = [(MCU_REF, p) for p in (3, 4, 23)]

    if rgb:
        # SK6812 MINI-E chain: per LED — 1=VDD, 2=DOUT, 3=GND, 4=DIN.
        # RGB_DATA0 runs MCU → the first LED in serpentine order (see
        # pcb.rgb_chain_indices); RGB_DATA{i} runs hop i → hop i+1; the
        # last DOUT is left unconnected. 24 = RAW (USB 5 V).
        from .pcb import rgb_chain_indices

        nets["VCC"] = [(MCU_REF, 24)]
        data_pin = PRO_MICRO_GPIO_PINS[len(rows) + len(cols)]
        chain = rgb_chain_indices(swlist)
        n = len(swlist)
        for sw in swlist:
            led_ref = f"LED{sw.id}"
            cap_ref = f"C{sw.id}"
            components.append((led_ref, LED_VALUE, LED_FOOTPRINT))
            components.append((cap_ref, CAP_VALUE, CAP_FOOTPRINT))
            nets["VCC"].append((led_ref, 1))
            nets["VCC"].append((cap_ref, 1))
            nets["GND"].append((led_ref, 3))
            nets["GND"].append((cap_ref, 2))
            j = chain[sw.id]
            din_net = f"RGB_DATA{j}"
            nets.setdefault(din_net, [])
            if j == 0:
                nets[din_net].append((MCU_REF, data_pin))
            nets[din_net].append((led_ref, 4))
            if j + 1 < n:
                nets.setdefault(f"RGB_DATA{j + 1}", []).append((led_ref, 2))

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
