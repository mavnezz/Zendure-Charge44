from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import Charge44Coordinator, State
from .entity import Charge44Entity


@dataclass(frozen=True, kw_only=True)
class Charge44SensorDescription(SensorEntityDescription):
    value_fn: Callable[[State], Any]
    attrs_fn: Callable[[State], dict[str, Any]] | None = None
    daily: bool = False  # resets at local midnight (sets last_reset)


SENSORS: tuple[Charge44SensorDescription, ...] = (
    Charge44SensorDescription(
        key="grid_power",
        name="Grid power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.grid_power,
    ),
    Charge44SensorDescription(
        key="soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.soc,
    ),
    Charge44SensorDescription(
        key="solar_input",
        name="Solar input",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.solar_input,
    ),
    Charge44SensorDescription(
        key="output_home_power",
        name="Output to home",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.output_home_power,
    ),
    Charge44SensorDescription(
        key="battery_charging",
        name="Battery charging",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.pack_input,
    ),
    Charge44SensorDescription(
        key="battery_discharging",
        name="Battery discharging",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.pack_output,
    ),
    Charge44SensorDescription(
        key="battery_net",
        name="Battery net flow",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            None
            if s.pack_output is None or s.pack_input is None
            else s.pack_output - s.pack_input
        ),
    ),
    Charge44SensorDescription(
        key="grid_import",
        name="Grid import",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            None if s.grid_power is None else max(0.0, s.grid_power)
        ),
    ),
    Charge44SensorDescription(
        key="grid_export",
        name="Grid export",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            None if s.grid_power is None else max(0.0, -s.grid_power)
        ),
    ),
    Charge44SensorDescription(
        key="output_limit",
        name="Output limit (device)",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.output_limit,
    ),
    Charge44SensorDescription(
        key="setpoint",
        name="Regulation setpoint",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: round(s.setpoint, 1),
    ),
    Charge44SensorDescription(
        key="regulation_error",
        name="Regulation error",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (
            None if s.grid_power is None else round(s.grid_power - s.grid_bias, 1)
        ),
    ),
    Charge44SensorDescription(
        key="pack_state",
        name="Pack state",
        value_fn=lambda s: s.pack_state,
    ),
    Charge44SensorDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.temperature,
    ),
    Charge44SensorDescription(
        key="current_price",
        name="Current price",
        native_unit_of_measurement="ct/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: (
            None if s.current_price is None else round(s.current_price * 100, 2)
        ),
        attrs_fn=lambda s: {"prices_24h": s.prices_24h} if s.prices_24h else {},
    ),
    Charge44SensorDescription(
        key="is_cheap_now",
        name="Cheap hour",
        value_fn=lambda s: "yes" if s.is_cheap_now else "no",
    ),
    Charge44SensorDescription(
        key="cheap_mode_active",
        name="Cheap-charge active",
        value_fn=lambda s: "on" if s.cheap_mode_active else "off",
        attrs_fn=lambda s: {"reason": s.charge_reason} if s.charge_reason else {},
    ),
    Charge44SensorDescription(
        key="next_cheap_start",
        name="Next cheap window",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.next_cheap_start,
    ),
    Charge44SensorDescription(
        key="is_expensive_now",
        name="Expensive hour",
        value_fn=lambda s: "yes" if s.is_expensive_now else "no",
    ),
    Charge44SensorDescription(
        key="next_expensive_start",
        name="Next expensive window",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.next_expensive_start,
    ),
    Charge44SensorDescription(
        key="solar_remaining_kwh",
        name="Solar forecast remaining",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.solar_remaining_kwh,
    ),
    Charge44SensorDescription(
        key="grid_charge_needed_kwh",
        name="Grid charge needed",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.grid_charge_needed_kwh,
    ),
    Charge44SensorDescription(
        key="battery_capacity",
        name="Battery capacity",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery_capacity,
    ),
    Charge44SensorDescription(
        key="today_max_price",
        name="Today max price",
        native_unit_of_measurement="ct/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.today_max_price,
    ),
    Charge44SensorDescription(
        key="today_min_price",
        name="Today min price",
        native_unit_of_measurement="ct/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.today_min_price,
    ),
    Charge44SensorDescription(
        key="spread_now_ct",
        name="Spread now",
        native_unit_of_measurement="ct/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.spread_now_ct,
    ),
    Charge44SensorDescription(
        key="required_spread_ct",
        name="Required spread",
        native_unit_of_measurement="ct/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.required_spread_ct,
    ),
    Charge44SensorDescription(
        key="profitable_now",
        name="Charge profitable",
        value_fn=lambda s: "yes" if s.profitable_now else "no",
    ),
    # Health / guard diagnostics
    Charge44SensorDescription(
        key="health",
        name="Health",
        value_fn=lambda s: s.health,
    ),
    Charge44SensorDescription(
        key="temperature_guard",
        name="Temperature guard",
        value_fn=lambda s: s.temperature_guard,
    ),
    Charge44SensorDescription(
        key="drift_count",
        name="Drift cycles",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.drift_count,
    ),
    # Savings (derived from the restored EUR counters below)
    Charge44SensorDescription(
        key="savings_today_eur",
        name="Savings today",
        native_unit_of_measurement="EUR",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        daily=True,
        value_fn=lambda s: round(
            s.value_discharged_today_eur - s.cost_charged_today_eur, 3
        ),
    ),
    Charge44SensorDescription(
        key="savings_total_eur",
        name="Savings total",
        native_unit_of_measurement="EUR",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda s: round(
            s.value_discharged_total_eur - s.cost_charged_total_eur, 3
        ),
    ),
)


ENERGY_SENSORS: tuple[tuple[str, str, str], ...] = (
    ("energy_charged_total", "Battery charged energy", "energy_charged_kwh"),
    ("energy_discharged_total", "Battery discharged energy", "energy_discharged_kwh"),
    ("energy_solar_total", "Solar produced energy", "energy_solar_kwh"),
    ("energy_home_total", "Output to home energy", "energy_home_kwh"),
)

# RestoreSensor-backed EUR counters (persisted across restarts).
# (key, name, state_key, daily)
COST_SENSORS: tuple[tuple[str, str, str, bool], ...] = (
    ("cost_charged_today_eur", "Cost charged today", "cost_charged_today_eur", True),
    ("value_discharged_today_eur", "Discharge value today", "value_discharged_today_eur", True),
    ("cost_charged_total_eur", "Cost charged total", "cost_charged_total_eur", False),
    ("value_discharged_total_eur", "Discharge value total", "value_discharged_total_eur", False),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Charge44Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [Charge44Sensor(coordinator, d) for d in SENSORS]
    entities.extend(
        Charge44EnergySensor(coordinator, key, name, state_key)
        for key, name, state_key in ENERGY_SENSORS
    )
    entities.extend(
        Charge44CostSensor(coordinator, key, name, state_key, daily)
        for key, name, state_key, daily in COST_SENSORS
    )
    async_add_entities(entities)


class Charge44Sensor(Charge44Entity, SensorEntity):
    entity_description: Charge44SensorDescription
    # The 96-slot price list is for dashboard cards, not for the database.
    _unrecorded_attributes = frozenset({"prices_24h"})

    def __init__(
        self, coordinator: Charge44Coordinator, description: Charge44SensorDescription
    ) -> None:
        super().__init__(coordinator, description.key, description.name)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.state)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.daily:
            return self.coordinator.today_start()
        return None


class Charge44EnergySensor(Charge44Entity, RestoreSensor):
    """Cumulative kWh counter with persistence across HA restarts.

    Feeds the HA Energy Dashboard directly (state_class=total_increasing).
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: Charge44Coordinator,
        key: str,
        name: str,
        state_key: str,
    ) -> None:
        super().__init__(coordinator, key, name)
        self._state_key = state_key

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                setattr(
                    self.coordinator.state,
                    self._state_key,
                    float(last.native_value),
                )
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self) -> Any:
        return round(getattr(self.coordinator.state, self._state_key, 0.0), 3)


class Charge44CostSensor(Charge44Entity, RestoreSensor):
    """Persisted EUR counter. Daily counters restore only a value from today,
    so a restart no longer zeroes them mid-day."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: Charge44Coordinator,
        key: str,
        name: str,
        state_key: str,
        daily: bool,
    ) -> None:
        super().__init__(coordinator, key, name)
        self._state_key = state_key
        self._daily = daily

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        try:
            value = float(last.native_value)
        except (ValueError, TypeError):
            return
        if not self._daily:
            setattr(self.coordinator.state, self._state_key, value)
            return
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.restore_daily_counter(
                self._state_key, value, last_state.last_updated
            )

    @property
    def native_value(self) -> Any:
        return round(getattr(self.coordinator.state, self._state_key, 0.0), 3)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.today_start() if self._daily else None

