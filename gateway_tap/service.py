"""Gateway tap: counts the frames each gateway hands to the bridge, per time slice.

Separate from the ingest service on purpose. It listens to the radio side of the pipeline
(``<prefix>/gateway/+/event/up``, published by the gateway bridge) and writes the measurement
``gateway_frames`` (tag ``gateway_id``, field ``frames``) to InfluxDB, without decoding the
payload. Grafana compares it with the rows of ``uplinks``: frames at the gateway and nothing
in the database means the radio side works and the data side is stalled (see the alert rule
in grafana/provisioning/alerting).

Environment (defaults match docker-compose.yml, local development only):
  MQTT_HOST, MQTT_PORT, GATEWAY_TOPIC_PREFIX (default eu868), FLUSH_S (default 2)
  INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from telemetry import gateway_tap

log = logging.getLogger("gateway-tap")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    prefix = os.environ.get("GATEWAY_TOPIC_PREFIX", "eu868")
    flush_s = float(os.environ.get("FLUSH_S", "2"))
    org = os.environ.get("INFLUX_ORG", "local-org")
    bucket = os.environ.get("INFLUX_BUCKET", "telemetry")
    influx = InfluxDBClient(
        url=os.environ.get("INFLUX_URL", "http://influxdb:8086"),
        token=os.environ.get("INFLUX_TOKEN", "local-dev-token"),
        org=org,
    )
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    counter = gateway_tap.FrameCounter()
    lock = threading.Lock()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="telemetry-gateway-tap")

    def on_connect(cli, _userdata, _flags, reason_code, _props):
        topic = f"{prefix}/gateway/+/event/up"
        log.info("MQTT connected (%s), subscribing to %s", reason_code, topic)
        cli.subscribe(topic, qos=0)

    def on_message(_cli, _userdata, msg):
        gateway_id = gateway_tap.gateway_id_from_topic(msg.topic, prefix)
        if gateway_id:
            with lock:
                counter.add(gateway_id)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(
        os.environ.get("MQTT_HOST", "mosquitto"), int(os.environ.get("MQTT_PORT", "1883"))
    )
    client.loop_start()

    while True:
        time.sleep(flush_s)
        with lock:
            counts = counter.drain()
        if not counts:
            continue
        now = datetime.now(UTC)
        points = [
            Point("gateway_frames").tag("gateway_id", gw).field("frames", n).time(now)
            for gw, n in counts.items()
        ]
        try:
            write_api.write(bucket=bucket, org=org, record=points)
            log.info("frames %s", counts)
        except Exception as exc:  # noqa: BLE001 - keep tapping, the counts of this slice are lost
            log.warning(
                "influx write failed, %d frame(s) not counted: %s", sum(counts.values()), exc
            )


if __name__ == "__main__":
    sys.exit(main())
