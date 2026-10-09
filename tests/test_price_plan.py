"""Price planning: frozen cheap-slot plan, profitability against the window
max, next-cheap-window sensor."""
from __future__ import annotations

import datetime

import pytest

BASE = datetime.datetime(2026, 10, 8, 0, 0, tzinfo=datetime.timezone.utc)
H = datetime.timedelta(hours=1)


def _window(values):
    return [{"start": BASE + i * H, "value": v} for i, v in enumerate(values)]


def _set_prices(coord, values):
    coord.state.slot_minutes = 60
    coord.state.today_prices = [
        {"startsAt": (BASE + i * H).isoformat(), "total": v}
        for i, v in enumerate(values)
    ]


def _replan_at(coord, now):
    window = coord._prices_next_24h(now)
    coord._maybe_replan(window, coord._price_for(now, window))


@pytest.fixture
def priced(coord):
    coord.state.efficiency = 85
    coord.state.min_spread_ct = 5.0
    coord.state.cheap_hours = 3
    return coord


# --- point 5: next cheap window ------------------------------------------

def test_next_cheap_start_follows_block_mode(priced):
    """Cheapest single slot is +1 h, cheapest 3-block starts at +3 h."""
    priced.state.contiguous_block_mode = True
    window = _window([0.30, 0.10, 0.35, 0.12, 0.11, 0.13] + [0.40] * 18)
    assert priced._compute_next_cheap_start(window, BASE) == BASE + 3 * H


def test_next_cheap_start_scattered_mode(priced):
    window = _window([0.30, 0.10, 0.35, 0.12, 0.11, 0.13] + [0.40] * 18)
    assert priced._compute_next_cheap_start(window, BASE) == BASE + 1 * H


def test_next_cheap_start_skips_unprofitable_slots(priced):
    """Cheapest slots that would not pay off are not announced."""
    priced.state.min_spread_ct = 10.0
    window = _window([0.30] * 2 + [0.29] * 3 + [0.30] * 19)
    assert priced._compute_next_cheap_start(window, BASE) is None


# --- profitability: against the window max -------------------------------

def test_profitability_uses_window_max(priced):
    """Spread is measured against the priciest slot of the window (v0.10
    behaviour, restored in v0.11.1) — not an average over many hours."""
    priced.state.min_spread_ct = 20.0
    window = _window([0.20] + [0.30] * 22 + [0.80])
    assert priced._compute_is_cheap(window, window[0]) is True
    assert priced.state.spread_now_ct == 60.0


# --- point 13: frozen plan -------------------------------------------------

def test_block_not_abandoned_mid_charge(priced):
    """New prices with a cheaper block later arrive while inside the planned
    block — the running block must finish."""
    priced.state.contiguous_block_mode = True
    _set_prices(priced, [0.10] * 3 + [0.30] * 21)
    _replan_at(priced, BASE)
    block = {BASE, BASE + H, BASE + 2 * H}
    assert priced._plan.cheap_starts == block

    _set_prices(priced, [0.10] * 3 + [0.30] * 7 + [0.01] * 3 + [0.30] * 20)
    _replan_at(priced, BASE + 1.5 * H)  # inside the block
    assert priced._plan.cheap_starts == block

    _replan_at(priced, BASE + 3.5 * H)  # block done → new prices take over
    assert priced._plan.cheap_starts == {BASE + 10 * H, BASE + 11 * H, BASE + 12 * H}


def test_no_repick_without_new_prices(priced):
    """Scattered mode used to keep picking the next-cheapest slots as the
    cheap ones passed — charging far more than N hours."""
    _set_prices(priced, [0.10] * 3 + [0.20] * 3 + [0.30] * 18)
    _replan_at(priced, BASE)
    _replan_at(priced, BASE + 3.5 * H)
    assert priced._plan.cheap_starts == {BASE, BASE + H, BASE + 2 * H}


def test_block_toggle_requests_replan(priced):
    _set_prices(priced, [0.30, 0.10, 0.35, 0.12, 0.11, 0.13] + [0.40] * 18)
    _replan_at(priced, BASE)
    priced._evaluate = lambda: None
    priced.set_contiguous_block(True)
    _replan_at(priced, BASE + 0.5 * H)
    assert priced._plan.cheap_starts == {BASE + 3 * H, BASE + 4 * H, BASE + 5 * H}

