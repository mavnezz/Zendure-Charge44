"""Regulation loop (`_tick`): zero-export PI + additive smart-discharge.

Each test stubs `_publish_limit` so we observe what the loop *would* publish
without touching MQTT.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def regulating(coord):
    """Coordinator with sane defaults for regulation tests."""
    coord.state.enabled = True
    coord.state.grid_power = 0.0
    coord.state.grid_power_ts = time.monotonic()
    coord.state.soc = 60
    coord.state.min_soc = 10
    coord.state.target_soc = 80
    coord.state.max_output = 800
    coord.state.kp = 0.5
    coord.state.deadzone = 5
    coord.state.temperature_guard = "ok"
    coord._publish_calls = []
    coord._publish_limit = lambda v: coord._publish_calls.append(v) or setattr(
        coord, "_last_published", v
    ) or setattr(coord, "_last_publish_ts", time.monotonic())
    return coord


# --- early exits -------------------------------------------------------------

def test_cheap_mode_active_skips_regulation(regulating):
    regulating.state.cheap_mode_active = True
    regulating._tick()
    assert regulating._publish_calls == []


def test_disabled_skips_regulation(regulating):
    regulating.state.enabled = False
    regulating._tick()
    assert regulating._publish_calls == []


def test_missing_grid_power_skips(regulating):
    regulating.state.grid_power = None
    regulating._tick()
    assert regulating._publish_calls == []


def test_missing_soc_skips(regulating):
    regulating.state.soc = None
    regulating._tick()
    assert regulating._publish_calls == []


def test_stale_grid_power_skips(regulating):
    regulating.state.grid_power_ts = time.monotonic() - 60
    regulating._tick()
    assert regulating._publish_calls == []


# --- safety blocks ----------------------------------------------------------

@pytest.mark.parametrize("guard", ["too_cold", "too_hot"])
def test_temperature_guard_forces_zero(regulating, guard):
    regulating.state.temperature_guard = guard
    regulating.state.setpoint = 250.0
    regulating._tick()
    assert regulating._publish_calls == [0]
    assert regulating.state.setpoint == 0.0


def test_soc_at_min_forces_zero(regulating):
    regulating.state.soc = 10
    regulating.state.min_soc = 10
    regulating.state.grid_power = 200.0  # would normally pull from battery
    regulating._tick()
    assert regulating._publish_calls == [0]


def test_soc_below_min_forces_zero(regulating):
    regulating.state.soc = 5
    regulating.state.min_soc = 10
    regulating.state.grid_power = 200.0
    regulating._tick()
    assert regulating._publish_calls == [0]


# --- smart-discharge: cheap-hour battery preservation ----------------------

def test_smart_discharge_in_cheap_hour_pauses_output(regulating):
    """v0.10.5: with a feed-in tariff of zero, exporting is pure loss. The
    smart-discharge switch now means 'preserve battery during cheap hours'
    so cheap grid covers the home directly."""
    regulating.state.smart_discharge_enabled = True
    regulating.state.is_cheap_now = True
    regulating.state.grid_power = 200.0  # home pulling from grid
    regulating.state.setpoint = 150.0  # was discharging; should stop
    regulating._tick()
    assert regulating._publish_calls == [0]
    assert regulating.state.setpoint == 0.0


def test_smart_discharge_in_expensive_hour_runs_pi_zero_export(regulating):
    """During expensive hours we still cover home load — but never export."""
    regulating.state.smart_discharge_enabled = True
    regulating.state.is_cheap_now = False
    regulating.state.is_expensive_now = True
    regulating.state.grid_power = 200.0
    regulating._tick()
    # Standard PI loop: 0 + 200 * 0.5 = 100 (zero-export, no max-pin)
    assert regulating._publish_calls == [100]


def test_smart_discharge_in_normal_hour_runs_pi(regulating):
    regulating.state.smart_discharge_enabled = True
    regulating.state.is_cheap_now = False
    regulating.state.is_expensive_now = False
    regulating.state.grid_power = 200.0
    regulating._tick()
    assert regulating._publish_calls == [100]


def test_smart_discharge_off_ignores_cheap_hour(regulating):
    """With the switch off, the regulation runs regardless of price."""
    regulating.state.smart_discharge_enabled = False
    regulating.state.is_cheap_now = True
    regulating.state.grid_power = 200.0
    regulating._tick()
    assert regulating._publish_calls == [100]


def test_smart_discharge_idempotent_when_already_paused(regulating):
    regulating.state.smart_discharge_enabled = True
    regulating.state.is_cheap_now = True
    regulating._last_published = 0
    regulating.state.setpoint = 0.0
    regulating._tick()
    assert regulating._publish_calls == []  # no redundant publish


# --- PI loop ----------------------------------------------------------------

def test_pi_loop_single_tick_uses_kp(regulating):
    """One tick: new_setpoint = old + error * Kp."""
    regulating.state.setpoint = 0.0
    regulating.state.grid_power = 300.0
    regulating._tick()
    # 0 + 300 * 0.5 = 150
    assert regulating.state.setpoint == 150.0


def test_pi_loop_decays_when_setpoint_overshoots(regulating):
    """If we're publishing more than the home needs, error goes negative."""
    regulating.state.setpoint = 400.0
    regulating.state.grid_power = -100.0  # exporting 100 W
    regulating._tick()
    # 400 + (-100) * 0.5 = 350
    assert regulating.state.setpoint == 350.0


def test_pi_loop_clamps_to_max_output(regulating):
    regulating.state.grid_power = 5000.0  # huge import
    regulating._tick()
    assert regulating.state.setpoint == 800.0  # clamped at max


def test_pi_loop_clamps_to_zero_floor(regulating):
    regulating.state.grid_power = -500.0  # exporting → setpoint should drop
    regulating.state.setpoint = 100.0
    regulating._tick()
    # 100 + (-500) * 0.5 = -150 → clamped to 0
    assert regulating.state.setpoint == 0.0


def test_grid_bias_offsets_target(regulating):
    """grid_bias > 0 means we tolerate some import (don't fully cover)."""
    regulating.state.grid_power = 100.0
    regulating.state.grid_bias = 100  # accept 100 W import as the target
    regulating._last_published = 0  # already at the steady-state value
    regulating._tick()
    # error = 100 - 100 = 0 → setpoint unchanged
    assert regulating.state.setpoint == 0.0
    assert regulating._publish_calls == []  # no change → no publish


# --- deadzone & rate-limit -------------------------------------------------

def test_publish_skipped_within_deadzone(regulating):
    regulating.state.grid_power = 10.0  # tiny error → 5 W setpoint change
    regulating._last_published = 0
    regulating._tick()
    # delta = 5 W which is NOT > deadzone (5) → no publish
    assert regulating._publish_calls == []


def test_publish_emitted_outside_deadzone(regulating):
    regulating.state.grid_power = 100.0
    regulating._last_published = 0
    regulating._tick()
    assert regulating._publish_calls == [50]  # 0 + 100*0.5 = 50, > deadzone
