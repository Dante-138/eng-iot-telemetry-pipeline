"""Delivery policy of the ingest service: retry with backoff, then park, then ack.

Pure logic: the caller injects the storage functions and the sleep, so it is tested
without a broker or a database. The rule for an MQTT message with manual acknowledgement:

* stored: acknowledge.
* every attempt failed, but the raw message and the error were parked in the dead letter
  table: acknowledge (nothing is lost, ``python -m ingest.replay`` can reprocess it).
* neither storing nor parking worked: do NOT acknowledge, so the broker delivers the
  message again when the client reconnects.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from time import sleep as _sleep

from telemetry import chirpstack_event

STORED = "stored"
PARKED = "parked"
UNACKED = "unacked"


@dataclass(frozen=True)
class Outcome:
    status: str  # STORED, PARKED or UNACKED
    attempts: int
    error: str | None = None

    @property
    def ack(self) -> bool:
        return self.status in (STORED, PARKED)


def backoff_delays(attempts: int, base_s: float, cap_s: float = 30.0) -> list[float]:
    """Pauses between attempts: base, 2 x base, 4 x base ... capped. One fewer than attempts."""
    return [min(base_s * 2**i, cap_s) for i in range(max(attempts - 1, 0))]


def handle_message(
    payload: bytes,
    *,
    store: Callable[[chirpstack_event.UplinkRow], None],
    park: Callable[[bytes, str, int], None],
    attempts: int = 3,
    base_delay_s: float = 1.0,
    sleep: Callable[[float], None] = _sleep,
) -> Outcome:
    """Store one up event, retrying, and decide whether the message may be acknowledged.

    ``park(payload, error, attempts)`` writes the dead letter row and may raise.
    """
    try:
        row = chirpstack_event.extract(json.loads(payload))
    except (ValueError, TypeError) as exc:  # JSONDecodeError and EventError are ValueErrors
        # A malformed message will not get better by retrying: park it right away.
        error = f"invalid event: {exc}"
        return _park(park, payload, error, 0)

    delays = backoff_delays(attempts, base_delay_s)
    error = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            store(row)
            return Outcome(STORED, attempt)
        except Exception as exc:  # noqa: BLE001 - any sink failure is retried
            error = f"{type(exc).__name__}: {exc}"
            if attempt <= len(delays):
                sleep(delays[attempt - 1])
    return _park(park, payload, error, attempts)


def _park(park: Callable[[bytes, str, int], None], payload: bytes, error: str, attempts: int):
    try:
        park(payload, error, attempts)
    except Exception as exc:  # noqa: BLE001 - parking failed too: leave it to the broker
        return Outcome(UNACKED, attempts, f"{error}; parking failed: {type(exc).__name__}: {exc}")
    return Outcome(PARKED, attempts, error)
