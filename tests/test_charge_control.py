"""Charge control: services, stop_charge suppression, target-SOC hysteresis,
leaving the device in a sane acMode across unload / restart, and detecting a
grid-charge the device accepts but doesn't carry out."""
from __future__ import annotations

import asyncio
import datetime
import time

import pytest


@pytest.fixture
def ctl(coord):
    """Coordinator that records every device write as (prop, value)."""
    coord.calls = []

    def _limit(v):
        coord.calls.append(("outputLimit", v))
        coord._last_published = v

    coord._publish_limit = _limit
    coord._publish_ac_mode = lambda v: coord.calls.append(("acMode", v))
    coord._publish_input_limit = lambda v: coord.calls.append(("inputLimit", v))
    coord._publish_soc_set = lambda v: coord.calls.append(("socSet", v))
    coord._publish_min_soc = lambda v: coord.calls.append(("minSoc", v))
    coord._unsubs = []
    coord.state.soc = 40
    coord.state.target_soc = 80
    coord.state.temperature_guard = "ok"
    return coord


def _set_prices(coord, values):
    """Hourly prices starting at the current hour (so `_evaluate` sees them)."""
    start = datetime.datetime.now(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    coord.state.slot_minutes = 60
    coord.state.today_prices = [
        {"startsAt": (start + datetime.timedelta(hours=i)).isoformat(), "total": v}
        for i, v in enumerate(values)
    ]


CHEAP_NOW = [0.05] * 3 + [0.40] * 21
EXPENSIVE_NOW = [0.40] * 24


# --- point 1: force_charge ------------------------------------------------

def test_force_charge_survives_next_evaluation(ctl):
    """The 1-min evaluator used to kick a forced charge out right away."""
    asyncio.run(ctl.service_force_charge())
    assert ctl.state.manual_charge is True
    assert ctl.state.cheap_mode_active is True
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is True


def test_force_charge_ends_at_target(ctl):
    asyncio.run(ctl.service_force_charge())
    ctl.state.soc = 80
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is False
    assert ctl.state.manual_charge is False


# --- point 2: stop_charge -------------------------------------------------

def test_stop_charge_after_force_charge_stays_stopped(ctl):
    asyncio.run(ctl.service_force_charge())
    asyncio.run(ctl.service_stop_charge())
    assert ctl.state.cheap_mode_active is False
    assert ctl.state.manual_charge is False
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is False


def test_stop_charge_holds_until_cheap_window_ends(ctl):
    ctl.state.cheap_charge_enabled = True
    _set_prices(ctl, CHEAP_NOW)
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is True

    asyncio.run(ctl.service_stop_charge())
    assert ctl.state.cheap_mode_active is False
    ctl._evaluate()  # still cheap — must not restart
    assert ctl.state.cheap_mode_active is False

    _set_prices(ctl, EXPENSIVE_NOW)  # window over
    ctl._evaluate()
    assert ctl._charge_suppressed is False

    _set_prices(ctl, CHEAP_NOW)  # a later cheap window charges normally
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is True


def test_manual_charge_lifts_stop(ctl):
    ctl.state.cheap_charge_enabled = True
    _set_prices(ctl, CHEAP_NOW)
    ctl._evaluate()
    asyncio.run(ctl.service_stop_charge())
    assert ctl.state.cheap_mode_active is False
    ctl.set_manual_charge(True)
    assert ctl.state.cheap_mode_active is True


def test_stop_charge_when_idle_does_not_block_later(ctl):
    asyncio.run(ctl.service_stop_charge())
    assert ctl._charge_suppressed is False  # nothing to suppress


# --- point 3: set_target_soc ----------------------------------------------

def test_set_target_soc_service_forwards_soc_set(ctl):
    asyncio.run(ctl.service_set_target_soc(95))
    assert ctl.state.target_soc == 95
    assert ("socSet", 95) in ctl.calls


def test_set_target_soc_service_clamps_to_slider_range(ctl):
    asyncio.run(ctl.service_set_target_soc(30))
    assert ctl.state.target_soc == 51


# --- point 4: hysteresis at the target ------------------------------------

def test_no_reentry_until_hysteresis_band(ctl):
    """79→charge, 80→stop, then wait until 77 before charging again."""
    ctl.state.cheap_charge_enabled = True
    for soc, active in [
        (79, True),
        (80, False),
        (79, False),
        (78, False),
        (77, True),
        (80, False),
    ]:
        ctl.state.soc = soc
        ctl._apply_mode(is_cheap=True)
        assert ctl.state.cheap_mode_active is active, soc


def test_hysteresis_also_applies_to_free_charge(ctl):
    ctl.state.charge_when_free = True
    ctl.state.current_price = -0.01
    ctl.state.soc = 80
    ctl._apply_mode(is_cheap=False)
    ctl.state.soc = 79
    ctl._apply_mode(is_cheap=False)
    assert ctl.state.cheap_mode_active is False


def test_manual_charge_ignores_hysteresis(ctl):
    ctl._charge_hold = True
    ctl.state.soc = 79
    ctl.state.manual_charge = True
    assert ctl._want_cheap_charge(is_cheap=False) is True


def test_raising_target_releases_hold(ctl):
    ctl.state.cheap_charge_enabled = True
    ctl.state.soc = 80
    ctl._apply_mode(is_cheap=True)  # target hit → hold
    ctl.state.target_soc = 90
    ctl._apply_mode(is_cheap=True)
    assert ctl.state.cheap_mode_active is True


# --- point 7: cheap-mode entry zeroes the output --------------------------

def test_enter_cheap_mode_zeroes_output_first(ctl):
    ctl.state.charge_power = 800
    ctl._enter_cheap_mode()
    assert ctl.calls[0] == ("outputLimit", 0)
    assert ("acMode", "Input mode") in ctl.calls
    assert ("inputLimit", 800) in ctl.calls


# --- point 8: unload / restart --------------------------------------------

def test_async_stop_exits_active_charge(ctl):
    ctl.state.cheap_mode_active = True
    asyncio.run(ctl.async_stop())
    assert ctl.state.cheap_mode_active is False
    assert ("inputLimit", 0) in ctl.calls
    assert ("acMode", "Output mode") in ctl.calls


def test_async_stop_when_idle_writes_nothing(ctl):
    asyncio.run(ctl.async_stop())
    assert ctl.calls == []


def test_startup_resets_leftover_input_mode(ctl):
    """Device still in Input mode from before a restart, nothing wants to
    charge → hand it back, even with the regulation off."""
    ctl.state.enabled = False
    ctl.state.ac_mode = "Input mode"
    ctl._evaluate()
    assert ("inputLimit", 0) in ctl.calls
    assert ("acMode", "Output mode") in ctl.calls


def test_startup_keeps_input_mode_when_charging_is_wanted(ctl):
    ctl.state.ac_mode = "Input mode"
    ctl.state.manual_charge = True
    ctl._evaluate()
    assert ctl.state.cheap_mode_active is True
    assert ("acMode", "Output mode") not in ctl.calls


def test_reconcile_waits_for_soc(ctl):
    ctl.state.soc = None
    ctl.state.ac_mode = "Input mode"
    ctl._evaluate()
    assert ctl.calls == []
    assert ctl._acmode_reconciled is False


def test_reconcile_runs_only_once(ctl):
    ctl.state.ac_mode = "Output mode"
    ctl._evaluate()
    ctl.state.ac_mode = "Input mode"
    ctl._evaluate()
    assert ("acMode", "Output mode") not in ctl.calls


# --- stalled grid-charge detection ----------------------------------------

@pytest.fixture
def charging(ctl):
    """Cheap-charge active and confirmed by the device mirror."""
    ctl.state.cheap_mode_active = True
    ctl.state.ac_mode = "Input mode"
    ctl.state.soc = 10
    ctl.state.target_soc = 100
    ctl.hass.bus.async_fire.reset_mock()
    return ctl


def _stall_events(coord):
    return [
        c for c in coord.hass.bus.async_fire.call_args_list
        if c.args[0] == "charge44_charge_stalled"
    ]


def test_stall_reported_after_five_minutes_without_charge(charging):
    charging._stall_watch_since = time.monotonic() - 301
    charging._check_charge_stall()
    charging._update_health()
    assert charging.state.charge_stalled is True
    assert charging.state.health == "zendure_not_charging"
    assert len(_stall_events(charging)) == 1


def test_stall_event_fires_once_per_episode(charging):
    charging._stall_watch_since = time.monotonic() - 301
    charging._check_charge_stall()
    charging._check_charge_stall()
    assert len(_stall_events(charging)) == 1


def test_no_stall_within_grace_period(charging):
    charging._stall_watch_since = time.monotonic() - 120
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False


def test_real_charge_power_keeps_it_healthy(charging):
    charging._stall_watch_since = time.monotonic() - 600
    charging._update_zendure("sensor", "outputPackPower", "1000")
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False


def test_trickle_below_threshold_still_stalls(charging):
    """The 2026-10-09 signature: 20-75 W pulses for hours."""
    charging._stall_watch_since = time.monotonic() - 301
    charging._update_zendure("sensor", "outputPackPower", "75")
    charging._check_charge_stall()
    assert charging.state.charge_stalled is True


def test_recovery_clears_stall(charging):
    charging._stall_watch_since = time.monotonic() - 301
    charging._check_charge_stall()
    charging._update_zendure("sensor", "outputPackPower", "990")
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False


def test_no_watch_until_device_confirms_input_mode(charging):
    """A dropped acMode is the self-heal's job, not a stall."""
    charging.state.ac_mode = "Output mode"
    charging._stall_watch_since = time.monotonic() - 600
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False
    assert charging._stall_watch_since is None


def test_no_stall_near_target(charging):
    """The charge tapers in the last few % — not a stall."""
    charging.state.soc = 96
    charging._stall_watch_since = time.monotonic() - 600
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False


def test_no_watch_outside_cheap_mode(charging):
    charging.state.cheap_mode_active = False
    charging._stall_watch_since = time.monotonic() - 600
    charging._check_charge_stall()
    assert charging.state.charge_stalled is False


# --- standby protection: reserve top-up below SOC Min ---------------------

@pytest.fixture
def reserve(ctl):
    """Nothing else wants to charge: cheap/free/manual all off, price high."""
    ctl.state.min_soc = 10
    ctl.state.target_soc = 100
    ctl.state.current_price = 0.35
    ctl.hass.bus.async_fire.reset_mock()
    return ctl


def _started_reasons(coord):
    return [
        c.args[1]["reason"]
        for c in coord.hass.bus.async_fire.call_args_list
        if c.args[0] == "charge44_cheap_charge_started"
    ]


def test_reserve_tops_up_three_below_soc_min(reserve):
    reserve.state.soc = 7
    reserve._apply_mode(is_cheap=False)
    assert reserve.state.cheap_mode_active is True
    assert reserve.state.charge_reason == "reserve"
    assert _started_reasons(reserve) == ["reserve"]


def test_no_reserve_just_below_soc_min(reserve):
    reserve.state.soc = 8
    reserve._apply_mode(is_cheap=False)
    assert reserve.state.cheap_mode_active is False


def test_reserve_runs_until_three_above_soc_min(reserve):
    for soc, active in [(7, True), (10, True), (12, True), (13, False)]:
        reserve.state.soc = soc
        reserve._apply_mode(is_cheap=False)
        assert reserve.state.cheap_mode_active is active, soc
    assert reserve.state.charge_reason is None


def test_reserve_blocked_by_temperature_guard(reserve):
    reserve.state.temperature_guard = "too_cold"
    reserve.state.soc = 5
    reserve._apply_mode(is_cheap=False)
    assert reserve.state.cheap_mode_active is False


def test_stop_charge_does_not_block_reserve(reserve):
    reserve.state.soc = 6
    reserve._apply_mode(is_cheap=False)
    asyncio.run(reserve.service_stop_charge())
    assert reserve.state.cheap_mode_active is True


def test_reserve_ignores_target_hysteresis(reserve):
    reserve._charge_hold = True  # left over from an earlier full charge
    reserve.state.soc = 7
    reserve._apply_mode(is_cheap=False)
    assert reserve.state.cheap_mode_active is True


def test_soc_min_zero_disables_reserve(reserve):
    reserve.state.min_soc = 0
    reserve.state.soc = 0
    reserve._apply_mode(is_cheap=False)
    assert reserve.state.cheap_mode_active is False


def test_manual_reason_reported(reserve):
    reserve.set_manual_charge(True)
    assert reserve.state.charge_reason == "manual"
