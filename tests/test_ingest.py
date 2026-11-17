import json
import threading

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


class ForbiddenClient:
    """Any call fails: the worker must never touch the MQTT client."""

    def __getattr__(self, name):
        raise AssertionError(f"worker called client.{name}")


def run_all(ingest):
    """What the worker does for every queued item, then the network thread flushes the acks."""
    while not ingest.queue.empty():
        ingest.process(ingest.queue.get_nowait())
    ingest.flush_acks()


def test_process_never_touches_the_client():
    sinks = FakeSinks(store_fails=True, park_fails=True)  # the UNACKED path as well
    ingest = Ingest(sinks, ForbiddenClient())
    ingest.process(item())
    ingest.process(item(generation=5))
    sinks.store_fails = sinks.park_fails = False
    ingest.process(item())
    assert ingest.acks.qsize() == 3


def test_stored_message_is_acknowledged():
    sinks = FakeSinks()
    ingest, client = make(sinks)
    ingest.queue.put(item())
    run_all(ingest)
    assert len(sinks.stored) == 1 and client.acks == [(7, 1)] and client.disconnects == 0


def test_parked_message_is_acknowledged():
    sinks = FakeSinks(store_fails=True)
    ingest, client = make(sinks)
    ingest.queue.put(item())
    run_all(ingest)
    assert len(sinks.parked) == 1 and client.acks == [(7, 1)]


def test_message_that_cannot_be_stored_or_parked_disconnects_once_and_the_rest_is_not_acked():
    sinks = FakeSinks(store_fails=True, park_fails=True)
    ingest, client = make(sinks)
    ingest.queue.put(item(mid=1))
    ingest.process(ingest.queue.get_nowait())
    sinks.store_fails = sinks.park_fails = False  # the database is back for the next ones
    ingest.queue.put(item(mid=2))
    ingest.queue.put(item(mid=3))
    run_all(ingest)
    assert client.acks == [] and client.disconnects == 1
    assert len(sinks.stored) == 2  # still written (idempotent), only not acknowledged
    # after the reconnect the new connection is acknowledged again
    ingest.on_disconnect()
    ingest.queue.put(item(generation=1, mid=4))
    run_all(ingest)
    assert client.acks == [(4, 1)]


def test_old_connection_items_get_nothing_even_when_unacked():
    ingest, client = make(FakeSinks(store_fails=True, park_fails=True))
    ingest.on_disconnect()
    ingest.queue.put(item(generation=0))
    run_all(ingest)
    assert client.acks == [] and client.disconnects == 0


def test_item_of_an_old_connection_is_stored_but_not_acknowledged():
    # After a broker restart without its session nobody delivers it again, so it is written
    # now (the insert is idempotent); its message id is meaningless on the new connection.
    sinks = FakeSinks()
    ingest, client = make(sinks)
    ingest.on_disconnect()
    ingest.queue.put(item(generation=0))
    ingest.queue.put(item(generation=1, mid=9))
    run_all(ingest)
    assert len(sinks.stored) == 2 and client.acks == [(9, 1)]


def test_a_disconnect_that_runs_on_disconnect_at_once_does_not_deadlock():
    # paho calls on_disconnect in the thread that called disconnect(). That is the network
    # thread now, and nothing holds a lock, so it returns.
    class Client(FakeClient):
        def disconnect(self):
            super().disconnect()
            ingest.on_disconnect()

    client = Client()
    ingest = Ingest(FakeSinks(store_fails=True, park_fails=True), client)
    ingest.queue.put(item())
    ingest.process(ingest.queue.get_nowait())
    thread = threading.Thread(target=ingest.flush_acks)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert client.disconnects == 1 and ingest.generation == 1


def test_on_message_tags_the_current_generation():
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
