"""MCU form-factor profiles.

Everything MCU-specific lives here: pin positions (footprint-local,
anchor = pin 1, KiCad Y-down), pad geometry, the GPIO allocation order
(rows first, then cols, then the RGB chain pin), ground / 5 V pins, body
outline for fab/silk + the frontend marker, and the schematic symbol
metrics the grid post-pass needs. The pcb / dsn / schematic / netlist
generators are all driven off the same profile so the four outputs can
never disagree about a pin.

Geometry sources:
- Pro Micro: SparkFun graphical datasheet (the original hardcoded data).
- Raspberry Pi Pico: KiCad official `Module.pretty/RaspberryPi_Pico_
  Common_THT.kicad_mod` — pin 1 at (0,0), pins 1-20 down the left column,
  21-40 up the right column at x=17.78, pitch 2.54, pad 1.6 / drill 1.0.
- Seeed XIAO: Seeed OPL `XIAO-RP2040-DIP.kicad_mod` (TH: pad 1.524 /
  drill 0.889, rows 15.24 apart, 7 pins per side at 2.54 pitch) and
  `XIAO-RP2040-SMD.kicad_mod` (castellation sandwich pads 2.75 × 2.0,
  centers 0.4625 mm outboard of the pin line). All XIAO variants
  (RP2040 / SAMD21 / nRF52840 / ESP32-C3) share the form factor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McuProfile:
    key: str
    display: str
    # Value property / netlist value string.
    value: str
    footprint_name: str
    # Symbol inside the bundled Keyboard_MCU library.
    symbol_name: str
    # Footprint description / tags text.
    descr: str
    tags: str
    # Pin number → (local_x, local_y), anchor = pin 1, KiCad Y-down,
    # USB end at the pin-1 (top) edge.
    pins: dict[int, tuple[float, float]]
    # Pad geometry. TH: (pad_size_mm, drill_mm). SMD: (size_x, size_y).
    pad_size: tuple[float, float]
    drill_mm: float | None  # None ⇒ SMD pads (castellated sandwich mount)
    # Physical pins usable for the matrix + RGB chain, in allocation
    # order: rows first, then cols, then `gpio_pins[rows+cols]` drives
    # the RGB chain.
    gpio_pins: tuple[int, ...]
    gnd_pins: tuple[int, ...]
    # USB 5 V pin — powers the RGB chain ("VCC" net).
    power_5v_pin: int
    # Bounding radius per pad for obstacle / edge-setback checks.
    pad_obstacle_r_mm: float
    # Module body in footprint-local coords: (x_min, y_min, w, h).
    body: tuple[float, float, float, float]
    # Schematic symbol metrics (lib Y-up): top pin Y and body half height
    # — drive the grid post-pass header placement / Value positioning.
    sym_top_pin_y: float
    sym_body_half_h: float


def _two_column_pins(
    n_per_side: int, pitch: float, row_spacing: float
) -> dict[int, tuple[float, float]]:
    """Standard DIP module numbering: pins 1..n down the left column at
    x=0, pins n+1..2n UP the right column at x=row_spacing."""
    pins: dict[int, tuple[float, float]] = {}
    for i in range(1, n_per_side + 1):
        pins[i] = (0.0, (i - 1) * pitch)
    for i in range(n_per_side + 1, 2 * n_per_side + 1):
        pins[i] = (row_spacing, (2 * n_per_side - i) * pitch)
    return pins


_PRO_MICRO = McuProfile(
    key="pro_micro",
    display="Pro Micro",
    value="ProMicro",
    footprint_name="keeb:Arduino_Pro_Micro",
    symbol_name="ProMicro",
    descr="SparkFun Pro Micro - ATmega32U4 module, 24-pin DIP-style",
    tags="Pro Micro Arduino ATmega32U4",
    pins=_two_column_pins(12, 2.54, 17.78),
    pad_size=(1.7, 1.7),
    drill_mm=1.0,
    # Rows-then-cols allocation order; TX/RX (1, 2) last because some
    # firmware reserves them for serial.
    gpio_pins=(5, 6, 7, 8, 9, 10, 11, 12,
               13, 14, 15, 16, 17, 18, 19, 20,
               1, 2),
    gnd_pins=(3, 4, 23),
    power_5v_pin=24,  # RAW = USB 5 V
    pad_obstacle_r_mm=0.85,
    body=(-0.11, -1.5, 18.0, 33.0),
    sym_top_pin_y=13.97,
    sym_body_half_h=16.51,
)

# XIAO pin functions (USB at top, top view): left column top→bottom is
# D0..D6 (pins 1-7); right column top→bottom reads 5V, GND, 3V3, D10,
# D9, D8, D7 — i.e. DIP numbering pins 8..14 bottom→top are D7, D8, D9,
# D10, 3V3, GND, 5V.
_XIAO_GPIO = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)  # D0..D10
_XIAO_PINS = _two_column_pins(7, 2.54, 15.24)
# Body 17.8 × 21 mm centered on the pin field (pin columns 15.24 apart,
# pin span 15.24 tall): 1.28 mm side margins, 2.88 mm end margins.
_XIAO_BODY = (-1.28, -2.88, 17.8, 21.0)

_XIAO = McuProfile(
    key="xiao",
    display="XIAO (through-hole)",
    value="XIAO",
    footprint_name="keeb:XIAO",
    symbol_name="XIAO",
    descr="Seeed Studio XIAO module (RP2040/SAMD21/nRF52840/ESP32), "
          "14-pin through-hole",
    tags="XIAO Seeed RP2040",
    pins=_XIAO_PINS,
    pad_size=(1.524, 1.524),
    drill_mm=0.889,
    gpio_pins=_XIAO_GPIO,
    gnd_pins=(13,),
    power_5v_pin=14,
    pad_obstacle_r_mm=0.762,
    body=_XIAO_BODY,
    sym_top_pin_y=7.62,
    sym_body_half_h=10.16,
)

# Castellated sandwich mount: same pin map, but SMD pads at the
# castellations — centers 0.4625 mm OUTBOARD of each pin column, pads
# 2.75 (outward) × 2.0 (along the pitch), per Seeed's official SMD
# footprint. The board solders flat onto the PCB.
_XIAO_SMD_PINS = {
    num: (x - 0.4625 if x == 0.0 else x + 0.4625, y)
    for num, (x, y) in _XIAO_PINS.items()
}

_XIAO_SMD = McuProfile(
    key="xiao_smd",
    display="XIAO (SMD sandwich)",
    value="XIAO",
    footprint_name="keeb:XIAO_SMD",
    symbol_name="XIAO",
    descr="Seeed Studio XIAO module, castellated SMD sandwich mount "
          "(board soldered flat onto the PCB)",
    tags="XIAO Seeed RP2040 SMD castellated",
    pins=_XIAO_SMD_PINS,
    pad_size=(2.75, 2.0),
    drill_mm=None,
    gpio_pins=_XIAO_GPIO,
    gnd_pins=(13,),
    power_5v_pin=14,
    pad_obstacle_r_mm=1.70,  # half-diagonal of 2.75 × 2.0
    body=_XIAO_BODY,
    sym_top_pin_y=7.62,
    sym_body_half_h=10.16,
)

# Raspberry Pi Pico physical pin map (left 1-20 down, right 21-40 up):
# GND at 3, 8, 13, 18, 23, 28, 33 (AGND), 38; RUN=30, ADC_VREF=35,
# 3V3(OUT)=36, 3V3_EN=37, VSYS=39 left unconnected; VBUS=40 is USB 5 V.
# gpio_pins are the 26 GP-carrying pins in GP0..GP22, GP26..GP28 order.
_PICO = McuProfile(
    key="pico",
    display="Raspberry Pi Pico",
    value="RaspberryPi_Pico",
    footprint_name="keeb:RaspberryPi_Pico",
    symbol_name="RaspberryPi_Pico",
    descr="Raspberry Pi Pico (RP2040), 40-pin DIP-style module",
    tags="RaspberryPi Pico RP2040",
    pins=_two_column_pins(20, 2.54, 17.78),
    pad_size=(1.6, 1.6),
    drill_mm=1.0,
    gpio_pins=(1, 2, 4, 5, 6, 7, 9, 10, 11, 12,      # GP0..GP9
               14, 15, 16, 17, 19, 20, 21, 22,        # GP10..GP17
               24, 25, 26, 27, 29,                    # GP18..GP22
               31, 32, 34),                           # GP26..GP28
    gnd_pins=(3, 8, 13, 18, 23, 28, 33, 38),
    power_5v_pin=40,  # VBUS
    pad_obstacle_r_mm=0.8,
    body=(-1.61, -1.37, 21.0, 51.0),
    sym_top_pin_y=24.13,
    sym_body_half_h=26.67,
)

MCU_PROFILES: dict[str, McuProfile] = {
    p.key: p for p in (_PRO_MICRO, _XIAO, _XIAO_SMD, _PICO)
}
MCU_TYPES: tuple[str, ...] = tuple(MCU_PROFILES)
DEFAULT_MCU_TYPE = "pro_micro"


def get_mcu_profile(key: str) -> McuProfile:
    try:
        return MCU_PROFILES[key]
    except KeyError:
        raise ValueError(
            f"unknown mcu_type: {key!r} (expected one of {sorted(MCU_PROFILES)})"
        ) from None
