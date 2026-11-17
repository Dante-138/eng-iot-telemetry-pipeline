import pytest

from telemetry import gateway_tap


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("eu868/gateway/0102030405060708/event/up", "0102030405060708"),
        ("au915_0/gateway/ABCDEF0102030405/event/up", "abcdef0102030405"),
        ("eu868/gateway/0102030405060708/event/stats", None),
        ("eu868/gateway/0102030405060708/state/conn", None),
        ("us915/gateway/0102030405060708/event/up", None),
        ("eu868/gateway//event/up", None),
        ("application/x/device/y/event/up", None),
    ],
)
def test_gateway_id_from_topic(topic, expected):
    prefix = "au915_0" if topic.startswith("au915") else "eu868"
    assert gateway_tap.gateway_id_from_topic(topic, prefix) == expected


def test_prefix_may_have_several_levels_or_none():
    assert gateway_tap.gateway_id_from_topic("a/b/gateway/01/event/up", "a/b") == "01"
    assert gateway_tap.gateway_id_from_topic("gateway/01/event/up", "") == "01"


def test_counter_counts_per_gateway_and_drains():
    c = gateway_tap.FrameCounter()
    for gw in ("01", "01", "02"):
        c.add(gw)
    assert c.drain() == {"01": 2, "02": 1}
    assert c.drain() == {}
