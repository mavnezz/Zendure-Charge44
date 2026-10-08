"""Health, restored daily counters, forecast horizon, change-only notify,
and the inverseMaxPower subscription."""
from __future__ import annotations

import asyncio
import datetime
import time
from types import SimpleNamespace

from charge44 import coordinator as coordinator_module
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


# --- point 14: forecast horizon -------------------------------------------

def _forecasts(coord, monkeypatch, hour, today, tomorrow):
    values = {"sensor.today": today, "sensor.tomorrow": tomorrow}
    coord._forecast_entity = "sensor.today"
    coord._forecast_tomorrow_entity = "sensor.tomorrow"
    coord.hass.states.get = lambda eid: (
        SimpleNamespace(
            state=str(values[eid]), attributes={"unit_of_measurement": "kWh"}
        )
        if eid in values
        else None
    )
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "now",
        lambda: datetime.datetime(2026, 10, 8, hour, 0, tzinfo=UTC),
    )


def test_evening_uses_tomorrow_forecast(coord, monkeypatch):
    _forecasts(coord, monkeypatch, hour=22, today=0.0, tomorrow=8.5)
    assert coord._read_forecast_kwh() == 8.5


def test_daytime_uses_today_forecast(coord, monkeypatch):
    _forecasts(coord, monkeypatch, hour=14, today=3.2, tomorrow=8.5)
    assert coord._read_forecast_kwh() == 3.2


def test_dark_morning_keeps_today_forecast(coord, monkeypatch):
    _forecasts(coord, monkeypatch, hour=8, today=0.05, tomorrow=8.5)
    assert coord._read_forecast_kwh() == 0.05


def test_no_tomorrow_entity_keeps_today(coord, monkeypatch):
    _forecasts(coord, monkeypatch, hour=22, today=0.0, tomorrow=8.5)
    coord._forecast_tomorrow_entity = None
    assert coord._read_forecast_kwh() == 0.0


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
