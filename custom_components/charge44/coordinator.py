from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    AC_MODE_INPUT,
    AC_MODE_OUTPUT,
    CONF_FORECAST_ENTITY,
    CONF_SHELLY_ID,
    CONF_TIBBER_HOME_ID,
    CONF_TIBBER_TOKEN,
    CONF_ZENDURE_BATTERY_SNS,
    CONF_ZENDURE_SN,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGE_POWER,
    DEFAULT_CHEAP_HOURS,
    DEFAULT_DEADZONE,
    DEFAULT_EFFICIENCY,
    DEFAULT_EXPENSIVE_HOURS,
    DEFAULT_FALLBACK_DISCHARGE,
    DEFAULT_GRID_BIAS,
    DEFAULT_KP,
    DEFAULT_MAX_OUTPUT,
    DEFAULT_MIN_SOC,
    DEFAULT_MIN_SPREAD_CT,
    DEFAULT_TARGET_SOC,
    DEFAULT_TEMP_HIGH,
    DEFAULT_TEMP_LOW,
    DRIFT_TOLERANCE_W,
    DRIFT_WARN_THRESHOLD,
    EVENT_CHEAP_CHARGE_ENDED,
    EVENT_CHEAP_CHARGE_STARTED,
    EVENT_DRIFT_DETECTED,
    EVENT_TEMPERATURE_GUARD,
    MIN_PUBLISH_INTERVAL,
    SAFETY_TICK_INTERVAL,
    SIGNAL_UPDATE,
    STALE_GRID_AFTER,
    TIBBER_POLL_INTERVAL,
    TOPIC_SHELLY_RPC,
    TOPIC_ZENDURE_NUMBER,
    TOPIC_ZENDURE_SENSOR,
    TOPIC_ZENDURE_WRITE,
    ZENDURE_NUMBERS,
    ZENDURE_SENSORS,
)
from .discovery import publish_zendure_discovery, remove_zendure_discovery
from .tibber_api import TibberApiClient

_LOGGER = logging.getLogger(__name__)

CHEAP_EVAL_INTERVAL = timedelta(minutes=1)
TIBBER_POLL = timedelta(seconds=TIBBER_POLL_INTERVAL)


@dataclass
class State:
    # From Shelly
    grid_power: float | None = None
    grid_power_ts: float = 0.0

    # From Zendure
    soc: int | None = None
    output_home_power: float | None = None
    output_limit: int | None = None
    solar_input: float | None = None
    pack_input: float | None = None
    pack_output: float | None = None
    pack_state: str | None = None
    temperature: float | None = None

    # Regulation state
    setpoint: float = 0.0
    enabled: bool = False
    grid_bias: float = DEFAULT_GRID_BIAS
    max_output: int = DEFAULT_MAX_OUTPUT
    min_soc: int = DEFAULT_MIN_SOC
    kp: float = DEFAULT_KP
    deadzone: int = DEFAULT_DEADZONE
    fallback_discharge: int = DEFAULT_FALLBACK_DISCHARGE

    # Cheap-charge state
    cheap_charge_enabled: bool = False
    charge_when_free: bool = False
    manual_charge: bool = False
    cheap_mode_active: bool = False
    is_cheap_now: bool = False
    current_price: float | None = None
    next_cheap_start: datetime | None = None
    today_prices: list[dict[str, Any]] = field(default_factory=list)
    tomorrow_prices: list[dict[str, Any]] = field(default_factory=list)
    slot_minutes: int = 60  # detected from price spacing; 15 for QUARTER_HOURLY

    # Cheap-charge settings
    cheap_hours: int = DEFAULT_CHEAP_HOURS
    target_soc: int = DEFAULT_TARGET_SOC
    charge_power: int = DEFAULT_CHARGE_POWER
    min_spread_ct: float = DEFAULT_MIN_SPREAD_CT
    efficiency: int = DEFAULT_EFFICIENCY
    battery_capacity: float = DEFAULT_BATTERY_CAPACITY  # kWh

    # Solar-forecast derived
    solar_remaining_kwh: float | None = None
    grid_charge_needed_kwh: float | None = None

    # Price-derived diagnostics (ct/kWh)
    today_max_price: float | None = None
    today_min_price: float | None = None
    spread_now_ct: float | None = None           # today_max - current
    required_spread_ct: float | None = None      # max(min_spread_ct, break-even)
    profitable_now: bool = False

    # Cumulative energy counters (kWh) — for HA Energy Dashboard
    energy_charged_kwh: float = 0.0
    energy_discharged_kwh: float = 0.0
    energy_solar_kwh: float = 0.0
    energy_home_kwh: float = 0.0

    # Temperature guard
    temp_low_limit: float = DEFAULT_TEMP_LOW
    temp_high_limit: float = DEFAULT_TEMP_HIGH
    temperature_guard: str = "unknown"  # ok | too_cold | too_hot | unknown

    # Anti-drift watchdog
    drift_count: int = 0
    drift_active: bool = False

    # Health aggregate
    health: str = "ok"

    # Cost / savings tracking (EUR)
    cost_charged_today_eur: float = 0.0
    value_discharged_today_eur: float = 0.0
    cost_charged_total_eur: float = 0.0
    value_discharged_total_eur: float = 0.0
    tracking_day: str = ""

    # Price timeline (attributes on sensor.current_price)
    prices_24h: list[dict[str, Any]] = field(default_factory=list)

    # Block-mode
    contiguous_block_mode: bool = False

    # Smart-discharge
    smart_discharge_enabled: bool = False
    is_expensive_now: bool = False
    expensive_hours: int = DEFAULT_EXPENSIVE_HOURS
    next_expensive_start: datetime | None = None


class Charge44Coordinator:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.zendure_sn: str = entry.data[CONF_ZENDURE_SN]
        self.shelly_id: str = entry.data[CONF_SHELLY_ID]
        self._battery_sns: list[str] = list(
            entry.data.get(CONF_ZENDURE_BATTERY_SNS, []) or []
        )
        merged = {**entry.data, **entry.options}
        self._tibber_token: str | None = merged.get(CONF_TIBBER_TOKEN)
        self._tibber_home_id: str | None = merged.get(CONF_TIBBER_HOME_ID)
        self._forecast_entity: str | None = merged.get(CONF_FORECAST_ENTITY) or None
        self.state = State()
        self._unsubs: list[Callable[[], None]] = []
        self._last_published: int | None = None
        self._last_publish_ts: float = 0.0
        self._last_energy_ts: float = 0.0
        self._tibber: TibberApiClient | None = None
        # Seed battery capacity from known battery SNs before the first MQTT msg.
        self._update_battery_capacity()

    async def async_start(self) -> None:
        # MQTT subscriptions
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass,
                TOPIC_SHELLY_RPC.format(shelly_id=self.shelly_id),
                self._handle_shelly,
            )
        )
        for prop in ZENDURE_SENSORS:
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    TOPIC_ZENDURE_SENSOR.format(sn=self.zendure_sn, prop=prop),
                    self._make_zendure_handler("sensor", prop),
                )
            )
        for prop in ZENDURE_NUMBERS:
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    TOPIC_ZENDURE_NUMBER.format(sn=self.zendure_sn, prop=prop),
                    self._make_zendure_handler("number", prop),
                )
            )

        # Tibber
        if self._tibber_token and self._tibber_home_id:
            session = async_get_clientsession(self.hass)
            self._tibber = TibberApiClient(
                session, self._tibber_token, self._tibber_home_id
            )
            await self._fetch_prices()
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._periodic_fetch, TIBBER_POLL
                )
            )

        # Periodic cheap-charge evaluator
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._periodic_evaluate, CHEAP_EVAL_INTERVAL
            )
        )

        # Safety tick — Shelly silence detector. Even when no Shelly RPC
        # arrives we need to fall back to the user-configured base load,
        # otherwise outputLimit stays frozen at its last value forever.
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._periodic_safety,
                timedelta(seconds=SAFETY_TICK_INTERVAL),
            )
        )

        # Auto-Discovery so the Zendure shows up as a native MQTT device in HA
        # (read-only — charge44 is the only writer).
        try:
            # Clear any command-topic configs left over from older versions.
            await remove_zendure_discovery(
                self.hass, self.zendure_sn, self._battery_sns
            )
            await publish_zendure_discovery(
                self.hass, self.zendure_sn, self._battery_sns
            )
        except Exception as err:
            _LOGGER.warning("charge44: Zendure discovery publish failed: %s", err)

        _LOGGER.info(
            "charge44 started: zendure=%s batteries=%s shelly=%s tibber=%s",
            self.zendure_sn,
            self._battery_sns,
            self.shelly_id,
            bool(self._tibber),
        )

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # -------- MQTT handlers --------

    @callback
    def _handle_shelly(self, msg) -> None:
        try:
            raw = msg.payload.decode() if isinstance(msg.payload, bytes) else msg.payload
            data = json.loads(raw)
            em = data.get("params", {}).get("em:0", {})
            if "total_act_power" in em:
                self.state.grid_power = float(em["total_act_power"])
                self.state.grid_power_ts = time.monotonic()
                self._integrate_energy()
                self._tick()
                self._notify()
        except Exception as err:
            _LOGGER.debug("Shelly parse error: %s", err)

    def _integrate_energy(self) -> None:
        """Integrate pack charge/discharge power over the interval since the last
        Shelly tick (~5 s) into kWh counters. `total_increasing` sensors expose
        these to the HA Energy Dashboard. Also integrates cost and value in EUR
        using the current Tibber price."""
        now = time.monotonic()
        last = self._last_energy_ts
        self._last_energy_ts = now
        if last == 0.0:
            return
        dt_hours = (now - last) / 3600.0
        if dt_hours <= 0 or dt_hours > 1.0:
            return
        self._maybe_reset_today_counters()
        price = self.state.current_price  # EUR/kWh
        if self.state.pack_input is not None and self.state.pack_input > 0:
            kwh = self.state.pack_input * dt_hours / 1000.0
            self.state.energy_charged_kwh += kwh
            if price is not None and self.state.cheap_mode_active:
                # Only grid-charging during cheap mode costs money.
                cost = kwh * price
                self.state.cost_charged_today_eur += cost
                self.state.cost_charged_total_eur += cost
        if self.state.pack_output is not None and self.state.pack_output > 0:
            kwh = self.state.pack_output * dt_hours / 1000.0
            self.state.energy_discharged_kwh += kwh
            if price is not None:
                value = kwh * price
                self.state.value_discharged_today_eur += value
                self.state.value_discharged_total_eur += value
        if self.state.solar_input is not None and self.state.solar_input > 0:
            self.state.energy_solar_kwh += self.state.solar_input * dt_hours / 1000.0
        if self.state.output_home_power is not None and self.state.output_home_power > 0:
            self.state.energy_home_kwh += (
                self.state.output_home_power * dt_hours / 1000.0
            )

    def _maybe_reset_today_counters(self) -> None:
        today = dt_util.now().strftime("%Y-%m-%d")
        if self.state.tracking_day == "":
            self.state.tracking_day = today
            return
        if today != self.state.tracking_day:
            self.state.tracking_day = today
            self.state.cost_charged_today_eur = 0.0
            self.state.value_discharged_today_eur = 0.0

    def _update_temperature_guard(self) -> None:
        t = self.state.temperature
        if t is None:
            new = "unknown"
        elif t < self.state.temp_low_limit:
            new = "too_cold"
        elif t > self.state.temp_high_limit:
            new = "too_hot"
        else:
            new = "ok"
        if new != self.state.temperature_guard:
            prev = self.state.temperature_guard
            self.state.temperature_guard = new
            _LOGGER.info(
                "charge44: temperature guard %s -> %s (temp=%s)", prev, new, t
            )
            self.hass.bus.async_fire(
                EVENT_TEMPERATURE_GUARD,
                {"previous": prev, "current": new, "temperature": t},
            )

    def _check_drift(self) -> None:
        """Called after the device publishes outputLimit: compare to our last
        commanded value and track mismatch streaks."""
        if self._last_published is None or self.state.output_limit is None:
            return
        if abs(self.state.output_limit - self._last_published) > DRIFT_TOLERANCE_W:
            self.state.drift_count += 1
            if (
                self.state.drift_count >= DRIFT_WARN_THRESHOLD
                and not self.state.drift_active
            ):
                self.state.drift_active = True
                _LOGGER.warning(
                    "charge44: drift detected (commanded=%s observed=%s for %s cycles)",
                    self._last_published,
                    self.state.output_limit,
                    self.state.drift_count,
                )
                self.hass.bus.async_fire(
                    EVENT_DRIFT_DETECTED,
                    {
                        "commanded": self._last_published,
                        "observed": self.state.output_limit,
                        "cycles": self.state.drift_count,
                    },
                )
        else:
            self.state.drift_count = 0
            self.state.drift_active = False

    def _update_health(self) -> None:
        # Priority: worst state wins.
        if time.monotonic() - self.state.grid_power_ts > STALE_GRID_AFTER and (
            self.state.grid_power_ts != 0.0
        ):
            self.state.health = "shelly_stale"
            return
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            self.state.health = f"temperature_{self.state.temperature_guard}"
            return
        if self.state.drift_active:
            self.state.health = "zendure_drift"
            return
        if self._tibber is not None and not self.state.today_prices:
            self.state.health = "tibber_offline"
            return
        self.state.health = "ok"

    def _make_zendure_handler(self, kind: str, prop: str):
        @callback
        def handler(msg) -> None:
            raw = msg.payload.decode() if isinstance(msg.payload, bytes) else msg.payload
            self._update_zendure(kind, prop, raw)
        return handler

    def _update_zendure(self, kind: str, prop: str, raw: str) -> None:
        try:
            if kind == "sensor":
                if prop == "electricLevel":
                    self.state.soc = int(float(raw))
                elif prop == "outputHomePower":
                    self.state.output_home_power = float(raw)
                elif prop == "solarInputPower":
                    self.state.solar_input = float(raw)
                elif prop == "packInputPower":
                    self.state.pack_output = float(raw)
                elif prop == "outputPackPower":
                    self.state.pack_input = float(raw)
                elif prop == "hyperTmp":
                    self.state.temperature = float(raw)
                    self._update_temperature_guard()
                elif prop == "packState":
                    self.state.pack_state = str(raw)
                elif prop == "packNum":
                    self._update_battery_capacity(int(float(raw)))
            elif kind == "number":
                if prop == "outputLimit":
                    self.state.output_limit = int(float(raw))
                    self._check_drift()
                elif prop == "inverseMaxPower":
                    # Hardware ceiling for *output* (microinverter cap). The grid-charge
                    # ceiling is a separate App-side setting ("On-grid Input Mode") and
                    # is not exposed via MQTT — keep charge_power independent.
                    value = int(float(raw))
                    if value > 0:
                        self.state.max_output = value
            self._notify()
        except (ValueError, TypeError) as err:
            _LOGGER.debug("Zendure parse error %s=%s: %s", prop, raw, err)

    @staticmethod
    def _pack_kwh(sn: str) -> float:
        """Battery pack capacity from its serial prefix (zendure-ha convention)."""
        if not sn:
            return 1.92
        head = sn[0]
        sub = sn[3] if len(sn) > 3 else ""
        if head == "A":
            return 2.4 if sub == "3" else 0.96
        if head == "B":
            return 0.96
        if head == "C":
            return 1.92  # AB2000X / AB2000S
        if head in ("F", "G"):
            return 2.88  # AB3000 / AB3000L
        if head == "J":
            return 2.4
        return 1.92

    def _update_battery_capacity(self, pack_num: int | None = None) -> None:
        # Prefer SN-list summing only when it agrees with the live packNum.
        # If they disagree, the user almost certainly added or removed a
        # pack without re-running the config flow — trust packNum and fall
        # back to the AB2000X default (1.92 kWh × n).
        sn_count = len(self._battery_sns)
        if sn_count and (pack_num is None or pack_num == sn_count):
            total = sum(self._pack_kwh(s) for s in self._battery_sns)
            if total > 0:
                self.state.battery_capacity = round(total, 2)
                return
        if pack_num and pack_num > 0:
            self.state.battery_capacity = round(1.92 * pack_num, 2)

    # -------- Regulation --------

    def _tick(self) -> None:
        if self.state.cheap_mode_active:
            return
        if not self.state.enabled:
            return
        if self.state.grid_power is None or self.state.soc is None:
            return
        if time.monotonic() - self.state.grid_power_ts > STALE_GRID_AFTER:
            _LOGGER.warning("charge44: Shelly data stale, pausing regulation")
            return
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            # Safety: freeze output on temperature excursion.
            if self.state.setpoint != 0.0:
                self.state.setpoint = 0.0
                self._publish_limit(0)
            return
        if self.state.soc <= self.state.min_soc:
            self.state.setpoint = 0.0
            self._publish_limit(0)
            return

        # Smart-discharge: when enabled, suspend the loop during the cheapest
        # hours so the battery is preserved for normal/expensive hours. Cheap
        # grid covers the home directly. Otherwise we always run the zero-
        # export PI loop below — the battery covers the home, never exports.
        if self.state.smart_discharge_enabled and self.state.is_cheap_now:
            if self._last_published not in (0, None) or self.state.setpoint != 0.0:
                self.state.setpoint = 0.0
                self._publish_limit(0)
            return

        error = self.state.grid_power - self.state.grid_bias
        new_setpoint = self.state.setpoint + error * self.state.kp
        new_setpoint = max(0.0, min(float(self.state.max_output), new_setpoint))
        self.state.setpoint = new_setpoint

        new_int = int(round(new_setpoint))
        now = time.monotonic()
        if now - self._last_publish_ts < MIN_PUBLISH_INTERVAL:
            return
        if (
            self._last_published is None
            or abs(new_int - self._last_published) > self.state.deadzone
        ):
            self._publish_limit(new_int)

    def _publish_limit(self, value: int) -> None:
        topic = TOPIC_ZENDURE_WRITE.format(
            kind="number", sn=self.zendure_sn, prop="outputLimit"
        )
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, topic, str(value), qos=1)
        )
        self._last_published = value
        self._last_publish_ts = time.monotonic()
        _LOGGER.debug("charge44: outputLimit -> %s W", value)

    def set_enabled(self, enabled: bool) -> None:
        if self.state.enabled == enabled:
            return
        self.state.enabled = enabled
        if not enabled:
            self.state.setpoint = 0.0
            if not self.state.cheap_mode_active:
                self._publish_limit(0)
        else:
            self._last_published = None
            self._last_publish_ts = 0.0
        self._notify()

    def set_setting(self, key: str, value: Any) -> None:
        setattr(self.state, key, value)
        if key == "min_soc":
            self._publish_min_soc(int(value))
        elif key == "target_soc":
            self._publish_soc_set(int(value))
        self._notify()

    def _publish_min_soc(self, value: int) -> None:
        topic = TOPIC_ZENDURE_WRITE.format(
            kind="number", sn=self.zendure_sn, prop="minSoc"
        )
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, topic, str(value), qos=1)
        )
        _LOGGER.debug("charge44: minSoc -> %s%%", value)

    def _publish_soc_set(self, value: int) -> None:
        topic = TOPIC_ZENDURE_WRITE.format(
            kind="number", sn=self.zendure_sn, prop="socSet"
        )
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, topic, str(value), qos=1)
        )
        _LOGGER.debug("charge44: socSet -> %s%%", value)

    # -------- Tibber prices --------

    async def _periodic_fetch(self, _now) -> None:
        await self._fetch_prices()

    async def _fetch_prices(self) -> None:
        if self._tibber is None:
            return
        prices = await self._tibber.async_get_prices()
        if not prices:
            return
        self.state.today_prices = prices.get("today", []) or []
        self.state.tomorrow_prices = prices.get("tomorrow", []) or []
        current = prices.get("current")
        if current:
            self.state.current_price = float(current.get("total", 0))
        self._detect_slot_minutes()
        self._evaluate()

    def _detect_slot_minutes(self) -> None:
        combined = self.state.today_prices + self.state.tomorrow_prices
        if len(combined) < 2:
            return
        try:
            a = dt_util.parse_datetime(combined[0].get("startsAt") or "")
            b = dt_util.parse_datetime(combined[1].get("startsAt") or "")
        except Exception:
            return
        if a is None or b is None:
            return
        diff = int(round((b - a).total_seconds() / 60))
        if diff > 0:
            self.state.slot_minutes = diff

    # -------- Cheap-charge evaluator --------

    @callback
    def _periodic_evaluate(self, _now) -> None:
        self._evaluate()

    @callback
    def _periodic_safety(self, _now) -> None:
        """Detect Shelly silence and fall back to fallback_discharge.

        _tick is only called when a Shelly RPC arrives. If the meter goes
        silent — broker hiccup, Wi-Fi blip, malformed payload — outputLimit
        gets stranded at whatever was last published, while real home load
        keeps changing. This timer is the safety net.
        """
        if self.state.cheap_mode_active or not self.state.enabled:
            return
        if self.state.grid_power_ts == 0.0:
            return  # never received Shelly data — let _tick handle it
        if time.monotonic() - self.state.grid_power_ts <= STALE_GRID_AFTER:
            return  # data is fresh, _tick is in charge
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            return
        if self.state.soc is not None and self.state.soc <= self.state.min_soc:
            return
        if self.state.smart_discharge_enabled and self.state.is_cheap_now:
            return  # explicit "preserve battery" beats fallback
        target = int(self.state.fallback_discharge)
        if self._last_published != target:
            self.state.setpoint = float(target)
            self._publish_limit(target)

    def _evaluate(self) -> None:
        """Compute is_cheap_now / next_cheap_start, apply mode transitions."""
        now = dt_util.now()
        window = self._prices_next_24h(now)

        current_entry = self._price_for(now, window)
        if current_entry is not None:
            self.state.current_price = float(current_entry["value"])

        is_cheap = self._compute_is_cheap(window, current_entry)
        self.state.is_cheap_now = is_cheap
        self.state.next_cheap_start = self._compute_next_cheap_start(window, now)

        # Expose the price timeline as attributes-ready list.
        self.state.prices_24h = [
            {
                "start": p["start"].isoformat(),
                "price_ct": round(p["value"] * 100, 2),
            }
            for p in sorted(window, key=lambda p: p["start"])
        ]

        self._update_forecast_and_gap()
        self._apply_mode(is_cheap)
        self._update_health()
        self._maybe_reset_today_counters()
        self._notify()

    def _update_forecast_and_gap(self) -> None:
        self.state.solar_remaining_kwh = self._read_forecast_kwh()
        if self.state.soc is None:
            self.state.grid_charge_needed_kwh = None
            return
        needed_kwh = max(
            0.0,
            (self.state.target_soc - self.state.soc) / 100.0
            * self.state.battery_capacity,
        )
        if self.state.solar_remaining_kwh is None:
            self.state.grid_charge_needed_kwh = round(needed_kwh, 3)
        else:
            gap = max(0.0, needed_kwh - self.state.solar_remaining_kwh)
            self.state.grid_charge_needed_kwh = round(gap, 3)

    def _prices_next_24h(self, now: datetime) -> list[dict[str, Any]]:
        """Return upcoming price slots within 24 h of now. Slot length is
        auto-detected (60 or 15 min depending on what Tibber delivered)."""
        horizon = now + timedelta(hours=24)
        slot = timedelta(minutes=self.state.slot_minutes)
        result: list[dict[str, Any]] = []
        for p in list(self.state.today_prices) + list(self.state.tomorrow_prices):
            starts = p.get("startsAt")
            if not starts:
                continue
            try:
                dt = dt_util.parse_datetime(starts)
            except Exception:
                continue
            if dt is None:
                continue
            slot_end = dt + slot
            if slot_end <= now or dt >= horizon:
                continue
            result.append(
                {
                    "start": dt,
                    "value": float(p.get("total", 0) or 0),
                    "level": p.get("level"),
                }
            )
        return result

    def _price_for(
        self, moment: datetime, window: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        slot = timedelta(minutes=self.state.slot_minutes)
        for p in window:
            if p["start"] <= moment < p["start"] + slot:
                return p
        return None

    def _compute_is_cheap(
        self, window: list[dict[str, Any]], current: dict[str, Any] | None
    ) -> bool:
        if not window or current is None:
            self.state.today_max_price = None
            self.state.today_min_price = None
            self.state.spread_now_ct = None
            self.state.required_spread_ct = None
            self.state.profitable_now = False
            return False

        values = [p["value"] for p in window]
        day_max = max(values)
        day_min = min(values)
        current_val = current["value"]

        eff = max(0.5, min(1.0, self.state.efficiency / 100.0))
        break_even = current_val * (1.0 / eff - 1.0)
        user_min = self.state.min_spread_ct / 100.0
        required = max(user_min, break_even)
        spread_now = day_max - current_val

        self.state.today_max_price = round(day_max * 100, 2)
        self.state.today_min_price = round(day_min * 100, 2)
        self.state.spread_now_ct = round(spread_now * 100, 2)
        self.state.required_spread_ct = round(required * 100, 2)
        self.state.profitable_now = spread_now >= required

        # cheap_hours is expressed in equivalent HOURS — translate to slot count.
        slots_per_hour = max(1.0, 60.0 / max(1, self.state.slot_minutes))
        n = max(
            1,
            min(int(self.state.cheap_hours * slots_per_hour), len(window)),
        )
        if self.state.contiguous_block_mode:
            cheap_set_ids = self._cheapest_contiguous_block(window, n)
            in_cheap = id(current) in cheap_set_ids
        else:
            cheapest = sorted(window, key=lambda p: p["value"])[:n]
            in_cheap = current in cheapest

        # Smart-discharge gating: symmetric spread criterion.
        # Discharge is worth it whenever the current price sits high enough
        # above today's minimum to recover the round-trip loss.
        spread_from_min_now = current_val - day_min
        self.state.is_expensive_now = spread_from_min_now >= required
        upcoming_profitable = sorted(
            (
                p
                for p in window
                if p["start"] > dt_util.now() and (p["value"] - day_min) >= required
            ),
            key=lambda p: p["start"],
        )
        self.state.next_expensive_start = (
            upcoming_profitable[0]["start"] if upcoming_profitable else None
        )

        return in_cheap and self.state.profitable_now

    @staticmethod
    def _cheapest_contiguous_block(
        window: list[dict[str, Any]], size: int
    ) -> set[int]:
        """Return ids of the N contiguous entries (by startsAt order) with the
        smallest sum of prices."""
        if len(window) < size or size <= 0:
            return set()
        ordered = sorted(window, key=lambda p: p["start"])
        best_start = 0
        running = sum(p["value"] for p in ordered[:size])
        best_sum = running
        for i in range(1, len(ordered) - size + 1):
            running = running - ordered[i - 1]["value"] + ordered[i + size - 1]["value"]
            if running < best_sum:
                best_sum = running
                best_start = i
        return {id(p) for p in ordered[best_start : best_start + size]}

    def _compute_next_cheap_start(
        self, window: list[dict[str, Any]], now: datetime
    ) -> datetime | None:
        if not window:
            return None
        slots_per_hour = max(1.0, 60.0 / max(1, self.state.slot_minutes))
        n = max(
            1,
            min(int(self.state.cheap_hours * slots_per_hour), len(window)),
        )
        cheap_set = {
            id(p) for p in sorted(window, key=lambda p: p["value"])[:n]
        }
        upcoming = [p for p in window if id(p) in cheap_set and p["start"] > now]
        if not upcoming:
            return None
        upcoming.sort(key=lambda p: p["start"])
        return upcoming[0]["start"]

    # -------- Mode transitions --------

    def _apply_mode(self, is_cheap: bool) -> None:
        # Auto-cancel one-shot manual charge as soon as the target SOC is hit.
        if (
            self.state.manual_charge
            and self.state.soc is not None
            and self.state.soc >= self.state.target_soc
        ):
            self.state.manual_charge = False
        want_charge = self._want_cheap_charge(is_cheap)
        if want_charge and not self.state.cheap_mode_active:
            self._enter_cheap_mode()
        elif not want_charge and self.state.cheap_mode_active:
            self._exit_cheap_mode()

    def _want_cheap_charge(self, is_cheap: bool) -> bool:
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            return False
        if self.state.soc is None or self.state.soc >= self.state.target_soc:
            return False
        if self.state.manual_charge:
            return True
        if (
            self.state.charge_when_free
            and self.state.current_price is not None
            and self.state.current_price <= 0.0
        ):
            return True
        if not self.state.cheap_charge_enabled:
            return False
        if not is_cheap:
            return False
        gap = self.state.grid_charge_needed_kwh
        if gap is None:
            # No forecast info — fall back to always-charge-when-cheap.
            return True
        return gap > 0

    def _read_forecast_kwh(self) -> float | None:
        if not self._forecast_entity:
            return None
        state = self.hass.states.get(self._forecast_entity)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        if unit == "wh":
            value = value / 1000.0
        return value

    def _enter_cheap_mode(self) -> None:
        _LOGGER.info(
            "charge44: entering cheap-charge (price=%.3f EUR/kWh, SOC=%s%%)",
            self.state.current_price or 0.0,
            self.state.soc,
        )
        self.state.cheap_mode_active = True
        self._publish_ac_mode(AC_MODE_INPUT)
        self._publish_input_limit(int(self.state.charge_power))
        self.hass.bus.async_fire(
            EVENT_CHEAP_CHARGE_STARTED,
            {
                "price_eur_kwh": self.state.current_price,
                "soc": self.state.soc,
                "target_soc": self.state.target_soc,
                "charge_power_w": self.state.charge_power,
            },
        )

    def _exit_cheap_mode(self) -> None:
        _LOGGER.info("charge44: leaving cheap-charge")
        self.state.cheap_mode_active = False
        self._publish_input_limit(0)
        self._publish_ac_mode(AC_MODE_OUTPUT)
        self.state.setpoint = 0.0
        self._last_published = None
        self._last_publish_ts = 0.0
        self.hass.bus.async_fire(
            EVENT_CHEAP_CHARGE_ENDED,
            {"soc": self.state.soc, "target_soc": self.state.target_soc},
        )

    def _publish_ac_mode(self, value: str) -> None:
        topic = TOPIC_ZENDURE_WRITE.format(
            kind="select", sn=self.zendure_sn, prop="acMode"
        )
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, topic, value, qos=1)
        )
        _LOGGER.debug("charge44: acMode -> %s", value)

    def _publish_input_limit(self, value: int) -> None:
        topic = TOPIC_ZENDURE_WRITE.format(
            kind="number", sn=self.zendure_sn, prop="inputLimit"
        )
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, topic, str(value), qos=1)
        )
        _LOGGER.debug("charge44: inputLimit -> %s W", value)

    # -------- Switch hooks --------

    def set_cheap_enabled(self, enabled: bool) -> None:
        if self.state.cheap_charge_enabled == enabled:
            return
        self.state.cheap_charge_enabled = enabled
        if not enabled and self.state.cheap_mode_active:
            self._exit_cheap_mode()
        else:
            self._evaluate()
        self._notify()

    def set_charge_when_free(self, enabled: bool) -> None:
        if self.state.charge_when_free == enabled:
            return
        self.state.charge_when_free = enabled
        self._evaluate()
        self._notify()

    def set_manual_charge(self, enabled: bool) -> None:
        if self.state.manual_charge == enabled:
            return
        self.state.manual_charge = enabled
        self._evaluate()
        self._notify()

    def set_contiguous_block(self, enabled: bool) -> None:
        if self.state.contiguous_block_mode == enabled:
            return
        self.state.contiguous_block_mode = enabled
        self._evaluate()

    def set_smart_discharge(self, enabled: bool) -> None:
        if self.state.smart_discharge_enabled == enabled:
            return
        self.state.smart_discharge_enabled = enabled
        self._notify()

    # -------- Services --------

    async def service_force_charge(self) -> None:
        """Manually enter cheap-charge mode regardless of price gates."""
        if self.state.cheap_mode_active:
            return
        _LOGGER.info("charge44: force_charge invoked")
        self._enter_cheap_mode()
        self._notify()

    async def service_stop_charge(self) -> None:
        if self.state.cheap_mode_active:
            _LOGGER.info("charge44: stop_charge invoked")
            self._exit_cheap_mode()
            self._notify()

    async def service_set_target_soc(self, soc: int) -> None:
        soc = max(0, min(100, int(soc)))
        self.state.target_soc = soc
        self._evaluate()
        self._notify()

    async def service_refresh_prices(self) -> None:
        await self._fetch_prices()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
