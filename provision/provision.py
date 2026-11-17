"""Provision ChirpStack over gRPC with the simulator's gateway and devices.

Idempotent: run it as often as you like. It logs in with the default local
development admin account, then ensures that these exist in the default
tenant: the gateway, an application, a LoRaWAN 1.0.3 device profile without
OTAA, and 3 ABP devices (frame counter check off) with their session activated.

Environment (all optional):
  CHIRPSTACK_API      host:port of the gRPC API (default chirpstack:8080)
  CHIRPSTACK_EMAIL    default admin                  (local development only)
  CHIRPSTACK_PASSWORD default admin                  (local development only)
  REGION              eu868 (default) or au915_0, matching enabled_regions
"""

from __future__ import annotations

import logging
import os
import sys
import time

import grpc
from chirpstack_api import common
from chirpstack_api.api import (
    application_pb2,
    application_pb2_grpc,
    device_pb2,
    device_pb2_grpc,
    device_profile_pb2,
    device_profile_pb2_grpc,
    gateway_pb2,
    gateway_pb2_grpc,
    internal_pb2,
    internal_pb2_grpc,
    tenant_pb2,
    tenant_pb2_grpc,
)

from telemetry import scenario

log = logging.getLogger("provision")

PROFILE_NAME = "sim-abp-lorawan-1.0.3"

# REGION value -> (common.Region name)
REGIONS = {"eu868": "EU868", "au915_0": "AU915"}


def _wait_for_login(channel: grpc.Channel, email: str, password: str, timeout_s: int = 120) -> str:
    stub = internal_pb2_grpc.InternalServiceStub(channel)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            resp = stub.Login(internal_pb2.LoginRequest(email=email, password=password), timeout=5)
            return resp.jwt
        except grpc.RpcError as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"ChirpStack API not ready: {exc.code()}") from exc
            log.info("waiting for ChirpStack API (%s)", exc.code().name)
            time.sleep(2)


def _first(items, name):
    for item in items:
        if item.name == name:
            return item
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    target = os.environ.get("CHIRPSTACK_API", "chirpstack:8080")
    email = os.environ.get("CHIRPSTACK_EMAIL", "admin")
    password = os.environ.get("CHIRPSTACK_PASSWORD", "admin")
    region_id = os.environ.get("REGION", "eu868")
    if region_id not in REGIONS:
        raise SystemExit(f"REGION must be one of {sorted(REGIONS)}")

    channel = grpc.insecure_channel(target)
    jwt = _wait_for_login(channel, email, password)
    auth = [("authorization", f"Bearer {jwt}")]

    tenants = tenant_pb2_grpc.TenantServiceStub(channel)
    gateways = gateway_pb2_grpc.GatewayServiceStub(channel)
    apps = application_pb2_grpc.ApplicationServiceStub(channel)
    profiles = device_profile_pb2_grpc.DeviceProfileServiceStub(channel)
    devices = device_pb2_grpc.DeviceServiceStub(channel)

    tenant_list = tenants.List(tenant_pb2.ListTenantsRequest(limit=10), metadata=auth).result
    if not tenant_list:
        raise SystemExit("no tenant found")
    tenant_id = tenant_list[0].id
    log.info("tenant: %s (%s)", tenant_list[0].name, tenant_id)

    # Gateway
    gw_id = scenario.GATEWAY_EUI.hex()
    try:
        gateways.Get(gateway_pb2.GetGatewayRequest(gateway_id=gw_id), metadata=auth)
        log.info("gateway %s exists", gw_id)
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.NOT_FOUND:
            raise
        gateways.Create(
            gateway_pb2.CreateGatewayRequest(
                gateway=gateway_pb2.Gateway(
                    gateway_id=gw_id,
                    name=scenario.GATEWAY_NAME,
                    description="Simulated gateway (local development only)",
                    tenant_id=tenant_id,
                    stats_interval=30,
                )
            ),
            metadata=auth,
        )
        log.info("gateway %s created", gw_id)

    # Application
    app_list = apps.List(
        application_pb2.ListApplicationsRequest(limit=100, tenant_id=tenant_id), metadata=auth
    ).result
    app = _first(app_list, scenario.APPLICATION_NAME)
    if app is None:
        app_id = apps.Create(
            application_pb2.CreateApplicationRequest(
                application=application_pb2.Application(
                    name=scenario.APPLICATION_NAME,
                    description="Simulated sensors",
                    tenant_id=tenant_id,
                )
            ),
            metadata=auth,
        ).id
        log.info("application created: %s", app_id)
    else:
        app_id = app.id
        log.info("application exists: %s", app_id)

    # Device profile: LoRaWAN 1.0.3, ABP only
    profile_list = profiles.List(
        device_profile_pb2.ListDeviceProfilesRequest(limit=100, tenant_id=tenant_id),
        metadata=auth,
    ).result
    profile = _first(profile_list, PROFILE_NAME)
    if profile is None:
        profile_id = profiles.Create(
            device_profile_pb2.CreateDeviceProfileRequest(
                device_profile=device_profile_pb2.DeviceProfile(
                    tenant_id=tenant_id,
                    name=PROFILE_NAME,
                    description="LoRaWAN 1.0.3, ABP, no OTAA",
                    region=getattr(common.Region, REGIONS[region_id]),
                    region_config_id=region_id,
                    mac_version=common.MacVersion.LORAWAN_1_0_3,
                    reg_params_revision=common.RegParamsRevision.A,
                    adr_algorithm_id="default",
                    supports_otaa=False,
                    uplink_interval=3600,
                    device_status_req_interval=0,
                    abp_rx1_delay=1,
                    abp_rx2_dr=0,
                    abp_rx2_freq=869525000 if region_id == "eu868" else 923300000,
                )
            ),
            metadata=auth,
        ).id
        log.info("device profile created: %s", profile_id)
    else:
        profile_id = profile.id
        log.info("device profile exists: %s", profile_id)

    # Devices: ABP, frame counter check off, session activated
    for dev in scenario.DEVICES.values():
        eui = dev.dev_eui.hex()
        try:
            devices.Get(device_pb2.GetDeviceRequest(dev_eui=eui), metadata=auth)
            log.info("device %s (%s) exists", dev.name, eui)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise
            devices.Create(
                device_pb2.CreateDeviceRequest(
                    device=device_pb2.Device(
                        dev_eui=eui,
                        name=dev.name,
                        description="Simulated sensor (local development only)",
                        application_id=app_id,
                        device_profile_id=profile_id,
                        skip_fcnt_check=True,
                    )
                ),
                metadata=auth,
            )
            log.info("device %s (%s) created", dev.name, eui)
        # LoRaWAN 1.0.x has a single network session key: use it for all three.
        devices.Activate(
            device_pb2.ActivateDeviceRequest(
                device_activation=device_pb2.DeviceActivation(
                    dev_eui=eui,
                    dev_addr=f"{dev.dev_addr:08x}",
                    app_s_key=dev.app_s_key.hex(),
                    nwk_s_enc_key=dev.nwk_s_key.hex(),
                    s_nwk_s_int_key=dev.nwk_s_key.hex(),
                    f_nwk_s_int_key=dev.nwk_s_key.hex(),
                    f_cnt_up=0,
                    n_f_cnt_down=0,
                    a_f_cnt_down=0,
                )
            ),
            metadata=auth,
        )
        log.info("device %s activated (DevAddr %08x)", dev.name, dev.dev_addr)

    log.info("provisioning done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
