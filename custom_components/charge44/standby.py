"""Measures the hub's own consumption from the battery ("standby drain").

The Zendure reports its conversions as lossless, so what the hub itself draws
never shows up in the power readings — only as the SOC sinking while nothing
flows. While the battery is idle (no charge, no discharge, no PV, no grid
input) every SOC step down is standby drain. Timing is taken from the first to
the last 1 % step of an idle stretch, so the 1 % resolution doesn't skew short
windows. A drop of more than 1 % at once, or steps faster than one per
MIN_STEP_GAP_S, is a BMS correction, not drain — the window restarts there.

Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

MIN_STEPS = 1  # % of SOC a window must span
MIN_HOURS = 0.75  # ...between its first and last step
MIN_STEP_GAP_S = 600.0  # idle drain is far slower than 1 % per 10 min
AVERAGE_OVER = 5  # running mean of the first windows, then EMA with weight 1/5


class StandbyMeter:
    def __init__(self) -> None:
        self.value_w: float | None = None
        self.windows = 0
        self.last_w: float | None = None
        self.last_hours: float | None = None
        self._seen: tuple[float, int] | None = None  # last reading while idle
        self._anchor: tuple[float, int] | None = None  # first step of the window
        self._tail: tuple[float, int] | None = None  # latest step

    def restore(self, value_w: float, windows: int) -> None:
        self.value_w = value_w
        self.windows = max(1, windows)

    def update(self, ts: float, soc: int | None, idle: bool, capacity_wh: float) -> None:
        """Feed one reading (`ts` in seconds, monotonic)."""
        if soc is None or not idle:
            self._close(capacity_wh)
            return
        if self._seen is None:
            self._seen = (ts, soc)
            return
        prev_soc = self._seen[1]
        if soc > prev_soc:
            self._anchor = self._tail = None  # rising while "idle": not drain
        elif soc < prev_soc:
            correction = prev_soc - soc > 1 or (
                self._tail is not None and ts - self._tail[0] < MIN_STEP_GAP_S
            )
            if self._anchor is None or correction:
                self._anchor = self._tail = (ts, soc)
            else:
                self._tail = (ts, soc)
        self._seen = (ts, soc)

    def _close(self, capacity_wh: float) -> None:
        anchor, tail = self._anchor, self._tail
        self._seen = self._anchor = self._tail = None
        if anchor is None or tail is None or capacity_wh <= 0:
            return
        steps = anchor[1] - tail[1]
        hours = (tail[0] - anchor[0]) / 3600.0
        if steps < MIN_STEPS or hours < MIN_HOURS:
            return
        watts = steps / 100.0 * capacity_wh / hours
        self.windows += 1
        self.last_w = round(watts, 1)
        self.last_hours = round(hours, 1)
        if self.value_w is None:
            self.value_w = watts
        else:
            self.value_w += (watts - self.value_w) / min(self.windows, AVERAGE_OVER)


def fit_losses(
    charged_kwh: float,
    discharged_kwh: float,
    hours: float,
    standby_w: float,
    stored_delta_kwh: float,
    ceiling: float,
) -> tuple[float, float] | None:
    """Split the battery's measured losses into conversion round trip and the
    hub's own draw: (efficiency, standby W).

    Losses over the window = energy in − energy out − extra still stored at the
    end. With `standby_w` from the meter, the conversion round trip is
    1 − (losses − standby) / in. The energy balance is the hard number: if the
    meter's standby alone exceeds what went missing (round trip above
    `ceiling`, which cells plus inverter can't beat), the round trip is capped
    and the standby fitted to the balance instead. None without data.
    """
    if charged_kwh <= 0 or hours <= 0:
        return None
    losses = charged_kwh - discharged_kwh - stored_delta_kwh
    eff = 1.0 - (losses - standby_w * hours / 1000.0) / charged_kwh
    if eff > ceiling:
        eff = ceiling
        standby_w = max(0.0, (losses - (1.0 - eff) * charged_kwh) * 1000.0 / hours)
    return max(0.5, eff), standby_w
