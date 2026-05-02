"""Battery capacity auto-detection.

Two sources: the SN list configured at setup (sums known per-model capacities),
and the live `packNum` MQTT topic (assumes AB2000X). When they disagree the
user has almost certainly added/removed a pack without re-doing setup —
prefer packNum so the value reflects reality.
"""
from __future__ import annotations


def test_packNum_alone_uses_192_per_pack(coord):
    coord._battery_sns = []
    coord._update_battery_capacity(2)
    assert coord.state.battery_capacity == 3.84

    coord._update_battery_capacity(1)
    assert coord.state.battery_capacity == 1.92


def test_sn_list_sums_known_capacities(coord):
    """Two C-prefix packs (1.92 each) → 3.84 kWh."""
    coord._battery_sns = ["CO4EENJJN381071", "CO4ELNJ3N449248"]
    coord._update_battery_capacity(2)
    assert coord.state.battery_capacity == 3.84


def test_sn_list_with_mixed_models(coord):
    """C-prefix (1.92) + F-prefix (2.88) → 4.80 kWh."""
    coord._battery_sns = ["CO4EENJJN381071", "FO4XXXXXXX111111"]
    coord._update_battery_capacity(2)
    assert coord.state.battery_capacity == 4.80


def test_packNum_overrides_stale_sn_list(coord):
    """User added a 2nd battery without re-running setup. The SN list still
    has only 1 entry. Trust the live packNum value (= 2)."""
    coord._battery_sns = ["CO4EENJJN381071"]
    coord._update_battery_capacity(2)
    assert coord.state.battery_capacity == 3.84  # not 1.92


def test_packNum_overrides_when_user_removed_pack(coord):
    """User unplugged a battery. SN list says 2, packNum says 1."""
    coord._battery_sns = ["CO4EENJJN381071", "CO4ELNJ3N449248"]
    coord._update_battery_capacity(1)
    assert coord.state.battery_capacity == 1.92


def test_no_packNum_falls_back_to_sn_list(coord):
    """First call from __init__ — no packNum yet, but SN list has data."""
    coord._battery_sns = ["CO4EENJJN381071"]
    coord._update_battery_capacity(None)
    assert coord.state.battery_capacity == 1.92


def test_zero_packNum_is_ignored(coord):
    coord._battery_sns = []
    coord.state.battery_capacity = 99.0  # sentinel
    coord._update_battery_capacity(0)
    assert coord.state.battery_capacity == 99.0  # unchanged
