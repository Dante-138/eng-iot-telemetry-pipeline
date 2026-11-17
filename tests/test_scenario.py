import re
from pathlib import Path

from telemetry import rules, scenario

ROOT = Path(__file__).parent.parent
T_E2E = rules.load_thresholds(ROOT / "config" / "thresholds.e2e.toml")
T_DEFAULT = rules.load_thresholds(ROOT / "config" / "thresholds.toml")


def _by_device():
    out: dict[str, list[scenario.Step]] = {}
    for s in scenario.steps() + scenario.otaa_steps():
        out.setdefault(s.device.name, []).append(s)
    return out


def _metrics(name, steps):
    """What the SQL in sql/approval would measure, derived from the scenario itself."""
    counters = [s.f_cnt for s in steps]
    jumps = [b - a - 1 for a, b in zip(counters, counters[1:], strict=False) if b - a > 1]
    pauses = [s.pause_before_s for s in steps]
    batteries = [s.battery_mv for s in steps]
    rssis = sorted(s.rssi for s in steps)
    return rules.DeviceMetrics(
        dev_eui=name,
        name=name,
        uplink_count=len(steps),
        fcnt_gaps=len(jumps),
        fcnt_missing=sum(jumps),
        time_gaps=sum(1 for p in pauses if p > T_E2E.max_interval_s),
        max_gap_s=max(pauses),
        rssi_median=float(rssis[len(rssis) // 2]),
        rssi_min=rssis[0],
        battery_min_mv=min(batteries),
        battery_drop_mv=batteries[0] - batteries[-1],
    )


def test_expected_verdicts_with_e2e_thresholds():
    verdicts = {n: rules.evaluate(_metrics(n, s), T_E2E) for n, s in _by_device().items()}
    status = {n: v.status for n, v in verdicts.items()}
    assert status == {
        "dev-ok": rules.PASS,
        "dev-gap": rules.FAIL,
        "dev-weak": rules.FAIL,
        "dev-quiet": rules.FAIL,
        "dev-battery": rules.FAIL,
        "dev-otaa": rules.PASS,
    }
    assert len(verdicts["dev-quiet"].reasons) == 1
    assert verdicts["dev-quiet"].reasons[0].startswith("time gaps")
    assert len(verdicts["dev-battery"].reasons) == 1
    assert verdicts["dev-battery"].reasons[0].startswith("battery drop")


def test_quiet_pause_is_between_the_two_thresholds():
    # Longer than the e2e limit (so it is a gap there), shorter than the default one.
    assert T_E2E.max_interval_s < scenario.QUIET_PAUSE_S < T_DEFAULT.max_interval_s


def test_e2e_script_expects_the_scenario_uplink_count():
    text = (ROOT / "scripts" / "e2e.sh").read_text()
    match = re.search(r"^EXPECTED_UPLINKS=(\d+)", text, re.M)
    assert match, "EXPECTED_UPLINKS not found in scripts/e2e.sh"
    assert int(match.group(1)) == scenario.expected_uplinks()
    frames = re.search(r"^EXPECTED_FRAMES=(\d+)", text, re.M)
    assert frames, "EXPECTED_FRAMES not found in scripts/e2e.sh"
    assert int(frames.group(1)) == scenario.expected_gateway_frames()


def test_dual_gateway_rows_pick_the_best_reception():
    rows = scenario.dual_gateway_rows()
    assert [r[0] for r in rows] == sorted(scenario.DUAL_FRAMES)
    best = {f: (gw, rssi) for f, gw, rssi in rows}
    # +10 dB at the second gateway: it wins. -10 dB: the first gateway keeps it.
    assert best[0] == (scenario.GATEWAY2_EUI.hex(), -75)
    assert best[3] == (scenario.GATEWAY_EUI.hex(), -88)
    assert scenario.expected_gateway_frames() == scenario.expected_uplinks() + 4 + 1


def test_only_dev_ok_is_heard_by_the_second_gateway_and_bursts_never_are():
    heard = {s.device.name for s in scenario.steps() if s.rssi2 is not None}
    assert heard == {"dev-ok"}
    assert all(s.rssi2 is None for s in scenario.burst_steps("dev-ok", 12))
