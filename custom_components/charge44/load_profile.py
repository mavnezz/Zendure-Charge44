"""House load per weekday and hour (7 × 24 averages).

Seeded from recorder statistics at startup and learned online from the Shelly /
Zendure readings. Each weekday-hour sees one new value a week; after the first
few it moves as an exponential average with weight LEARN_WEIGHT, i.e. a memory
of roughly four weeks. The coordinator keeps a second profile of the same load
clipped to the inverter ceiling at every reading: the share the battery can
cover. That one can't be rebuilt from hourly statistics, so it is persisted
(to_dict / restore). Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

LEARN_WEIGHT = 0.25
DEFAULT_W = 300.0
_MIN_HOUR_COVER = 0.5  # h of samples needed before an hour counts
_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


class LoadProfile:
    def __init__(self) -> None:
        self._bins: dict[tuple[int, int], tuple[float, int]] = {}
        self._acc: datetime | None = None  # start of the hour being accumulated
        self._acc_wh = 0.0
        self._acc_h = 0.0
        self.last_hour: datetime | None = None  # latest hour folded in via add()

    def add_hour(self, weekday: int, hour: int, watts: float) -> None:
        """Fold one hour's average load into its weekday-hour bin."""
        value, n = self._bins.get((weekday, hour), (watts, 0))
        n += 1
        weight = max(1.0 / n, LEARN_WEIGHT)
        self._bins[(weekday, hour)] = (value + weight * (watts - value), n)

    def add(self, start: datetime, watts: float) -> None:
        """add_hour for the hour starting at `start`, remembering the latest."""
        self.add_hour(start.weekday(), start.hour, watts)
        if self.last_hour is None or start > self.last_hour:
            self.last_hour = start

    def sample(self, when: datetime, watts: float, dt_h: float) -> bool:
        """Accumulate a live reading; a finished hour is folded in on rollover
        (and True returned)."""
        start = when.replace(minute=0, second=0, microsecond=0)
        folded = False
        if self._acc is not None and start != self._acc:
            if self._acc_h >= _MIN_HOUR_COVER:
                self.add(self._acc, self._acc_wh / self._acc_h)
                folded = True
            self._acc_wh = self._acc_h = 0.0
        self._acc = start
        self._acc_wh += watts * dt_h
        self._acc_h += dt_h
        return folded

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "bins": [[d, h, round(v, 1), n] for (d, h), (v, n) in sorted(self._bins.items())],
            "last_hour": None if self.last_hour is None else self.last_hour.isoformat(),
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Take back what to_dict gave; raises on malformed data."""
        bins = {(int(d), int(h)): (float(v), int(n)) for d, h, v, n in data["bins"]}
        last = data.get("last_hour")
        self._bins = bins
        if last and (self.last_hour is None or datetime.fromisoformat(last) > self.last_hour):
            self.last_hour = datetime.fromisoformat(last)


def hourly_means(
    readings: Iterable[tuple[datetime, float]], reading_h: float
) -> list[tuple[datetime, float]]:
    """Fold equal-length readings (local start, W), in time order, into
    (hour start, mean W); hours with less than half an hour of readings are
    left out, like in LoadProfile.sample."""
    out: list[tuple[datetime, float]] = []
    hour: datetime | None = None
    total = 0.0
    count = 0

    def fold() -> None:
        if hour is not None and count * reading_h >= _MIN_HOUR_COVER:
            out.append((hour, total / count))

    for when, watts in readings:
        start = when.replace(minute=0, second=0, microsecond=0)
        if start != hour:
            fold()
            hour, total, count = start, 0.0, 0
        total += watts
        count += 1
    fold()
    return out
