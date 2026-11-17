import base64

import pytest

from telemetry import semtech_udp as su

EUI = bytes.fromhex("0102030405060708")


def test_push_data_layout():
    pkt = su.build_push_data(EUI, 0xABCD, [su.build_rxpk(b"\x40\x01", tmst=1000)])
    assert pkt[0] == 2
    assert pkt[1:3] == bytes([0xAB, 0xCD])
    assert pkt[3] == 0
    assert pkt[4:12] == EUI
    assert pkt[12:].startswith(b'{"rxpk":[')


def test_round_trip_and_rxpk_fields():
    phy = bytes(range(20))
    rxpk = su.build_rxpk(phy, tmst=42, rssi=-90, lsnr=5.5)
    parsed = su.parse_push_data(su.build_push_data(EUI, 7, [rxpk]))
    assert parsed.token == 7
    assert parsed.gateway_eui == EUI
    got = parsed.rxpk[0]
    assert base64.b64decode(got["data"]) == phy
    assert got["size"] == 20
    assert got["modu"] == "LORA"
    assert got["datr"] == "SF7BW125"
    assert got["rssi"] == -90
    assert got["lsnr"] == 5.5


def test_pull_data_and_ack():
    assert su.build_pull_data(EUI, 1) == bytes([2, 0, 1, 2]) + EUI
    assert su.is_push_ack(bytes([2, 0xAB, 0xCD, 1]), 0xABCD)
    assert not su.is_push_ack(bytes([2, 0xAB, 0xCE, 1]), 0xABCD)
    assert not su.is_push_ack(b"", 0)


def test_errors():
    with pytest.raises(su.SemtechError):
        su.build_push_data(b"\x01", 0, [])
    with pytest.raises(su.SemtechError):
        su.parse_push_data(b"\x02\x00\x00\x00short")
    with pytest.raises(su.SemtechError):
        su.parse_push_data(bytes([2, 0, 0, 0]) + EUI + b"not json")


def test_pull_resp_and_tx_ack():
    # Formats from PROTOCOL.TXT of Lora-net/packet_forwarder (PULL_RESP and TX_ACK).
    txpk = {"imme": False, "tmst": 5000000, "freq": 869.525, "datr": "SF12BW125", "data": "AQID"}
    resp = su.parse_pull_resp(su.build_pull_resp(0xABCD, txpk))
    assert resp.token == 0xABCD and resp.txpk == txpk
    assert resp.phy_payload == bytes([1, 2, 3])
    ack = su.build_tx_ack(bytes(range(8)), 0xABCD)
    assert ack[:4] == bytes([2, 0xAB, 0xCD, 5]) and ack[4:12] == bytes(range(8))
    assert b'"error":"NONE"' in ack[12:]
    assert su.is_pull_resp(su.build_pull_resp(1, txpk))
    assert not su.is_pull_resp(su.build_pull_data(bytes(8), 1))


def test_pull_resp_rejects_garbage():
    for bad in (b"", b"\x02\x00\x00\x03not json", b"\x02\x00\x00\x03{}", b"\x02\x00\x00\x04{}"):
        with pytest.raises(su.SemtechError):
            su.parse_pull_resp(bad)
    with pytest.raises(su.SemtechError):
        _ = su.PullResp(1, {"data": "***"}).phy_payload
