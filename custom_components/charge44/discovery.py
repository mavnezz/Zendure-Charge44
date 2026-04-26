"""Publishes Home Assistant MQTT Discovery configs so the raw Zendure device
shows up as a proper device in HA — **read-only**. The charge44 coordinator
is the only thing that writes to Zendure topics; the auto-discovered entities
here are observability only."""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _main_device(sn: str) -> dict[str, Any]:
    return {
        "identifiers": [f"zendure_{sn}"],
        "name": "Zendure SolarFlow 800 Pro",
        "manufacturer": "Zendure",
        "model": "SolarFlow 800 Pro",
        "serial_number": sn,
    }


def _battery_device(sn: str, main_sn: str) -> dict[str, Any]:
    return {
        "identifiers": [f"zendure_{sn}"],
        "name": f"Zendure Battery Pack {sn[-5:]}",
        "manufacturer": "Zendure",
        "model": "AB2000X",
        "serial_number": sn,
        "via_device": f"zendure_{main_sn}",
    }


# Everything below is read-only (no command_topic). The source `kind` decides the
# base topic (Zendure/<kind>/<sn>/<prop>).
# (object_id, kind, prop, display_name, unit, device_class, state_class, diagnostic)
MAIN_SENSORS: tuple[
    tuple[str, str, str, str, str | None, str | None, str | None, bool], ...
] = (
    # Live telemetry from the `sensor` tree
    ("electric_level", "sensor", "electricLevel", "Battery SOC", "%", "battery", "measurement", False),
    ("solar_input", "sensor", "solarInputPower", "Solar input", "W", "power", "measurement", False),
    ("solar_power_1", "sensor", "solarPower1", "Solar input 1", "W", "power", "measurement", True),
    ("solar_power_2", "sensor", "solarPower2", "Solar input 2", "W", "power", "measurement", True),
    ("solar_power_3", "sensor", "solarPower3", "Solar input 3", "W", "power", "measurement", True),
    ("solar_power_4", "sensor", "solarPower4", "Solar input 4", "W", "power", "measurement", True),
    ("output_home_power", "sensor", "outputHomePower", "Output to home", "W", "power", "measurement", False),
    ("output_pack_power", "sensor", "outputPackPower", "Battery charging", "W", "power", "measurement", False),
    ("pack_input_power", "sensor", "packInputPower", "Battery discharging", "W", "power", "measurement", False),
    ("grid_input_power", "sensor", "gridInputPower", "Grid input", "W", "power", "measurement", False),
    ("grid_off_power", "sensor", "gridOffPower", "Off-grid power", "W", "power", "measurement", True),
    ("hyper_tmp", "sensor", "hyperTmp", "Temperature", "°C", "temperature", "measurement", False),
    ("pack_state", "sensor", "packState", "Pack state", None, None, None, False),
    ("soc_status", "sensor", "socStatus", "SOC status", None, None, None, True),
    ("remain_out_time", "sensor", "remainOutTime", "Remaining output time", "min", None, None, True),
    ("pack_num", "sensor", "packNum", "Pack count", None, None, None, True),
    ("bypass", "sensor", "pass", "Bypass", None, None, None, True),
    ("heat_state", "sensor", "heatState", "Heat state", None, None, None, True),
    ("reverse_state", "sensor", "reverseState", "Reverse state", None, None, None, True),
    # Mirrors of the device's own settings — displayed as read-only diagnostics.
    ("output_limit", "number", "outputLimit", "Output limit", "W", "power", "measurement", True),
    ("input_limit", "number", "inputLimit", "Input limit", "W", "power", "measurement", True),
    ("min_soc_setting", "number", "minSoc", "Min SOC setting", "%", "battery", "measurement", True),
    ("soc_set", "number", "socSet", "Target SOC setting", "%", "battery", "measurement", True),
    ("inverse_max_power", "number", "inverseMaxPower", "Inverter max power", "W", "power", "measurement", True),
    ("ac_mode", "select", "acMode", "AC mode", None, None, None, True),
    ("grid_off_mode", "select", "gridOffMode", "Grid off mode", None, None, None, True),
    ("grid_reverse", "select", "gridReverse", "Grid reverse", None, None, None, True),
    ("smart_mode", "switch", "smartMode", "Smart mode", None, None, None, True),
    ("lamp_switch", "switch", "lampSwitch", "Lamp switch", None, None, None, True),
)

# Battery-pack sensors; topic format is Zendure/sensor/<bat_sn>/<bat_sn>_<suffix>
BATTERY_SENSORS: tuple[
    tuple[str, str, str, str | None, str | None, str | None], ...
] = (
    ("soc_level", "socLevel", "Pack SOC", "%", "battery", "measurement"),
    ("state", "state", "Pack state", None, None, None),
    ("power", "power", "Pack power", "W", "power", "measurement"),
    ("max_temp", "maxTemp", "Max temperature", "°C", "temperature", "measurement"),
    ("total_vol", "totalVol", "Total voltage", "V", "voltage", "measurement"),
    ("max_vol", "maxVol", "Max cell voltage", "V", "voltage", "measurement"),
    ("min_vol", "minVol", "Min cell voltage", "V", "voltage", "measurement"),
    ("bat_cur", "batcur", "Battery current", "A", "current", "measurement"),
    ("soft_version", "softVersion", "Software version", None, None, None),
)


def _build_main(sn: str) -> list[tuple[str, str, dict[str, Any]]]:
    device = _main_device(sn)
    out: list[tuple[str, str, dict[str, Any]]] = []

    for oid, kind, prop, name, unit, dev_class, state_class, diagnostic in MAIN_SENSORS:
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": f"zendure_{sn}_{oid}",
            "object_id": f"zendure_{sn}_{oid}",
            "state_topic": f"Zendure/{kind}/{sn}/{prop}",
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dev_class:
            payload["device_class"] = dev_class
        if state_class:
            payload["state_class"] = state_class
        if diagnostic:
            payload["entity_category"] = "diagnostic"
        out.append(("sensor", oid, payload))

    return out


def _build_battery(
    sn: str, main_sn: str
) -> list[tuple[str, str, dict[str, Any]]]:
    device = _battery_device(sn, main_sn)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for oid, suffix, name, unit, dev_class, state_class in BATTERY_SENSORS:
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": f"zendure_{sn}_{oid}",
            "object_id": f"zendure_{sn}_{oid}",
            "state_topic": f"Zendure/sensor/{sn}/{sn}_{suffix}",
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dev_class:
            payload["device_class"] = dev_class
        if state_class:
            payload["state_class"] = state_class
        out.append(("sensor", oid, payload))
    return out


def _config_topic(component: str, node_id: str, object_id: str) -> str:
    return f"homeassistant/{component}/{node_id}/{object_id}/config"


async def publish_zendure_discovery(
    hass: HomeAssistant,
    main_sn: str,
    battery_sns: Iterable[str] = (),
) -> None:
    entries = _build_main(main_sn)
    for bat in battery_sns:
        entries.extend(_build_battery(bat, main_sn))
    node_id = f"zendure_{main_sn}"
    for component, oid, payload in entries:
        topic = _config_topic(component, node_id, oid)
        await mqtt.async_publish(
            hass, topic, json.dumps(payload), qos=1, retain=True
        )
    _LOGGER.info(
        "charge44: published %d Zendure discovery configs (hub=%s batteries=%s)",
        len(entries),
        main_sn,
        list(battery_sns),
    )


async def remove_zendure_discovery(
    hass: HomeAssistant,
    main_sn: str,
    battery_sns: Iterable[str] = (),
) -> None:
    entries = _build_main(main_sn)
    for bat in battery_sns:
        entries.extend(_build_battery(bat, main_sn))
    node_id = f"zendure_{main_sn}"
    for component, oid, _payload in entries:
        topic = _config_topic(component, node_id, oid)
        await mqtt.async_publish(hass, topic, "", qos=1, retain=True)
    # Also clear the old v0.5.0 component slots we no longer publish.
    legacy = [
        ("number", "output_limit"),
        ("number", "input_limit"),
        ("number", "min_soc"),
        ("number", "soc_set"),
        ("number", "inverse_max_power"),
        ("select", "ac_mode"),
        ("select", "grid_off_mode"),
        ("select", "grid_reverse"),
        ("switch", "smart_mode"),
        ("switch", "lamp_switch"),
    ]
    for component, oid in legacy:
        await mqtt.async_publish(
            hass, _config_topic(component, node_id, oid), "", qos=1, retain=True
        )
    _LOGGER.info("charge44: cleared Zendure discovery configs for %s", main_sn)
