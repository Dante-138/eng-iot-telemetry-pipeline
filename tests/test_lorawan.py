import pytest

from telemetry import lorawan

# Public vector: ExamplePHYPayload_lorawan10Encode in brocaar/lorawan (MIT),
# https://github.com/brocaar/lorawan/blob/master/phypayload_test.go
# Confirmed data up, DevAddr 01020304, FCnt 0, FOpts = DevStatusAns (battery
# 115, margin 7), FPort 10, FRMPayload 01 02 03 04, LoRaWAN 1.0.
NWK_S_KEY = bytes(range(1, 17))
APP_S_KEY = bytes(range(16, 0, -1))
DEV_ADDR = int.from_bytes(bytes([1, 2, 3, 4]), "big")
VECTOR = bytes([128, 4, 3, 2, 1, 3, 0, 0, 6, 115, 7, 10, 226, 100, 212, 247, 225, 23, 210, 192])


def test_build_matches_public_vector():
    phy = lorawan.build_uplink(
        dev_addr=DEV_ADDR,
        nwk_s_key=NWK_S_KEY,
        app_s_key=APP_S_KEY,
        f_cnt=0,
        f_port=10,
        payload=bytes([1, 2, 3, 4]),
        confirmed=True,
        f_opts=bytes([6, 115, 7]),
    )
    assert phy == VECTOR


def test_parse_public_vector():
    up = lorawan.parse_uplink(VECTOR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
    assert up.dev_addr == DEV_ADDR
    assert up.f_cnt == 0
    assert up.f_port == 10
    assert up.payload == bytes([1, 2, 3, 4])
    assert up.confirmed
    assert up.f_opts == bytes([6, 115, 7])


def test_round_trip_multi_block_payload():
    payload = bytes(range(40))
    phy = lorawan.build_uplink(
        dev_addr=0x01000001,
        nwk_s_key=NWK_S_KEY,
        app_s_key=APP_S_KEY,
        f_cnt=0x12345,
        f_port=1,
        payload=payload,
    )
    up = lorawan.parse_uplink(phy, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY, f_cnt_msb=1)
    assert up.payload == payload
    assert up.f_cnt == 0x12345
    assert not up.confirmed


def test_payload_is_encrypted():
    phy = lorawan.build_uplink(
        dev_addr=1,
        nwk_s_key=NWK_S_KEY,
        app_s_key=APP_S_KEY,
        f_cnt=1,
        f_port=1,
        payload=b"hello world",
    )
    assert b"hello world" not in phy


def test_tampered_frame_fails_mic():
    bad = bytearray(VECTOR)
    bad[10] ^= 0x01
    with pytest.raises(lorawan.LorawanError, match="MIC"):
        lorawan.parse_uplink(bytes(bad), nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)


def test_wrong_key_fails_mic():
    with pytest.raises(lorawan.LorawanError, match="MIC"):
        lorawan.parse_uplink(VECTOR, nwk_s_key=bytes(16), app_s_key=APP_S_KEY)


def test_rejects_short_and_non_uplink():
    with pytest.raises(lorawan.LorawanError):
        lorawan.parse_uplink(b"\x40\x00", nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)
    downlink = bytes([0x60]) + VECTOR[1:]
    with pytest.raises(lorawan.LorawanError, match="uplink"):
        lorawan.parse_uplink(downlink, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY)


def test_rejects_bad_key_length():
    with pytest.raises(lorawan.LorawanError):
        lorawan.build_uplink(
            dev_addr=1, nwk_s_key=b"short", app_s_key=APP_S_KEY, f_cnt=0, f_port=1, payload=b""
        )
