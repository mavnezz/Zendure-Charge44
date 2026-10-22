# Zendure-Charge44

[![Release](https://img.shields.io/github/v/release/mavnezz/Zendure-Charge44?style=for-the-badge)](https://github.com/mavnezz/Zendure-Charge44/releases)
[![License](https://img.shields.io/github/license/mavnezz/Zendure-Charge44?style=for-the-badge)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz)
[![Tests](https://img.shields.io/github/actions/workflow/status/mavnezz/Zendure-Charge44/tests.yml?branch=main&style=for-the-badge&label=tests)](https://github.com/mavnezz/Zendure-Charge44/actions/workflows/tests.yml)

Home Assistant custom integration that controls a **Zendure SolarFlow 800 Pro** over local MQTT. Combines dynamic zero-export regulation (against a Shelly 3EM Pro) with a **cost-optimal charge / hold / discharge schedule** over the Tibber price horizon, using your measured round-trip efficiency, a learned weekday load profile and the solar forecast.

No cloud, no middleware — speaks directly to your local MQTT broker.

## What it does

### 1. Zero-export regulation
Reads live grid power from the Shelly 3EM Pro and adjusts the Zendure's `outputLimit` to keep the net flow near a configurable target (default 0 W). The battery covers the house, it never exports.

### 2. Price optimizer
With a Tibber API token, charge44 plans every 15-minute slot of the known price horizon (up to ~35 h once tomorrow's prices are out) as one of:
- **charge** — grid-charge the battery (Input mode),
- **hold** — keep the stored energy for pricier slots later (output 0; the house runs on grid, PV still charges the battery),
- **normal** — zero-export regulation (PV and battery cover the house).

The plan minimises the grid bill. No thresholds to tune: it only buys energy whose later use pays for the round-trip losses, only as much as the expected house load needs, and leaves room for the sun. It re-plans every slot from the measured SOC, and on new prices or setting changes.

### 3. Battery ledger
Tracks what the usable energy in the battery (above SOC Min) cost: grid charges at their price, PV at 0 €. Shown as **Battery cost basis** (ct/kWh); the EUR counters count real grid charging only.

### 4. Manual charge & standby protection
- **Charge Manual** — one-shot grid charge to SOC Max, regardless of price.
- **Reserve top-up** — keeps the hub's standby drain from deep-discharging the battery (see below).

## Hardware

- Zendure SolarFlow 800 Pro with local MQTT enabled in the device settings. The device must publish on topics like `Zendure/sensor/<SN>/...` and listen on `.../set`.
- Shelly 3EM Pro (or compatible) publishing `total_act_power` on `<device_id>/events/rpc`.
- Both devices on the same MQTT broker as Home Assistant.
- (Optional) Tibber account + API token for the optimizer.
- (Optional) Solar forecast sensors (kWh remaining today / tomorrow), e.g. Forecast.Solar or PV-forecast.

> **Required for grid charging:** the device's **"On-grid Input Mode"** must be enabled in the Zendure mobile app, set to your desired charge power (e.g. 800 or 1000 W). Without this app-side setting the firmware accepts the MQTT commands (`acMode=Input`, `inputLimit=...`) but never actually pulls from the grid — every status topic looks correct, yet `gridInputPower` stays at 0.

## Install

### HACS (recommended)
1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/mavnezz/Zendure-Charge44` as type *Integration*
3. Pick `Zendure-Charge44` from the list and install
4. Restart Home Assistant
5. Settings → Devices & Services → **Add Integration** → charge44

### Manual
Copy `custom_components/charge44/` into `<HA_config>/custom_components/charge44/` and restart HA.

## Setup

Three steps in the config flow:

1. **Devices** — the plugin scans MQTT for 3 seconds and offers detected Zendure SNs and Shelly IDs as dropdowns. Manual entry stays available if your devices don't auto-detect.
2. **Tibber + solar forecast** (all optional) — paste the API token, pick the solar sensor for *remaining today* and the one for *tomorrow*. Leave blank if you only want zero-export regulation.
3. **Tibber home** — only shown when the account has multiple homes.

Token, home and forecast sensors can be changed later via **Configure** (options flow).

Requires Home Assistant **2025.10** or newer (with the recorder, for seeding the load profile).

## Entities

Entity IDs below are the usual ones; Home Assistant may prefix new entities with the device's area.

### Sensors
| Entity | Description |
|---|---|
| `sensor.charge44_grid_power` | Net grid flow (Shelly) |
| `sensor.charge44_grid_import` / `_export` | Positive-only import/export, for the Energy Dashboard |
| `sensor.charge44_battery_soc` | SOC % |
| `sensor.charge44_battery_charging` / `_discharging` / `_net_flow` | Battery flow (W) |
| `sensor.charge44_solar_input` | PV input |
| `sensor.charge44_output_to_home` | Zendure → home |
| `sensor.charge44_output_limit_device` | current Zendure setpoint |
| `sensor.charge44_regulation_setpoint` / `_error` | plugin's PI controller |
| `sensor.charge44_pack_state`, `_temperature` | state + temperature |
| `sensor.charge44_current_price` | current electricity price (ct/kWh); attribute `prices_24h` |
| `sensor.charge44_today_min_price` / `_max_price` | min/max of the next 24 h (ct/kWh) |
| **Planned action** | `charge` / `hold` / `normal` for the current slot; attributes `plan` (every slot: start, action, SOC, price), `plan_cost_eur`, `baseline_cost_eur` (same horizon without battery) |
| `sensor.charge44_next_cheap_window` (**Next grid charge**) | start of the next planned charge slot |
| `sensor.charge44_cheap_charge_active` (**Grid charging**) | "on"/"off"; attribute `reason`: `optimizer`, `manual`, `reserve` |
| **Round-trip efficiency** | conversion round trip the plan uses, with the hub's own draw taken out (measured over 28 days, ≤ 95 %; 85 % until measured); attributes `source`, `standby_w` (the SOC drop the plan assumes), `standby_balance_w` (the standby that fits the 28-day energy balance), `lifetime_out_in_pct` |
| **House load forecast** | expected load this hour; attribute `profile` (7 days × 24 h) |
| **Battery cost basis** | average cost of the usable energy above SOC Min (ct/kWh); attribute `ledger_eur` |
| **Standby consumption** | the hub's own draw from the battery, measured while idle (W); attributes `windows`, `last_window_w`, `last_window_hours` |
| `sensor.charge44_solar_forecast_remaining`, **Solar forecast tomorrow** | kWh from your forecast sensors |
| `sensor.charge44_cost_charged_today` / `_total` | cost of grid charging (EUR) |
| `sensor.charge44_discharge_value_today` / `_total` | value of discharged energy (EUR) |
| `sensor.charge44_savings_today` / `_total` | value minus cost (EUR) |

### Sliders (numbers)
| Entity | Default | Meaning |
|---|---|---|
| `number.charge44_target_soc` | 80 % | upper bound for charging (51–100 %) — friendly name **SOC Max** |
| `number.charge44_min_soc` | 10 % | discharge floor (0–50 %) — friendly name **SOC Min** |
| `number.charge44_fallback_discharge` | 0 W | output while the Shelly is silent |

### Switches
- `switch.charge44_regulation` — zero-export regulation on/off (friendly name **0-Regulation**)
- `switch.charge44_cheap_charge` — price optimizer on/off (friendly name **Optimizer**). Off: zero-export only, no price-based charging or holding.
- `switch.charge44_manual_charge` — immediate grid charge regardless of price; auto-disables when SOC Max is reached. One-shot, doesn't survive an HA restart (friendly name **Charge Manual**)

### Services
- `charge44.force_charge` — one-shot grid charge up to SOC Max right now (same as **Charge Manual**).
- `charge44.stop_charge` — stop grid charging; turns **Charge Manual** off and keeps the optimizer from grid-charging until its current planned charge period has ended.
- `charge44.set_target_soc` — set SOC Max (51–100 %); same as the slider, also forwarded to the device.
- `charge44.refresh_prices` — re-fetch Tibber prices now (and re-plan).

### Health & events
`sensor.charge44_health` reports the worst current condition: `ok`, `shelly_stale`, `temperature_too_cold` / `temperature_too_hot`, `zendure_drift`, `zendure_not_charging`, `tibber_offline`.

Events on the HA bus (for automations): `charge44_cheap_charge_started` (with `reason`), `charge44_cheap_charge_ended`, `charge44_temperature_guard`, `charge44_drift_detected`, `charge44_charge_stalled`.

`charge44_charge_stalled` / `zendure_not_charging`: a grid charge is running and the device confirms *Input mode*, but the battery has taken less than 100 W for 5 minutes (not judged in the last 5 % below SOC Max, where the charge tapers). Seen after a deep discharge — the Zendure accepted every command but only trickle-charged until the hub was restarted. charge44 only reports this, it doesn't intervene. Example notification:
```yaml
triggers:
  - trigger: event
    event_type: charge44_charge_stalled
actions:
  - action: notify.notify
    data:
      message: "Zendure lädt nicht (SOC {{ trigger.event.data.soc }} %) — Hub neu starten?"
```

## Logic

### Regulation (per Shelly tick, ~5 s)
```
error  = grid_power - grid_bias
setpoint += error × Kp
setpoint  = clamp(0, max_output)
→ Zendure/number/<SN>/outputLimit/set
```
Hysteresis: only publish when the change exceeds the deadzone (5 W) AND the last publish is ≥ 3 s old. The error is only integrated when a publish is allowed, so Shelly ticks inside the 3 s window don't add up the same unanswered error. A standing setpoint is re-sent every 45 s (heartbeat); guards that park the output at 0 W (temperature, SOC Min, optimizer *hold*) re-send only on change or heartbeat.

### Optimizer
Dynamic programming over the energy stored in the battery (20 Wh steps), slot by slot over all known prices:
```
inputs   prices            Tibber, 15-min (or hourly) slots
         SOC, SOC Min/Max  live / sliders
         charge power      1000 W (inputLimit), inverter ceiling from inverseMaxPower
         round trip η      conversion round trip without the hub's own draw,
                           split √η in / √η out
         standby           the hub's own draw, taken from the battery in every slot
         house load        weekday × hour profile
         PV                remaining-today / tomorrow kWh, spread along a sun bow
                           between sunrise and sunset

per slot  charge  house from grid, grid + PV into the battery (up to SOC Max)
          hold    house from grid, PV into the battery; PV it can't take
                  covers the house (Zendure "excess energy export")
          normal  PV covers the house first, the battery the rest (down to
                  SOC Min, within the inverter ceiling), surplus PV charges

minimise  Σ grid import × price  (+ a prohibitive penalty for letting the
          standby drain the battery below SOC Min − 2 %)
```
**Standby:** the hub keeps drawing from a parked battery (output 0 or at SOC Min). The plan takes that draw out every slot and must not let it sink the battery more than 2 % below SOC Min — so it keeps a buffer or tops up at the cheapest moment in time, instead of leaving it to the price-blind reserve top-up one step further down. The rate is the standby meter's measured SOC drop (30 W until it has two windows): that is what moves the displayed SOC, and SOC Min, the floor and the reserve all act on it.

Energy still in the battery at the end of the horizon is valued at the cheapest known price, so the plan never buys speculatively beyond what it can use within the known prices; tomorrow's prices (~13:00) extend the horizon and the plan. Only the current slot is executed; it re-plans every slot from the measured SOC, on new prices, on SOC Min/Max changes and when the SOC drifts ≥ 3 % from the plan. The calculation (~0.1 s) runs in an executor.

**Load profile:** one average per weekday and hour (7 × 24). Seeded at startup from the last 28 days of recorder statistics (Shelly grid power + Zendure output − Zendure grid input), then learned live; each weekday-hour moves with weight ¼ per week (~4-week memory). Hours without data fall back to the same hour on other days.

**Round trip:** the Zendure reports its conversions as lossless, so all losses only show up as energy that went into the battery and never came out. Over the last 28 days of statistics (re-measured daily): losses = energy in − energy out − extra still stored. The metered standby is taken out of that, the rest is the conversion round trip, capped at 95 % (cells plus inverter can't do better); the standby that would fit the balance at that cap is shown as `standby_balance_w`. It comes out lower than the metered SOC drop — near the bottom the displayed SOC falls faster than the kWh counters suggest — so the plan's drift uses the meter, the round trip uses the balance. Until the statistics are read, 85 % is assumed.

**Standby protection (reserve top-up):** below SOC Min the regulation parks the output at 0 W, but the hub's own consumption (~10 W) keeps draining the battery. When SOC falls 3 % below SOC Min (e.g. 7 % at SOC Min 10 %), charge44 grid-charges back to SOC Min + 3 % regardless of price (~0.25 kWh per top-up). Only the temperature guard blocks it; `stop_charge` does not. SOC Min 0 % disables it.

### Standby meter
The hub draws power for itself that its readings never show (it reports conversions as lossless) — only the SOC sinks. While the battery is idle (no charge, no discharge, no PV, no grid input) every 1 % step down is that drain. A window is timed from its first to its last step (≥ 1 step, ≥ 45 min, so it also learns at SOC Min, where the plan tops up after ~2 %), so the 1 % resolution doesn't skew it; a drop of more than 1 % at once or faster than 1 % per 10 min is a BMS correction and restarts the window. Running mean over the first five windows, then an EMA. Drives the plan's standby drift (from two windows on).

### Battery ledger
```
charge     ledger += grid share × price      (grid share = min(grid input, battery charge); PV is free)
discharge  ledger −= kWh out × ledger / usable
cost basis = ledger / usable                  (usable = (SOC − SOC Min) × capacity; losses raise it)
SOC Min    ledger = 0                         (the rest is written off)
```

### Mode transitions
On entering a grid charge the plugin publishes:
```
Zendure/number/<SN>/outputLimit/set   → 0
Zendure/select/<SN>/acMode/set        → "Input mode"
Zendure/number/<SN>/inputLimit/set    → charge_power
```
On exit it sets `inputLimit` to 0 and `acMode` back to Output. The zero-export regulation pauses while charging.

Self-healing, both directions: if the device's `acMode` mirror doesn't match the mode charge44 needs (a dropped command), the command is repeated at most every 10 s. Unloading the integration mid-charge ends the charge; after a restart, a device left in Input mode that charge44 doesn't want charging is reset once — even with the regulation off.

## Known limitations

- Single instance only (`single_instance_allowed`)
- Tested on **Zendure 800 Pro** + **Shelly 3EM Pro** only; other devices with the same MQTT topic structure should work but aren't verified
- Battery wear isn't priced into the plan; holidays count as their weekday in the load profile

## Tests

The decision logic is covered by `pytest`. Locally:

```bash
pip install pytest
pytest tests/ -v
```

Suites:
- `tests/test_optimizer.py` — the DP: charging only when it pays, holding for pricier slots, partial charges, PV, negative prices, SOC limits; sun-bow PV distribution
- `tests/test_load_profile.py` — weekday × hour profile, learning, fallbacks
- `tests/test_standby.py` — standby-drain windows, BMS corrections, averaging, loss split / energy-balance fit
- `tests/test_plan_execution.py` — planning inputs, executing the current slot, re-plan triggers, ledger, measured round trip, load learning
- `tests/test_regulation.py` — PI loop + optimizer hold (safety blocks, deadzone, acMode self-heal, publish pacing)
- `tests/test_publish.py` — MQTT topics + payloads (grid-charge commands, minSoc forwarding)
- `tests/test_charge_control.py` — services, stop_charge suppression, unload/restart handling, stalled-charge detection, reserve top-up
- `tests/test_state_handling.py` — health, restored daily counters, change-only updates

CI runs on every push and PR — see `.github/workflows/tests.yml`.

## Releases

See [GitHub Releases](https://github.com/mavnezz/Zendure-Charge44/releases).

## License

MIT
