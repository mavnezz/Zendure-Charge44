"""Health, restored daily counters, change-only notify, and the
inverseMaxPower subscription."""
from __future__ import annotations

import asyncio
import datetime
import json
import time

from charge44.const import ZENDURE_NUMBERS

UTC = datetime.timezone.utc


# --- point 9: Tibber health -----------------------------------------------

def test_health_tibber_offline_without_current_price(coord):
    coord._tibber = object()
    coord._has_current_price = False
    coord._last_price_ok_ts = time.monotonic()
    coord._update_health()
    assert coord.state.health == "tibber_offline"


def test_health_tibber_offline_when_fetch_stale(coord):
    coord._tibber = object()
    coord._has_current_price = True
    coord._last_price_ok_ts = time.monotonic() - 3700
    coord._update_health()
    assert coord.state.health == "tibber_offline"


def test_health_ok_with_fresh_prices(coord):
    coord._tibber = object()
    coord._has_current_price = True
    coord._last_price_ok_ts = time.monotonic()
    coord._update_health()
    assert coord.state.health == "ok"


def test_successful_fetch_marks_prices_fresh(coord):
    class _Tibber:
        async def async_get_prices(self):
            return {"current": None, "today": [], "tomorrow": []}

    coord._tibber = _Tibber()
    coord._evaluate = lambda: None
    asyncio.run(coord._fetch_prices())
    assert coord._last_price_ok_ts > 0


def test_price_poll_replans_only_on_new_prices(coord):
    prices = {
        "current": None,
        "today": [{"startsAt": "2026-10-30T12:00:00+01:00", "total": 0.42}],
        "tomorrow": [],
    }

    class _Tibber:
        async def async_get_prices(self):
            return json.loads(json.dumps(prices))

    coord._tibber = _Tibber()
    coord._evaluate = lambda: None
    asyncio.run(coord._fetch_prices())
    assert coord._replan_requested is True
    coord._replan_requested = False
    asyncio.run(coord._fetch_prices())
    assert coord._replan_requested is False  # same prices, nothing to re-plan
    prices["tomorrow"] = [{"startsAt": "2026-10-31T00:00:00+01:00", "total": 0.44}]
    asyncio.run(coord._fetch_prices())
    assert coord._replan_requested is True
    coord._replan_requested = False
    asyncio.run(coord.service_refresh_prices())
    assert coord._replan_requested is True  # asked for by hand: re-plan anyway


# --- point 10: daily EUR counters -----------------------------------------

def test_daily_counter_restored_from_today(coord):
    coord.restore_daily_counter(
        "cost_charged_today_eur", 0.42, datetime.datetime.now(UTC)
    )
    assert coord.state.cost_charged_today_eur == 0.42
    coord._maybe_reset_today_counters()  # same day → kept
    assert coord.state.cost_charged_today_eur == 0.42


def test_daily_counter_from_yesterday_dropped(coord):
    yesterday = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
    coord.restore_daily_counter("cost_charged_today_eur", 0.42, yesterday)
    assert coord.state.cost_charged_today_eur == 0.0


# --- point 17: notify only on change --------------------------------------

def test_unchanged_zendure_value_does_not_notify(coord):
    calls = []
    coord._notify = lambda: calls.append(1)
    coord._update_zendure("sensor", "electricLevel", "55")
    coord._update_zendure("sensor", "electricLevel", "55")
    assert len(calls) == 1
    coord._update_zendure("sensor", "electricLevel", "56")
    assert len(calls) == 2


def test_unparseable_zendure_value_does_not_notify(coord):
    calls = []
    coord._notify = lambda: calls.append(1)
    coord._update_zendure("sensor", "electricLevel", "n/a")
    assert calls == []


# --- point 19: inverseMaxPower --------------------------------------------

def test_inverse_max_power_subscribed_and_applied(coord):
    assert "inverseMaxPower" in ZENDURE_NUMBERS
    coord._update_zendure("number", "inverseMaxPower", "600")
    assert coord.state.max_output == 600
