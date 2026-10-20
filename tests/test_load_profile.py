"""Weekday × hour load profile."""
from __future__ import annotations

import datetime

from charge44.load_profile import DEFAULT_W, LoadProfile

MON = datetime.datetime(2026, 10, 19, 0, 0)  # a Monday


def test_default_without_data():
    assert LoadProfile().get(0, 12) == DEFAULT_W


def test_running_mean_then_four_week_average():
    p = LoadProfile()
    for w in (200, 400):
        p.add_hour(0, 18, w)
    assert p.get(0, 18) == 300  # plain mean of the first values
    for _ in range(20):
        p.add_hour(0, 18, 1000)
    assert p.get(0, 18) > 990  # converges to the new level


def test_weekdays_are_separate():
    p = LoadProfile()
    p.add_hour(0, 18, 300)  # Monday
    p.add_hour(5, 18, 900)  # Saturday
    assert p.get(0, 18) == 300
    assert p.get(5, 18) == 900


def test_fallback_to_same_hour_other_days():
    p = LoadProfile()
    p.add_hour(0, 18, 300)
    p.add_hour(1, 18, 500)
    assert p.get(3, 18) == 400


def test_live_samples_fold_in_on_hour_rollover():
    p = LoadProfile()
    t = MON.replace(hour=10)
    for i in range(12):  # 1 h of 5-min samples at 600 W
        p.sample(t + datetime.timedelta(minutes=5 * i), 600.0, 5 / 60)
    assert p.filled == 0  # hour still open
    p.sample(MON.replace(hour=11), 100.0, 5 / 60)
    assert p.get(0, 10) == 600.0


def test_short_hours_are_not_folded_in():
    p = LoadProfile()
    p.sample(MON.replace(hour=10, minute=50), 600.0, 10 / 60)  # only 10 min
    p.sample(MON.replace(hour=11), 100.0, 5 / 60)
    assert p.filled == 0


def test_table_has_seven_days_of_24_hours():
    t = LoadProfile().table()
    assert list(t) == ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    assert all(len(v) == 24 for v in t.values())
