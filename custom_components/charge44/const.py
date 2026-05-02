from __future__ import annotations

DOMAIN = "charge44"

CONF_ZENDURE_SN = "zendure_sn"
CONF_ZENDURE_BATTERY_SNS = "zendure_battery_sns"
CONF_SHELLY_ID = "shelly_id"
CONF_TIBBER_TOKEN = "tibber_token"
CONF_TIBBER_HOME_ID = "tibber_home_id"
CONF_FORECAST_ENTITY = "forecast_entity"

# Zendure topic patterns (state topics; append "/set" for commands)
TOPIC_ZENDURE_SENSOR = "Zendure/sensor/{sn}/{prop}"
TOPIC_ZENDURE_NUMBER = "Zendure/number/{sn}/{prop}"
TOPIC_ZENDURE_WRITE = "Zendure/{kind}/{sn}/{prop}/set"

# Shelly Pro 3EM publishes a NotifyStatus JSON to this topic ~every 5s
TOPIC_SHELLY_RPC = "{shelly_id}/events/rpc"

# Zendure properties we track
ZENDURE_SENSORS = (
    "electricLevel",
    "outputHomePower",
    "solarInputPower",
    "packInputPower",
    "outputPackPower",
    "hyperTmp",
    "packState",
    "packNum",
)
ZENDURE_NUMBERS = ("outputLimit", "inputLimit", "minSoc")

# Hardware / regulation defaults
DEFAULT_MAX_OUTPUT = 800       # W, Zendure 800 Pro hardware ceiling
DEFAULT_GRID_BIAS = 0          # W, target net grid flow (positive = allow import)
DEFAULT_MIN_SOC = 10           # %, stop discharging below this
DEFAULT_KP = 0.5               # P-controller gain
DEFAULT_DEADZONE = 5           # W, don't republish changes smaller than this
MIN_PUBLISH_INTERVAL = 3.0     # s, rate limit between outputLimit writes
STALE_GRID_AFTER = 15.0        # s, pause regulation if no fresh Shelly reading
SAFETY_TICK_INTERVAL = 10.0    # s, how often to check for Shelly silence
DEFAULT_FALLBACK_DISCHARGE = 0  # W, outputLimit when Shelly stops reporting

# Cheap-charge defaults
DEFAULT_CHEAP_HOURS = 6                   # charge during N cheapest hours of 24h
DEFAULT_TARGET_SOC = 80                   # stop charging when SOC reaches this
DEFAULT_CHARGE_POWER = 1000               # inputLimit (W) during cheap-charge — Zendure clamps to App's "On-grid Input Mode" value
DEFAULT_MIN_SPREAD_CT = 10.0              # ct/kWh min gap (today_max - current) for profitable charge
DEFAULT_EFFICIENCY = 85                   # % round-trip efficiency used to compute break-even
DEFAULT_EXPENSIVE_HOURS = 6               # discharge during N most expensive hours
DEFAULT_BATTERY_CAPACITY = 1.92           # kWh (1 × AB2000X)
DEFAULT_TEMP_LOW = 5.0                    # °C, pause below
DEFAULT_TEMP_HIGH = 45.0                  # °C, pause above
DRIFT_TOLERANCE_W = 15                    # W, allowed gap between commanded and observed outputLimit
DRIFT_WARN_THRESHOLD = 3                  # consecutive mismatches before health degrades
TIBBER_POLL_INTERVAL = 900                # s (15 min)

# Event names fired on the HA event bus
EVENT_CHEAP_CHARGE_STARTED = f"{DOMAIN}_cheap_charge_started"
EVENT_CHEAP_CHARGE_ENDED = f"{DOMAIN}_cheap_charge_ended"
EVENT_TEMPERATURE_GUARD = f"{DOMAIN}_temperature_guard"
EVENT_DRIFT_DETECTED = f"{DOMAIN}_drift_detected"

# Service names
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_TARGET_SOC = "set_target_soc"
SERVICE_REFRESH_PRICES = "refresh_prices"

# Tibber API
TIBBER_API_ENDPOINT = "https://api.tibber.com/v1-beta/gql"

# Zendure command values
AC_MODE_OUTPUT = "Output mode"
AC_MODE_INPUT = "Input mode"

SIGNAL_UPDATE = f"{DOMAIN}_update"
