from pathlib import Path

import pytest

from telemetry import rules, scenario

T = rules.load_thresholds(Path(__file__).parent.parent / "config" / "thresholds.toml")


def _m(**kw):
    base = {
        "dev_eui": "x",
        "name": "d",
        "uplink_count": 12,
        "rssi_median": -86.0,
        "rssi_min": -88,
        "battery_min_mv": 3190,
        "battery_drop_mv": 110,
    }
    base.update(kw)
    return rules.DeviceMetrics(**base)


def test_thresholds_are_loaded_from_the_single_file():
    assert T.rssi_min_dbm == -110
    assert T.battery_min_mv == 3000


def test_good_device_passes():
    v = rules.evaluate(_m(), T)
    assert v.status == rules.PASS and v.reasons == []


@pytest.mark.parametrize(
    ("override", "keyword"),
    [
        ({"fcnt_gaps": 1, "fcnt_missing": 3}, "fcnt"),
        ({"time_gaps": 1, "max_gap_s": 2000.0}, "time gaps"),
        ({"rssi_median": -117.0}, "rssi median"),
        ({"rssi_min": -111}, "rssi minimum"),
        ({"battery_min_mv": 2900}, "battery minimum"),
        ({"battery_drop_mv": 400}, "battery drop"),
        ({"uplink_count": 2}, "uplinks"),
    ],
)
def test_each_rule_fails_with_a_reason(override, keyword):
    v = rules.evaluate(_m(**override), T)
    assert v.status == rules.FAIL
    assert any(keyword in r for r in v.reasons)


def test_boundaries_pass():
    v = rules.evaluate(
        _m(
            rssi_min=-110,
            rssi_median=-105.0,
            battery_min_mv=3000,
            battery_drop_mv=200,
            uplink_count=5,
        ),
        T,
    )
    assert v.status == rules.PASS


def test_unknown_readings_are_not_failures():
    v = rules.evaluate(
        _m(rssi_median=None, rssi_min=None, battery_min_mv=None, battery_drop_mv=None), T
    )
    assert v.status == rules.PASS


def test_multiple_reasons_are_all_reported():
    v = rules.evaluate(_m(fcnt_missing=2, fcnt_gaps=1, rssi_median=-120.0), T)
    assert len(v.reasons) == 2


def test_scenario_shape():
    steps = scenario.steps()
    by = {n: [s for s in steps if s.device.name == n] for n in scenario.DEVICES}
    assert len(by["dev-ok"]) == 12 and len(by["dev-weak"]) == 12 and len(by["dev-battery"]) == 12
    assert len(by["dev-gap"]) == 9 and len(by["dev-quiet"]) == 8
    assert len(steps) == 53 and scenario.expected_uplinks() == 69
    assert max(s.rssi for s in by["dev-weak"]) < T.rssi_min_dbm
    assert min(s.rssi for s in by["dev-ok"]) >= T.rssi_min_dbm
    assert all(s.battery_mv >= T.battery_min_mv for s in steps)
    fc = [s.f_cnt for s in by["dev-gap"]]
    assert [b - a for a, b in zip(fc, fc[1:], strict=False) if b - a > 1] == [4]


def test_shell_scripts_have_no_carriage_return():
    # A CR inside a script (for example in tr -d '<CR>') breaks it on Linux and makes git treat
    # the file as binary. Write '\r' as backslash and r instead.
    import pathlib

    scripts = sorted((pathlib.Path(__file__).resolve().parent.parent / "scripts").glob("*.sh"))
    assert scripts
    for path in scripts:
        assert b"\r" not in path.read_bytes(), f"{path} contains a carriage return"
