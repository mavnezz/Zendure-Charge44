"""Standby-drain meter: SOC decline while the battery is idle."""
from __future__ import annotations

import pytest

from charge44.standby import StandbyMeter

CAP = 3840.0
H = 3600.0


def _night(meter, steps, start_soc=29, start_ts=0.0, idle=True):
    """Feed one reading per minute; `steps` = [(hour, soc), ...] step times."""
    soc = start_soc
    plan = dict((int(h * 60), s) for h, s in steps)
    last = max(plan)
    for minute in range(0, last + 30):
        soc = plan.get(minute, soc)
        meter.update(start_ts + minute * 60.0, soc, idle, CAP)


def test_measures_drain_between_first_and_last_step():
    """Last night: 29 → 23 % in 8.5 h of hold."""
    m = StandbyMeter()
    steps = [(0.9, 28), (2.25, 27), (3.6, 26), (4.9, 25), (6.0, 24), (7.4, 23)]
    _night(m, steps)
    m.update(9 * H, 23, False, CAP)  # idle ends → window closes
    # 5 % between the first (0.9 h) and last (7.4 h) step = 192 Wh / 6.5 h
    assert m.windows == 1
    assert m.value_w == pytest.approx(29.5, abs=0.5)


def test_bms_jump_restarts_the_window():
    m = StandbyMeter()
    _night(m, [(0.5, 28), (1.5, 27), (1.6, 24), (3.0, 23), (4.5, 22), (6.0, 21)])
    m.update(7 * H, 21, False, CAP)
    # the 27 → 24 jump is a correction; only 24 → 21 over 1.6–6.0 h counts
    assert m.value_w == pytest.approx(3 / 100 * CAP / 4.4, abs=0.5)


def test_steps_too_fast_are_a_correction():
    m = StandbyMeter()
    _night(m, [(0.5, 28), (0.6, 27), (2.6, 26), (4.6, 25)])  # 28 → 27 in 6 min
    m.update(5 * H, 25, False, CAP)
    assert m.value_w == pytest.approx(2 / 100 * CAP / 4.0, abs=0.5)


def test_short_or_flat_windows_are_ignored():
    m = StandbyMeter()
    _night(m, [(0.5, 28), (1.5, 27)])  # one hour, one step
    m.update(2 * H, 27, False, CAP)
    assert m.windows == 0 and m.value_w is None


def test_rising_soc_resets():
    m = StandbyMeter()
    _night(m, [(0.5, 28), (2.0, 27), (2.5, 28), (4.0, 27), (5.5, 26)])
    m.update(6 * H, 26, False, CAP)
    # restart after the rise: 27 at 4.0 h → 26 at 5.5 h is too short
    assert m.windows == 0


def test_averages_over_windows():
    m = StandbyMeter()
    m.restore(20.0, 1)
    _night(m, [(0.5, 28), (2.5, 27), (4.5, 26)])  # 2 % / 4 h ≈ 19.2 W
    m.update(5 * H, 26, False, CAP)
    assert m.windows == 2
    assert m.value_w == pytest.approx((20.0 + 19.2) / 2, abs=0.2)
    assert m.last_w == pytest.approx(19.2, abs=0.1)
