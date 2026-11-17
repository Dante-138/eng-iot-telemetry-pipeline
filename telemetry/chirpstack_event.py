"""Extract table fields from a ChirpStack v4 MQTT "up" event (JSON)."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime

from telemetry import lpp


class EventError(ValueError):
    """Raised when the event lacks the fields needed for a row."""


@dataclass(frozen=True)
class UplinkRow:
    dev_eui: str
    device_name: str
    received_at: datetime
    f_cnt: int
    rssi: int | None
    snr: float | None
    temperature_c: float | None
    humidity_pct: float | None
    battery_mv: int | None
    gateway_id: str | None


def parse_time(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, keeping microsecond precision."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        digits = ""
        for ch in rest:
            if not ch.isdigit():
                break
            digits += ch
        tail = rest[len(digits) :]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def extract(event: dict) -> UplinkRow:
    """Build a row from an up event. Cayenne LPP is decoded from ``data``."""
    try:
        info = event["deviceInfo"]
        dev_eui = str(info["devEui"]).lower()
        received_at = parse_time(event["time"])
    except (KeyError, ValueError, TypeError) as exc:
        raise EventError(f"missing or invalid field: {exc}") from exc

    # ChirpStack JSON omits fields that equal their default, such as fCnt 0.
    f_cnt = int(event.get("fCnt", 0))

    best = None
    for rx in event.get("rxInfo") or []:
        if best is None or rx.get("rssi", -999) > best.get("rssi", -999):
            best = rx
    rssi = snr = gateway_id = None
    if best is not None:
        rssi = int(best.get("rssi", 0))
        snr = float(best.get("snr", 0.0))
        gateway_id = best.get("gatewayId")

    reading = lpp.Reading(None, None, None)
    data = event.get("data")
    if data:
        try:
            reading = lpp.decode_reading(base64.b64decode(data, validate=True))
        except (binascii.Error, lpp.LppError) as exc:
            raise EventError(f"invalid payload: {exc}") from exc

    return UplinkRow(
        dev_eui=dev_eui,
        device_name=str(info.get("deviceName", dev_eui)),
        received_at=received_at,
        f_cnt=f_cnt,
        rssi=rssi,
        snr=snr,
        temperature_c=reading.temperature_c,
        humidity_pct=reading.humidity_pct,
        battery_mv=reading.battery_mv,
        gateway_id=gateway_id,
    )
