"""LoRaWAN 1.0.3 uplink PHYPayload, ABP (no join): build and parse.

Layout (LoRaWAN 1.0.3 specification, chapter 4):
``MHDR | DevAddr | FCtrl | FCnt | FOpts | FPort | FRMPayload | MIC``
with multi-byte fields little endian. FRMPayload is encrypted with AES-128 in
the counter-like scheme of section 4.3.3 (AppSKey for FPort > 0, NwkSKey for
FPort 0). The MIC is the first 4 bytes of AES-CMAC(NwkSKey, B0 | msg)
(section 4.4). Only the uplink direction is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Hash import CMAC

MTYPE_UNCONFIRMED_UP = 0b010
MTYPE_CONFIRMED_UP = 0b100
DIR_UP = 0
MIC_LEN = 4


class LorawanError(ValueError):
    """Raised on a malformed frame or a MIC mismatch."""


@dataclass(frozen=True)
class Uplink:
    dev_addr: int
    f_cnt: int  # 16 bit value carried in the frame, plus the supplied upper bits
    f_port: int | None
    payload: bytes  # decrypted FRMPayload
    confirmed: bool
    adr: bool
    f_opts: bytes
    mic: bytes


def _check_key(key: bytes, name: str) -> None:
    if len(key) != 16:
        raise LorawanError(f"{name} must be 16 bytes")


def _a_block(dev_addr: int, f_cnt: int, i: int) -> bytes:
    # 0x01 | 4 x 0x00 | Dir | DevAddr | FCnt (32 bit) | 0x00 | block index
    return (
        bytes([0x01, 0, 0, 0, 0, DIR_UP])
        + dev_addr.to_bytes(4, "little")
        + f_cnt.to_bytes(4, "little")
        + bytes([0x00, i])
    )


def crypt_payload(key: bytes, dev_addr: int, f_cnt: int, data: bytes) -> bytes:
    """Encrypt or decrypt FRMPayload (the operation is its own inverse)."""
    _check_key(key, "key")
    cipher = AES.new(key, AES.MODE_ECB)
    out = bytearray()
    for block in range((len(data) + 15) // 16):
        stream = cipher.encrypt(_a_block(dev_addr, f_cnt, block + 1))
        chunk = data[block * 16 : (block + 1) * 16]
        out += bytes(a ^ b for a, b in zip(chunk, stream, strict=False))
    return bytes(out)


def compute_mic(nwk_s_key: bytes, dev_addr: int, f_cnt: int, msg: bytes) -> bytes:
    """MIC of ``msg`` (MHDR through FRMPayload) for an uplink."""
    _check_key(nwk_s_key, "nwk_s_key")
    b0 = (
        bytes([0x49, 0, 0, 0, 0, DIR_UP])
        + dev_addr.to_bytes(4, "little")
        + f_cnt.to_bytes(4, "little")
        + bytes([0x00, len(msg)])
    )
    mac = CMAC.new(nwk_s_key, ciphermod=AES)
    mac.update(b0 + msg)
    return mac.digest()[:MIC_LEN]


def build_uplink(
    *,
    dev_addr: int,
    nwk_s_key: bytes,
    app_s_key: bytes,
    f_cnt: int,
    f_port: int,
    payload: bytes,
    confirmed: bool = False,
    adr: bool = False,
    f_opts: bytes = b"",
) -> bytes:
    """Build an uplink PHYPayload. ``f_cnt`` is the full 32 bit counter."""
    _check_key(app_s_key, "app_s_key")
    if len(f_opts) > 15:
        raise LorawanError("FOpts is at most 15 bytes")
    if not 0 <= f_port <= 255:
        raise LorawanError("FPort out of range")
    mhdr = (MTYPE_CONFIRMED_UP if confirmed else MTYPE_UNCONFIRMED_UP) << 5  # Major 0
    fctrl = (0x80 if adr else 0) | len(f_opts)
    key = nwk_s_key if f_port == 0 else app_s_key
    msg = (
        bytes([mhdr])
        + dev_addr.to_bytes(4, "little")
        + bytes([fctrl])
        + (f_cnt & 0xFFFF).to_bytes(2, "little")
        + f_opts
        + bytes([f_port])
        + crypt_payload(key, dev_addr, f_cnt, payload)
    )
    return msg + compute_mic(nwk_s_key, dev_addr, f_cnt, msg)


def parse_uplink(phy: bytes, *, nwk_s_key: bytes, app_s_key: bytes, f_cnt_msb: int = 0) -> Uplink:
    """Parse an uplink, verify the MIC and decrypt the payload.

    ``f_cnt_msb`` supplies the upper 16 bits of the 32 bit counter, which are
    not carried in the frame.
    """
    if len(phy) < 1 + 7 + MIC_LEN:
        raise LorawanError("frame too short")
    mtype = phy[0] >> 5
    if mtype not in (MTYPE_UNCONFIRMED_UP, MTYPE_CONFIRMED_UP):
        raise LorawanError("not an uplink data frame")
    dev_addr = int.from_bytes(phy[1:5], "little")
    fctrl = phy[5]
    f_cnt = (f_cnt_msb << 16) | int.from_bytes(phy[6:8], "little")
    pos = 8 + (fctrl & 0x0F)
    if pos > len(phy) - MIC_LEN:
        raise LorawanError("FOpts overruns the frame")
    f_opts = phy[8:pos]
    msg, mic = phy[:-MIC_LEN], phy[-MIC_LEN:]
    if compute_mic(nwk_s_key, dev_addr, f_cnt, msg) != mic:
        raise LorawanError("MIC mismatch")
    f_port = None
    payload = b""
    if pos < len(msg):
        f_port = msg[pos]
        key = nwk_s_key if f_port == 0 else app_s_key
        payload = crypt_payload(key, dev_addr, f_cnt, msg[pos + 1 :])
    return Uplink(
        dev_addr=dev_addr,
        f_cnt=f_cnt,
        f_port=f_port,
        payload=payload,
        confirmed=mtype == MTYPE_CONFIRMED_UP,
        adr=bool(fctrl & 0x80),
        f_opts=f_opts,
        mic=mic,
    )
