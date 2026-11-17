"""Approval rules: device metrics and thresholds in, verdict and reasons out.

Pure logic, no database or network. The metrics are computed by the SQL files
in ``sql/approval``; the thresholds come from ``config/thresholds.toml``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"


@dataclass(frozen=True)
class Thresholds:
    min_uplinks: int
    max_fcnt_missing: int
    max_interval_s: float
    rssi_min_dbm: float
    rssi_median_min_dbm: float
    battery_min_mv: int
    battery_max_drop_mv: int


def load_thresholds(path: str | Path) -> Thresholds:
    with open(path, "rb") as fh:
        return Thresholds(**tomllib.load(fh))


@dataclass(frozen=True)
class DeviceMetrics:
    dev_eui: str
    name: str
    uplink_count: int = 0
    fcnt_gaps: int = 0
    fcnt_missing: int = 0
    time_gaps: int = 0
    max_gap_s: float = 0.0
    rssi_median: float | None = None
    rssi_min: float | None = None
    battery_min_mv: int | None = None
    battery_drop_mv: int | None = None


@dataclass(frozen=True)
class Verdict:
    dev_eui: str
    name: str
    status: str
    reasons: list[str] = field(default_factory=list)


def evaluate(m: DeviceMetrics, t: Thresholds) -> Verdict:
    """FAIL with the list of reasons, or PASS with none."""
    reasons: list[str] = []
    if m.uplink_count < t.min_uplinks:
        reasons.append(f"uplinks: {m.uplink_count} received, at least {t.min_uplinks} required")
    if m.fcnt_missing > t.max_fcnt_missing:
        reasons.append(
            f"fcnt gaps: {m.fcnt_gaps} jump(s), {m.fcnt_missing} frame(s) missing "
            f"(allowed {t.max_fcnt_missing})"
        )
    if m.time_gaps > 0:
        reasons.append(
            f"time gaps: {m.time_gaps} interval(s) above {t.max_interval_s:g} s "
            f"(longest {m.max_gap_s:.0f} s)"
        )
    if m.rssi_median is not None and m.rssi_median < t.rssi_median_min_dbm:
        reasons.append(f"rssi median {m.rssi_median:.1f} dBm below {t.rssi_median_min_dbm:g} dBm")
    if m.rssi_min is not None and m.rssi_min < t.rssi_min_dbm:
        reasons.append(f"rssi minimum {m.rssi_min:g} dBm below {t.rssi_min_dbm:g} dBm")
    if m.battery_min_mv is not None and m.battery_min_mv < t.battery_min_mv:
        reasons.append(f"battery minimum {m.battery_min_mv} mV below {t.battery_min_mv} mV")
    if m.battery_drop_mv is not None and m.battery_drop_mv > t.battery_max_drop_mv:
        reasons.append(f"battery drop {m.battery_drop_mv} mV above {t.battery_max_drop_mv} mV")
    return Verdict(m.dev_eui, m.name, FAIL if reasons else PASS, reasons)
