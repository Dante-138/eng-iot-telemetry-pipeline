"""Simulator: fake gateways forwarding the uplinks of fake sensors.

Sends Semtech UDP PUSH_DATA to the gateway bridge and keeps the gateway "alive" with PULL_DATA so
that the bridge can send downlinks (PULL_RESP), which are answered with TX_ACK. The scenario is
fixed (see telemetry/scenario.py): dev-ok is regular with good RSSI, dev-gap skips some frame
counters, dev-weak has RSSI below the approval threshold, dev-quiet goes silent for a while,
dev-battery loses too much battery, and dev-otaa joins over the air (OTAA), sends uplinks and
decrypts the downlink queued for it.

Environment (all optional):
  BRIDGE_HOST  default chirpstack-gateway-bridge (use 127.0.0.1 from the host)
  BRIDGE_PORT  default 1700
  INTERVAL_S   pause between uplinks, default 0.3
  SIM_DEV_NONCE  DevNonce of the OTAA join (default: seconds since the epoch, modulo 65536, so
                 that every run uses a fresh one: ChirpStack refuses a repeated DevNonce)
  SIM_EXPECT_DOWNLINK  set to 1 to exit with an error unless dev-otaa receives and decrypts the
                       downlink payload of the scenario (scripts/e2e.sh does)

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

from telemetry import lorawan, lpp, otaa, scenario, semtech_udp

log = logging.getLogger("simulator")

PULL_INTERVAL_S = 5.0


class Gateway:
    """One fake gateway: a UDP socket to the bridge, the PULL keep-alive and downlink receipt."""

    def __init__(self, eui: bytes, host: str, port: int, start: float) -> None:
        self.eui = eui
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = (socket.gethostbyname(host), port)
        self.start = start
        self.token = 0
        self.unacked = 0
        self.sent = 0
        self.downlinks: list[bytes] = []  # PHYPayloads received in PULL_RESP, oldest first
        self._last_pull = float("-inf")

    def _next_token(self) -> int:
        self.token = (self.token + 1) & 0xFFFF
        return self.token

    def keepalive(self) -> None:
        """PULL_DATA tells the bridge where to send downlinks. Repeat it every few seconds."""
        if time.monotonic() - self._last_pull >= PULL_INTERVAL_S:
            self.sock.sendto(semtech_udp.build_pull_data(self.eui, self._next_token()), self.addr)
            self._last_pull = time.monotonic()

    def _handle(self, datagram: bytes) -> int | None:
        """Process one datagram from the bridge. Returns the token of a PUSH_ACK, if it is one."""
        if semtech_udp.is_pull_resp(datagram):
            try:
                resp = semtech_udp.parse_pull_resp(datagram)
                self.downlinks.append(resp.phy_payload)
                error = "NONE"
            except semtech_udp.SemtechError as exc:
                log.warning("bad PULL_RESP: %s", exc)
                error = "TX_FREQ"
            self.sock.sendto(
                semtech_udp.build_tx_ack(self.eui, int.from_bytes(datagram[1:3], "big"), error),
                self.addr,
            )
            return None
        if len(datagram) == 4 and datagram[3] == semtech_udp.PUSH_ACK:
            return int.from_bytes(datagram[1:3], "big")
        return None  # PULL_ACK and anything else

    def pump(self, seconds: float) -> None:
        """Receive and answer datagrams for up to ``seconds`` (downlinks and acknowledgements)."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            self.keepalive()
            self.sock.settimeout(min(left, 0.5))
            try:
                self._handle(self.sock.recvfrom(2048)[0])
            except TimeoutError:
                pass

    def push(self, phy: bytes, rssi: int, snr: float) -> None:
        """Send one received frame (PUSH_DATA) and wait for its PUSH_ACK."""
        self.keepalive()
        token = self._next_token()
        rxpk = semtech_udp.build_rxpk(
            phy,
            tmst=int((time.monotonic() - self.start) * 1_000_000) & 0xFFFFFFFF,
            rssi=rssi,
            lsnr=snr,
            time=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        self.sock.sendto(semtech_udp.build_push_data(self.eui, token, [rxpk]), self.addr)
        self.sent += 1
        acked = False
        deadline = time.monotonic() + 2.0
        while not acked and time.monotonic() < deadline:
            self.sock.settimeout(max(deadline - time.monotonic(), 0.05))
            try:
                acked = self._handle(self.sock.recvfrom(2048)[0]) == token
            except TimeoutError:
                break
        if not acked:
            self.unacked += 1


def run_otaa(gw: Gateway, interval: float) -> tuple[bool, list[str]]:
    """Join dev-otaa, send its uplinks, decrypt its downlinks: (joined, downlink texts)."""
    dev_nonce = int(os.environ.get("SIM_DEV_NONCE", int(time.time()))) & 0xFFFF
    key = scenario.OTAA_APP_KEY
    gw.push(
        otaa.build_join_request(key, scenario.OTAA_JOIN_EUI, scenario.OTAA_DEV_EUI, dev_nonce),
        rssi=-85,
        snr=9.0,
    )
    accept = None
    deadline = time.monotonic() + 15
    while accept is None and time.monotonic() < deadline:
        gw.pump(0.5)
        for phy in gw.downlinks:
            if phy[:1] == bytes([otaa.MHDR_JOIN_ACCEPT]):
                accept = otaa.parse_join_accept(phy, key)
        gw.downlinks.clear()
    if accept is None:
        log.error("no JoinAccept for %s within 15 s", scenario.OTAA_NAME)
        return False, []
    nwk_s_key, app_s_key = otaa.derive_session_keys(key, accept.app_nonce, accept.net_id, dev_nonce)
    log.info(
        "joined %s DevAddr=%08x NetID=%06x DevNonce=%d",
        scenario.OTAA_NAME,
        accept.dev_addr,
        accept.net_id,
        dev_nonce,
    )

    received: list[str] = []

    def collect() -> None:
        for phy in gw.downlinks:
            try:
                down = lorawan.parse_downlink(phy, nwk_s_key=nwk_s_key, app_s_key=app_s_key)
            except lorawan.LorawanError as exc:
                log.warning("downlink for %s not valid: %s", scenario.OTAA_NAME, exc)
                continue
            text = down.payload.decode("ascii", "replace")
            log.info(
                "downlink %s fCnt=%d fPort=%s payload=%s text=%s",
                scenario.OTAA_NAME,
                down.f_cnt,
                down.f_port,
                down.payload.hex(),
                text,
            )
            if down.payload:
                received.append(text)
        gw.downlinks.clear()

    for step in scenario.otaa_steps():
        phy = lorawan.build_uplink(
            dev_addr=accept.dev_addr,
            nwk_s_key=nwk_s_key,
            app_s_key=app_s_key,
            f_cnt=step.f_cnt,
            f_port=scenario.F_PORT,
            payload=lpp.encode_reading(step.temperature_c, step.humidity_pct, step.battery_mv),
        )
        gw.push(phy, step.rssi, step.snr)
        log.info(
            "sent %s fCnt=%d rssi=%d temp=%.1f",
            scenario.OTAA_NAME,
            step.f_cnt,
            step.rssi,
            step.temperature_c,
        )
        gw.pump(interval)  # the downlink for this uplink arrives here
        collect()
    gw.pump(3.0)
    collect()
    return True, received


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = os.environ.get("BRIDGE_HOST", "chirpstack-gateway-bridge")
    port = int(os.environ.get("BRIDGE_PORT", "1700"))
    interval = float(os.environ.get("INTERVAL_S", "0.3"))
    gateway_eui = bytes.fromhex(os.environ.get("SIM_GATEWAY_EUI", scenario.GATEWAY_EUI.hex()))
    wrong_key = os.environ.get("SIM_WRONG_KEY") == "1"
    only = os.environ.get("SIM_DEVICE")

    gw = Gateway(gateway_eui, host, port, time.monotonic())
    gw.keepalive()

    if only:
        steps = scenario.burst_steps(only, int(os.environ.get("SIM_COUNT", "5")))
    else:
        steps = scenario.steps()
    for step in steps:
        if step.pause_before_s:
            log.info("silence of %.0f s before %s", step.pause_before_s, step.device.name)
            gw.pump(step.pause_before_s)
        phy = lorawan.build_uplink(
            dev_addr=step.device.dev_addr,
            nwk_s_key=bytes(16) if wrong_key else step.device.nwk_s_key,
            app_s_key=step.device.app_s_key,
            f_cnt=step.f_cnt,
            f_port=scenario.F_PORT,
            payload=lpp.encode_reading(step.temperature_c, step.humidity_pct, step.battery_mv),
        )
        gw.push(phy, step.rssi, step.snr)
        log.info(
            "sent %s fCnt=%d rssi=%d temp=%.1f",
            step.device.name,
            step.f_cnt,
            step.rssi,
            step.temperature_c,
        )
        gw.pump(interval)

    status = 0
    total = len(steps)
    if not only:
        joined, downlinks = run_otaa(gw, scenario.OTAA_INTERVAL_S)
        total += scenario.OTAA_UPLINKS if joined else 0
        log.info("downlinks decrypted by %s: %s", scenario.OTAA_NAME, downlinks)
        if os.environ.get("SIM_EXPECT_DOWNLINK") == "1" and (
            scenario.DOWNLINK_PAYLOAD.decode() not in downlinks
        ):
            log.error("expected downlink %r not received", scenario.DOWNLINK_PAYLOAD)
            status = 1
        if not joined:
            status = 1

    log.info("done: %d uplinks sent, %d without PUSH_ACK", total, gw.unacked)
    return status


if __name__ == "__main__":
    sys.exit(main())
