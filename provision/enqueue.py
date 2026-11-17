"""Queue the scenario downlink for dev-otaa through the ChirpStack gRPC API.

Usage: python -m provision.enqueue

The payload and FPort come from telemetry/scenario.py. ChirpStack sends a Class A downlink in
the receive window after the next uplink of the device, so run this after the join and before
the last uplinks (scripts/e2e.sh does it while the simulator is running).

Environment (all optional): CHIRPSTACK_API (default chirpstack:8080), CHIRPSTACK_EMAIL and
CHIRPSTACK_PASSWORD (default admin, local development only).
"""

from __future__ import annotations

import logging
import os
import sys

import grpc
from chirpstack_api.api import device_pb2, device_pb2_grpc

from provision.provision import _wait_for_login
from telemetry import scenario

log = logging.getLogger("enqueue")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    channel = grpc.insecure_channel(os.environ.get("CHIRPSTACK_API", "chirpstack:8080"))
    jwt = _wait_for_login(
        channel,
        os.environ.get("CHIRPSTACK_EMAIL", "admin"),
        os.environ.get("CHIRPSTACK_PASSWORD", "admin"),
    )
    devices = device_pb2_grpc.DeviceServiceStub(channel)
    resp = devices.Enqueue(
        device_pb2.EnqueueDeviceQueueItemRequest(
            queue_item=device_pb2.DeviceQueueItem(
                dev_eui=scenario.OTAA_DEV_EUI.hex(),
                confirmed=False,
                f_port=scenario.DOWNLINK_F_PORT,
                data=scenario.DOWNLINK_PAYLOAD,
            )
        ),
        metadata=[("authorization", f"Bearer {jwt}")],
    )
    log.info(
        "queued downlink %s for %s: fPort %d, %r",
        resp.id,
        scenario.OTAA_NAME,
        scenario.DOWNLINK_F_PORT,
        scenario.DOWNLINK_PAYLOAD,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
