import json

import pytest

from ingest import replay
from ingest.service import Ingest, Item

EVENT = json.dumps(
    {
        "time": "2026-01-01T00:00:00Z",
        "deviceInfo": {"devEui": "0a0b0c0d0e0f0001", "deviceName": "dev-ok"},
        "fCnt": 1,
    }
).encode()


class FakeClient:
    def __init__(self):
        self.acks = []
        self.disconnects = 0

    def ack(self, mid, qos):
        self.acks.append((mid, qos))

    def disconnect(self):
        self.disconnects += 1


class FakeSinks:
    def __init__(self, store_fails=False, park_fails=False):
        self.store_fails = store_fails
        self.park_fails = park_fails
        self.stored = []
        self.parked = []

    def store(self, row):
        if self.store_fails:
            raise ConnectionError("database down")
        self.stored.append(row)

    def park(self, topic, payload, error, attempts):
        if self.park_fails:
            raise ConnectionError("database down")
        self.parked.append((topic, payload, attempts))


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setenv("INGEST_ATTEMPTS", "2")
    monkeypatch.setenv("INGEST_BACKOFF_S", "0")


def make(sinks):
    client = FakeClient()
    return Ingest(sinks, client), client


def item(generation=0, mid=7, payload=EVENT):
    return Item(generation, mid, 1, "application/x/device/y/event/up", payload)


def test_stored_message_is_acknowledged():
    sinks = FakeSinks()
    ingest, client = make(sinks)
    ingest.process(item())
    assert len(sinks.stored) == 1 and client.acks == [(7, 1)] and client.disconnects == 0


def test_parked_message_is_acknowledged():
    sinks = FakeSinks(store_fails=True)
    ingest, client = make(sinks)
    ingest.process(item())
    assert len(sinks.parked) == 1 and client.acks == [(7, 1)]


def test_message_that_cannot_be_stored_or_parked_is_not_acknowledged():
    ingest, client = make(FakeSinks(store_fails=True, park_fails=True))
    ingest.process(item())
    assert client.acks == [] and client.disconnects == 1


def test_item_of_an_old_connection_is_stored_but_not_acknowledged():
    # After a broker restart without its session nobody delivers it again, so it is written
    # now (the insert is idempotent); its message id is meaningless on the new connection.
    sinks = FakeSinks()
    ingest, client = make(sinks)
    ingest.on_disconnect()
    ingest.process(item(generation=0))
    assert len(sinks.stored) == 1 and client.acks == []
    ingest.process(item(generation=1, mid=9))
    assert client.acks == [(9, 1)]


def test_on_message_tags_the_current_generation_and_work_drains_the_queue():
    sinks = FakeSinks()
    ingest, client = make(sinks)

    class Msg:
        mid, qos, topic, payload = 3, 1, "t", EVENT

    ingest.on_disconnect()
    ingest.on_message(None, None, Msg())
    queued = ingest.queue.get_nowait()
    assert queued.generation == 1 and queued.mid == 3


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return FakeResult(self.rows)


class ReplaySinks(FakeSinks):
    def __init__(self, rows, store_fails=False):
        super().__init__(store_fails=store_fails)
        self.conn = FakeConn(rows)

    def _pg(self):
        return self.conn


def test_replay_marks_the_pending_row_as_replayed():
    sinks = ReplaySinks([(5, EVENT)])
    assert replay.replay(sinks) == (1, 0)
    assert len(sinks.stored) == 1
    assert any("replayed_at = now()" in sql and p == (5,) for sql, p in sinks.conn.statements)


def test_replay_failure_increments_attempts_and_keeps_the_row():
    sinks = ReplaySinks([(5, EVENT)], store_fails=True)
    assert replay.replay(sinks) == (0, 1)
    sql, params = sinks.conn.statements[-1]
    assert "attempts = attempts + 1" in sql and params[1] == 5
    assert "ConnectionError" in params[0]


def test_replay_limit_must_be_positive():
    with pytest.raises(ValueError):
        replay.replay(ReplaySinks([]), limit=0)
    with pytest.raises(SystemExit):
        replay.main(["--limit", "0"])
