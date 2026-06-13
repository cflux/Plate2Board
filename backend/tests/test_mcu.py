"""MCU profile registry sanity checks."""

import pytest

from app.services.mcu import (
    MCU_TYPES,
    get_mcu_profile,
)


def test_registry_keys() -> None:
    assert set(MCU_TYPES) == {"pro_micro", "xiao", "xiao_smd", "pico"}


@pytest.mark.parametrize(
    "key,total_pins,gpio_count",
    [
        ("pro_micro", 24, 18),
        ("xiao", 14, 11),
        ("xiao_smd", 14, 11),
        ("pico", 40, 26),
    ],
)
def test_pin_and_gpio_counts(key, total_pins, gpio_count) -> None:
    p = get_mcu_profile(key)
    assert len(p.pins) == total_pins
    assert len(p.gpio_pins) == gpio_count
    # Every gpio / gnd / 5V pin is a real pin number.
    valid = set(p.pins)
    assert set(p.gpio_pins) <= valid
    assert set(p.gnd_pins) <= valid
    assert p.power_5v_pin in valid
    # No pin is double-booked as both GPIO and a power/ground rail.
    assert not (set(p.gpio_pins) & set(p.gnd_pins))
    assert p.power_5v_pin not in p.gpio_pins


def test_xiao_th_and_smd_share_pin_map_and_function() -> None:
    th = get_mcu_profile("xiao")
    smd = get_mcu_profile("xiao_smd")
    # Same logical pins, gpio order, gnd / 5V.
    assert th.gpio_pins == smd.gpio_pins
    assert th.gnd_pins == smd.gnd_pins
    assert th.power_5v_pin == smd.power_5v_pin
    # TH drills; SMD doesn't.
    assert th.drill_mm is not None
    assert smd.drill_mm is None
    # SMD pads sit outboard of the TH pin columns (same Y).
    for num, (tx, ty) in th.pins.items():
        sx, sy = smd.pins[num]
        assert sy == ty
        assert abs(sx) >= abs(tx)


def test_pin1_is_top_of_left_column() -> None:
    # Pin 1 anchors the footprint at the USB end (minimum Y = top, left
    # column). TH profiles put it exactly at the origin; the XIAO-SMD
    # pads shift outboard in X but keep Y = 0.
    for key in MCU_TYPES:
        p = get_mcu_profile(key)
        x1, y1 = p.pins[1]
        assert y1 == 0.0
        assert y1 == min(y for _x, y in p.pins.values())
        if p.drill_mm is not None:
            assert (x1, y1) == (0.0, 0.0)


def test_unknown_mcu_raises() -> None:
    with pytest.raises(ValueError, match="unknown mcu_type"):
        get_mcu_profile("teensy")
