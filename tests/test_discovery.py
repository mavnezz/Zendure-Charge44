"""MQTT discovery publish/clear/remove.

Reload-safety regression (2026-09-17): on every setup the coordinator used to
remove *all* discovery configs and republish them. The empty-payload removal
deletes the entities in HA, which then stay `unavailable` until a restart.
Setup must only clear the obsolete legacy slots and (idempotently) republish the
current configs — never remove the current entities.
"""
from __future__ import annotations

import asyncio

from charge44 import discovery


def _run(coro):
    """Run one discovery coroutine, recording every MQTT publish."""
    calls: list[tuple[str, str]] = []

    async def _rec(hass, topic, payload, qos=0, retain=False):
        calls.append((topic, payload))

    orig = discovery.mqtt.async_publish
    discovery.mqtt.async_publish = _rec
    try:
        asyncio.run(coro())
    finally:
        discovery.mqtt.async_publish = orig
    return calls


SN = "TESTSN"


def _legacy_topics() -> set[str]:
    node = f"zendure_{SN}"
    return {
        discovery._config_topic(comp, node, oid)
        for comp, oid in discovery._LEGACY_SLOTS
    }


def test_clear_legacy_only_touches_legacy_slots():
    calls = _run(lambda: discovery.clear_legacy_discovery(None, SN))
    topics = {t for t, _ in calls}
    # every publish is an empty payload to a legacy slot, nothing else
    assert topics == _legacy_topics()
    assert all(payload == "" for _, payload in calls)


def test_publish_emits_current_configs_and_no_deletions():
    calls = _run(lambda: discovery.publish_zendure_discovery(None, SN, ["BAT01"]))
    # never publishes an empty payload (an empty payload would delete an entity)
    assert all(payload != "" for _, payload in calls)
    # the temperature sensor carries the Kelvin→°C template
    hyper = discovery._config_topic("sensor", f"zendure_{SN}", "hyper_tmp")
    payloads = dict(calls)
    assert hyper in payloads
    assert "273.15" in payloads[hyper]
    # none of the current-entity topics overlap the legacy slots
    assert _legacy_topics().isdisjoint(set(payloads))


def test_remove_clears_both_current_and_legacy():
    calls = _run(
        lambda: discovery.remove_zendure_discovery(None, SN, ["BAT01"])
    )
    topics = {t for t, _ in calls}
    # full removal: every legacy slot is cleared ...
    assert _legacy_topics() <= topics
    # ... and the current hub sensor too, all with empty payloads
    assert discovery._config_topic("sensor", f"zendure_{SN}", "hyper_tmp") in topics
    assert all(payload == "" for _, payload in calls)
