"""LoRaWAN 1.0.3 OTAA: JoinRequest, JoinAccept and session key derivation (device side).

Specification: LoRaWAN 1.0.3, chapter 6.2 (join procedure). Identifiers are kept in the
order people write them (most significant byte first, like ``scenario.py``); on the air
they are little endian, so this module reverses them.

* JoinRequest: ``MHDR 0x00 | AppEUI | DevEUI | DevNonce | MIC`` where
  ``MIC = AES-CMAC(AppKey, MHDR | AppEUI | DevEUI | DevNonce)[:4]``.
* JoinAccept: ``MHDR 0x20 | AES-ECB-decrypt(AppKey, AppNonce | NetID | DevAddr | DLSettings |
  RxDelay | [CFList] | MIC)``. The network server "decrypts" to encrypt, so the device
  "encrypts" to read it. ``MIC = AES-CMAC(AppKey, MHDR | fields before the MIC)[:4]``.
* Session keys: ``NwkSKey = AES(AppKey, 0x01 | AppNonce | NetID | DevNonce | pad)`` and
  ``AppSKey`` with 0x02.
"""

from __future__ import annotations

from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Hash import CMAC

MHDR_JOIN_REQUEST = 0x00
MHDR_JOIN_ACCEPT = 0x20
MIC_LEN = 4


class OtaaError(ValueError):
    """Raised on a malformed join frame or a MIC mismatch."""


def _check(value: bytes, size: int, name: str) -> None:
    if len(value) != size:
        raise OtaaError(f"{name} must be {size} bytes")


def _cmac(key: bytes, data: bytes) -> bytes:
    _check(key, 16, "app_key")
    mac = CMAC.new(key, ciphermod=AES)
    mac.update(data)
    return mac.digest()[:MIC_LEN]


@dataclass(frozen=True)
class JoinRequest:
    join_eui: bytes  # AppEUI, most significant byte first
    dev_eui: bytes
    dev_nonce: int


def build_join_request(app_key: bytes, join_eui: bytes, dev_eui: bytes, dev_nonce: int) -> bytes:
    _check(join_eui, 8, "join_eui")
    _check(dev_eui, 8, "dev_eui")
    if not 0 <= dev_nonce <= 0xFFFF:
        raise OtaaError("dev_nonce must fit 16 bits")
    msg = (
        bytes([MHDR_JOIN_REQUEST])
        + join_eui[::-1]
        + dev_eui[::-1]
        + dev_nonce.to_bytes(2, "little")
    )
    return msg + _cmac(app_key, msg)


def parse_join_request(phy: bytes, app_key: bytes) -> JoinRequest:
    """Parse a JoinRequest and verify its MIC (the network server side, used by the tests)."""
    if len(phy) != 23 or phy[0] != MHDR_JOIN_REQUEST:
        raise OtaaError("not a JoinRequest")
    if _cmac(app_key, phy[:-MIC_LEN]) != phy[-MIC_LEN:]:
        raise OtaaError("MIC mismatch")
    return JoinRequest(
        join_eui=phy[1:9][::-1],
        dev_eui=phy[9:17][::-1],
        dev_nonce=int.from_bytes(phy[17:19], "little"),
    )


@dataclass(frozen=True)
class JoinAccept:
    app_nonce: int
    net_id: int
    dev_addr: int
    dl_settings: int
    rx_delay: int
    cf_list: bytes  # empty or 16 bytes


def build_join_accept(
    app_key: bytes,
    *,
    app_nonce: int,
    net_id: int,
    dev_addr: int,
    dl_settings: int = 0,
    rx_delay: int = 0,
    cf_list: bytes = b"",
) -> bytes:
    """Build an encrypted JoinAccept (the network server side, used by the tests)."""
    if len(cf_list) not in (0, 16):
        raise OtaaError("CFList is 0 or 16 bytes")
    msg = (
        bytes([MHDR_JOIN_ACCEPT])
        + app_nonce.to_bytes(3, "little")
        + net_id.to_bytes(3, "little")
        + dev_addr.to_bytes(4, "little")
        + bytes([dl_settings, rx_delay])
        + cf_list
    )
    plain = msg[1:] + _cmac(app_key, msg)
    cipher = AES.new(app_key, AES.MODE_ECB)
    return msg[:1] + b"".join(cipher.decrypt(plain[i : i + 16]) for i in range(0, len(plain), 16))


def parse_join_accept(phy: bytes, app_key: bytes) -> JoinAccept:
    """Decrypt a JoinAccept, verify its MIC and return its fields (the device side)."""
    _check(app_key, 16, "app_key")
    if len(phy) not in (17, 33) or phy[0] != MHDR_JOIN_ACCEPT:
        raise OtaaError("not a JoinAccept")
    cipher = AES.new(app_key, AES.MODE_ECB)
    body = phy[1:]
    plain = b"".join(cipher.encrypt(body[i : i + 16]) for i in range(0, len(body), 16))
    fields, mic = plain[:-MIC_LEN], plain[-MIC_LEN:]
    if _cmac(app_key, phy[:1] + fields) != mic:
        raise OtaaError("MIC mismatch")
    return JoinAccept(
        app_nonce=int.from_bytes(fields[0:3], "little"),
        net_id=int.from_bytes(fields[3:6], "little"),
        dev_addr=int.from_bytes(fields[6:10], "little"),
        dl_settings=fields[10],
        rx_delay=fields[11],
        cf_list=fields[12:],
    )


def derive_session_keys(
    app_key: bytes, app_nonce: int, net_id: int, dev_nonce: int
) -> tuple[bytes, bytes]:
    """Return ``(nwk_s_key, app_s_key)`` for LoRaWAN 1.0.x."""
    _check(app_key, 16, "app_key")
    cipher = AES.new(app_key, AES.MODE_ECB)

    def derive(prefix: int) -> bytes:
        block = (
            bytes([prefix])
            + app_nonce.to_bytes(3, "little")
            + net_id.to_bytes(3, "little")
            + dev_nonce.to_bytes(2, "little")
        )
        return cipher.encrypt(block.ljust(16, b"\x00"))

    return derive(0x01), derive(0x02)
