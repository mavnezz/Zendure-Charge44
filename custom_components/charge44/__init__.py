from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ZENDURE_BATTERY_SNS,
    CONF_ZENDURE_SN,
    DOMAIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_REFRESH_PRICES,
    SERVICE_SET_TARGET_SOC,
    SERVICE_STOP_CHARGE,
)
from .coordinator import Charge44Coordinator
from .discovery import remove_zendure_discovery

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = Charge44Coordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: Charge44Coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
        if not hass.data[DOMAIN]:
            for name in (
                SERVICE_FORCE_CHARGE,
                SERVICE_STOP_CHARGE,
                SERVICE_SET_TARGET_SOC,
                SERVICE_REFRESH_PRICES,
            ):
                hass.services.async_remove(DOMAIN, name)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    sn = entry.data.get(CONF_ZENDURE_SN)
    battery_sns = entry.data.get(CONF_ZENDURE_BATTERY_SNS, []) or []
    if not sn:
        return
    try:
        await remove_zendure_discovery(hass, sn, battery_sns)
    except Exception as err:
        _LOGGER.warning("charge44: discovery cleanup failed: %s", err)


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_CHARGE):
        return

    def _coordinators() -> list[Charge44Coordinator]:
        return list(hass.data.get(DOMAIN, {}).values())

    async def force_charge(call: ServiceCall) -> None:
        for c in _coordinators():
            await c.service_force_charge()

    async def stop_charge(call: ServiceCall) -> None:
        for c in _coordinators():
            await c.service_stop_charge()

    async def set_target_soc(call: ServiceCall) -> None:
        soc = call.data["soc"]
        for c in _coordinators():
            await c.service_set_target_soc(soc)

    async def refresh_prices(call: ServiceCall) -> None:
        for c in _coordinators():
            await c.service_refresh_prices()

    hass.services.async_register(DOMAIN, SERVICE_FORCE_CHARGE, force_charge)
    hass.services.async_register(DOMAIN, SERVICE_STOP_CHARGE, stop_charge)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TARGET_SOC,
        set_target_soc,
        schema=vol.Schema({vol.Required("soc"): vol.All(int, vol.Range(min=0, max=100))}),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_PRICES, refresh_prices)
