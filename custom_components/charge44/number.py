from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DEFAULT_FALLBACK_DISCHARGE,
    DEFAULT_MIN_SOC,
    DEFAULT_MIN_SPREAD_CT,
    DEFAULT_TARGET_SOC,
    DEFAULT_TEMP_HIGH,
    DEFAULT_TEMP_LOW,
    DOMAIN,
    TARGET_SOC_MIN,
)
from .coordinator import Charge44Coordinator
from .entity import Charge44Entity


@dataclass(frozen=True, kw_only=True)
class Charge44NumberDescription(NumberEntityDescription):
    state_key: str
    default: float


# Deliberately minimal user-facing control surface:
#   - Only what a user has to decide daily (target/min SOC) stays top-level.
#   - Temperature guards live under the "Configuration" section (set-and-forget).
# Everything else (cheap/expensive hours, grid bias, Kp, min spread, efficiency,
# charge power, max output, battery capacity) is either hard-coded to a sensible
# default or auto-detected from Zendure telemetry — see coordinator.py.
NUMBERS: tuple[Charge44NumberDescription, ...] = (
    Charge44NumberDescription(
        key="target_soc",
        name="SOC Max",
        native_min_value=TARGET_SOC_MIN,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        state_key="target_soc",
        default=DEFAULT_TARGET_SOC,
    ),
    Charge44NumberDescription(
        key="min_soc",
        name="SOC Min",
        native_min_value=0,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        state_key="min_soc",
        default=DEFAULT_MIN_SOC,
    ),
    Charge44NumberDescription(
        key="min_spread",
        name="Price Spread",
        native_min_value=0,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement="ct/kWh",
        mode=NumberMode.SLIDER,
        state_key="min_spread_ct",
        default=DEFAULT_MIN_SPREAD_CT,
    ),
    Charge44NumberDescription(
        key="fallback_discharge",
        name="Fallback Discharge",
        native_min_value=0,
        native_max_value=400,
        native_step=10,
        native_unit_of_measurement="W",
        mode=NumberMode.SLIDER,
        state_key="fallback_discharge",
        default=DEFAULT_FALLBACK_DISCHARGE,
    ),
    Charge44NumberDescription(
        key="temp_low_limit",
        name="Temperature low limit",
        native_min_value=-20,
        native_max_value=40,
        native_step=1,
        native_unit_of_measurement="°C",
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        state_key="temp_low_limit",
        default=DEFAULT_TEMP_LOW,
    ),
    Charge44NumberDescription(
        key="temp_high_limit",
        name="Temperature high limit",
        native_min_value=20,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement="°C",
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        state_key="temp_high_limit",
        default=DEFAULT_TEMP_HIGH,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Charge44Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(Charge44Number(coordinator, d) for d in NUMBERS)


class Charge44Number(Charge44Entity, NumberEntity, RestoreEntity):
    """Slider backed by coordinator state, so a change made elsewhere (e.g. the
    set_target_soc service) shows up here and is what gets restored."""

    entity_description: Charge44NumberDescription

    def __init__(
        self, coordinator: Charge44Coordinator, description: Charge44NumberDescription
    ) -> None:
        super().__init__(coordinator, description.key, description.name)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value: float = self.entity_description.default
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                value = float(last.state)
            except ValueError:
                pass
        value = max(
            self.entity_description.native_min_value,
            min(self.entity_description.native_max_value, value),
        )
        self.coordinator.set_setting(self.entity_description.state_key, value)

    @property
    def native_value(self) -> float:
        return getattr(self.coordinator.state, self.entity_description.state_key)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_setting(self.entity_description.state_key, value)
