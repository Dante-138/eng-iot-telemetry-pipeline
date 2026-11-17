import logging
from types import SimpleNamespace

from simulator import run
from telemetry import otaa


class FakeGateway:
    """Never produces a valid JoinAccept: the first downlink is a corrupted one."""

    def __init__(self):
        self.downlinks: list[bytes] = []
        self.pushed = []

    def push(self, phy, rssi, snr):
        self.pushed.append(phy)

    def pump(self, seconds):
        if not self.downlinks:
            self.downlinks.append(bytes([otaa.MHDR_JOIN_ACCEPT]) + bytes(16))


def test_invalid_join_accept_is_logged_not_raised(monkeypatch, caplog):
    ticks = iter(range(0, 10_000, 4))  # a fake clock that jumps past the 15 s deadline fast
    monkeypatch.setattr(
        run, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 1_000)
    )
    gw = FakeGateway()
    with caplog.at_level(logging.WARNING, logger="simulator"):
        joined, downlinks = run.run_otaa(gw, interval=0.0)
    assert (joined, downlinks) == (False, [])
    assert len(gw.pushed) == 1  # only the JoinRequest
    assert any("not valid" in r.message for r in caplog.records)
