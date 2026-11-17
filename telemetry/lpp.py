"""Cayenne LPP encoder and decoder.

Frame layout: repeated records of ``channel (1 byte) | type (1 byte) | data``.
Only the types this project needs are implemented. Public format description:
https://docs.mydevices.com/docs/lorawan/cayenne-lpp

Battery voltage: Cayenne LPP has no battery type, so the battery is sent as an
Analog Input (type 0x02, signed 16 bit, 0.01 resolution) in volts on channel 3.
A battery of 3300 mV is therefore encoded as the value 3.30 (raw 330). The
resolution of this encoding is 10 mV.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CH_TEMPERATURE = 1
CH_HUMIDITY = 2
CH_BATTERY = 3

TYPE_DIGITAL_INPUT = 0x00
TYPE_DIGITAL_OUTPUT = 0x01
TYPE_ANALOG_INPUT = 0x02
TYPE_ANALOG_OUTPUT = 0x03
TYPE_ILLUMINANCE = 0x65
TYPE_PRESENCE = 0x66
TYPE_TEMPERATURE = 0x67
TYPE_HUMIDITY = 0x68
TYPE_BAROMETER = 0x73

# type -> (data length in bytes, struct format, scale divisor)
_TYPES: dict[int, tuple[int, str, float]] = {
    TYPE_DIGITAL_INPUT: (1, ">B", 1),
    TYPE_DIGITAL_OUTPUT: (1, ">B", 1),
    TYPE_ANALOG_INPUT: (2, ">h", 100),
    TYPE_ANALOG_OUTPUT: (2, ">h", 100),
    TYPE_ILLUMINANCE: (2, ">H", 1),
    TYPE_PRESENCE: (1, ">B", 1),
    TYPE_TEMPERATURE: (2, ">h", 10),
    TYPE_HUMIDITY: (1, ">B", 2),
    TYPE_BAROMETER: (2, ">H", 10),
}


class LppError(ValueError):
    """Raised on a malformed or unsupported LPP frame."""


@dataclass(frozen=True)
class LppValue:
    channel: int
    type: int
    value: float


def _encode(channel: int, type_: int, value: float) -> bytes:
    _, fmt, scale = _TYPES[type_]
    raw = round(value * scale)
    try:
        return bytes([channel, type_]) + struct.pack(fmt, raw)
    except struct.error as exc:
        raise LppError(f"value {value} out of range for type 0x{type_:02x}") from exc


def encode_temperature(channel: int, celsius: float) -> bytes:
    """Temperature Sensor: signed 16 bit, 0.1 degC per bit."""
    return _encode(channel, TYPE_TEMPERATURE, celsius)


def encode_humidity(channel: int, percent: float) -> bytes:
    """Humidity Sensor: unsigned 8 bit, 0.5 %RH per bit."""
    return _encode(channel, TYPE_HUMIDITY, percent)


def encode_analog_input(channel: int, value: float) -> bytes:
    """Analog Input: signed 16 bit, 0.01 per bit."""
    return _encode(channel, TYPE_ANALOG_INPUT, value)


def encode_battery_mv(channel: int, millivolts: int) -> bytes:
    """Battery voltage as an Analog Input in volts (see module docstring)."""
    return encode_analog_input(channel, millivolts / 1000)


def encode_reading(temperature_c: float, humidity_pct: float, battery_mv: int) -> bytes:
    """The frame this project's sensors send: temperature, humidity, battery."""
    return (
        encode_temperature(CH_TEMPERATURE, temperature_c)
        + encode_humidity(CH_HUMIDITY, humidity_pct)
        + encode_battery_mv(CH_BATTERY, battery_mv)
    )


def decode(data: bytes) -> list[LppValue]:
    """Decode a full LPP frame into a list of values."""
    out: list[LppValue] = []
    i = 0
    while i < len(data):
        if i + 2 > len(data):
            raise LppError("truncated record header")
        channel, type_ = data[i], data[i + 1]
        if type_ not in _TYPES:
            raise LppError(f"unsupported type 0x{type_:02x}")
        length, fmt, scale = _TYPES[type_]
        start = i + 2
        if start + length > len(data):
            raise LppError("truncated record data")
        (raw,) = struct.unpack(fmt, data[start : start + length])
        out.append(LppValue(channel, type_, raw / scale))
        i = start + length
    return out


@dataclass(frozen=True)
class Reading:
    temperature_c: float | None
    humidity_pct: float | None
    battery_mv: int | None


def decode_reading(data: bytes) -> Reading:
    """Decode the project frame: temperature ch 1, humidity ch 2, battery ch 3."""
    temperature = humidity = None
    battery = None
    for v in decode(data):
        if v.channel == CH_TEMPERATURE and v.type == TYPE_TEMPERATURE:
            temperature = v.value
        elif v.channel == CH_HUMIDITY and v.type == TYPE_HUMIDITY:
            humidity = v.value
        elif v.channel == CH_BATTERY and v.type == TYPE_ANALOG_INPUT:
            battery = round(v.value * 1000)
    return Reading(temperature, humidity, battery)
