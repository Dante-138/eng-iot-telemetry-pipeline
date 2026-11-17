import pytest

from telemetry import lpp


def test_public_example_from_cayenne_docs():
    # Example in the Cayenne LPP documentation (see telemetry/lpp.py):
    # channel 3 temperature 27.2 degC, channel 5 temperature 25.5 degC.
    values = lpp.decode(bytes.fromhex("03670110" + "056700ff"))
    assert [(v.channel, v.value) for v in values] == [(3, 27.2), (5, 25.5)]


def test_encode_project_frame_layout():
    # 22.5 degC -> 225 (0x00e1); 46.5 %RH -> 93 (0x5d); 3.30 V -> 330 (0x014a)
    frame = lpp.encode_reading(22.5, 46.5, 3300)
    assert frame == bytes.fromhex("016700e1" + "02685d" + "0302014a")


def test_round_trip():
    reading = lpp.decode_reading(lpp.encode_reading(-4.3, 80.0, 3190))
    assert reading == lpp.Reading(-4.3, 80.0, 3190)


def test_negative_temperature():
    assert lpp.decode(lpp.encode_temperature(1, -10.5))[0].value == -10.5


def test_missing_channels_are_none():
    reading = lpp.decode_reading(lpp.encode_temperature(1, 20.0))
    assert reading == lpp.Reading(20.0, None, None)


def test_truncated_and_unknown_type():
    with pytest.raises(lpp.LppError):
        lpp.decode(bytes.fromhex("0167" + "00"))
    with pytest.raises(lpp.LppError):
        lpp.decode(bytes.fromhex("01ff00"))
    with pytest.raises(lpp.LppError):
        lpp.decode(b"\x01")


def test_out_of_range():
    with pytest.raises(lpp.LppError):
        lpp.encode_temperature(1, 5000.0)
