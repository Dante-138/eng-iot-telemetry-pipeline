"""Regenerate grafana/dashboards/telemetry.json (kept as a script for readability)."""

import json
from pathlib import Path

PG = {"type": "postgres", "uid": "pg-telemetry"}
INFLUX = {"type": "influxdb", "uid": "influx-telemetry"}

FLUX_TEMPERATURE = (
    'from(bucket: "telemetry")\n'
    "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
    '  |> filter(fn: (r) => r._measurement == "uplink" and r._field == "temperature_c")\n'
    '  |> keep(columns: ["_time", "_value", "device"])\n'
    '  |> group(columns: ["device"])'
)

SQL_RSSI = (
    "SELECT u.received_at AS time, d.name AS metric, u.rssi AS value\n"
    "FROM uplinks u JOIN devices d USING (dev_eui)\n"
    "WHERE $__timeFilter(u.received_at) AND u.rssi IS NOT NULL\n"
    "ORDER BY 1"
)

SQL_PER_HOUR = (
    "SELECT date_trunc('hour', u.received_at) AS time, d.name AS metric, COUNT(*) AS value\n"
    "FROM uplinks u JOIN devices d USING (dev_eui)\n"
    "WHERE $__timeFilter(u.received_at)\n"
    "GROUP BY 1, 2\n"
    "ORDER BY 1"
)

SQL_GAPS = (
    "WITH s AS (\n"
    "  SELECT d.name AS device, u.received_at, u.f_cnt,\n"
    "         u.f_cnt - LAG(u.f_cnt) OVER (PARTITION BY u.dev_eui ORDER BY u.received_at, u.f_cnt) AS step\n"
    "  FROM uplinks u JOIN devices d USING (dev_eui)\n"
    "  WHERE $__timeFilter(u.received_at)\n"
    ")\n"
    'SELECT device, received_at AS "received after the gap", f_cnt - step + 1 AS "first missing fCnt",\n'
    '       f_cnt - 1 AS "last missing fCnt", step - 1 AS "missing frames"\n'
    "FROM s WHERE step > 1 ORDER BY received_at"
)


def ts(title, pos, ds, target, unit, draw="line"):
    return {
        "type": "timeseries",
        "title": title,
        "gridPos": pos,
        "datasource": ds,
        "targets": [target],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"drawStyle": draw, "lineWidth": 1, "showPoints": "auto"},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }


def sql_target(sql, fmt):
    return {
        "refId": "A",
        "datasource": PG,
        "editorMode": "code",
        "format": fmt,
        "rawQuery": True,
        "rawSql": sql,
    }


panels = [
    ts(
        "Temperature per device",
        {"x": 0, "y": 0, "w": 12, "h": 8},
        INFLUX,
        {"refId": "A", "datasource": INFLUX, "query": FLUX_TEMPERATURE},
        "celsius",
    ),
    ts(
        "RSSI per device",
        {"x": 12, "y": 0, "w": 12, "h": 8},
        PG,
        sql_target(SQL_RSSI, "time_series"),
        "dBm",
        "points",
    ),
    ts(
        "Uplinks per hour",
        {"x": 0, "y": 8, "w": 12, "h": 8},
        PG,
        sql_target(SQL_PER_HOUR, "time_series"),
        "short",
        "bars",
    ),
    {
        "type": "table",
        "title": "Frame counter gaps",
        "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
        "datasource": PG,
        "targets": [sql_target(SQL_GAPS, "table")],
        "fieldConfig": {"defaults": {}, "overrides": []},
    },
]
for i, p in enumerate(panels, start=1):
    p["id"] = i

dashboard = {
    "uid": "telemetry-overview",
    "title": "Telemetry overview",
    "tags": ["telemetry"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
}

out = Path(__file__).resolve().parent.parent / "grafana" / "dashboards" / "telemetry.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out}")
