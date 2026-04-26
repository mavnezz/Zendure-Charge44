"""Stubs so the plugin can be imported and exercised outside Home Assistant.

The coordinator pulls in `homeassistant.*` and `aiohttp` at import time. We
don't want HA installed for unit tests, so we replace those modules with
lightweight stand-ins before any plugin code is imported.
"""
from __future__ import annotations

import datetime
import sys
import types
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# --- homeassistant package tree ---
for name in (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.mqtt",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.event",
    "homeassistant.util",
    "homeassistant.util.dt",
):
    if name not in sys.modules:
        _make_module(name)

sys.modules["homeassistant.config_entries"].ConfigEntry = MagicMock
sys.modules["homeassistant.core"].HomeAssistant = MagicMock
sys.modules["homeassistant.core"].ServiceCall = MagicMock
sys.modules["homeassistant.core"].callback = lambda f: f

# `homeassistant.const.Platform` and `homeassistant.helpers.config_validation`
# are only used by __init__.py for HA setup wiring; tests don't load __init__.
for extra in ("homeassistant.const", "homeassistant.helpers.config_validation"):
    if extra not in sys.modules:
        _make_module(extra)
sys.modules["homeassistant.const"].Platform = MagicMock()
sys.modules["homeassistant.helpers.dispatcher"].async_dispatcher_send = MagicMock()
sys.modules["homeassistant.helpers.event"].async_track_time_interval = MagicMock(
    return_value=lambda: None
)
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = (
    MagicMock()
)
sys.modules["homeassistant.components.mqtt"].async_publish = MagicMock()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_datetime(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


sys.modules["homeassistant.util.dt"].now = _now
sys.modules["homeassistant.util.dt"].parse_datetime = _parse_datetime
sys.modules["homeassistant.util.dt"].utcnow = _now

# --- aiohttp (only TibberApiClient touches it; we don't exercise that path) ---
if "aiohttp" not in sys.modules:
    aiohttp_stub = _make_module("aiohttp")
    aiohttp_stub.ClientSession = MagicMock
    aiohttp_stub.ClientError = Exception

# --- voluptuous (only used by __init__.py for service schemas, not by tests) ---
if "voluptuous" not in sys.modules:
    vol_stub = _make_module("voluptuous")
    vol_stub.Schema = MagicMock
    vol_stub.Required = MagicMock
    vol_stub.Optional = MagicMock

# --- expose plugin path on PYTHONPATH ---
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components"))


# --- shared fixtures ---
import pytest


@pytest.fixture
def coord():
    """Build a Charge44Coordinator with __new__ — no HA setup, fresh State."""
    from charge44.coordinator import Charge44Coordinator, State

    obj = Charge44Coordinator.__new__(Charge44Coordinator)
    obj.state = State()
    obj.hass = MagicMock()
    obj.entry = MagicMock()
    obj.zendure_sn = "TESTSN"
    obj._forecast_entity = None
    obj._tibber = None
    obj._last_published = None
    obj._last_publish_ts = 0.0
    obj._publish_calls = []
    return obj
