import json

import pytest

from telemetry import delivery

EVENT = json.dumps(
    {
        "time": "2026-01-01T00:00:00Z",
        "deviceInfo": {"devEui": "0a0b0c0d0e0f0001", "deviceName": "dev-ok"},
        "fCnt": 1,
    }
).encode()


class Recorder:
    def __init__(self, store_failures=0, park_fails=False):
        self.store_failures = store_failures
        self.park_fails = park_fails
        self.stored = []
        self.parked = []
        self.sleeps = []

    def store(self, row):
        if self.store_failures:
            self.store_failures -= 1
            raise ConnectionError("database down")
        self.stored.append(row)

    def park(self, payload, error, attempts):
        if self.park_fails:
            raise ConnectionError("database down")
        self.parked.append((payload, error, attempts))

    def run(self, payload=EVENT, **kw):
        return delivery.handle_message(
            payload, store=self.store, park=self.park, sleep=self.sleeps.append, **kw
        )


def test_stored_on_first_try_is_acked_without_sleep():
    r = Recorder()
    out = r.run()
    assert out == delivery.Outcome(delivery.STORED, 1)
    assert out.ack and len(r.stored) == 1 and r.sleeps == [] and r.parked == []


def test_retries_with_backoff_then_succeeds():
    r = Recorder(store_failures=2)
    out = r.run(attempts=3, base_delay_s=0.5)
    assert out.status == delivery.STORED and out.attempts == 3
    assert r.sleeps == [0.5, 1.0]


def test_exhausted_attempts_are_parked_and_acked():
    r = Recorder(store_failures=99)
    out = r.run(attempts=3, base_delay_s=1.0)
    assert out.status == delivery.PARKED and out.ack
    assert r.stored == [] and r.sleeps == [1.0, 2.0]  # no sleep after the last attempt
    payload, error, attempts = r.parked[0]
    assert payload == EVENT and attempts == 3 and "ConnectionError" in error


def test_not_acked_when_parking_fails_too():
    r = Recorder(store_failures=99, park_fails=True)
    out = r.run()
    assert out.status == delivery.UNACKED and not out.ack
    assert "parking failed" in out.error


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"time": "x", "deviceInfo": {}}'])
def test_malformed_message_is_parked_at_once(payload):
    r = Recorder()
    out = r.run(payload)
    assert out.status == delivery.PARKED and r.sleeps == [] and r.stored == []
    assert r.parked[0][2] == 0 and r.parked[0][1].startswith("invalid event")


def test_backoff_delays_are_capped():
    assert delivery.backoff_delays(5, 10.0, cap_s=30.0) == [10.0, 20.0, 30.0, 30.0]
    assert delivery.backoff_delays(1, 1.0) == []
