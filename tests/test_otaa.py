import base64

import pytest

from telemetry import lorawan, otaa

# Public vectors: TestPHYPayloadJoinRequest and TestPHYPayloadJoinAccept in brocaar/lorawan (MIT),
# https://github.com/brocaar/lorawan/blob/master/phypayload_test.go
# EUIs are written most significant byte first there, like here.
JOIN_REQUEST = base64.b64decode("AAQDAgEEAwIBBQQDAgUEAwItEGqZDhI=")
REQUEST_APP_KEY = bytes([1] * 16)

JOIN_ACCEPT = bytes.fromhex("20493eeb51fba2116f810edb3742975142")
ACCEPT_APP_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


def test_join_request_public_vector():
    req = otaa.parse_join_request(JOIN_REQUEST, REQUEST_APP_KEY)
    assert req.join_eui == bytes([1, 2, 3, 4, 1, 2, 3, 4])
    assert req.dev_eui == bytes([2, 3, 4, 5, 2, 3, 4, 5])
    assert req.dev_nonce == 4141


def test_build_join_request_matches_public_vector():
    phy = otaa.build_join_request(
        REQUEST_APP_KEY, bytes([1, 2, 3, 4, 1, 2, 3, 4]), bytes([2, 3, 4, 5, 2, 3, 4, 5]), 4141
    )
    assert phy == JOIN_REQUEST


def test_join_request_wrong_key_fails_mic():
    with pytest.raises(otaa.OtaaError, match="MIC"):
        otaa.parse_join_request(JOIN_REQUEST, bytes(16))


def test_join_accept_public_vector():
    acc = otaa.parse_join_accept(JOIN_ACCEPT, ACCEPT_APP_KEY)
    assert acc.app_nonce == 5704647
    assert acc.net_id == 0x221101  # NetID {34, 17, 1}
    assert acc.dev_addr == 0x02031980  # DevAddr {2, 3, 25, 128}
    assert acc.dl_settings == 0 and acc.rx_delay == 0 and acc.cf_list == b""


def test_build_join_accept_matches_public_vector():
    phy = otaa.build_join_accept(
        ACCEPT_APP_KEY, app_nonce=5704647, net_id=0x221101, dev_addr=0x02031980
    )
    assert phy == JOIN_ACCEPT


def test_join_accept_wrong_key_or_tamper_fails():
    with pytest.raises(otaa.OtaaError, match="MIC"):
        otaa.parse_join_accept(JOIN_ACCEPT, bytes(16))
    bad = bytearray(JOIN_ACCEPT)
    bad[5] ^= 1
    with pytest.raises(otaa.OtaaError, match="MIC"):
        otaa.parse_join_accept(bytes(bad), ACCEPT_APP_KEY)


def test_join_accept_with_cflist_round_trip():
    # Regions such as EU868 and AU915 add a 16 byte CFList: two cipher blocks.
    cf = bytes(range(16))
    phy = otaa.build_join_accept(
        ACCEPT_APP_KEY,
        app_nonce=0x123456,
        net_id=0,
        dev_addr=0x01000000,
        dl_settings=0x05,
        rx_delay=1,
        cf_list=cf,
    )
    assert len(phy) == 33
    acc = otaa.parse_join_accept(phy, ACCEPT_APP_KEY)
    assert (acc.app_nonce, acc.dev_addr, acc.dl_settings, acc.rx_delay) == (
        0x123456,
        0x01000000,
        5,
        1,
    )
    assert acc.cf_list == cf


def test_rejects_malformed_join_frames():
    with pytest.raises(otaa.OtaaError):
        otaa.parse_join_accept(JOIN_ACCEPT[:-1], ACCEPT_APP_KEY)
    with pytest.raises(otaa.OtaaError):
        otaa.parse_join_request(JOIN_REQUEST[:-1], REQUEST_APP_KEY)
    with pytest.raises(otaa.OtaaError):
        otaa.build_join_request(REQUEST_APP_KEY, bytes(8), bytes(8), 0x10000)


def test_derive_session_keys():
    # LoRaWAN 1.0.x section 6.2.5: NwkSKey = aes128_encrypt(AppKey, 0x01 | AppNonce | NetID |
    # DevNonce | pad16), AppSKey the same with 0x02. Restated here with raw AES-ECB (the same
    # formula written out again, not a public test vector). The proof against a real network
    # server is the end to end test: ChirpStack accepts the MICs made with these keys.
    from Crypto.Cipher import AES

    app_nonce, net_id, dev_nonce = 5704647, 0x221101, 4141

    def expected(prefix: int) -> bytes:
        block = (
            bytes([prefix])
            + app_nonce.to_bytes(3, "little")
            + net_id.to_bytes(3, "little")
            + dev_nonce.to_bytes(2, "little")
        ).ljust(16, bytes(1))
        return AES.new(ACCEPT_APP_KEY, AES.MODE_ECB).encrypt(block)

    nwk, app = otaa.derive_session_keys(ACCEPT_APP_KEY, app_nonce, net_id, dev_nonce)
    assert nwk == expected(0x01) and app == expected(0x02) and nwk != app
    other, _ = otaa.derive_session_keys(ACCEPT_APP_KEY, app_nonce, net_id, dev_nonce + 1)
    assert other != nwk  # a new DevNonce gives new keys


# --- downlink data frames -------------------------------------------------------------
# There is no public LoRaWAN 1.0.x downlink data vector cited here. The build/parse pair
# below is checked by round trip, by tampering, and (the real proof) by ChirpStack in the
# end to end test, which sends a downlink that the simulator decrypts.
NWK = bytes(range(1, 17))
APP = bytes(range(16, 0, -1))


def test_downlink_round_trip():
    phy = lorawan.build_downlink(
        dev_addr=0x01000006,
        nwk_s_key=NWK,
        app_s_key=APP,
        f_cnt=3,
        f_port=10,
        payload=b"hello-down",
        confirmed=True,
        ack=True,
        f_pending=True,
    )
    down = lorawan.parse_downlink(phy, nwk_s_key=NWK, app_s_key=APP)
    assert down.dev_addr == 0x01000006 and down.f_cnt == 3 and down.f_port == 10
    assert down.payload == b"hello-down"
    assert down.confirmed and down.ack and down.f_pending
    assert b"hello-down" not in phy


def test_downlink_uses_the_downlink_direction():
    up = lorawan.build_uplink(
        dev_addr=1, nwk_s_key=NWK, app_s_key=APP, f_cnt=1, f_port=1, payload=b"x" * 8
    )
    down = lorawan.build_downlink(
        dev_addr=1, nwk_s_key=NWK, app_s_key=APP, f_cnt=1, f_port=1, payload=b"x" * 8
    )
    # Same fields, other direction bit in the A block and in B0: other ciphertext and MIC.
    assert up[9:-4] != down[9:-4] and up[-4:] != down[-4:]
    with pytest.raises(lorawan.LorawanError, match="downlink"):
        lorawan.parse_downlink(up, nwk_s_key=NWK, app_s_key=APP)
    with pytest.raises(lorawan.LorawanError, match="uplink"):
        lorawan.parse_uplink(down, nwk_s_key=NWK, app_s_key=APP)


def test_downlink_tamper_and_wrong_key_fail_mic():
    phy = lorawan.build_downlink(
        dev_addr=1, nwk_s_key=NWK, app_s_key=APP, f_cnt=0, f_port=1, payload=b"abc"
    )
    bad = bytearray(phy)
    bad[9] ^= 1
    with pytest.raises(lorawan.LorawanError, match="MIC"):
        lorawan.parse_downlink(bytes(bad), nwk_s_key=NWK, app_s_key=APP)
    with pytest.raises(lorawan.LorawanError, match="MIC"):
        lorawan.parse_downlink(phy, nwk_s_key=bytes(16), app_s_key=APP)


def test_downlink_without_payload_and_with_fopts():
    phy = lorawan.build_downlink(
        dev_addr=7,
        nwk_s_key=NWK,
        app_s_key=APP,
        f_cnt=9,
        f_port=None,
        payload=b"",
        f_opts=bytes([0x02]),
    )
    down = lorawan.parse_downlink(phy, nwk_s_key=NWK, app_s_key=APP)
    assert down.f_port is None and down.payload == b"" and down.f_opts == bytes([0x02])
