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
            Charge44OptimizerSwitch(coordinator),
            Charge44ManualChargeSwitch(coordinator),
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


class Charge44OptimizerSwitch(Charge44Entity, SwitchEntity, RestoreEntity):
    """Price optimizer on/off. Keeps the old "cheap_charge" key so the entity
    ID — and automations using it — survive the switch to the optimizer."""

    def __init__(self, coordinator: Charge44Coordinator) -> None:
        super().__init__(coordinator, "cheap_charge", "Optimizer")
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.set_optimizer_enabled(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.optimizer_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_optimizer_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_optimizer_enabled(False)


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
