"""Semtech UDP packet forwarder protocol, version 2: PUSH_DATA with rxpk.

Protocol description (public):
https://github.com/Lora-net/packet_forwarder/blob/master/PROTOCOL.TXT

Downlinks: the server sends ``PULL_RESP`` (``version | token | 0x03 | JSON {"txpk": {...}}``)
to the address of the last PULL_DATA and the gateway answers ``TX_ACK`` (``version | same
token | 0x05 | gateway EUI | optional JSON {"txpk_ack": {"error": "NONE"}}``).

PUSH_DATA datagram: ``version (0x02) | token (2 bytes) | 0x00 | gateway EUI
(8 bytes) | JSON``. The server answers with PUSH_ACK:
``version | same token | 0x01``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

PROTOCOL_VERSION = 0x02
PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_RESP = 0x03
PULL_ACK = 0x04
TX_ACK = 0x05


class SemtechError(ValueError):
    """Raised on a malformed datagram."""


def build_rxpk(
    phy: bytes,
    *,
    tmst: int,
    freq_mhz: float = 868.1,
    datr: str = "SF7BW125",
    rssi: int = -80,
    lsnr: float = 7.0,
    chan: int = 0,
    rfch: int = 0,
    time: str | None = None,
) -> dict:
    """One received packet record (rxpk) for a LoRa uplink."""
    rxpk = {
        "tmst": tmst,
        "chan": chan,
        "rfch": rfch,
        "freq": freq_mhz,
        "stat": 1,
        "modu": "LORA",
        "datr": datr,
        "codr": "4/5",
        "rssi": rssi,
        "lsnr": lsnr,
        "size": len(phy),
        "data": base64.b64encode(phy).decode("ascii"),
    }
    if time is not None:
        rxpk["time"] = time
    return rxpk


def _header(token: int, identifier: int, gateway_eui: bytes) -> bytes:
    if len(gateway_eui) != 8:
        raise SemtechError("gateway EUI must be 8 bytes")
    return (
        bytes([PROTOCOL_VERSION])
        + (token & 0xFFFF).to_bytes(2, "big")
        + bytes([identifier])
        + gateway_eui
    )


def build_push_data(gateway_eui: bytes, token: int, rxpks: list[dict]) -> bytes:
    body = json.dumps({"rxpk": rxpks}, separators=(",", ":")).encode()
    return _header(token, PUSH_DATA, gateway_eui) + body


def build_pull_data(gateway_eui: bytes, token: int) -> bytes:
    """Keep-alive that lets the server know where to send downlinks."""
    return _header(token, PULL_DATA, gateway_eui)


@dataclass(frozen=True)
class PushData:
    token: int
    gateway_eui: bytes
    rxpk: list[dict]


def parse_push_data(datagram: bytes) -> PushData:
    if len(datagram) < 12 or datagram[0] != PROTOCOL_VERSION or datagram[3] != PUSH_DATA:
        raise SemtechError("not a PUSH_DATA datagram")
    try:
        body = json.loads(datagram[12:].decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemtechError("invalid JSON body") from exc
    return PushData(
        token=int.from_bytes(datagram[1:3], "big"),
        gateway_eui=bytes(datagram[4:12]),
        rxpk=list(body.get("rxpk", [])),
    )


def is_push_ack(datagram: bytes, token: int) -> bool:
    return (
        len(datagram) == 4
        and datagram[0] == PROTOCOL_VERSION
        and int.from_bytes(datagram[1:3], "big") == token & 0xFFFF
        and datagram[3] == PUSH_ACK
    )


@dataclass(frozen=True)
class PullResp:
    token: int
    txpk: dict

    @property
    def phy_payload(self) -> bytes:
        """The LoRaWAN PHYPayload to transmit (``data`` of the txpk, base64)."""
        try:
            return base64.b64decode(self.txpk["data"], validate=True)
        except (KeyError, ValueError) as exc:
            raise SemtechError("txpk has no valid data") from exc


def parse_pull_resp(datagram: bytes) -> PullResp:
    if len(datagram) < 5 or datagram[0] != PROTOCOL_VERSION or datagram[3] != PULL_RESP:
        raise SemtechError("not a PULL_RESP datagram")
    try:
        txpk = json.loads(datagram[4:].decode())["txpk"]
        return PullResp(token=int.from_bytes(datagram[1:3], "big"), txpk=dict(txpk))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SemtechError("invalid PULL_RESP body") from exc


def build_pull_resp(token: int, txpk: dict) -> bytes:
    """What the server sends. Used by the tests."""
    body = json.dumps({"txpk": txpk}, separators=(",", ":")).encode()
    return (
        bytes([PROTOCOL_VERSION]) + (token & 0xFFFF).to_bytes(2, "big") + bytes([PULL_RESP]) + body
    )


def build_tx_ack(gateway_eui: bytes, token: int, error: str = "NONE") -> bytes:
    body = json.dumps({"txpk_ack": {"error": error}}, separators=(",", ":")).encode()
    return _header(token, TX_ACK, gateway_eui) + body


def is_pull_resp(datagram: bytes) -> bool:
    return len(datagram) >= 5 and datagram[0] == PROTOCOL_VERSION and datagram[3] == PULL_RESP
