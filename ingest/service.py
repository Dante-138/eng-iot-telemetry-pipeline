"""Ingest service: MQTT up events -> InfluxDB (time series) and PostgreSQL.

Subscribes to the ChirpStack MQTT integration topic ``application/+/device/+/event/up``
(JSON) and stores each event in both databases without losing it:

* QoS 1 with a persistent session (fixed client id, no clean session): while the service is
  down the broker queues the events and delivers them when it comes back.
* Manual acknowledgement. ``on_message`` only puts the message in a queue (it runs on the
  MQTT network thread and must never block). A worker thread stores it, retrying with
  backoff, and only then acknowledges it.
* After the last attempt the raw message and the error go to the ``failed_uplinks`` table
  and the message is acknowledged; ``python -m ingest.replay`` reprocesses that table. If even
  that fails the message is not acknowledged and the client reconnects, so the broker delivers
  it again. Duplicates are harmless: the uplinks insert has ON CONFLICT DO NOTHING.

Environment (defaults match docker-compose.yml, local development only):
  MQTT_HOST, MQTT_PORT, MQTT_TOPIC, MQTT_CLIENT_ID
  POSTGRES_DSN
  INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
  INGEST_ATTEMPTS (default 3), INGEST_BACKOFF_S (default 1.0)
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from ingest.sinks import Sinks
from telemetry import delivery

log = logging.getLogger("ingest")


@dataclass(frozen=True)
class Item:
    generation: int  # connection the message arrived on; its mid is only valid there
    mid: int
    qos: int
    topic: str
    payload: bytes


class Ingest:
    def __init__(self, sinks: Sinks, client: mqtt.Client) -> None:
        self.sinks = sinks
        self.client = client
        self.queue: queue.Queue[Item] = queue.Queue()
        self.generation = 0
        self.attempts = int(os.environ.get("INGEST_ATTEMPTS", "3"))
        self.backoff_s = float(os.environ.get("INGEST_BACKOFF_S", "1.0"))

    # --- network thread: nothing here may block ---
    def on_message(self, _cli, _userdata, msg: mqtt.MQTTMessage) -> None:
        self.queue.put(Item(self.generation, msg.mid, msg.qos, msg.topic, bytes(msg.payload)))

    def on_disconnect(self, *_args) -> None:
        # Unacknowledged messages are delivered again after the reconnect; their old
        # message ids must not be acknowledged on the new connection.
        self.generation += 1

    # --- worker thread ---
    def work(self) -> None:
        while True:
            item = self.queue.get()
            if item.generation != self.generation:
                continue  # the broker delivers it again on the current connection
            outcome = delivery.handle_message(
                item.payload,
                store=self.sinks.store,
                park=lambda payload, error, attempts, topic=item.topic: self.sinks.park(
                    topic, payload, error, attempts
                ),
                attempts=self.attempts,
                base_delay_s=self.backoff_s,
            )
            if outcome.status == delivery.STORED:
                log.info("stored %s (attempt %d)", item.topic, outcome.attempts)
            elif outcome.status == delivery.PARKED:
                log.error("parked %s in failed_uplinks: %s", item.topic, outcome.error)
            else:
                log.error("not acknowledged, reconnecting: %s", outcome.error)
                self.client.disconnect()  # loop_forever returns, main reconnects
                continue
            if item.generation == self.generation:
                self.client.ack(item.mid, item.qos)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sinks = Sinks()
    while True:  # PostgreSQL may still be starting
        try:
            sinks.ensure_schema()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("waiting for PostgreSQL: %s", exc)
            sinks.pg = None
            time.sleep(2)

    topic = os.environ.get("MQTT_TOPIC", "application/+/device/+/event/up")
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=os.environ.get("MQTT_CLIENT_ID", "telemetry-ingest"),
        clean_session=False,  # persistent session: the broker queues while we are away
        manual_ack=True,  # acknowledged by the worker after the write
    )
    ingest = Ingest(sinks, client)

    def on_connect(cli, _userdata, flags, reason_code, _props):
        log.info(
            "MQTT connected (%s, session present: %s), subscribing to %s",
            reason_code,
            flags.session_present,
            topic,
        )
        cli.subscribe(topic, qos=1)

    client.on_connect = on_connect
    client.on_message = ingest.on_message
    client.on_disconnect = ingest.on_disconnect
    threading.Thread(target=ingest.work, name="worker", daemon=True).start()

    host = os.environ.get("MQTT_HOST", "mosquitto")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    client.connect_async(host, port)
    while True:
        client.loop_forever(retry_first_connection=True)
        # loop_forever returns only after disconnect(): the worker asked for a redelivery.
        time.sleep(1)
        try:
            client.reconnect()
        except OSError as exc:
            log.warning("reconnect failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
