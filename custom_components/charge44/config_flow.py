from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_FORECAST_ENTITY,
    CONF_SHELLY_ID,
    CONF_TIBBER_HOME_ID,
    CONF_TIBBER_TOKEN,
    CONF_ZENDURE_BATTERY_SNS,
    CONF_ZENDURE_SN,
    DOMAIN,
)
from .tibber_api import TibberApiClient

_LOGGER = logging.getLogger(__name__)

SCAN_SECONDS = 3.0


class Charge44ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "Charge44OptionsFlow":
        return Charge44OptionsFlow(config_entry)

    def __init__(self) -> None:
        self._zendure_sns: list[str] = []
        self._shelly_ids: list[str] = []
        self._devices: dict[str, Any] = {}
        self._tibber_token: str | None = None
        self._forecast_entity: str | None = None
        self._homes: list[dict[str, Any]] = []

    def _build_data(self, home_id: str) -> dict[str, Any]:
        data = {
            **self._devices,
            CONF_TIBBER_TOKEN: self._tibber_token,
            CONF_TIBBER_HOME_ID: home_id,
        }
        if self._forecast_entity:
            data[CONF_FORECAST_ENTITY] = self._forecast_entity
        return data

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if not self._zendure_sns and not self._shelly_ids:
            await self._scan_mqtt()

        if user_input is not None:
            hub_sn = user_input[CONF_ZENDURE_SN]
            battery_sns = [s for s in self._zendure_sns if s and s != hub_sn]
            self._devices = {
                CONF_ZENDURE_SN: hub_sn,
                CONF_SHELLY_ID: user_input[CONF_SHELLY_ID],
                CONF_ZENDURE_BATTERY_SNS: battery_sns,
            }
            return await self.async_step_tibber()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZENDURE_SN): self._device_selector(self._zendure_sns),
                vol.Required(CONF_SHELLY_ID): self._device_selector(self._shelly_ids),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_tibber(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            token = (user_input.get(CONF_TIBBER_TOKEN) or "").strip()
            forecast = user_input.get(CONF_FORECAST_ENTITY) or None
            if not token:
                # Skip Tibber; apply forecast if set and finish.
                data = {**self._devices}
                if forecast:
                    data[CONF_FORECAST_ENTITY] = forecast
                return self.async_create_entry(title="charge44", data=data)

            session = async_get_clientsession(self.hass)
            client = TibberApiClient(session, token)
            homes = await client.async_get_homes()
            if not homes:
                errors["base"] = "invalid_token"
            else:
                self._tibber_token = token
                self._forecast_entity = forecast
                self._homes = homes
                if len(homes) == 1:
                    return self.async_create_entry(
                        title="charge44",
                        data=self._build_data(homes[0]["id"]),
                    )
                return await self.async_step_tibber_home()

        schema = vol.Schema(
            {
                vol.Optional(CONF_TIBBER_TOKEN, default=""): str,
                vol.Optional(CONF_FORECAST_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }
        )
        return self.async_show_form(
            step_id="tibber", data_schema=schema, errors=errors
        )

    async def async_step_tibber_home(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="charge44",
                data=self._build_data(user_input[CONF_TIBBER_HOME_ID]),
            )

        options = [
            SelectOptionDict(
                value=h["id"],
                label=h.get("appNickname")
                or (h.get("address") or {}).get("address1")
                or h["id"],
            )
            for h in self._homes
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_TIBBER_HOME_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="tibber_home", data_schema=schema)

    async def _scan_mqtt(self) -> None:
        zendure_sns: set[str] = set()
        shelly_ids: set[str] = set()

        @callback
        def on_zendure(msg) -> None:
            parts = msg.topic.split("/")
            if len(parts) >= 3 and parts[0] == "Zendure":
                zendure_sns.add(parts[2])

        @callback
        def on_shelly(msg) -> None:
            parts = msg.topic.split("/")
            if parts and parts[0].lower().startswith("shelly"):
                shelly_ids.add(parts[0])

        try:
            unsubs = [
                await mqtt.async_subscribe(self.hass, "Zendure/+/+/+", on_zendure),
                await mqtt.async_subscribe(self.hass, "+/online", on_shelly),
                await mqtt.async_subscribe(self.hass, "+/events/rpc", on_shelly),
            ]
        except Exception as err:
            _LOGGER.warning("charge44: MQTT scan failed: %s", err)
            return

        await asyncio.sleep(SCAN_SECONDS)
        for unsub in unsubs:
            unsub()

        self._zendure_sns = sorted(zendure_sns)
        self._shelly_ids = sorted(shelly_ids)

    @staticmethod
    def _device_selector(options: list[str]):
        if not options:
            return str
        return SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=o, label=o) for o in options],
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
            )
        )


class Charge44OptionsFlow(OptionsFlow):
    """Lets the user change Tibber token, home, and forecast entity without
    removing/re-adding the integration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._new_token: str | None = None
        self._homes: list[dict[str, Any]] = []
        self._new_forecast: str | None = None

    async def async_step_init(self, user_input=None):
        data = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            token = (user_input.get(CONF_TIBBER_TOKEN) or "").strip() or None
            forecast = user_input.get(CONF_FORECAST_ENTITY) or None
            new_opts = dict(self._config_entry.options)

            if forecast is None:
                new_opts.pop(CONF_FORECAST_ENTITY, None)
            else:
                new_opts[CONF_FORECAST_ENTITY] = forecast

            if token is None:
                new_opts.pop(CONF_TIBBER_TOKEN, None)
                new_opts.pop(CONF_TIBBER_HOME_ID, None)
                return self.async_create_entry(title="", data=new_opts)

            # Validate token; if there are multiple homes, jump to selection.
            session = async_get_clientsession(self.hass)
            client = TibberApiClient(session, token)
            homes = await client.async_get_homes()
            if not homes:
                errors["base"] = "invalid_token"
            else:
                self._new_token = token
                self._new_forecast = forecast
                self._homes = homes
                if len(homes) == 1:
                    new_opts[CONF_TIBBER_TOKEN] = token
                    new_opts[CONF_TIBBER_HOME_ID] = homes[0]["id"]
                    return self.async_create_entry(title="", data=new_opts)
                return await self.async_step_pick_home()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TIBBER_TOKEN,
                    default=data.get(CONF_TIBBER_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_FORECAST_ENTITY,
                    description={"suggested_value": data.get(CONF_FORECAST_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )

    async def async_step_pick_home(self, user_input=None):
        if user_input is not None:
            new_opts = dict(self._config_entry.options)
            new_opts[CONF_TIBBER_TOKEN] = self._new_token
            new_opts[CONF_TIBBER_HOME_ID] = user_input[CONF_TIBBER_HOME_ID]
            if self._new_forecast:
                new_opts[CONF_FORECAST_ENTITY] = self._new_forecast
            return self.async_create_entry(title="", data=new_opts)

        options = [
            SelectOptionDict(
                value=h["id"],
                label=h.get("appNickname")
                or (h.get("address") or {}).get("address1")
                or h["id"],
            )
            for h in self._homes
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_TIBBER_HOME_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="pick_home", data_schema=schema)
