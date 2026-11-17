"""Simulator: one fake gateway forwarding uplinks of fake sensors.

Sends Semtech UDP PUSH_DATA to the gateway bridge. The scenario is fixed (see
telemetry/scenario.py): dev-ok is regular with good RSSI, dev-gap skips some
frame counters, dev-weak has RSSI below the approval threshold, dev-quiet goes silent for a
while and dev-battery loses too much battery.

Environment (all optional):
  BRIDGE_HOST  default chirpstack-gateway-bridge (use 127.0.0.1 from the host)
  BRIDGE_PORT  default 1700
  INTERVAL_S   pause between uplinks, default 0.3

Fault injection, used by scripts/fault-lab.sh (default: the full scenario, no fault):
  SIM_DEVICE       send only SIM_COUNT (default 5) regular uplinks of this device
  SIM_COUNT        number of uplinks in that burst
  SIM_GATEWAY_EUI  hex EUI to use instead of the scenario gateway (a gateway nobody registered)
  SIM_WRONG_KEY    set to 1 to sign the frames with a wrong NwkSKey (invalid MIC)
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from datetime import UTC, datetime

from telemetry import lorawan, lpp, scenario, semtech_udp

log = logging.getLogger("simulator")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = os.environ.get("BRIDGE_HOST", "chirpstack-gateway-bridge")
    port = int(os.environ.get("BRIDGE_PORT", "1700"))
    interval = float(os.environ.get("INTERVAL_S", "0.3"))

    gateway_eui = bytes.fromhex(os.environ.get("SIM_GATEWAY_EUI", scenario.GATEWAY_EUI.hex()))
    wrong_key = os.environ.get("SIM_WRONG_KEY") == "1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    addr = (socket.gethostbyname(host), port)
    start = time.monotonic()
    token = 0
    unacked = 0

    # PULL_DATA tells the bridge the gateway is alive (needed for downlinks).
    sock.sendto(semtech_udp.build_pull_data(gateway_eui, 0xFFFF), addr)

    only = os.environ.get("SIM_DEVICE")
    if only:
        steps = scenario.burst_steps(only, int(os.environ.get("SIM_COUNT", "5")))
    else:
        steps = scenario.steps()
    for step in steps:
        if step.pause_before_s:
            log.info("silence of %.0f s before %s", step.pause_before_s, step.device.name)
            time.sleep(step.pause_before_s)
        token = (token + 1) & 0xFFFF
        phy = lorawan.build_uplink(
            dev_addr=step.device.dev_addr,
            nwk_s_key=bytes(16) if wrong_key else step.device.nwk_s_key,
            app_s_key=step.device.app_s_key,
            f_cnt=step.f_cnt,
            f_port=scenario.F_PORT,
            payload=lpp.encode_reading(step.temperature_c, step.humidity_pct, step.battery_mv),
        )
        rxpk = semtech_udp.build_rxpk(
            phy,
            tmst=int((time.monotonic() - start) * 1_000_000) & 0xFFFFFFFF,
            rssi=step.rssi,
            lsnr=step.snr,
            time=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        sock.sendto(semtech_udp.build_push_data(gateway_eui, token, [rxpk]), addr)
        # The bridge also answers PULL_DATA with PULL_ACK: skip it, wait for our PUSH_ACK.
        acked = False
        deadline = time.monotonic() + 2.0
        while not acked and time.monotonic() < deadline:
            try:
                ack, _ = sock.recvfrom(64)
            except TimeoutError:
                break
            acked = semtech_udp.is_push_ack(ack, token)
        if not acked:
            unacked += 1
        log.info(
            "sent %s fCnt=%d rssi=%d temp=%.1f",
            step.device.name,
            step.f_cnt,
            step.rssi,
            step.temperature_c,
        )
        time.sleep(interval)

    log.info("done: %d uplinks sent, %d without PUSH_ACK", len(steps), unacked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
