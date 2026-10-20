"""House load per weekday and hour (7 × 24 averages).

Seeded from recorder statistics at startup and learned online from the Shelly /
Zendure readings. Each weekday-hour sees one new value a week; after the first
few it moves as an exponential average with weight LEARN_WEIGHT, i.e. a memory
of roughly four weeks. Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

from datetime import date, datetime

LEARN_WEIGHT = 0.25
DEFAULT_W = 300.0
_MIN_HOUR_COVER = 0.5  # h of samples needed before an hour counts
_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


class LoadProfile:
    def __init__(self) -> None:
        self._bins: dict[tuple[int, int], tuple[float, int]] = {}
        self._acc: tuple[int, int, date] | None = None
        self._acc_wh = 0.0
        self._acc_h = 0.0

    def add_hour(self, weekday: int, hour: int, watts: float) -> None:
        """Fold one hour's average load into its weekday-hour bin."""
        value, n = self._bins.get((weekday, hour), (watts, 0))
        n += 1
        weight = max(1.0 / n, LEARN_WEIGHT)
        self._bins[(weekday, hour)] = (value + weight * (watts - value), n)

    def sample(self, when: datetime, watts: float, dt_h: float) -> None:
        """Accumulate a live reading; a finished hour is folded in on rollover."""
        key = (when.weekday(), when.hour, when.date())
        if self._acc is not None and key != self._acc:
            if self._acc_h >= _MIN_HOUR_COVER:
                self.add_hour(self._acc[0], self._acc[1], self._acc_wh / self._acc_h)
            self._acc_wh = self._acc_h = 0.0
        self._acc = key
        self._acc_wh += watts * dt_h
        self._acc_h += dt_h

    def get(self, weekday: int, hour: int) -> float:
        """Expected load in W; falls back to the same hour on other days, then
        to the overall average, then to DEFAULT_W."""
        if (weekday, hour) in self._bins:
            return self._bins[(weekday, hour)][0]
        same_hour = [v for (_, h), (v, _) in self._bins.items() if h == hour]
        if same_hour:
            return sum(same_hour) / len(same_hour)
        if self._bins:
            return sum(v for v, _ in self._bins.values()) / len(self._bins)
        return DEFAULT_W

    def table(self) -> dict[str, list[float]]:
        """{"Mo": [24 W values], ...} — for the sensor attribute."""
        return {
            name: [round(self.get(d, h)) for h in range(24)]
            for d, name in enumerate(_DAYS)
        }

    @property
    def filled(self) -> int:
        return len(self._bins)
