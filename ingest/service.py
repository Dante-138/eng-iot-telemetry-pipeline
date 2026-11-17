"""Ingest service: MQTT up events -> InfluxDB (time series) and PostgreSQL.

Subscribes to the ChirpStack MQTT integration topic ``application/+/device/+/event/up``
(JSON) and stores each event in both databases without losing it:

* QoS 1 with a persistent session (fixed client id, no clean session): while the service is
  down the broker queues the events and delivers them when it comes back.
* Manual acknowledgement. ``on_message`` only puts the message in a queue (it runs on the
  MQTT network thread and must never block). A worker thread stores it, retrying with
  backoff, and hands (message, outcome) back through a second queue. The worker never
  touches the MQTT client: the paho client is not safe to call from another thread when it
  runs with ``loop()`` (a write failure runs ``on_disconnect`` in the calling thread). The
  main thread drives ``client.loop()`` and, after each turn, ``flush_acks()`` acknowledges.
* After the last attempt the raw message and the error go to the ``failed_uplinks`` table
  and the message is acknowledged; ``python -m ingest.replay`` reprocesses that table. If even
  that fails the message is not acknowledged and the client reconnects, so the broker delivers
  it again (the rest of that connection's messages are not acknowledged either). Duplicates
  are harmless: the uplinks insert has ON CONFLICT DO NOTHING. A message taken from an older
  connection is still stored (the broker may have lost its session) but its id is not
  acknowledged. ``generation`` is read and written by the network thread only.

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
        self.queue: queue.Queue[Item] = queue.Queue()  # network thread -> worker
        self.acks: queue.Queue[tuple[Item, delivery.Outcome]] = queue.Queue()  # worker -> network
        self.generation = 0  # network thread only
        self._stopped_generation: int | None = None  # network thread only
        self.attempts = int(os.environ.get("INGEST_ATTEMPTS", "3"))
        self.backoff_s = float(os.environ.get("INGEST_BACKOFF_S", "1.0"))

    # --- network thread: nothing here may block ---
    def on_message(self, _cli, _userdata, msg: mqtt.MQTTMessage) -> None:
        self.queue.put(Item(self.generation, msg.mid, msg.qos, msg.topic, bytes(msg.payload)))

    def on_disconnect(self, *_args) -> None:
        # Unacknowledged messages are delivered again after the reconnect; their old
        # message ids must not be acknowledged on the new connection.
        self.generation += 1

    def flush_acks(self) -> None:
        """Acknowledge what the worker finished. Call it from the network thread only."""
        while True:
            try:
                item, outcome = self.acks.get_nowait()
            except queue.Empty:
                return
            if item.generation != self.generation:
                # The connection it came from is gone (and the current one may be healthy).
                if outcome.status == delivery.UNACKED:
                    log.error("not acknowledged (old connection): %s", outcome.error)
                continue
            if item.generation == self._stopped_generation:
                continue  # the reconnect is under way: the broker delivers these again
            if outcome.status == delivery.UNACKED:
                log.error("not acknowledged, reconnecting: %s", outcome.error)
                self._stopped_generation = item.generation
                self.client.disconnect()  # loop() fails, main reconnects
            else:
                self.client.ack(item.mid, item.qos)

    # --- worker thread: never touches the client ---
    def work(self) -> None:
        while True:
            self.process(self.queue.get())

    def process(self, item: Item) -> None:
        """Store one message and report the outcome. Always written, even when it arrived on an
        older connection: the insert is idempotent, and after a broker restart without its
        session nobody would deliver it again. Only the acknowledgement depends on the
        connection, and flush_acks decides it."""
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
        self.acks.put((item, outcome))


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
        manual_ack=True,  # acknowledged by flush_acks after the worker wrote it
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
    while True:  # the broker may still be starting
        try:
            client.connect(host, port)
            break
        except OSError as exc:
            log.warning("waiting for MQTT %s:%d: %s", host, port, exc)
            time.sleep(2)
    # One thread drives the client: loop() here, flush_acks() after every turn.
    while True:
        rc = client.loop(timeout=0.2)
        ingest.flush_acks()
        if rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("MQTT loop returned %s, reconnecting in 1 s", mqtt.error_string(rc))
            time.sleep(1)
            try:
                client.reconnect()
            except OSError as exc:
                log.warning("reconnect failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
