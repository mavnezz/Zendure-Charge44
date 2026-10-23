"""Cost-optimal charge / hold / discharge schedule over the known price horizon.

Dynamic programming over the energy stored in the battery: for every price slot
pick the action that minimises the grid bill for the rest of the horizon, given
the expected house load and PV. The coordinator executes only the current slot
and re-plans every slot with the measured SOC (receding horizon), so forecast
errors don't pile up.

Actions, as the device executes them:
- NORMAL: zero-export regulation — PV covers the house first, the battery the
  rest (up to the inverter ceiling), PV surplus charges the battery.
- HOLD:   output 0 — the house runs on grid, PV still charges the battery; PV the
  battery can't take covers the house (Zendure "excess energy export").
- CHARGE: Input mode — the house runs on grid, grid + PV charge the battery.

Pure Python, no Home Assistant imports: runs in an executor and is unit-tested
directly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

NORMAL = "normal"
HOLD = "hold"
CHARGE = "charge"
# Order doubles as the tie-break: on equal cost prefer doing nothing special.
ACTIONS = (NORMAL, HOLD, CHARGE)

_TIE_EUR = 1e-5  # a different action must beat NORMAL by more than this
# Each switch into or out of grid charging (Input ↔ Output mode) costs this in
# the plan, so near-equal 15-min prices give one charge block instead of a
# string of short ones (2026-10-20 dry run: 3 fragments within 0.5 ct).
SWITCH_EUR = 0.005
# Below this many Wh under SOC Min the plan pays a prohibitive price: the hub's
# own draw keeps sinking a parked battery, and the plan must top it up (at the
# cheapest moment) or keep a buffer before it gets there — not leave it to the
# price-blind reserve top-up that kicks in one step further down.
_FLOOR_PENALTY_EUR_PER_WH = 0.01


@dataclass(frozen=True)
class Slot:
    start: datetime
    hours: float  # slot length; the current slot may be partial
    price: float  # EUR/kWh
    load_wh: float  # expected house consumption
    pv_wh: float  # expected PV production
    # The part of the load the inverter can cover: the load clipped to its
    # ceiling at every moment. Peaks raise the mean load, but the battery can't
    # follow them past the ceiling (2026-10-23: the profile said 1036 W for an
    # hour in which the house drew ~120 W). None: the mean load.
    coverable_wh: float | None = None


@dataclass(frozen=True)
class Battery:
    capacity_wh: float
    min_wh: float  # SOC Min — NORMAL never discharges below
    max_wh: float  # SOC Max — nothing charges above
    charge_w: float  # grid-charge power (inputLimit)
    output_w: float  # inverter AC ceiling
    efficiency: float  # conversion round trip, split evenly between in and out
    standby_w: float = 0.0  # hub's own draw from the battery, in every slot
    floor_wh: float | None = None  # stored energy the plan must not drift below


@dataclass(frozen=True)
class Step:
    start: datetime
    action: str
    energy_wh: float  # stored energy at the slot start
    price: float
    grid_wh: float  # expected grid import in the slot


def _feed_cap(slot: Slot, output_w: float) -> float:
    """Most the inverter can put into the house in the slot."""
    cap = output_w * slot.hours
    return cap if slot.coverable_wh is None else min(cap, slot.coverable_wh)


def _simulate(
    action: str, energy: float, slot: Slot, bat: Battery, eta_in: float, eta_out: float
) -> tuple[float, float]:
    """(stored energy after the slot, grid import in Wh)."""
    e2, grid = _flows(action, energy, slot, bat, eta_in, eta_out)
    # The hub's own draw comes out of the battery whatever the action.
    return max(0.0, e2 - bat.standby_w * slot.hours), grid


def _flows(
    action: str, energy: float, slot: Slot, bat: Battery, eta_in: float, eta_out: float
) -> tuple[float, float]:
    room = max(0.0, bat.max_wh - energy)
    load, pv = slot.load_wh, slot.pv_wh
    if action == CHARGE:
        pv_in = min(pv * eta_in, room)
        grid_charge = min(bat.charge_w * slot.hours, (room - pv_in) / eta_in)
        return energy + pv_in + grid_charge * eta_in, load + grid_charge
    if action == HOLD:
        pv_in = min(pv * eta_in, room)
        pv_left = pv - pv_in / eta_in
        return energy + pv_in, max(0.0, load - min(pv_left, _feed_cap(slot, bat.output_w)))
    ac_max = _feed_cap(slot, bat.output_w)
    pv_direct = min(pv, load, ac_max)
    energy += min((pv - pv_direct) * eta_in, room)
    deficit = load - pv_direct
    available = max(0.0, energy - bat.min_wh) * eta_out
    out = min(deficit, ac_max - pv_direct, available)
    return energy - out / eta_out, deficit - out


def _interp(values: list[float], energy: float, step_wh: float) -> float:
    pos = min(max(energy / step_wh, 0.0), len(values) - 1.0)
    i = int(pos)
    if i >= len(values) - 1:
        return values[-1]
    frac = pos - i
    return values[i] * (1.0 - frac) + values[i + 1] * frac


def _best(
    energy: float,
    charging: bool,
    slot: Slot,
    bat: Battery,
    eta_in: float,
    eta_out: float,
    nxt: tuple[list[float], list[float]],
    step_wh: float,
    switch_eur: float,
) -> tuple[float, str, float, float]:
    """(cost to go, action, next energy, grid Wh) of the cheapest action.

    `charging` is whether the previous slot was a grid charge; `nxt` holds the
    next slot's cost to go for (not charging, charging)."""
    best: tuple[float, str, float, float] | None = None
    for action in ACTIONS:
        e2, grid = _simulate(action, energy, slot, bat, eta_in, eta_out)
        now_charging = action == CHARGE
        cost = grid * slot.price / 1000.0 + _interp(nxt[now_charging], e2, step_wh)
        if bat.floor_wh is not None and e2 < bat.floor_wh:
            cost += (bat.floor_wh - e2) * _FLOOR_PENALTY_EUR_PER_WH
        if now_charging != charging:
            cost += switch_eur
        if best is None or cost < best[0] - _TIE_EUR:
            best = (cost, action, e2, grid)
    return best  # type: ignore[return-value]


def optimise(
    slots: list[Slot],
    bat: Battery,
    energy_wh: float,
    step_wh: float = 20.0,
    charging: bool = False,
    switch_eur: float = SWITCH_EUR,
) -> list[Step]:
    """Cheapest action per slot, starting from `energy_wh` stored; `charging`
    is whether the device is grid-charging right now."""
    if not slots or bat.capacity_wh <= 0:
        return []
    eta = min(1.0, max(0.5, bat.efficiency))
    eta_in = eta_out = math.sqrt(eta)
    states = [i * step_wh for i in range(int(math.ceil(bat.capacity_wh / step_wh)) + 1)]

    # Energy left at the end of the horizon still displaces grid power later,
    # but at prices nobody knows yet. Value it at the cheapest known price: an
    # average would make buying anything below it look like a deal and fill
    # the battery speculatively. New day-ahead prices extend the horizon and
    # the plan anyway.
    term_price = max(0.0, min(s.price for s in slots))
    idle = [-max(0.0, e - bat.min_wh) * eta_out * term_price / 1000.0 for e in states]
    # Ending the horizon mid-charge still owes the switch back to Output mode.
    layers = [(idle, [v + switch_eur for v in idle])]
    for slot in reversed(slots):
        nxt = layers[-1]
        layers.append(
            tuple(
                [
                    _best(e, flag, slot, bat, eta_in, eta_out, nxt, step_wh, switch_eur)[0]
                    for e in states
                ]
                for flag in (False, True)
            )
        )
    layers.reverse()  # layers[i] = cost to go from the start of slot i

    steps: list[Step] = []
    energy = min(max(energy_wh, 0.0), bat.capacity_wh)
    for i, slot in enumerate(slots):
        _, action, e2, grid = _best(
            energy, charging, slot, bat, eta_in, eta_out, layers[i + 1], step_wh, switch_eur
        )
        steps.append(Step(slot.start, action, energy, slot.price, grid))
        energy, charging = e2, action == CHARGE
    return steps


def baseline_cost(slots: list[Slot], output_w: float) -> float:
    """EUR the same horizon costs without a battery (PV still feeds the house)."""
    return sum(
        max(0.0, s.load_wh - min(s.pv_wh, _feed_cap(s, output_w))) * s.price / 1000.0
        for s in slots
    )


def sine_share(start: datetime, end: datetime, rise: datetime, set_: datetime) -> float:
    """Integral of a sun bow sin(pi * (t - rise) / L) over [start, end] ∩ [rise, set],
    in hours (so shares of one day add up to 2L/pi)."""
    a, b = max(start, rise), min(end, set_)
    if b <= a:
        return 0.0
    length = (set_ - rise).total_seconds()
    if length <= 0:
        return 0.0

    def phase(t: datetime) -> float:
        return math.pi * (t - rise).total_seconds() / length

    return length / math.pi * (math.cos(phase(a)) - math.cos(phase(b))) / 3600.0


def distribute_pv(
    bounds: list[tuple[datetime, datetime]],
    days: list[tuple[datetime, datetime, datetime, float]],
) -> list[float]:
    """Spread daily PV totals over slots along a sun bow.

    `bounds` are the slot (start, end) pairs; `days` are (from, rise, set, kWh):
    `kWh` is produced between max(from, rise) and set — "from" is now for the
    remaining-today forecast and midnight for tomorrow's total.
    """
    out = [0.0] * len(bounds)
    for from_, rise, set_, kwh in days:
        if kwh <= 0:
            continue
        total = sine_share(max(from_, rise), set_, rise, set_)
        if total <= 0:
            continue
        for i, (start, end) in enumerate(bounds):
            share = sine_share(max(start, from_), end, rise, set_)
            out[i] += kwh * 1000.0 * share / total
    return out


def slot_bounds(starts: list[datetime], minutes: int, now: datetime) -> list[tuple[datetime, datetime]]:
    """(start, end) per slot; the current one is clipped to start at `now`."""
    length = timedelta(minutes=minutes)
    return [(max(s, now), s + length) for s in starts]
