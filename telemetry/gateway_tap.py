"""Counting of gateway uplink events by gateway, from the MQTT topic only.

The gateway bridge publishes every received radio frame on
``<prefix>/gateway/<gateway id>/event/up``. The gateway id is in the topic, so the frames
can be counted without decoding (or even looking at) the payload.
"""

from __future__ import annotations

from collections import Counter


def gateway_id_from_topic(topic: str, prefix: str) -> str | None:
    """Gateway id of ``<prefix>/gateway/<id>/event/up``, or None for any other topic."""
    parts = topic.split("/")
    expected = prefix.strip("/").split("/") if prefix.strip("/") else []
    n = len(expected)
    if len(parts) != n + 4 or parts[:n] != expected:
        return None
    if parts[n] != "gateway" or parts[n + 2 :] != ["event", "up"] or not parts[n + 1]:
        return None
    return parts[n + 1].lower()


class FrameCounter:
    """Accumulates frames per gateway until ``drain`` hands them over."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def add(self, gateway_id: str) -> None:
        self._counts[gateway_id] += 1

    def drain(self) -> dict[str, int]:
        out = dict(self._counts)
        self._counts.clear()
        return out
