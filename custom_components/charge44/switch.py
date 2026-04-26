from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import Charge44Coordinator
from .entity import Charge44Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Charge44Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            Charge44RegulationSwitch(coordinator),
            Charge44CheapChargeSwitch(coordinator),
            Charge44ChargeWhenFreeSwitch(coordinator),
            Charge44ManualChargeSwitch(coordinator),
            Charge44SmartDischargeSwitch(coordinator),
            Charge44ContiguousBlockSwitch(coordinator),
        ]
    )


class Charge44RegulationSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "regulation", "0-Regulation")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_enabled(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_enabled(False)


class Charge44CheapChargeSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "cheap_charge", "Charge Cheap")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_cheap_enabled(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.cheap_charge_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_cheap_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_cheap_enabled(False)


class Charge44ChargeWhenFreeSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "charge_when_free", "Charge Free")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_charge_when_free(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.charge_when_free

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_charge_when_free(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_charge_when_free(False)


class Charge44ManualChargeSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "manual_charge", "Charge Manual")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Intentionally do not restore — manual charge is a one-shot intent
        # that auto-cancels at target SOC. Surviving a HA restart would defeat
        # that contract.

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.manual_charge

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_manual_charge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_manual_charge(False)


class Charge44SmartDischargeSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "smart_discharge", "Discharge Smart")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_smart_discharge(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.smart_discharge_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_smart_discharge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_smart_discharge(False)


class Charge44ContiguousBlockSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    _attr_entity_category = "config"

    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "contiguous_block", "Contiguous block mode")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_contiguous_block(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.contiguous_block_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_contiguous_block(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_contiguous_block(False)
