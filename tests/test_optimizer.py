"""Optimizer: the DP picks the cheapest charge / hold / normal schedule."""
from __future__ import annotations

import datetime

import pytest

from charge44.optimizer import (
    CHARGE,
    HOLD,
    NORMAL,
    Battery,
    Slot,
    baseline_cost,
    distribute_pv,
    optimise,
    sine_share,
)

T0 = datetime.datetime(2026, 10, 20, 0, 0, tzinfo=datetime.timezone.utc)
H = datetime.timedelta(hours=1)


def _bat(**kw):
    base = dict(
        capacity_wh=3840.0,
        min_wh=384.0,  # 10 %
        max_wh=3840.0,
        charge_w=1000.0,
        output_w=800.0,
        efficiency=0.8,
    )
    base.update(kw)
    return Battery(**base)


def _slots(prices, load_w=300.0, pv=None):
    pv = pv or [0.0] * len(prices)
    return [
        Slot(T0 + i * H, 1.0, p, load_w, pv[i]) for i, p in enumerate(prices)
    ]


def _actions(steps):
    return [s.action for s in steps]


def test_charges_in_cheap_slots_for_expensive_ones():
    """Empty battery, 4 cheap night hours then 10 expensive hours."""
    steps = optimise(_slots([0.15] * 4 + [0.40] * 10), _bat(), 384.0)
    acts = _actions(steps)
    assert CHARGE in acts[:4]
    assert all(a != CHARGE for a in acts[4:])
    assert acts[4:].count(NORMAL) >= 9


def test_no_charge_when_spread_does_not_cover_losses():
    """20 → 24 ct at 80 % round trip: 24 × 0.8 = 19.2 < 20 — not worth it."""
    steps = optimise(_slots([0.20] * 4 + [0.24] * 10), _bat(), 384.0)
    assert CHARGE not in _actions(steps)


def test_flat_prices_never_charge():
    steps = optimise(_slots([0.30] * 24), _bat(), 384.0)
    assert CHARGE not in _actions(steps)


def test_holds_cheap_hours_to_save_battery_for_expensive_ones():
    """Battery holds ~2 h of load; cheap hours now, expensive later — keep it."""
    bat = _bat()
    energy = 384.0 + 600.0 / 0.894  # ~2 h of 300 W above SOC Min (after loss)
    steps = optimise(_slots([0.10, 0.10, 0.40, 0.40]), bat, energy)
    assert _actions(steps)[:2] == [HOLD, HOLD]
    assert _actions(steps)[2:] == [NORMAL, NORMAL]


def test_charges_only_what_is_needed():
    """One expensive hour of 300 W needs one cheap charge slot, not four."""
    steps = optimise(_slots([0.10] * 4 + [0.50]), _bat(), 384.0)
    assert _actions(steps)[:4].count(CHARGE) == 1


def test_pv_tomorrow_replaces_grid_charge():
    """Plenty of PV right before the expensive hours — no grid charge needed."""
    prices = [0.20] * 4 + [0.25] * 4 + [0.40] * 4
    pv = [0.0] * 4 + [1500.0] * 4 + [0.0] * 4
    steps = optimise(_slots(prices, pv=pv), _bat(), 384.0)
    assert CHARGE not in _actions(steps)[:4]


def test_negative_price_charges():
    steps = optimise(_slots([-0.05] + [0.30] * 6), _bat(), 1000.0)
    assert steps[0].action == CHARGE


def test_respects_soc_limits():
    bat = _bat(max_wh=3072.0)  # SOC Max 80 %
    steps = optimise(_slots([0.05] * 6 + [0.50] * 12), bat, 384.0)
    assert max(s.energy_wh for s in steps) <= 3072.0 + 1e-6
    assert min(s.energy_wh for s in steps) >= 384.0 - 1e-6


def test_already_full_does_not_charge():
    steps = optimise(_slots([0.05] * 2 + [0.50] * 2), _bat(), 3840.0)
    assert steps[0].action != CHARGE


def test_empty_inputs():
    assert optimise([], _bat(), 1000.0) == []


def test_plan_beats_baseline():
    slots = _slots([0.15] * 4 + [0.40] * 10)
    steps = optimise(slots, _bat(), 384.0)
    planned = sum(s.grid_wh * s.price / 1000 for s in steps)
    assert planned < baseline_cost(slots, 800.0)


def _charge_blocks(steps):
    acts = _actions(steps)
    return sum(1 for i, a in enumerate(acts) if a == CHARGE and (i == 0 or acts[i - 1] != CHARGE))


def test_near_equal_prices_charge_in_one_block():
    """Cheapest slots interleaved with ones 0.1 ct dearer: one block, not three."""
    prices = [0.100, 0.101, 0.100, 0.101, 0.100] + [0.40] * 12
    steps = optimise(_slots(prices), _bat(), 384.0)
    assert _actions(steps).count(CHARGE) >= 3
    assert _charge_blocks(steps) == 1


def test_without_switch_cost_it_fragments():
    prices = [0.100, 0.101, 0.100, 0.101, 0.100] + [0.40] * 12
    steps = optimise(_slots(prices), _bat(), 384.0, switch_eur=0.0)
    assert _charge_blocks(steps) > 1


def test_running_charge_not_cut_for_a_fraction_of_a_cent():
    """Already charging: finishing now beats stopping and restarting later at a
    price 0.1 ct lower."""
    prices = [0.101, 0.100] + [0.40] * 12
    bat = _bat(max_wh=384.0 + 900.0)  # room for about one slot
    assert optimise(_slots(prices), bat, 384.0, charging=True)[0].action == CHARGE


# --- PV distribution ---------------------------------------------------------

RISE = T0.replace(hour=7)
SET = T0.replace(hour=17)


def test_sun_bow_peaks_at_noon_and_sums_up():
    bounds = [(T0 + i * H, T0 + (i + 1) * H) for i in range(24)]
    pv = distribute_pv(bounds, [(T0, RISE, SET, 2.0)])
    assert sum(pv) == pytest.approx(2000.0)
    assert pv[6] == 0 and pv[17] == 0
    assert max(pv) == pv[11] or max(pv) == pv[12]


def test_remaining_today_only_after_now():
    now = T0.replace(hour=13)
    bounds = [(T0 + i * H, T0 + (i + 1) * H) for i in range(13, 24)]
    pv = distribute_pv(bounds, [(now, RISE, SET, 1.0)])
    assert sum(pv) == pytest.approx(1000.0)
    assert pv[0] > pv[3]  # 13:00 more than 16:00


def test_sine_share_outside_daylight_is_zero():
    assert sine_share(T0, RISE, RISE, SET) == 0.0
