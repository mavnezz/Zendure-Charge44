"""Decision matrix for `_want_cheap_charge`.

Covers every branch: temperature guard, SOC ceiling, free-charge override,
cheap-charge gating, solar-forecast gap.
"""
from __future__ import annotations

import pytest


# --- temperature guard ------------------------------------------------------

@pytest.mark.parametrize("guard", ["too_cold", "too_hot"])
def test_temperature_guard_blocks_unconditionally(coord, guard):
    coord.state.temperature_guard = guard
    coord.state.charge_when_free = True
    coord.state.current_price = -0.05
    coord.state.cheap_charge_enabled = True
    coord.state.soc = 50
    coord.state.target_soc = 80
    assert coord._want_cheap_charge(is_cheap=True) is False


# --- SOC ceiling ------------------------------------------------------------

def test_soc_unknown_blocks(coord):
    coord.state.soc = None
    coord.state.charge_when_free = True
    coord.state.current_price = -0.05
    assert coord._want_cheap_charge(is_cheap=True) is False


def test_soc_at_or_above_target_blocks(coord):
    coord.state.soc = 80
    coord.state.target_soc = 80
    coord.state.charge_when_free = True
    coord.state.current_price = -0.05
    assert coord._want_cheap_charge(is_cheap=True) is False

    coord.state.soc = 95
    assert coord._want_cheap_charge(is_cheap=True) is False


# --- free-charge override ---------------------------------------------------

def test_free_charge_engages_at_zero_or_below(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.charge_when_free = True
    coord.state.cheap_charge_enabled = False  # explicitly off — must still charge
    coord.state.grid_charge_needed_kwh = 0.0  # solar would normally cover it

    coord.state.current_price = 0.0
    assert coord._want_cheap_charge(is_cheap=False) is True

    coord.state.current_price = -0.10
    assert coord._want_cheap_charge(is_cheap=False) is True


def test_free_charge_off_falls_through(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.charge_when_free = False  # disabled
    coord.state.current_price = -0.10
    coord.state.cheap_charge_enabled = False
    assert coord._want_cheap_charge(is_cheap=True) is False


def test_free_charge_with_positive_price_falls_through(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.charge_when_free = True
    coord.state.current_price = 0.01  # one tenth of a cent — still positive
    coord.state.cheap_charge_enabled = False
    assert coord._want_cheap_charge(is_cheap=True) is False


def test_free_charge_with_unknown_price_falls_through(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.charge_when_free = True
    coord.state.current_price = None
    coord.state.cheap_charge_enabled = False
    assert coord._want_cheap_charge(is_cheap=True) is False


# --- cheap-charge gating ----------------------------------------------------

def test_cheap_charge_disabled_blocks(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.cheap_charge_enabled = False
    assert coord._want_cheap_charge(is_cheap=True) is False


def test_cheap_charge_enabled_but_not_cheap_hour(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.cheap_charge_enabled = True
    assert coord._want_cheap_charge(is_cheap=False) is False


def test_cheap_charge_no_forecast_falls_back_to_charge(coord):
    """No solar forecast → can't compute gap → trust the cheap-hour signal."""
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.cheap_charge_enabled = True
    coord.state.grid_charge_needed_kwh = None
    assert coord._want_cheap_charge(is_cheap=True) is True


def test_cheap_charge_solar_covers_target_blocks(coord):
    """Forecast says the sun will fill the battery → don't burn money on grid."""
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.cheap_charge_enabled = True
    coord.state.grid_charge_needed_kwh = 0.0  # solar fully covers
    assert coord._want_cheap_charge(is_cheap=True) is False


def test_cheap_charge_solar_partial_charges(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.cheap_charge_enabled = True
    coord.state.grid_charge_needed_kwh = 1.5  # solar leaves a gap
    assert coord._want_cheap_charge(is_cheap=True) is True


# --- precedence: free-charge > cheap-charge --------------------------------

def test_free_charge_wins_over_solar_skip(coord):
    """Even when solar covers everything, ≤0 prices should still trigger charging."""
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.charge_when_free = True
    coord.state.current_price = -0.05
    coord.state.cheap_charge_enabled = True
    coord.state.grid_charge_needed_kwh = 0.0  # would normally block cheap-charge
    assert coord._want_cheap_charge(is_cheap=False) is True


# --- manual charge override -------------------------------------------------

def test_manual_charge_engages_regardless_of_price(coord):
    """User-initiated charge should bypass price/window/forecast gates."""
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.manual_charge = True
    coord.state.cheap_charge_enabled = False
    coord.state.charge_when_free = False
    coord.state.current_price = 0.30  # very expensive
    coord.state.grid_charge_needed_kwh = 0.0
    assert coord._want_cheap_charge(is_cheap=False) is True


def test_manual_charge_still_respects_temperature(coord):
    coord.state.soc = 50
    coord.state.target_soc = 80
    coord.state.manual_charge = True
    coord.state.temperature_guard = "too_cold"
    assert coord._want_cheap_charge(is_cheap=False) is False


def test_manual_charge_still_respects_target_soc(coord):
    coord.state.soc = 80
    coord.state.target_soc = 80
    coord.state.manual_charge = True
    assert coord._want_cheap_charge(is_cheap=False) is False


def test_manual_charge_auto_cancels_when_target_reached(coord):
    """_apply_mode flips manual_charge off as soon as SOC hits target."""
    coord.state.soc = 80
    coord.state.target_soc = 80
    coord.state.manual_charge = True
    coord._apply_mode(is_cheap=False)
    assert coord.state.manual_charge is False


def test_manual_charge_persists_below_target(coord):
    coord.state.soc = 60
    coord.state.target_soc = 80
    coord.state.manual_charge = True
    coord.state.cheap_mode_active = True  # don't trigger _enter_cheap_mode again
    coord._apply_mode(is_cheap=False)
    assert coord.state.manual_charge is True
