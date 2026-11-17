"""Ingest service: MQTT up events -> InfluxDB (time series) and PostgreSQL.

Subscribes to the ChirpStack MQTT integration topic
``application/+/device/+/event/up`` (JSON), extracts the fields with
telemetry.chirpstack_event and stores them in both databases.

Environment (defaults match docker-compose.yml, local development only):
  MQTT_HOST, MQTT_PORT, MQTT_TOPIC
  POSTGRES_DSN
  INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt
import psycopg
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from telemetry import chirpstack_event

log = logging.getLogger("ingest")

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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sinks = Sinks()
    topic = os.environ.get("MQTT_TOPIC", "application/+/device/+/event/up")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="telemetry-ingest")

    def on_connect(cli, _userdata, _flags, reason_code, _props):
        log.info("MQTT connected (%s), subscribing to %s", reason_code, topic)
        cli.subscribe(topic, qos=0)

    def on_message(_cli, _userdata, msg):
        try:
            row = chirpstack_event.extract(json.loads(msg.payload))
        except (json.JSONDecodeError, chirpstack_event.EventError) as exc:
            log.warning("skipping %s: %s", msg.topic, exc)
            return
        for attempt in range(1, 4):
            try:
                sinks.store(row)
                log.info(
                    "stored %s fCnt=%d rssi=%s temp=%s",
                    row.device_name,
                    row.f_cnt,
                    row.rssi,
                    row.temperature_c,
                )
                return
            except Exception as exc:  # noqa: BLE001 - retry any sink failure
                log.warning("store failed (attempt %d): %s", attempt, exc)
                time.sleep(1)
        log.error("giving up on %s fCnt=%d", row.device_name, row.f_cnt)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(
        os.environ.get("MQTT_HOST", "mosquitto"), int(os.environ.get("MQTT_PORT", "1883"))
    )
    client.loop_forever(retry_first_connection=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
