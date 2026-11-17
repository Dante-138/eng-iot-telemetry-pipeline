"""Deterministic simulator scenario: 3 fake sensors behind 1 fake gateway.

All keys and identifiers are fixed test values, clearly fake, used only on the
local bench. They are shared by the simulator and the provisioning script.
"""

from __future__ import annotations

from dataclasses import dataclass

GATEWAY_EUI = bytes.fromhex("0102030405060708")
GATEWAY_NAME = "sim-gateway"
APPLICATION_NAME = "sim-sensors"
F_PORT = 1

UPLINKS_PER_DEVICE = 12
# dev-gap never sends these frame counters: 3 missing frames, one jump.
GAP_SKIPPED = frozenset({4, 5, 6})
# dev-quiet sends 8 frames: 0 to 3 with the others, then stays silent for
# QUIET_PAUSE_S and sends 4 to 7 after the main run. The approval report only fails it
# with config/thresholds.e2e.toml, whose max_interval_s is shorter than the pause.
QUIET_UPLINKS = 8
QUIET_HEAD = 4
QUIET_PAUSE_S = 8.0
# dev-battery starts at 3600 mV and loses 30 mV per frame: 330 mV drop in 12 frames,
# above battery_max_drop_mv (200) but never below battery_min_mv (3000).
BATTERY_START_MV = 3600
BATTERY_STEP_MV = 30

# dev-otaa joins over the air (LoRaWAN 1.0.3 OTAA) after the ABP devices are done, sends
# OTAA_UPLINKS uplinks one per OTAA_INTERVAL_S and receives the downlink that the end to end
# script queues through the ChirpStack API. Fixed, obviously fake local development values.
OTAA_NAME = "dev-otaa"
OTAA_DEV_EUI = bytes.fromhex("0a0b0c0d0e0f0006")
OTAA_JOIN_EUI = bytes.fromhex("1011121314151617")
OTAA_APP_KEY = bytes.fromhex("c0c1c2c3c4c5c6c7c8c9cacbcccdcecf")
OTAA_UPLINKS = 12
OTAA_INTERVAL_S = 1.0
DOWNLINK_F_PORT = 10
DOWNLINK_PAYLOAD = b"bench-downlink"


@dataclass(frozen=True)
class SimDevice:
    name: str
    dev_eui: bytes
    dev_addr: int
    nwk_s_key: bytes
    app_s_key: bytes


def _device(name: str, index: int) -> SimDevice:
    # DevAddr below 0x02000000 sits inside the NetID 000000 address space.
    return SimDevice(
        name=name,
        dev_eui=bytes.fromhex("0a0b0c0d0e0f00") + bytes([index]),
        dev_addr=0x01000000 + index,
        nwk_s_key=bytes.fromhex("000102030405060708090a0b0c0d0e") + bytes([index]),
        app_s_key=bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfe") + bytes([index]),
    )


DEVICES = {
    d.name: d
    for d in (
        _device("dev-ok", 1),
        _device("dev-gap", 2),
        _device("dev-weak", 3),
        _device("dev-quiet", 4),
        _device("dev-battery", 5),
    )
}


@dataclass(frozen=True)
class Step:
    device: SimDevice
    f_cnt: int
    rssi: int
    snr: float
    temperature_c: float
    humidity_pct: float
    battery_mv: int
    pause_before_s: float = 0.0  # the simulator sleeps this long before sending


def _step(dev: SimDevice, n: int, pause_before_s: float = 0.0) -> Step:
    if dev.name == "dev-weak":
        rssi, snr = -118 - (n % 3), -12.0 + (n % 3) * 0.5
    else:
        rssi, snr = -85 - (n % 4), 9.0 - (n % 4) * 0.25
    if dev.name == "dev-battery":
        battery_mv = BATTERY_START_MV - BATTERY_STEP_MV * n
    else:
        battery_mv = 3300 - 10 * n
    return Step(
        device=dev,
        f_cnt=n,
        rssi=rssi,
        snr=snr,
        temperature_c=round(22.0 + 0.1 * n, 1),
        humidity_pct=45.0 + (n % 5),
        battery_mv=battery_mv,
        pause_before_s=pause_before_s,
    )


def steps() -> list[Step]:
    """Uplinks in transmission order, interleaving the devices.

    dev-ok: regular, RSSI around -87 dBm. dev-gap: same radio, but frames 4 to
    6 are never sent. dev-weak: regular, RSSI around -119 dBm. dev-quiet: goes
    silent after frame 3 (its last 4 frames come after a pause, at the end).
    dev-battery: regular, but the battery falls 330 mV.
    """
    out: list[Step] = []
    for n in range(UPLINKS_PER_DEVICE):
        for name, dev in DEVICES.items():
            if name == "dev-gap" and n in GAP_SKIPPED:
                continue
            if name == "dev-quiet" and n >= QUIET_HEAD:
                continue
            out.append(_step(dev, n))
    quiet = DEVICES["dev-quiet"]
    for n in range(QUIET_HEAD, QUIET_UPLINKS):
        out.append(_step(quiet, n, QUIET_PAUSE_S if n == QUIET_HEAD else 0.0))
    return out


def burst_steps(device_name: str, count: int, first_f_cnt: int = 0) -> list[Step]:
    """A short run of regular uplinks of one device (used by scripts/fault-lab.sh)."""
    return [_step(DEVICES[device_name], first_f_cnt + n) for n in range(count)]


def otaa_steps() -> list[Step]:
    """Uplinks of dev-otaa. DevAddr and session keys come from the join, so they are empty here."""
    dev = SimDevice(OTAA_NAME, OTAA_DEV_EUI, 0, b"", b"")
    return [_step(dev, n) for n in range(OTAA_UPLINKS)]


def expected_uplinks() -> int:
    """Rows expected in the uplinks table after the whole scenario."""
    return len(steps()) + OTAA_UPLINKS


def expected_gateway_frames() -> int:
    """Frames the gateway hands to the bridge: every uplink plus the OTAA JoinRequest."""
    return expected_uplinks() + 1
