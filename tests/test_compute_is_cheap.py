"""Tibber price evaluation: cheap-hour selection + spread profitability."""
from __future__ import annotations

import datetime


def _make_window(prices_eur):
    """Build a price window — list of dicts as `_compute_is_cheap` expects."""
    base = datetime.datetime(2026, 4, 26, 0, 0, tzinfo=datetime.timezone.utc)
    return [
        {"start": base + datetime.timedelta(hours=i), "value": p}
        for i, p in enumerate(prices_eur)
    ]


# --- empty / unknown inputs -------------------------------------------------

def test_empty_window_returns_false(coord):
    coord.state.cheap_hours = 6
    assert coord._compute_is_cheap([], None) is False
    assert coord.state.profitable_now is False


def test_no_current_returns_false(coord):
    window = _make_window([0.20] * 24)
    assert coord._compute_is_cheap(window, None) is False


# --- profitability ----------------------------------------------------------

def test_profitable_when_spread_exceeds_threshold(coord):
    """today_max - current >= max(min_spread_ct/100, break_even) → profitable."""
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 10.0
    coord.state.cheap_hours = 6
    # 24 prices: 6 cheap (0.05 EUR), 18 expensive (0.40 EUR)
    prices = [0.05] * 6 + [0.40] * 18
    window = _make_window(prices)
    current = window[0]  # 0.05 EUR
    assert coord._compute_is_cheap(window, current) is True
    assert coord.state.profitable_now is True
    assert coord.state.today_max_price == 40.0
    assert coord.state.today_min_price == 5.0


def test_not_profitable_when_spread_too_small(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 10.0
    coord.state.cheap_hours = 6
    # spread of only 5 ct — below threshold
    prices = [0.10] * 12 + [0.15] * 12
    window = _make_window(prices)
    current = window[0]
    assert coord._compute_is_cheap(window, current) is False
    assert coord.state.profitable_now is False


def test_break_even_dominates_when_min_spread_low(coord):
    """At very high prices, break-even (loss compensation) sets the floor."""
    coord.state.efficiency = 50  # awful efficiency → break_even = current * 1.0
    coord.state.min_spread_ct = 1.0  # user set tiny threshold
    coord.state.cheap_hours = 6
    # current 0.30, max 0.45 → spread 0.15. break_even = 0.30 * 1.0 = 0.30
    # required = max(0.01, 0.30) = 0.30 → spread 0.15 < 0.30 → NOT profitable
    prices = [0.30] * 6 + [0.45] * 18
    window = _make_window(prices)
    current = window[0]
    assert coord._compute_is_cheap(window, current) is False


# --- cheap-hour selection ---------------------------------------------------

def test_top_n_cheapest_are_in_set(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 5.0
    coord.state.cheap_hours = 3
    # 0..23 EUR cents → cheapest three are 0, 1, 2
    prices = [c / 100 for c in range(24)]
    window = _make_window(prices)
    assert coord._compute_is_cheap(window, window[0]) is True
    assert coord._compute_is_cheap(window, window[2]) is True
    assert coord._compute_is_cheap(window, window[3]) is False  # 4th cheapest


def test_negative_prices_are_in_cheap_set(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 5.0
    coord.state.cheap_hours = 3
    prices = [-0.05, -0.03, -0.01] + [0.30] * 21
    window = _make_window(prices)
    assert coord._compute_is_cheap(window, window[0]) is True


# --- expensive_now / next_expensive_start ----------------------------------

def test_expensive_flag_uses_symmetric_spread(coord):
    """Discharge at peak uses today_min as the reference, not today_max."""
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 10.0
    coord.state.cheap_hours = 6
    # min 0.05, current 0.40 → spread above min = 0.35 → way past required
    prices = [0.05] * 6 + [0.40] * 18
    window = _make_window(prices)
    current = window[10]  # one of the 0.40s
    coord._compute_is_cheap(window, current)
    assert coord.state.is_expensive_now is True


def test_not_expensive_when_close_to_min(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 10.0
    coord.state.cheap_hours = 6
    prices = [0.10] * 12 + [0.15] * 12
    window = _make_window(prices)
    current = window[20]  # 0.15
    coord._compute_is_cheap(window, current)
    assert coord.state.is_expensive_now is False


# --- contiguous-block mode --------------------------------------------------

def test_contiguous_block_picks_consecutive_slots(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 5.0
    coord.state.cheap_hours = 3
    coord.state.contiguous_block_mode = True
    # cheap pocket at indices 10,11,12 — splatter of low prices elsewhere
    prices = [0.30] * 24
    prices[5] = 0.04
    prices[10] = 0.05
    prices[11] = 0.05
    prices[12] = 0.05
    window = _make_window(prices)
    # Index 5 is low but isolated → not part of contiguous block
    assert coord._compute_is_cheap(window, window[5]) is False
    # 10,11,12 form the cheapest contiguous trio
    assert coord._compute_is_cheap(window, window[10]) is True
    assert coord._compute_is_cheap(window, window[11]) is True
    assert coord._compute_is_cheap(window, window[12]) is True


# --- 15-minute slots --------------------------------------------------------

def test_quarter_hourly_slots_scale_cheap_hours(coord):
    """cheap_hours stays in HOURS but is converted to slot count internally."""
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 5.0
    coord.state.cheap_hours = 1  # 1 hour = 4 quarter-hour slots
    coord.state.slot_minutes = 15
    # 96 quarter-hour slots in 24h, cheapest 4 should match 1h
    prices = [0.05] * 4 + [0.40] * 92
    base = datetime.datetime(2026, 4, 26, tzinfo=datetime.timezone.utc)
    window = [
        {"start": base + datetime.timedelta(minutes=15 * i), "value": p}
        for i, p in enumerate(prices)
    ]
    assert coord._compute_is_cheap(window, window[0]) is True
    assert coord._compute_is_cheap(window, window[3]) is True
    assert coord._compute_is_cheap(window, window[4]) is False
