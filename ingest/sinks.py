"""Storage side of the ingest service: PostgreSQL, InfluxDB and the dead letter table.

Shared by ``ingest.service`` (live messages) and ``ingest.replay`` (parked messages).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from telemetry import chirpstack_event

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

UPSERT_DEVICE = """
INSERT INTO devices (dev_eui, name, first_seen, last_seen)
VALUES (%(dev_eui)s, %(name)s, %(at)s, %(at)s)
ON CONFLICT (dev_eui) DO UPDATE SET
    name = EXCLUDED.name,
    first_seen = LEAST(devices.first_seen, EXCLUDED.first_seen),
    last_seen = GREATEST(devices.last_seen, EXCLUDED.last_seen)
"""

INSERT_UPLINK = """
INSERT INTO uplinks (dev_eui, received_at, f_cnt, rssi, snr, temperature_c, humidity_pct,
                     battery_mv, gateway_id)
VALUES (%(dev_eui)s, %(at)s, %(f_cnt)s, %(rssi)s, %(snr)s, %(temperature_c)s, %(humidity_pct)s,
        %(battery_mv)s, %(gateway_id)s)
ON CONFLICT (dev_eui, f_cnt, received_at) DO NOTHING
"""

PARK = """
INSERT INTO failed_uplinks (topic, payload, error, attempts) VALUES (%s, %s, %s, %s)
"""


class Sinks:
    """PostgreSQL and InfluxDB writers, reconnecting when needed."""

    def __init__(self) -> None:
        self.dsn = os.environ.get(
            "POSTGRES_DSN", "postgresql://chirpstack:chirpstack@postgres:5432/telemetry"
        )
        self.influx = InfluxDBClient(
            url=os.environ.get("INFLUX_URL", "http://influxdb:8086"),
            token=os.environ.get("INFLUX_TOKEN", "local-dev-token"),
            org=os.environ.get("INFLUX_ORG", "local-org"),
        )
        self.org = os.environ.get("INFLUX_ORG", "local-org")
        self.bucket = os.environ.get("INFLUX_BUCKET", "telemetry")
        self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)
        self.pg: psycopg.Connection | None = None

    def _pg(self) -> psycopg.Connection:
        if self.pg is None or self.pg.closed:
            self.pg = psycopg.connect(self.dsn, autocommit=True)
        return self.pg

    def ensure_schema(self) -> None:
        """Apply sql/schema.sql (every statement is IF NOT EXISTS), for volumes older than it."""
        self._pg().execute(SCHEMA.read_text())

    def park(self, topic: str, payload: bytes, error: str, attempts: int) -> None:
        """Keep a message that could not be stored. Raises if the database is unreachable."""
        try:
            self._pg().execute(PARK, (topic, payload, error, attempts))
        except psycopg.OperationalError:
            self.pg = None
            raise

    def store(self, row: chirpstack_event.UplinkRow) -> None:
        params = {
            "dev_eui": row.dev_eui,
            "name": row.device_name,
            "at": row.received_at,
            "f_cnt": row.f_cnt,
            "rssi": row.rssi,
            "snr": row.snr,
            "temperature_c": row.temperature_c,
            "humidity_pct": row.humidity_pct,
            "battery_mv": row.battery_mv,
            "gateway_id": row.gateway_id,
        }
        try:
            with self._pg().transaction():
                cur = self._pg().cursor()
                cur.execute(UPSERT_DEVICE, params)
                cur.execute(INSERT_UPLINK, params)
        except psycopg.OperationalError:
            self.pg = None
            raise

        point = (
            Point("uplink")
            .tag("dev_eui", row.dev_eui)
            .tag("device", row.device_name)
            .time(row.received_at)
            .field("f_cnt", row.f_cnt)
        )
        if row.gateway_id:
            point = point.tag("gateway_id", row.gateway_id)
        for name in ("rssi", "snr", "temperature_c", "humidity_pct", "battery_mv"):
            value = getattr(row, name)
            if value is not None:
                point = point.field(name, float(value))
        self.write_api.write(bucket=self.bucket, org=self.org, record=point)
