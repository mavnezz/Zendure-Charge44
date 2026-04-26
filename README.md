# Zendure-Charge44

[![Release](https://img.shields.io/github/v/release/mavnezz/Zendure-Charge44?style=for-the-badge)](https://github.com/mavnezz/Zendure-Charge44/releases)
[![License](https://img.shields.io/github/license/mavnezz/Zendure-Charge44?style=for-the-badge)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz)
[![Tests](https://img.shields.io/github/actions/workflow/status/mavnezz/Zendure-Charge44/tests.yml?branch=main&style=for-the-badge&label=tests)](https://github.com/mavnezz/Zendure-Charge44/actions/workflows/tests.yml)

Home Assistant custom integration that controls a **Zendure SolarFlow 800 Pro** over local MQTT. Combines dynamic zero-export regulation (against a Shelly 3EM Pro) with price-based grid charging (Tibber) and a solar-forecast skip (Forecast.Solar).

No cloud, no middleware — speaks directly to your local MQTT broker.

## What it does

### 1. Zero-export regulation
Reads live grid power from the Shelly 3EM Pro and adjusts the Zendure's `outputLimit` to keep the net flow near a configurable target (default 0 W).

### 2. Price-based grid charging (Cheap-Charge)
With a Tibber API token, the plugin pulls the next 24 h of prices. During the N cheapest hours it switches the Zendure to **Input mode** and pulls power through `inputLimit` — but only if the spread between the current price and the day's max covers round-trip losses.

### 3. Solar-forecast smart skip
If the `forecast_solar` integration is installed, the plugin compares expected remaining production to the kWh still needed to reach target SOC. **If the sun alone will fill the battery, no grid charging happens — even during a cheap window.**

### 4. Free-Charge / Manual-Charge / Smart-Discharge
- **Free-Charge** — auto-charges whenever the current price is ≤ 0 ct/kWh, ignoring solar forecast and cheap-window logic.
- **Manual-Charge** — one-shot grid-charge override that auto-disables once target SOC is reached.
- **Smart-Discharge** — pauses the regulation during the cheapest hours so the battery is preserved for higher-priced hours. Never exports to grid.

## Hardware

- Zendure SolarFlow 800 Pro with local MQTT enabled in the device settings. The device must publish on topics like `Zendure/sensor/<SN>/...` and listen on `.../set`.
- Shelly 3EM Pro (or compatible) publishing `total_act_power` on `<device_id>/events/rpc`.
- Both devices on the same MQTT broker as Home Assistant.
- (Optional) Tibber account + API token for price-based charging.
- (Optional) Forecast.Solar integration for the smart skip.

> **Required for Cheap-Charge / Free-Charge / Manual-Charge:** the device's **"On-grid Input Mode"** must be enabled in the Zendure mobile app, set to your desired charge power (e.g. 800 or 1000 W). Without this app-side setting the firmware accepts the MQTT quartet (`acMode=Input`, `inputLimit=...`) but never actually pulls from the grid — every status topic looks correct, yet `gridInputPower` stays at 0.

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
2. **Tibber + solar forecast** (both optional) — paste the API token, pick the solar sensor. Leave blank if you only want zero-export regulation.
3. **Tibber home** — only shown when the account has multiple homes.

## Entities

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
| `sensor.charge44_current_price` | current electricity price (ct/kWh) |
| `sensor.charge44_cheap_hour` | "yes"/"no" — is the current hour in the cheap window |
| `sensor.charge44_cheap_charge_active` | "on"/"off" — currently in a charging cycle |
| `sensor.charge44_next_cheap_window` | timestamp of the next cheap hour |
| `sensor.charge44_solar_forecast_remaining` | kWh of solar left today |
| `sensor.charge44_grid_charge_needed` | shortfall vs target SOC |
| `sensor.charge44_today_min_price` / `_max_price` | day min/max (ct/kWh) |
| `sensor.charge44_spread_now` | today's max minus current |
| `sensor.charge44_required_spread` | required spread (min-spread vs break-even) |
| `sensor.charge44_charge_profitable` | "yes"/"no" — would charging pay off |

### Sliders (numbers)
| Entity | Default | Meaning |
|---|---|---|
| `number.charge44_target_soc` | 80 % | upper bound for charging — friendly name **SOC Max** |
| `number.charge44_min_soc` | 10 % | discharge floor — friendly name **SOC Min** |

### Switches
- `switch.charge44_regulation` — zero-export regulation on/off (friendly name **0-Regulation**)
- `switch.charge44_cheap_charge` — automatic cheap-window charging (friendly name **Charge Cheap**)
- `switch.charge44_charge_when_free` — always charge when price ≤ 0 ct/kWh, ignoring solar forecast and cheap-window (only temperature and target-SOC guards still apply) (friendly name **Charge Free**)
- `switch.charge44_manual_charge` — immediate grid charge regardless of price; auto-disables when target SOC is reached. One-shot, doesn't survive an HA restart (friendly name **Charge Manual**)
- `switch.charge44_smart_discharge` — preserve battery during cheap hours (friendly name **Discharge Smart**)
- `switch.charge44_contiguous_block` — pick the cheapest contiguous N-hour block instead of the cheapest scattered N hours (config category)

## Logic

### Regulation (per Shelly tick, ~5 s)
```
error  = grid_power - grid_bias
setpoint += error × Kp
setpoint  = clamp(0, max_output)
→ Zendure/number/<SN>/outputLimit/set
```
Hysteresis: only publish when the change exceeds the deadzone (5 W) AND the last publish is ≥ 3 s old.

### Cheap-Charge decision (per minute)
```
cheap_hour      = current_price is in the cheapest N of the next 24 h
spread_now      = today_max - current_price
break_even      = current_price × (1 / efficiency - 1)
required_spread = max(min_spread_ct, break_even)
profitable      = spread_now ≥ required_spread

needed_kwh      = (target_soc - soc) / 100 × battery_capacity
forecast_kwh    = Forecast.Solar sensor (kWh remaining today)
gap             = max(0, needed_kwh - forecast_kwh)

charge if:      cheap_charge_enabled
              ∧ cheap_hour
              ∧ profitable
              ∧ soc < target_soc
              ∧ gap > 0

or:             charge_when_free ∧ current_price ≤ 0
              ∧ soc < target_soc
              (solar forecast and cheap-window are ignored)

or:             manual_charge ∧ soc < target_soc
              (everything except temperature + target SOC is ignored)
```

### Mode transitions
On entering cheap-charge mode the plugin publishes:
```
Zendure/select/<SN>/acMode/set        → "Input mode"
Zendure/number/<SN>/inputLimit/set    → charge_power
```
On exit it reverses both. The zero-export regulation pauses while cheap-mode is active.

### Smart-Discharge
Zero-export regulation always covers the home load only — it never actively exports (with a Tibber feed-in tariff near 0 ct/kWh, exporting is pure loss). Smart-Discharge **suspends** the regulation during the cheapest hours so the battery is preserved for normal/expensive ones:
```
if smart_discharge_enabled ∧ cheap_hour:
   outputLimit = 0              (battery idle, cheap grid covers the home)
else:
   outputLimit = PI loop        (battery covers home load, no export)
```
With Smart-Discharge OFF the regulation runs every hour — the battery always covers the home.

## Known limitations

- Single instance only (`single_instance_allowed`)
- Tested on **Zendure 800 Pro** + **Shelly 3EM Pro** only; other devices with the same MQTT topic structure should work but aren't verified
- No options-flow yet; to change the Tibber token or forecast entity, remove the integration and re-add it

## Tests

The decision logic is covered by `pytest`. Locally:

```bash
pip install pytest
pytest tests/ -v
```

Suites:
- `tests/test_want_cheap_charge.py` — decision matrix (temperature guard, SOC cap, free-charge override, manual-charge override, solar skip, cheap-charge gating)
- `tests/test_compute_is_cheap.py` — Tibber price evaluation (spread, break-even, top-N, contiguous-block mode, 15-minute slots)
- `tests/test_regulation.py` — PI loop + smart-discharge (safety blocks, deadzone, cheap-hour pause)
- `tests/test_publish.py` — MQTT topics + payloads (cheap-mode quartet, minSoc forwarding)

CI runs on every push and PR — see `.github/workflows/tests.yml`.

## Releases

See [GitHub Releases](https://github.com/mavnezz/Zendure-Charge44/releases).

## License

MIT
