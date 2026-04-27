"""MQTT publish helpers + cheap-mode entry/exit choreography.

We capture every payload that would hit MQTT by replacing
`hass.async_create_task` with a recorder.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def publishing(coord):
    """Coordinator wired up to record every MQTT call."""
    coord._published = []

    def _record_task(coro):
        # mqtt.async_publish is a MagicMock returning a coroutine; we call it
        # synchronously here and inspect the recorded args afterwards.
        try:
            coro.close()
        except Exception:
            pass

    # Replace the publishers with direct recorders to bypass the MagicMock
    # async_publish chain — easier to assert against.
    def _rec_publish(topic, payload):
        coord._published.append((topic, payload))

    def _publish_input_limit(value: int) -> None:
        _rec_publish(
            f"Zendure/number/{coord.zendure_sn}/inputLimit/set", str(value)
        )

    def _publish_min_soc(value: int) -> None:
        _rec_publish(
            f"Zendure/number/{coord.zendure_sn}/minSoc/set", str(value)
        )

    def _publish_soc_set(value: int) -> None:
        _rec_publish(
            f"Zendure/number/{coord.zendure_sn}/socSet/set", str(value)
        )

    def _publish_ac_mode(value: str) -> None:
        _rec_publish(f"Zendure/select/{coord.zendure_sn}/acMode/set", value)

    def _publish_limit(value: int) -> None:
        _rec_publish(
            f"Zendure/number/{coord.zendure_sn}/outputLimit/set", str(value)
        )
        coord._last_published = value

    coord._publish_input_limit = _publish_input_limit
    coord._publish_min_soc = _publish_min_soc
    coord._publish_soc_set = _publish_soc_set
    coord._publish_ac_mode = _publish_ac_mode
    coord._publish_limit = _publish_limit
    return coord


# --- inputLimit sign --------------------------------------------------------

def test_input_limit_publishes_positive(publishing):
    """v0.10.2 confirmed positive values are correct (Zendure 800 Pro)."""
    publishing._publish_input_limit(800)
    assert publishing._published == [
        ("Zendure/number/TESTSN/inputLimit/set", "800")
    ]


def test_input_limit_zero_stops(publishing):
    publishing._publish_input_limit(0)
    assert publishing._published == [
        ("Zendure/number/TESTSN/inputLimit/set", "0")
    ]


def test_input_limit_topic_includes_sn(publishing):
    publishing.zendure_sn = "ABC123"

    def _publish_input_limit(value: int) -> None:
        publishing._published.append(
            (f"Zendure/number/{publishing.zendure_sn}/inputLimit/set", str(value))
        )

    publishing._publish_input_limit = _publish_input_limit
    publishing._publish_input_limit(500)
    assert publishing._published[0][0] == "Zendure/number/ABC123/inputLimit/set"


# --- min_soc forwarding (v0.10.3) ------------------------------------------

def test_min_soc_setter_forwards_to_zendure(publishing):
    """Plugin must publish minSoc/set when the user adjusts the slider."""
    publishing.state.min_soc = 10
    publishing.set_setting("min_soc", 25)
    assert publishing.state.min_soc == 25
    assert ("Zendure/number/TESTSN/minSoc/set", "25") in publishing._published


def test_target_soc_setter_forwards_to_zendure(publishing):
    """The HA SOC Max slider must reach the device's socSet, otherwise the
    Zendure keeps its own ceiling (default 100%) regardless of the slider."""
    publishing.state.target_soc = 80
    publishing.set_setting("target_soc", 85)
    assert publishing.state.target_soc == 85
    assert ("Zendure/number/TESTSN/socSet/set", "85") in publishing._published


def test_set_setting_does_not_publish_for_unrelated_keys(publishing):
    publishing.set_setting("efficiency", 90)
    assert publishing._published == []


# --- cheap-mode entry/exit choreography ------------------------------------

def test_enter_cheap_mode_publishes_quartet(publishing):
    publishing.state.charge_power = 1000
    publishing.state.current_price = -0.05
    publishing.state.soc = 50
    publishing.state.target_soc = 80
    publishing.hass.bus.async_fire = lambda *a, **kw: None
    publishing._enter_cheap_mode()
    topics = [t for t, _ in publishing._published]
    payloads = dict(publishing._published)
    assert "Zendure/select/TESTSN/acMode/set" in topics
    assert payloads["Zendure/select/TESTSN/acMode/set"] == "Input mode"
    assert payloads["Zendure/number/TESTSN/inputLimit/set"] == "1000"
    assert publishing.state.cheap_mode_active is True


def test_exit_cheap_mode_reverts_quartet(publishing):
    publishing.state.cheap_mode_active = True
    publishing.state.setpoint = 250.0
    publishing.hass.bus.async_fire = lambda *a, **kw: None
    publishing._exit_cheap_mode()
    payloads = dict(publishing._published)
    assert payloads["Zendure/number/TESTSN/inputLimit/set"] == "0"
    assert payloads["Zendure/select/TESTSN/acMode/set"] == "Output mode"
    assert publishing.state.cheap_mode_active is False
    assert publishing.state.setpoint == 0.0
