"""Safety tick: fall back to fallback_discharge when Shelly goes silent.

`_tick` only runs when a Shelly RPC arrives. If the meter stops reporting,
outputLimit gets stranded at its last value while real home load drifts.
`_periodic_safety` runs on its own timer and pushes outputLimit to a
user-configurable baseline once Shelly's been silent past STALE_GRID_AFTER.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def safety(coord):
    coord.state.enabled = True
    coord.state.soc = 60
    coord.state.min_soc = 10
    coord.state.fallback_discharge = 150
    coord.state.temperature_guard = "ok"
    coord._publish_calls = []
    coord._publish_limit = lambda v: coord._publish_calls.append(v) or setattr(
        coord, "_last_published", v
    )
    return coord


# --- triggers correctly when stale -----------------------------------------

def test_publishes_fallback_when_shelly_stale(safety):
    safety.state.grid_power_ts = time.monotonic() - 30  # 30 s old
    safety._last_published = 200  # was discharging at 200 W
    safety._periodic_safety(None)
    assert safety._publish_calls == [150]


def test_no_publish_when_shelly_fresh(safety):
    safety.state.grid_power_ts = time.monotonic() - 5  # well within window
    safety._last_published = 200
    safety._periodic_safety(None)
    assert safety._publish_calls == []


def test_no_publish_when_already_at_fallback(safety):
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._last_published = 150  # already there
    safety._periodic_safety(None)
    assert safety._publish_calls == []


def test_no_publish_when_never_received_data(safety):
    """grid_power_ts == 0 means we've never seen Shelly. Don't fire fallback
    on first startup before the first message."""
    safety.state.grid_power_ts = 0.0
    safety._periodic_safety(None)
    assert safety._publish_calls == []


# --- bailouts respected -----------------------------------------------------

def test_disabled_skips_safety(safety):
    safety.state.enabled = False
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._periodic_safety(None)
    assert safety._publish_calls == []


def test_cheap_mode_active_skips_safety(safety):
    safety.state.cheap_mode_active = True
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._periodic_safety(None)
    assert safety._publish_calls == []


@pytest.mark.parametrize("guard", ["too_cold", "too_hot"])
def test_temperature_guard_blocks_safety(safety, guard):
    safety.state.temperature_guard = guard
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._periodic_safety(None)
    assert safety._publish_calls == []


def test_min_soc_blocks_safety(safety):
    safety.state.soc = 5
    safety.state.min_soc = 10
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._periodic_safety(None)
    assert safety._publish_calls == []


def test_smart_discharge_in_cheap_hour_overrides_safety(safety):
    """User explicitly opted to preserve the battery during cheap hours;
    that intent beats the safety fallback."""
    safety.state.smart_discharge_enabled = True
    safety.state.is_cheap_now = True
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._periodic_safety(None)
    assert safety._publish_calls == []


# --- range / value handling -------------------------------------------------

def test_zero_fallback_means_idle(safety):
    safety.state.fallback_discharge = 0
    safety.state.grid_power_ts = time.monotonic() - 30
    safety._last_published = 200
    safety._periodic_safety(None)
    assert safety._publish_calls == [0]
