import base64
from datetime import UTC, datetime

import pytest

from telemetry import chirpstack_event as ce
from telemetry import lpp


def _event(**over):
    event = {
        "deduplicationId": "11111111-2222-3333-4444-555555555555",
        "time": "2026-01-02T03:04:05.123456789+00:00",
        "deviceInfo": {
            "tenantName": "ChirpStack",
            "applicationName": "sim-sensors",
            "deviceName": "dev-ok",
            "devEui": "0A0B0C0D0E0F0001",
        },
        "devAddr": "01000001",
        "fCnt": 7,
        "fPort": 1,
        "data": base64.b64encode(lpp.encode_reading(22.5, 46.0, 3250)).decode(),
        "rxInfo": [
            {"gatewayId": "0102030405060708", "rssi": -95, "snr": 4.5},
            {"gatewayId": "aaaaaaaaaaaaaaaa", "rssi": -80, "snr": 8.0},
        ],
    }
    event.update(over)
    return event


def test_extract_fields():
    row = ce.extract(_event())
    assert row.dev_eui == "0a0b0c0d0e0f0001"
    assert row.device_name == "dev-ok"
    assert row.received_at == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert row.f_cnt == 7
    # the best gateway reception wins
    assert (row.rssi, row.snr, row.gateway_id) == (-80, 8.0, "aaaaaaaaaaaaaaaa")
    assert (row.temperature_c, row.humidity_pct, row.battery_mv) == (22.5, 46.0, 3250)


def test_default_valued_fields_may_be_omitted():
    event = _event()
    del event["fCnt"]
    event["rxInfo"] = [{"gatewayId": "0102030405060708", "rssi": -70}]
    row = ce.extract(event)
    assert row.f_cnt == 0
    assert row.snr == 0.0


def test_no_payload_and_no_rxinfo():
    row = ce.extract(_event(data="", rxInfo=[]))
    assert row.rssi is None and row.gateway_id is None
    assert row.temperature_c is None


def test_time_without_fraction_and_with_z():
    assert ce.parse_time("2026-01-02T03:04:05Z").microsecond == 0
    assert ce.parse_time("2026-01-02T03:04:05.5Z").microsecond == 500000


def test_missing_fields_raise():
    with pytest.raises(ce.EventError):
        ce.extract({"time": "2026-01-02T03:04:05Z"})
    with pytest.raises(ce.EventError):
        ce.extract(_event(time="not a time"))
    with pytest.raises(ce.EventError):
        ce.extract(_event(data="!!!"))
