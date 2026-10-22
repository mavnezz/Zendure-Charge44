"""Coordinator side of the optimizer: planning inputs, executing the current
slot, re-plan triggers, the battery ledger and the measured round trip."""
from __future__ import annotations

import datetime
import time

import pytest

from charge44.optimizer import CHARGE, HOLD, NORMAL, Step

UTC = datetime.timezone.utc


def _hour():
    return datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def _set_prices(coord, values):
    start = _hour()
    coord.state.slot_minutes = 60
    coord.state.today_prices = [
        {"startsAt": (start + datetime.timedelta(hours=i)).isoformat(), "total": v}
        for i, v in enumerate(values)
    ]


@pytest.fixture
def opt(coord):
    coord.calls = []

    def _limit(v):
        coord.calls.append(("outputLimit", v))
        coord._last_published = v

    coord._publish_limit = _limit
    coord._publish_ac_mode = lambda v: coord.calls.append(("acMode", v))
    coord._publish_input_limit = lambda v: coord.calls.append(("inputLimit", v))
    coord._publish_soc_set = lambda v: None
    coord._publish_min_soc = lambda v: None
    coord.state.optimizer_enabled = True
    coord.state.soc = 40
    coord.state.min_soc = 10
    coord.state.target_soc = 100
    coord.state.battery_capacity = 3.84
    coord.state.temperature_guard = "ok"
    return coord


# --- executing the plan -------------------------------------------------------

def test_cheap_now_expensive_later_charges(opt):
    _set_prices(opt, [0.10, 0.15, 0.15] + [0.40] * 20)
    opt.plan_now()
    opt._evaluate()
    assert opt.state.plan_action == CHARGE
    assert opt.state.cheap_mode_active is True
    assert opt.state.charge_reason == "optimizer"
    assert opt.state.next_charge_start is not None
    assert opt.state.plan and opt.state.plan[0]["action"] == CHARGE
    assert opt.state.plan_cost_eur < opt.state.plan_baseline_eur


def test_full_battery_holds_through_cheap_hours(opt):
    """At SOC Max nothing can be charged, and the battery can't cover all 20
    expensive hours — keep the energy for 40 ct instead of spending it at 20 ct."""
    opt.state.soc = 100
    _set_prices(opt, [0.20] * 3 + [0.40] * 20)
    opt.plan_now()
    opt._evaluate()
    assert opt.state.plan_action == HOLD
    assert opt.state.cheap_mode_active is False


def test_flat_prices_run_normal(opt):
    """Without standby drain nothing beats just covering the house. (With it,
    holding a buffer instead of buying the drain back later can be cheaper.)"""
    opt._standby_fit = 0.0
    _set_prices(opt, [0.30] * 24)
    opt.plan_now()
    opt._evaluate()
    assert opt.state.plan_action == NORMAL
    assert opt.state.next_charge_start is None


def test_optimizer_off_means_normal_and_no_plan(opt):
    _set_prices(opt, [0.10] + [0.40] * 20)
    opt.plan_now()
    opt._evaluate()
    assert opt.state.cheap_mode_active is True
    opt.set_optimizer_enabled(False)
    assert opt.state.plan_action == NORMAL
    assert opt.state.plan == []
    assert opt.state.cheap_mode_active is False


def test_current_action_follows_slot_times(opt):
    h = _hour()
    opt._plan_steps = [
        Step(h - datetime.timedelta(hours=1), CHARGE, 0, 0.1, 0),
        Step(h, HOLD, 0, 0.2, 0),
        Step(h + datetime.timedelta(hours=1), CHARGE, 0, 0.1, 0),
    ]
    assert opt._current_action(h + datetime.timedelta(minutes=30)) == HOLD


# --- grid-charge decision -----------------------------------------------------

@pytest.mark.parametrize(
    "setup, expected",
    [
        ({"plan_action": CHARGE}, True),
        ({"plan_action": HOLD}, False),
        ({"plan_action": CHARGE, "optimizer_enabled": False}, False),
        ({"plan_action": CHARGE, "soc": 100}, False),
        ({"plan_action": CHARGE, "temperature_guard": "too_cold"}, False),
        ({"plan_action": NORMAL, "manual_charge": True}, True),
        ({"plan_action": CHARGE, "soc": None}, False),
    ],
)
def test_want_grid_charge_matrix(opt, setup, expected):
    for key, value in setup.items():
        setattr(opt.state, key, value)
    assert opt._want_grid_charge() is expected


# --- re-plan triggers ---------------------------------------------------------

def test_plan_due_triggers(opt):
    _set_prices(opt, [0.30] * 6)
    now = datetime.datetime.now(UTC)
    window = opt._price_slots(now)
    current = opt._price_for(now, window)
    assert opt._plan_due(now, window, current)  # no plan yet
    opt.plan_now()
    assert not opt._plan_due(now, window, current)
    opt.state.soc += 3
    assert opt._plan_due(now, window, current)  # SOC drifted from the plan
    opt.plan_now()
    opt._plan_slot = current["start"] - datetime.timedelta(hours=1)
    assert opt._plan_due(now, window, current)  # a new slot started
    opt.plan_now()
    opt._replan_requested = True
    assert opt._plan_due(now, window, current)  # settings / prices changed
    opt.state.optimizer_enabled = False
    assert not opt._plan_due(now, window, current)


def test_new_day_ahead_prices_trigger_replan(opt):
    _set_prices(opt, [0.30] * 6)
    opt.plan_now()
    now = datetime.datetime.now(UTC)
    _set_prices(opt, [0.30] * 30)  # horizon extended
    window = opt._price_slots(now)
    assert opt._plan_due(now, window, opt._price_for(now, window))


# --- planning inputs ----------------------------------------------------------

def test_plan_inputs(opt):
    _set_prices(opt, [0.30] * 4)
    now = _hour() + datetime.timedelta(minutes=45)
    opt._load.add_hour(now.weekday(), now.hour, 400.0)
    slots, bat, energy, charging = opt._build_plan_inputs(now, opt._price_slots(now))
    assert slots[0].hours == pytest.approx(0.25)  # 15 min left of this hour
    assert slots[0].load_wh == pytest.approx(100.0)
    assert bat.min_wh == pytest.approx(384.0)
    assert bat.max_wh == pytest.approx(3840.0)
    assert energy == pytest.approx(1536.0)
    assert charging is False
    assert bat.standby_w == 30.0  # default until the meter has two windows
    assert bat.floor_wh == pytest.approx(3840.0 * 0.08)  # SOC Min 10 % − 2 %


def test_pv_days_from_forecast_and_sun(opt):
    now = datetime.datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    rise = now.replace(hour=7)
    set_ = now.replace(hour=17)
    opt._forecast_entity = "sensor.today"
    opt._forecast_tomorrow_entity = "sensor.tomorrow"
    opt._read_kwh = lambda eid: {"sensor.today": 1.2, "sensor.tomorrow": 2.0}[eid]
    opt._sun_window = lambda day: (
        rise.replace(day=day.day),
        set_.replace(day=day.day),
    )
    days = opt._pv_days(now)
    assert days[0] == (now, rise, set_, 1.2)
    assert days[1][3] == 2.0 and days[1][1].day == 21


# --- ledger -------------------------------------------------------------------

def _tick_energy(coord, seconds=1800):
    coord._last_energy_ts = time.monotonic() - seconds
    coord._integrate_energy()


def test_ledger_grid_charge_costs_pv_is_free(opt):
    opt.state.current_price = 0.20
    opt.state.pack_input = 1000.0
    opt.state.grid_input_power = 600.0  # 400 W of the charge is PV
    _tick_energy(opt)  # 30 min: 0.3 kWh from grid, 0.2 kWh PV
    assert opt.state.ledger_eur == pytest.approx(0.06, rel=1e-3)
    assert opt.state.cost_charged_total_eur == pytest.approx(0.06, rel=1e-3)
    assert opt.state.energy_charged_kwh == pytest.approx(0.5, rel=1e-3)


def test_ledger_discharge_at_average_cost(opt):
    opt.state.soc = 50  # 1.536 kWh usable above SOC Min 10 %
    opt.state.ledger_eur = 0.384  # 25 ct/kWh
    opt.state.current_price = 0.35
    opt.state.pack_output = 960.0
    _tick_energy(opt)  # 30 min: takes 0.48 kWh at 25 ct
    assert opt.state.ledger_eur == pytest.approx(0.264, rel=1e-3)
    assert opt.state.value_discharged_total_eur == pytest.approx(0.168, rel=1e-3)


def test_cost_basis_on_usable_energy_written_off_at_soc_min(opt):
    """Only the energy above SOC Min counts; reaching SOC Min writes the rest
    off — the battery no longer gets to 0 %, so costs piled up before."""
    opt.state.soc = 50
    opt.state.ledger_eur = 0.384
    opt._update_cost_basis()
    assert opt.state.cost_basis_ct == pytest.approx(25.0)
    opt.state.soc = 10  # SOC Min
    opt._update_cost_basis()
    assert opt.state.ledger_eur == 0.0
    assert opt.state.cost_basis_ct is None


def test_ledger_falls_back_without_grid_input_reading(opt):
    opt.state.current_price = 0.20
    opt.state.pack_input = 1000.0
    opt.state.grid_input_power = None
    _tick_energy(opt)
    assert opt.state.ledger_eur == 0.0  # not grid-charging → PV
    opt.state.cheap_mode_active = True
    _tick_energy(opt)
    assert opt.state.ledger_eur == pytest.approx(0.1, rel=1e-3)


# --- measured round trip ------------------------------------------------------

IDS = {
    "energy_charged_total": "sensor.charged",
    "energy_discharged_total": "sensor.discharged",
    "soc": "sensor.soc",
}


def _stats(hours, charged, discharged, soc_start, soc_end):
    return {
        "sensor.charged": [{"change": charged / hours}] * hours,
        "sensor.discharged": [{"change": discharged / hours}] * hours,
        "sensor.soc": [{"mean": soc_start}] + [{"mean": 50}] * (hours - 2) + [{"mean": soc_end}],
    }


def test_efficiency_default_until_measured(opt):
    assert opt._efficiency() == pytest.approx(0.85)


def test_efficiency_measured_with_standby_taken_out(opt):
    """28 days: 85 kWh in, 60 out, SOC unchanged; 10 W standby → 6.72 kWh
    drawn by the hub → (60 + 6.72) / 85 = 78.5 %."""
    opt._standby.restore(10.0, 3)
    opt.apply_efficiency_statistics(_stats(672, 85.0, 60.0, 30, 30), IDS)
    assert opt._efficiency() == pytest.approx(0.785, abs=0.001)
    assert opt._replan_requested is True


def test_efficiency_capped_plan_keeps_metered_standby(opt):
    """Meter says 27 W, but only 15 kWh went missing in 672 h: the round trip
    caps at 95 %. The balance's standby is shown, but the plan keeps the
    metered SOC drop — that is what reaches the floor."""
    opt._standby.restore(27.0, 3)
    opt.apply_efficiency_statistics(_stats(672, 85.0, 70.0, 30, 30), IDS)
    assert opt._efficiency() == 0.95
    assert opt._standby_w() == 27.0
    assert opt.state.standby_balance_w == pytest.approx((15.0 - 0.05 * 85.0) * 1000 / 672, abs=0.1)


def test_standby_default_until_two_windows(opt):
    assert opt._standby_w() == 30.0
    opt._standby.restore(27.0, 1)
    assert opt._standby_w() == 30.0
    opt._standby.restore(27.0, 2)
    assert opt._standby_w() == 27.0


# --- load learning ------------------------------------------------------------

def test_house_load_learned_without_device_grid_charge(opt):
    seen = []
    opt._load.sample = lambda when, watts, dt_h: seen.append(watts)
    opt.state.grid_power = 1100.0  # house 300 + charging 800
    opt.state.output_home_power = 0.0
    opt.state.grid_input_power = 800.0
    _tick_energy(opt, seconds=5)
    assert seen == [pytest.approx(300.0)]


# --- standby meter wiring -------------------------------------------------------

def test_standby_meter_only_fed_when_idle(opt):
    seen = []
    opt._standby.update = lambda ts, soc, idle, cap: seen.append(idle)
    opt.state.pack_input = opt.state.pack_output = 0.0
    opt.state.solar_input = 0.0
    opt.state.grid_input_power = 0.0
    opt._track_standby()
    opt.state.pack_output = 120.0  # discharging → not idle
    opt._track_standby()
    opt.state.pack_output = 0.0
    opt.state.cheap_mode_active = True  # grid-charging → not idle
    opt._track_standby()
    opt.state.cheap_mode_active = False
    opt.state.pack_input = None  # unknown → not idle
    opt._track_standby()
    assert seen == [True, False, False, False]


def test_standby_restore(opt):
    opt.restore_standby(23.4, 3)
    assert opt.state.standby_w == 23.4
    assert opt.state.standby_windows == 3
