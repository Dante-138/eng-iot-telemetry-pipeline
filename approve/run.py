"""Device approval report: run the SQL in sql/approval and print PASS or FAIL.

Usage: python -m approve.run [--json] [--strict]
  --json    machine readable output
  --strict  exit 1 if any device fails (default exit 0 once the report is made)

Environment: POSTGRES_DSN (default matches docker-compose.yml, local only),
THRESHOLDS (default config/thresholds.toml).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from telemetry import rules

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql" / "approval"


def _run(conn: psycopg.Connection, name: str, params: dict) -> dict[str, dict]:
    sql = (SQL_DIR / name).read_text()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return {row["dev_eui"]: row for row in cur.fetchall()}


def collect(conn: psycopg.Connection, thresholds: rules.Thresholds) -> list[rules.DeviceMetrics]:
    params = {"max_interval_s": thresholds.max_interval_s}
    fcnt = _run(conn, "01_fcnt_gaps.sql", params)
    time_gaps = _run(conn, "02_time_gaps.sql", params)
    rssi = _run(conn, "03_rssi.sql", params)
    battery = _run(conn, "04_battery.sql", params)
    with conn.cursor() as cur:
        cur.execute("SELECT dev_eui, name FROM devices ORDER BY name")
        devices = cur.fetchall()

    out = []
    for dev_eui, name in devices:
        f, t, r, b = (
            fcnt.get(dev_eui, {}),
            time_gaps.get(dev_eui, {}),
            rssi.get(dev_eui, {}),
            battery.get(dev_eui, {}),
        )
        out.append(
            rules.DeviceMetrics(
                dev_eui=dev_eui,
                name=name,
                uplink_count=int(r.get("uplink_count", 0)),
                fcnt_gaps=int(f.get("fcnt_gaps", 0)),
                fcnt_missing=int(f.get("fcnt_missing", 0)),
                time_gaps=int(t.get("time_gaps", 0)),
                max_gap_s=float(t.get("max_gap_s", 0.0)),
                rssi_median=None if r.get("rssi_median") is None else float(r["rssi_median"]),
                rssi_min=r.get("rssi_min"),
                battery_min_mv=b.get("battery_min_mv"),
                battery_drop_mv=b.get("battery_drop_mv"),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    thresholds = rules.load_thresholds(
        os.environ.get("THRESHOLDS", ROOT / "config" / "thresholds.toml")
    )
    dsn = os.environ.get(
        "POSTGRES_DSN", "postgresql://chirpstack:chirpstack@postgres:5432/telemetry"
    )
    with psycopg.connect(dsn) as conn:
        verdicts = [rules.evaluate(m, thresholds) for m in collect(conn, thresholds)]

    if args.json:
        print(json.dumps([asdict(v) for v in verdicts], indent=2))
    else:
        for v in verdicts:
            print(f"{v.name:<10} {v.status}  {'; '.join(v.reasons)}".rstrip())
    failed = any(v.status == rules.FAIL for v in verdicts)
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
