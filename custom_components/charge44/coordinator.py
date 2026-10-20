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
    ACMODE_RETRY_INTERVAL,
    CHARGE_STALL_AFTER,
    CHARGE_STALL_SOC_MARGIN,
    CHARGE_STALL_W,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_SHELLY_ID,
    CONF_TIBBER_HOME_ID,
    CONF_TIBBER_TOKEN,
    CONF_ZENDURE_BATTERY_SNS,
    CONF_ZENDURE_SN,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGE_POWER,
    DEFAULT_DEADZONE,
    DEFAULT_EFFICIENCY,
    DEFAULT_FALLBACK_DISCHARGE,
    DEFAULT_GRID_BIAS,
    DEFAULT_KP,
    DEFAULT_MAX_OUTPUT,
    DEFAULT_MIN_SOC,
    DEFAULT_TARGET_SOC,
    DEFAULT_TEMP_HIGH,
    DEFAULT_TEMP_LOW,
    DRIFT_TOLERANCE_W,
    DRIFT_WARN_THRESHOLD,
    DOMAIN,
    EFFICIENCY_MIN_KWH,
    EVENT_CHEAP_CHARGE_ENDED,
    EVENT_CHEAP_CHARGE_STARTED,
    EVENT_CHARGE_STALLED,
    EVENT_DRIFT_DETECTED,
    EVENT_TEMPERATURE_GUARD,
    HEARTBEAT_INTERVAL,
    LOAD_SEED_DAYS,
    MIN_PUBLISH_INTERVAL,
    OPTIMIZER_STEP_WH,
    REPLAN_SOC_DELTA,
    RESERVE_TARGET_ABOVE,
    RESERVE_TRIGGER_BELOW,
    SAFETY_TICK_INTERVAL,
    SIGNAL_UPDATE,
    STALE_GRID_AFTER,
    TARGET_SOC_MIN,
    TIBBER_POLL_INTERVAL,
    TIBBER_STALE_AFTER,
    TOPIC_SHELLY_RPC,
    TOPIC_ZENDURE_NUMBER,
    TOPIC_ZENDURE_SELECT,
    TOPIC_ZENDURE_SENSOR,
    TOPIC_ZENDURE_WRITE,
    ZENDURE_NUMBERS,
    ZENDURE_SELECTS,
    ZENDURE_SENSORS,
)
from .discovery import clear_legacy_discovery, pack_info, publish_zendure_discovery
from .load_profile import LoadProfile
from .optimizer import (
    CHARGE,
    HOLD,
    NORMAL,
    Battery,
    Slot,
    Step,
    baseline_cost,
    distribute_pv,
    optimise,
    slot_bounds,
)
from .tibber_api import TibberApiClient

_LOGGER = logging.getLogger(__name__)

CHEAP_EVAL_INTERVAL = timedelta(minutes=1)
TIBBER_POLL = timedelta(seconds=TIBBER_POLL_INTERVAL)

# State fields a Zendure MQTT message can touch — an update only notifies the
# entities when one of them actually changed.
_ZENDURE_FIELDS = (
    "soc",
    "output_home_power",
    "solar_input",
    "pack_input",
    "pack_output",
    "grid_input_power",
    "pack_state",
    "ac_mode",
    "temperature",
    "temperature_guard",
    "output_limit",
    "max_output",
    "battery_capacity",
    "drift_count",
    "drift_active",
)


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
    grid_input_power: float | None = None  # AC drawn from grid by the device
    pack_state: str | None = None
    ac_mode: str | None = None  # device mirror: "Output mode" | "Input mode"
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

    # Grid charging (optimizer / manual / reserve)
    optimizer_enabled: bool = False
    manual_charge: bool = False
    cheap_mode_active: bool = False  # device in Input mode, grid-charging
    charge_reason: str | None = None  # manual | reserve | optimizer, while charging
    current_price: float | None = None
    today_prices: list[dict[str, Any]] = field(default_factory=list)
    tomorrow_prices: list[dict[str, Any]] = field(default_factory=list)
    slot_minutes: int = 60  # detected from price spacing; 15 for QUARTER_HOURLY
    target_soc: int = DEFAULT_TARGET_SOC
    charge_power: int = DEFAULT_CHARGE_POWER
    battery_capacity: float = DEFAULT_BATTERY_CAPACITY  # kWh

    # Optimizer plan
    plan_action: str = NORMAL  # charge | hold | normal for the current slot
    plan: list[dict[str, Any]] = field(default_factory=list)
    plan_cost_eur: float | None = None  # expected grid bill over the horizon
    plan_baseline_eur: float | None = None  # same horizon without the battery
    next_charge_start: datetime | None = None
    round_trip_efficiency: float | None = None  # %, measured (or fallback)
    load_now_w: float | None = None  # load-profile value for this hour
    load_profile: dict[str, list[float]] = field(default_factory=dict)

    # Solar forecast (kWh)
    solar_remaining_kwh: float | None = None
    solar_tomorrow_kwh: float | None = None

    # Price diagnostics over the next 24 h (ct/kWh)
    today_max_price: float | None = None
    today_min_price: float | None = None

    # Battery ledger: what the stored energy cost (grid at its price, PV free)
    ledger_eur: float = 0.0
    cost_basis_ct: float | None = None

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

    # Grid-charge commanded and confirmed, but the battery takes ~nothing
    charge_stalled: bool = False

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
        self._forecast_tomorrow_entity: str | None = (
            merged.get(CONF_FORECAST_TOMORROW_ENTITY) or None
        )
        self.state = State()
        self._unsubs: list[Callable[[], None]] = []
        self._last_published: int | None = None
        self._last_publish_ts: float = 0.0
        self._last_energy_ts: float = 0.0
        self._last_acmode_ts: float = 0.0
        self._acmode_forced: str | None = None  # mode the self-heal is pushing
        self._acmode_reconciled: bool = False
        self._charge_suppressed: bool = False  # stop_charge until the reason ends
        self._reserve_active: bool = False  # standby-drain top-up below SOC Min
        self._stall_watch_since: float | None = None
        self._charge_seen_ts: float | None = None  # last real charge into the battery
        self._load = LoadProfile()
        self._plan_steps: list[Step] = []
        self._plan_slot: datetime | None = None  # price slot the plan was made in
        self._plan_soc: int | None = None
        self._plan_horizon: datetime | None = None
        self._planning: bool = False
        self._replan_requested: bool = False
        self._has_current_price: bool = False
        self._last_price_ok_ts: float = 0.0
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
        for prop in ZENDURE_SELECTS:
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    TOPIC_ZENDURE_SELECT.format(sn=self.zendure_sn, prop=prop),
                    self._make_zendure_handler("select", prop),
                )
            )

        # Tibber
        if self._tibber_token and self._tibber_home_id:
            session = async_get_clientsession(self.hass)
            self._tibber = TibberApiClient(
                session, self._tibber_token, self._tibber_home_id
            )
            # Don't hold up HA setup on a slow Tibber API.
            self.entry.async_create_background_task(
                self.hass, self._fetch_prices(), "charge44_initial_price_fetch"
            )
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._periodic_fetch, TIBBER_POLL
                )
            )

        # Seed the weekday load profile from recorder statistics.
        self.entry.async_create_background_task(
            self.hass, self._async_seed_load_profile(), "charge44_seed_load_profile"
        )

        # Periodic evaluator: (re)plan when due, execute the current slot
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
            # Clear only the obsolete v0.5.0 command-topic slots, then (re)publish
            # the current configs. Republishing an unchanged retained config is a
            # no-op for HA, so the mirror entities keep their state across a
            # reload — unlike a remove-then-publish, which deletes them and leaves
            # HA showing 'unavailable' until a restart.
            await clear_legacy_discovery(self.hass, self.zendure_sn)
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
        # Unload/reload mid-charge: hand the device back in Output mode. The
        # next instance starts with cheap_mode_active=False and would otherwise
        # never send the exit, leaving the Zendure grid-charging.
        if self.state.cheap_mode_active:
            self._exit_cheap_mode()
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
        pack_in = max(0.0, self.state.pack_input or 0.0)
        if pack_in > 0:
            kwh = pack_in * dt_hours / 1000.0
            self.state.energy_charged_kwh += kwh
            # Only the grid share of a charge costs money; PV goes in at 0 €.
            # The device reports grid input and battery charge 1:1, so the grid
            # share is min(grid input, battery charge). Without a gridInputPower
            # reading yet, fall back to "all of it while grid-charging".
            grid_w = self.state.grid_input_power
            if grid_w is None:
                grid_w = pack_in if self.state.cheap_mode_active else 0.0
            grid_kwh = min(pack_in, max(0.0, grid_w)) * dt_hours / 1000.0
            if price is not None and grid_kwh > 0:
                cost = grid_kwh * price
                self.state.cost_charged_today_eur += cost
                self.state.cost_charged_total_eur += cost
                self.state.ledger_eur += cost
        if self.state.pack_output is not None and self.state.pack_output > 0:
            kwh = self.state.pack_output * dt_hours / 1000.0
            self.state.energy_discharged_kwh += kwh
            if price is not None:
                value = kwh * price
                self.state.value_discharged_today_eur += value
                self.state.value_discharged_total_eur += value
            self._ledger_take(kwh)
        if self.state.solar_input is not None and self.state.solar_input > 0:
            self.state.energy_solar_kwh += self.state.solar_input * dt_hours / 1000.0
        if self.state.output_home_power is not None and self.state.output_home_power > 0:
            self.state.energy_home_kwh += (
                self.state.output_home_power * dt_hours / 1000.0
            )
        self._update_cost_basis()
        # Learn the house load: grid draw + Zendure output, minus what the
        # Zendure itself pulls from the grid to charge.
        if self.state.grid_power is not None and self.state.output_home_power is not None:
            load = (
                self.state.grid_power
                + self.state.output_home_power
                - max(0.0, self.state.grid_input_power or 0.0)
            )
            self._load.sample(dt_util.now(), max(0.0, load), dt_hours)

    def _stored_kwh(self) -> float | None:
        if self.state.soc is None:
            return None
        return max(0.0, self.state.soc / 100.0 * self.state.battery_capacity)

    def _ledger_take(self, kwh: float) -> None:
        """Energy leaves the battery at the ledger's average cost."""
        stored = self._stored_kwh()
        if stored is None or stored < 0.05 or self.state.ledger_eur <= 0:
            return
        self.state.ledger_eur = max(
            0.0, self.state.ledger_eur - kwh * self.state.ledger_eur / stored
        )

    def _update_cost_basis(self) -> None:
        """Average cost of the stored energy. Losses and standby drain shrink the
        content but not the ledger, so they raise the price per kWh left; an
        empty battery writes the rest off."""
        stored = self._stored_kwh()
        if stored is None:
            return
        if self.state.soc is not None and self.state.soc <= 0:
            self.state.ledger_eur = 0.0
        self.state.cost_basis_ct = (
            round(self.state.ledger_eur / stored * 100.0, 2) if stored >= 0.05 else None
        )

    def restore_ledger(self, eur: float) -> None:
        self.state.ledger_eur = max(0.0, eur)

    def restore_daily_counter(
        self, key: str, value: float, last_updated: datetime
    ) -> None:
        """Restore a per-day EUR counter, but only if it is from today."""
        today = dt_util.now().strftime("%Y-%m-%d")
        if dt_util.as_local(last_updated).strftime("%Y-%m-%d") != today:
            return
        setattr(self.state, key, value)
        self.state.tracking_day = today

    @staticmethod
    def today_start() -> datetime:
        return dt_util.start_of_local_day()

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
        if self.state.charge_stalled:
            self.state.health = "zendure_not_charging"
            return
        if self._tibber is not None and (
            not self._has_current_price
            or time.monotonic() - self._last_price_ok_ts > TIBBER_STALE_AFTER
        ):
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
        before = tuple(getattr(self.state, f) for f in _ZENDURE_FIELDS)
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
                elif prop == "gridInputPower":
                    self.state.grid_input_power = float(raw)
                elif prop == "outputPackPower":
                    self.state.pack_input = float(raw)
                    if self.state.pack_input >= CHARGE_STALL_W:
                        self._charge_seen_ts = time.monotonic()
                elif prop == "hyperTmp":
                    # Device publishes Kelvin (e.g. 305.1), not °C.
                    self.state.temperature = round(float(raw) - 273.15, 1)
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
            elif kind == "select":
                if prop == "acMode":
                    self.state.ac_mode = str(raw)
        except (ValueError, TypeError) as err:
            _LOGGER.debug("Zendure parse error %s=%s: %s", prop, raw, err)
            return
        if tuple(getattr(self.state, f) for f in _ZENDURE_FIELDS) != before:
            self._notify()

    @staticmethod
    def _pack_kwh(sn: str) -> float:
        """Battery pack capacity from its serial prefix (zendure-ha convention)."""
        return pack_info(sn)[1]

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
            # Grid-charging: the regulation is paused, but make sure the device
            # actually took the Input-mode command (see method).
            self._ensure_input_mode()
            return
        if not self.state.enabled:
            return
        if self.state.grid_power is None or self.state.soc is None:
            return
        # Past the cheap_mode_active guard above we are regulating output, so the
        # device must be in Output mode. Self-heal a stuck acMode (see method).
        self._ensure_output_mode()
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            # Safety: freeze output on temperature excursion.
            self._hold_zero()
            return
        if self.state.soc <= self.state.min_soc:
            self._hold_zero()
            return

        # Optimizer "hold": keep the stored energy for pricier slots later —
        # the house runs on grid meanwhile, PV still charges the battery.
        # Otherwise the zero-export loop below: the battery covers the home,
        # never exports.
        if self.state.optimizer_enabled and self.state.plan_action == HOLD:
            self._hold_zero()
            return

        # Integrate only when we may publish. Shelly ticks inside the rate
        # limit see the same, not-yet-answered error; summing them would
        # overshoot the moment the next command goes out.
        now = time.monotonic()
        if now - self._last_publish_ts < MIN_PUBLISH_INTERVAL:
            return
        error = self.state.grid_power - self.state.grid_bias
        new_setpoint = self.state.setpoint + error * self.state.kp
        new_setpoint = max(0.0, min(float(self.state.max_output), new_setpoint))
        self.state.setpoint = new_setpoint

        new_int = int(round(new_setpoint))
        moved = (
            self._last_published is None
            or abs(new_int - self._last_published) > self.state.deadzone
        )
        # Heartbeat: re-publish the standing setpoint periodically even when it
        # hasn't moved. Without it the deadzone makes us go silent at a stable
        # setpoint, so a Zendure that dropped to standby on its own never gets a
        # fresh command edge to wake it (observed 2026-09-17: outputLimit pinned
        # at max while the device output 0 W and the house imported from grid).
        stale = now - self._last_publish_ts >= HEARTBEAT_INTERVAL
        if moved or stale:
            self._publish_limit(new_int)

    def _hold_zero(self) -> None:
        """Park output at 0 W. Re-sent only on change or when the heartbeat is
        due — not on every Shelly tick for the hours a guard can last."""
        self.state.setpoint = 0.0
        stale = time.monotonic() - self._last_publish_ts >= HEARTBEAT_INTERVAL
        if self._last_published != 0 or stale:
            self._publish_limit(0)

    def _ensure_output_mode(self) -> None:
        """Self-heal a stuck acMode.

        A cheap-charge cycle switches the device to "Input mode". If the exit's
        acMode revert is dropped by the device (observed 2026-09-18), the device
        stays in Input mode and silently ignores every outputLimit command — it
        sits in standby with no output and curtails PV (nowhere for it to go),
        while the house imports from grid. Whenever we're actively regulating
        output (we are past the cheap_mode_active guard in _tick), force the
        device back to Output mode until its status mirror confirms the change.
        """
        if self.state.ac_mode in (None, AC_MODE_OUTPUT):
            self._acmode_forced = None
            return
        # Retry at most every ACMODE_RETRY_INTERVAL — this also gives the device
        # time to echo our own exit command before we call it stuck.
        if time.monotonic() - self._last_acmode_ts < ACMODE_RETRY_INTERVAL:
            return
        if self._acmode_forced != AC_MODE_OUTPUT:
            _LOGGER.warning(
                "charge44: device in %s while regulating output — forcing %s "
                "(stale cheap-charge state was making outputLimit a no-op)",
                self.state.ac_mode,
                AC_MODE_OUTPUT,
            )
            self._acmode_forced = AC_MODE_OUTPUT
        self._publish_ac_mode(AC_MODE_OUTPUT)

    def _ensure_input_mode(self) -> None:
        """Self-heal the other direction: a cheap-charge cycle whose acMode=Input
        was dropped leaves the device in Output mode — not charging, and still
        discharging at whatever outputLimit it last had. Repeat the entry
        commands until the status mirror confirms Input mode."""
        if self.state.ac_mode in (None, AC_MODE_INPUT):
            self._acmode_forced = None
            return
        if time.monotonic() - self._last_acmode_ts < ACMODE_RETRY_INTERVAL:
            return
        if self._acmode_forced != AC_MODE_INPUT:
            _LOGGER.warning(
                "charge44: device in %s during cheap-charge — forcing %s",
                self.state.ac_mode,
                AC_MODE_INPUT,
            )
            self._acmode_forced = AC_MODE_INPUT
        self._publish_ac_mode(AC_MODE_INPUT)
        self._publish_input_limit(int(self.state.charge_power))

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
            self._replan_requested = True
        elif key == "target_soc":
            self._publish_soc_set(int(value))
            self._replan_requested = True
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
        self._last_price_ok_ts = time.monotonic()
        self.state.today_prices = prices.get("today", []) or []
        self.state.tomorrow_prices = prices.get("tomorrow", []) or []
        current = prices.get("current")
        if current:
            self.state.current_price = float(current.get("total", 0))
        self._detect_slot_minutes()
        self._replan_requested = True
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

    # -------- Evaluator / optimizer --------

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
        if self.state.optimizer_enabled and self.state.plan_action == HOLD:
            return  # the plan keeps the battery for later — beats the fallback
        target = int(self.state.fallback_discharge)
        stale = time.monotonic() - self._last_publish_ts >= HEARTBEAT_INTERVAL
        if self._last_published != target or stale:
            self.state.setpoint = float(target)
            self._publish_limit(target)

    def _evaluate(self) -> None:
        """Refresh price / forecast state, re-plan when due, execute the slot."""
        now = dt_util.now()
        window = self._price_slots(now)

        current_entry = self._price_for(now, window)
        self._has_current_price = current_entry is not None
        if current_entry is not None:
            self.state.current_price = float(current_entry["value"])

        day = sorted(
            (p for p in window if p["start"] < now + timedelta(hours=24)),
            key=lambda p: p["start"],
        )
        values = [p["value"] for p in day]
        self.state.today_max_price = round(max(values) * 100, 2) if values else None
        self.state.today_min_price = round(min(values) * 100, 2) if values else None
        # Expose the price timeline as attributes-ready list.
        self.state.prices_24h = [
            {"start": p["start"].isoformat(), "price_ct": round(p["value"] * 100, 2)}
            for p in day
        ]

        self.state.solar_remaining_kwh = self._read_kwh(self._forecast_entity)
        self.state.solar_tomorrow_kwh = self._read_kwh(self._forecast_tomorrow_entity)
        self.state.round_trip_efficiency = round(self._efficiency() * 100, 1)
        local = dt_util.as_local(now)
        self.state.load_now_w = round(self._load.get(local.weekday(), local.hour))
        self.state.load_profile = self._load.table()

        if self._plan_due(now, window, current_entry):
            self._request_plan(now, window, current_entry)
        self._apply_plan(now)

    def _apply_plan(self, now: datetime) -> None:
        """Execute the plan's action for the current slot."""
        self.state.plan_action = self._current_action(now)
        self._apply_mode()
        self._reconcile_ac_mode()
        self._check_charge_stall()
        self._update_health()
        self._maybe_reset_today_counters()
        self._notify()

    def _price_slots(self, now: datetime) -> list[dict[str, Any]]:
        """All known price slots that haven't ended yet. Slot length is
        auto-detected (60 or 15 min depending on what Tibber delivered)."""
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
            if dt is None or dt + slot <= now:
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

    def _known_horizon(self) -> datetime | None:
        """Start of the latest price slot Tibber has delivered so far."""
        latest: datetime | None = None
        for p in self.state.today_prices + self.state.tomorrow_prices:
            dt = dt_util.parse_datetime(p.get("startsAt") or "")
            if dt is not None and (latest is None or dt > latest):
                latest = dt
        return latest

    def _efficiency(self) -> float:
        """Round trip measured by the integration's own counters (energy out of
        the battery / energy into it). The Zendure reports its conversions as
        lossless, so every loss — charger, cells, inverter, standby — shows up
        in this ratio. Fallback until enough energy has gone through."""
        charged = self.state.energy_charged_kwh
        if charged < EFFICIENCY_MIN_KWH:
            return DEFAULT_EFFICIENCY / 100.0
        return min(0.98, max(0.5, self.state.energy_discharged_kwh / charged))

    def _plan_due(
        self,
        now: datetime,
        window: list[dict[str, Any]],
        current: dict[str, Any] | None,
    ) -> bool:
        if not self.state.optimizer_enabled or not window or self._planning:
            return False
        if self.state.soc is None:
            return False
        if self._replan_requested or not self._plan_steps:
            return True
        if self._known_horizon() != self._plan_horizon:
            return True  # new day-ahead prices
        if current is not None and current["start"] != self._plan_slot:
            return True  # every slot: re-plan from the measured SOC
        if (
            self._plan_soc is not None
            and abs(self.state.soc - self._plan_soc) >= REPLAN_SOC_DELTA
        ):
            return True
        return False

    def _request_plan(
        self,
        now: datetime,
        window: list[dict[str, Any]],
        current: dict[str, Any] | None,
    ) -> None:
        inputs = self._build_plan_inputs(now, window)
        if inputs is None:
            return
        self._planning = True
        self._replan_requested = False
        self.hass.async_create_task(
            self._async_replan(inputs, current["start"] if current else None)
        )

    async def _async_replan(
        self, inputs: tuple[list[Slot], Battery, float, bool], slot_start: datetime | None
    ) -> None:
        slots, battery, energy, charging = inputs
        try:
            steps = await self.hass.async_add_executor_job(
                optimise, slots, battery, energy, OPTIMIZER_STEP_WH, charging
            )
        except Exception as err:  # never let a planning bug stop the regulation
            _LOGGER.warning("charge44: optimizer failed: %s", err)
            steps = []
        finally:
            self._planning = False
        self._store_plan(steps, slots, battery, slot_start)
        self._apply_plan(dt_util.now())

    def plan_now(self) -> None:
        """Synchronous re-plan (tests, diagnostics)."""
        now = dt_util.now()
        window = self._price_slots(now)
        current = self._price_for(now, window)
        inputs = self._build_plan_inputs(now, window)
        if inputs is None:
            return
        slots, battery, energy, charging = inputs
        self._store_plan(
            optimise(slots, battery, energy, OPTIMIZER_STEP_WH, charging),
            slots,
            battery,
            current["start"] if current else None,
        )
        self._replan_requested = False

    def _build_plan_inputs(
        self, now: datetime, window: list[dict[str, Any]]
    ) -> tuple[list[Slot], Battery, float, bool] | None:
        if self.state.soc is None or not window:
            return None
        cap_wh = self.state.battery_capacity * 1000.0
        ordered = sorted(window, key=lambda p: p["start"])
        bounds = slot_bounds(
            [p["start"] for p in ordered], self.state.slot_minutes, now
        )
        pv = distribute_pv(bounds, self._pv_days(now))
        slots: list[Slot] = []
        for p, (a, b), pv_wh in zip(ordered, bounds, pv):
            hours = (b - a).total_seconds() / 3600.0
            if hours <= 0:
                continue
            local = dt_util.as_local(a)
            load_w = self._load.get(local.weekday(), local.hour)
            slots.append(Slot(p["start"], hours, p["value"], load_w * hours, pv_wh))
        battery = Battery(
            capacity_wh=cap_wh,
            min_wh=cap_wh * self.state.min_soc / 100.0,
            max_wh=cap_wh * self.state.target_soc / 100.0,
            charge_w=float(self.state.charge_power),
            output_w=float(self.state.max_output),
            efficiency=self._efficiency(),
        )
        # Whether the device is grid-charging right now: stopping costs a mode
        # switch, so the plan won't cut a running charge for a fraction of a cent.
        return slots, battery, cap_wh * self.state.soc / 100.0, self.state.cheap_mode_active

    def _store_plan(
        self,
        steps: list[Step],
        slots: list[Slot],
        battery: Battery,
        slot_start: datetime | None,
    ) -> None:
        self._plan_steps = steps
        self._plan_slot = slot_start
        self._plan_soc = self.state.soc
        self._plan_horizon = self._known_horizon()
        cap = battery.capacity_wh or 1.0
        self.state.plan = [
            {
                "start": s.start.isoformat(),
                "action": s.action,
                "soc": round(s.energy_wh / cap * 100),
                "price_ct": round(s.price * 100, 2),
            }
            for s in steps
        ]
        self.state.plan_cost_eur = (
            round(sum(s.grid_wh * s.price for s in steps) / 1000.0, 3) if steps else None
        )
        self.state.plan_baseline_eur = (
            round(baseline_cost(slots, battery.output_w), 3) if steps else None
        )
        self.state.next_charge_start = next(
            (s.start for s in steps if s.action == CHARGE), None
        )

    def _current_action(self, now: datetime) -> str:
        if not self.state.optimizer_enabled:
            return NORMAL
        length = timedelta(minutes=self.state.slot_minutes)
        for step in self._plan_steps:
            if step.start <= now < step.start + length:
                return step.action
        return NORMAL

    def _pv_days(self, now: datetime) -> list[tuple[datetime, datetime, datetime, float]]:
        """(from, sunrise, sunset, kWh) for remaining-today and tomorrow."""
        days = []
        today = dt_util.as_local(now).date()
        remaining = self._read_kwh(self._forecast_entity)
        sun = self._sun_window(today)
        if remaining and sun:
            days.append((now, sun[0], sun[1], remaining))
        tomorrow = self._read_kwh(self._forecast_tomorrow_entity)
        sun = self._sun_window(today + timedelta(days=1))
        if tomorrow and sun:
            days.append((sun[0], sun[0], sun[1], tomorrow))
        return days

    def _sun_window(self, day) -> tuple[datetime, datetime] | None:
        try:
            from homeassistant.helpers.sun import get_astral_event_date

            rise = get_astral_event_date(self.hass, "sunrise", day)
            set_ = get_astral_event_date(self.hass, "sunset", day)
        except Exception:  # no location / helper unavailable
            return None
        if rise is None or set_ is None or set_ <= rise:
            return None
        return rise, set_

    async def _async_seed_load_profile(self) -> None:
        """Fill the weekday profile from the last weeks of hourly statistics:
        load = Shelly grid power + Zendure output − Zendure grid input."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.helpers import entity_registry as er
        except ImportError:
            return
        reg = er.async_get(self.hass)
        entry_id = self.entry.entry_id
        grid = reg.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_grid_power")
        out = reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry_id}_output_home_power"
        )
        grid_in = reg.async_get_entity_id(
            "sensor", "mqtt", f"zendure_{self.zendure_sn}_grid_input_power"
        )
        if not grid or not out:
            return
        end = dt_util.utcnow()
        start = end - timedelta(days=LOAD_SEED_DAYS)
        ids = {e for e in (grid, out, grid_in) if e}
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                ids,
                "hour",
                None,
                {"mean"},
            )
        except Exception as err:
            _LOGGER.debug("charge44: load-profile seed failed: %s", err)
            return
        hours: dict[float, dict[str, float]] = {}
        for eid, rows in stats.items():
            for row in rows:
                ts, mean = row.get("start"), row.get("mean")
                if ts is None or mean is None:
                    continue
                if isinstance(ts, datetime):
                    ts = ts.timestamp()
                hours.setdefault(float(ts), {})[eid] = float(mean)
        seeded = 0
        for ts in sorted(hours):
            h = hours[ts]
            if grid not in h or out not in h:
                continue
            load = max(0.0, h[grid] + h[out] - max(0.0, h.get(grid_in, 0.0)))
            local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
            self._load.add_hour(local.weekday(), local.hour, load)
            seeded += 1
        _LOGGER.info("charge44: load profile seeded from %d hours of statistics", seeded)
        self._replan_requested = True

    # -------- Mode transitions --------

    def _apply_mode(self) -> None:
        soc = self.state.soc
        target = self.state.target_soc
        # Auto-cancel one-shot manual charge as soon as the target SOC is hit.
        if self.state.manual_charge and soc is not None and soc >= target:
            self.state.manual_charge = False
        # Standby protection: below SOC Min the regulation parks the output,
        # but the hub's own ~10 W keeps draining the battery. On 2026-10-08/09
        # that took it from 5 % to 0 % overnight, and the firmware's deep-
        # discharge trickle then got stuck. Top up regardless of price.
        if soc is not None:
            if soc <= self.state.min_soc - RESERVE_TRIGGER_BELOW:
                if not self._reserve_active:
                    _LOGGER.warning(
                        "charge44: SOC %s%% is %s%% below SOC Min — reserve top-up "
                        "to %s%% regardless of price",
                        soc,
                        RESERVE_TRIGGER_BELOW,
                        self.state.min_soc + RESERVE_TARGET_ABOVE,
                    )
                self._reserve_active = True
            elif soc >= self.state.min_soc + RESERVE_TARGET_ABOVE:
                self._reserve_active = False
        want_charge = self._want_grid_charge()
        # stop_charge: stay off until the reason that would charge has ended.
        # The reserve top-up is a protection and is not suppressed — otherwise
        # a stop near empty would leave the battery draining indefinitely.
        if self._charge_suppressed and not self._reserve_active:
            if self._charge_reason():
                want_charge = False
            else:
                self._charge_suppressed = False
        if want_charge and not self.state.cheap_mode_active:
            self._enter_cheap_mode(self._active_reason())
        elif not want_charge and self.state.cheap_mode_active:
            self._exit_cheap_mode()

    def _charge_reason(self) -> bool:
        """Whether anything currently asks for grid charging (guards aside)."""
        return self.state.manual_charge or (
            self.state.optimizer_enabled and self.state.plan_action == CHARGE
        )

    def _active_reason(self) -> str:
        """Why a grid charge is starting — for the event and sensor attribute."""
        if self.state.manual_charge:
            return "manual"
        if self._reserve_active:
            return "reserve"
        return "optimizer"

    def _want_grid_charge(self) -> bool:
        if self.state.temperature_guard in ("too_cold", "too_hot"):
            return False
        if self.state.soc is None or self.state.soc >= self.state.target_soc:
            return False
        if self.state.manual_charge or self._reserve_active:
            return True
        return self.state.optimizer_enabled and self.state.plan_action == CHARGE

    def _reconcile_ac_mode(self) -> None:
        """Once after start: Input mode that this instance doesn't want is a
        leftover from before a restart (HA doesn't unload integrations on
        shutdown). Hand the device back — even with the regulation off, where
        _ensure_output_mode never runs."""
        if self._acmode_reconciled:
            return
        if self.state.ac_mode is None or self.state.soc is None:
            return  # wait until the status mirror and a decision exist
        self._acmode_reconciled = True
        if self.state.ac_mode == AC_MODE_INPUT and not self.state.cheap_mode_active:
            _LOGGER.warning(
                "charge44: device still in %s from before the restart — resetting",
                AC_MODE_INPUT,
            )
            self._publish_input_limit(0)
            self._publish_ac_mode(AC_MODE_OUTPUT)

    def _check_charge_stall(self) -> None:
        """Grid-charge commanded and confirmed (status mirror says Input mode),
        yet the battery takes next to nothing. Seen after a deep discharge on
        2026-10-09: inputLimit 1000 accepted, ~20 W trickle for hours, only a
        hub restart helped. Report it (health + event); don't intervene.

        Judged on battery charge power, not grid input, so PV-covered charging
        doesn't count as a stall; the last few % below target are skipped
        because the charge tapers there anyway."""
        watching = (
            self.state.cheap_mode_active
            and self.state.ac_mode == AC_MODE_INPUT
            and self.state.soc is not None
            and self.state.soc < self.state.target_soc - CHARGE_STALL_SOC_MARGIN
        )
        if not watching:
            self._stall_watch_since = None
            self.state.charge_stalled = False
            return
        now = time.monotonic()
        if self._stall_watch_since is None:
            self._stall_watch_since = now
        last_ok = self._stall_watch_since
        if self._charge_seen_ts is not None:
            last_ok = max(last_ok, self._charge_seen_ts)
        stalled = now - last_ok >= CHARGE_STALL_AFTER
        if stalled and not self.state.charge_stalled:
            _LOGGER.warning(
                "charge44: device in %s with inputLimit %s W, but the battery "
                "has taken < %s W for %d min — the Zendure may need a restart",
                AC_MODE_INPUT,
                self.state.charge_power,
                CHARGE_STALL_W,
                (now - last_ok) // 60,
            )
            self.hass.bus.async_fire(
                EVENT_CHARGE_STALLED,
                {
                    "soc": self.state.soc,
                    "target_soc": self.state.target_soc,
                    "charge_power_w": self.state.charge_power,
                    "battery_charge_w": self.state.pack_input,
                },
            )
        self.state.charge_stalled = stalled

    def _read_kwh(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
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

    def _enter_cheap_mode(self, reason: str = "optimizer") -> None:
        _LOGGER.info(
            "charge44: entering grid charge (%s, price=%.3f EUR/kWh, SOC=%s%%)",
            reason,
            self.state.current_price or 0.0,
            self.state.soc,
        )
        self.state.cheap_mode_active = True
        self.state.charge_reason = reason
        # Zero the output first: if the acMode write is dropped, the device
        # would otherwise keep discharging at the last regulation value while
        # the regulation is paused.
        self.state.setpoint = 0.0
        self._publish_limit(0)
        self._publish_ac_mode(AC_MODE_INPUT)
        self._publish_input_limit(int(self.state.charge_power))
        self.hass.bus.async_fire(
            EVENT_CHEAP_CHARGE_STARTED,
            {
                "price_eur_kwh": self.state.current_price,
                "soc": self.state.soc,
                "target_soc": self.state.target_soc,
                "charge_power_w": self.state.charge_power,
                "reason": reason,
            },
        )

    def _exit_cheap_mode(self) -> None:
        _LOGGER.info("charge44: leaving grid charge")
        self.state.cheap_mode_active = False
        self.state.charge_reason = None
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
        self._last_acmode_ts = time.monotonic()
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

    def set_optimizer_enabled(self, enabled: bool) -> None:
        if self.state.optimizer_enabled == enabled:
            return
        self.state.optimizer_enabled = enabled
        if not enabled:
            self._plan_steps = []
            self.state.plan = []
            self.state.next_charge_start = None
            self.state.plan_cost_eur = self.state.plan_baseline_eur = None
        self._replan_requested = True
        self._evaluate()

    def set_manual_charge(self, enabled: bool) -> None:
        if enabled:
            self._charge_suppressed = False  # an explicit start beats stop_charge
        if self.state.manual_charge == enabled:
            return
        self.state.manual_charge = enabled
        self._evaluate()

    # -------- Services --------

    async def service_force_charge(self) -> None:
        """One-shot grid charge up to the target SOC — the Charge Manual switch."""
        _LOGGER.info("charge44: force_charge invoked")
        self.set_manual_charge(True)

    async def service_stop_charge(self) -> None:
        """Stop grid charging and keep it off until the current reason ends
        (the optimizer's planned charge slots are over)."""
        _LOGGER.info("charge44: stop_charge invoked")
        self.state.manual_charge = False
        self._charge_suppressed = True
        self._evaluate()

    async def service_set_target_soc(self, soc: int) -> None:
        # Same path as the SOC Max slider: forwards socSet to the device and
        # the slider (which reads state) shows and persists the new value.
        self.set_setting(
            "target_soc", max(TARGET_SOC_MIN, min(100, int(soc)))
        )
        self._evaluate()

    async def service_refresh_prices(self) -> None:
        await self._fetch_prices()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
