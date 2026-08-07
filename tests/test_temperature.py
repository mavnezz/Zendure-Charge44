"""hyperTmp arrives in Kelvin (e.g. "305.1"), not °C.

Regression for the false "too_hot" guard: treating the raw value as °C put
every reading (~300) far above the 45 °C limit and froze output permanently.
"""
from __future__ import annotations


def test_hyperTmp_is_converted_from_kelvin(coord):
    coord._update_zendure("sensor", "hyperTmp", "305.1")
    assert coord.state.temperature == 32.0
    assert coord.state.temperature_guard == "ok"


def test_genuinely_hot_still_trips_guard(coord):
    """322.1 K = 49.0 °C > 45 °C default limit."""
    coord._update_zendure("sensor", "hyperTmp", "322.1")
    assert coord.state.temperature == 49.0
    assert coord.state.temperature_guard == "too_hot"


def test_cold_pack_trips_low_guard(coord):
    """275.1 K = 2.0 °C < 5 °C default limit."""
    coord._update_zendure("sensor", "hyperTmp", "275.1")
    assert coord.state.temperature == 2.0
    assert coord.state.temperature_guard == "too_cold"
