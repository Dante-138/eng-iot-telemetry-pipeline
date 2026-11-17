"""Provision ChirpStack over gRPC with the simulator's gateway and devices.

Idempotent: run it as often as you like (an ABP device whose activation already matches is
left alone, so its frame counters are not reset). It logs in with the default local
development admin account, then ensures that these exist in the default
tenant: the gateway, an application, a LoRaWAN 1.0.3 device profile without
OTAA, the ABP devices (frame counter check off) with their session activated, and an OTAA
profile with the OTAA device dev-otaa and its keys.

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

from telemetry import provisioning, scenario

log = logging.getLogger("provision")

PROFILE_NAME = "sim-abp-lorawan-1.0.3"
OTAA_PROFILE_NAME = "sim-otaa-lorawan-1.0.3"

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

    # Gateways
    for eui, name in (
        (scenario.GATEWAY_EUI, scenario.GATEWAY_NAME),
        (scenario.GATEWAY2_EUI, scenario.GATEWAY2_NAME),
    ):
        gw_id = eui.hex()
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
                        name=name,
                        description="Simulated gateway (local development only)",
                        tenant_id=tenant_id,
                        stats_interval=30,
                    )
                ),
                metadata=auth,
            )
            log.info("gateway %s created", gw_id)

    # Application
    def fetch_apps(limit, offset):
        resp = apps.List(
            application_pb2.ListApplicationsRequest(
                limit=limit, offset=offset, tenant_id=tenant_id
            ),
            metadata=auth,
        )
        return list(resp.result), resp.total_count

    app_list = provisioning.list_all(fetch_apps)
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
    def fetch_profiles(limit, offset):
        resp = profiles.List(
            device_profile_pb2.ListDeviceProfilesRequest(
                limit=limit, offset=offset, tenant_id=tenant_id
            ),
            metadata=auth,
        )
        return list(resp.result), resp.total_count

    profile_list = provisioning.list_all(fetch_profiles)

    def ensure_profile(name: str, description: str, otaa: bool) -> str:
        profile = _first(profile_list, name)
        if profile is not None:
            log.info("device profile %s exists: %s", name, profile.id)
            return profile.id
        profile_id = profiles.Create(
            device_profile_pb2.CreateDeviceProfileRequest(
                device_profile=device_profile_pb2.DeviceProfile(
                    tenant_id=tenant_id,
                    name=name,
                    description=description,
                    region=getattr(common.Region, REGIONS[region_id]),
                    region_config_id=region_id,
                    mac_version=common.MacVersion.LORAWAN_1_0_3,
                    reg_params_revision=common.RegParamsRevision.A,
                    adr_algorithm_id="default",
                    supports_otaa=otaa,
                    uplink_interval=3600,
                    device_status_req_interval=0,
                    abp_rx1_delay=1,
                    abp_rx2_dr=0,
                    abp_rx2_freq=869525000 if region_id == "eu868" else 923300000,
                )
            ),
            metadata=auth,
        ).id
        log.info("device profile %s created: %s", name, profile_id)
        return profile_id

    profile_id = ensure_profile(PROFILE_NAME, "LoRaWAN 1.0.3, ABP, no OTAA", otaa=False)
    otaa_profile_id = ensure_profile(OTAA_PROFILE_NAME, "LoRaWAN 1.0.3, OTAA", otaa=True)

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
        wanted = {
            "dev_addr": f"{dev.dev_addr:08x}",
            "app_s_key": dev.app_s_key.hex(),
            "nwk_s_enc_key": dev.nwk_s_key.hex(),
            "s_nwk_s_int_key": dev.nwk_s_key.hex(),
            "f_nwk_s_int_key": dev.nwk_s_key.hex(),
        }
        try:
            current = devices.GetActivation(
                device_pb2.GetDeviceActivationRequest(dev_eui=eui), metadata=auth
            ).device_activation
            current_fields = {f: getattr(current, f) for f in provisioning.ACTIVATION_FIELDS}
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise
            current_fields = None
        if provisioning.activation_matches(current_fields, wanted):
            # Activating again would reset the frame counters for nothing.
            log.info("device %s already activated (DevAddr %08x)", dev.name, dev.dev_addr)
            continue
        devices.Activate(
            device_pb2.ActivateDeviceRequest(
                device_activation=device_pb2.DeviceActivation(
                    dev_eui=eui,
                    f_cnt_up=0,
                    n_f_cnt_down=0,
                    a_f_cnt_down=0,
                    **wanted,
                )
            ),
            metadata=auth,
        )
        log.info("device %s activated (DevAddr %08x)", dev.name, dev.dev_addr)

    # dev-otaa: OTAA device. For LoRaWAN 1.0.x the AppKey goes in the nwk_key field of
    # DeviceKeys (see the comment of DeviceKeys in chirpstack/api/proto/api/device.proto).
    eui = scenario.OTAA_DEV_EUI.hex()
    try:
        devices.Get(device_pb2.GetDeviceRequest(dev_eui=eui), metadata=auth)
        log.info("device %s (%s) exists", scenario.OTAA_NAME, eui)
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.NOT_FOUND:
            raise
        devices.Create(
            device_pb2.CreateDeviceRequest(
                device=device_pb2.Device(
                    dev_eui=eui,
                    join_eui=scenario.OTAA_JOIN_EUI.hex(),
                    name=scenario.OTAA_NAME,
                    description="Simulated OTAA sensor (local development only)",
                    application_id=app_id,
                    device_profile_id=otaa_profile_id,
                )
            ),
            metadata=auth,
        )
        log.info("device %s (%s) created", scenario.OTAA_NAME, eui)
    keys = device_pb2.DeviceKeys(
        dev_eui=eui, nwk_key=scenario.OTAA_APP_KEY.hex(), app_key="00" * 16
    )
    try:
        current = devices.GetKeys(device_pb2.GetDeviceKeysRequest(dev_eui=eui), metadata=auth)
        if current.device_keys.nwk_key.lower() == keys.nwk_key:
            log.info("device %s keys already set", scenario.OTAA_NAME)
        else:
            devices.UpdateKeys(device_pb2.UpdateDeviceKeysRequest(device_keys=keys), metadata=auth)
            log.info("device %s keys updated", scenario.OTAA_NAME)
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.NOT_FOUND:
            raise
        devices.CreateKeys(device_pb2.CreateDeviceKeysRequest(device_keys=keys), metadata=auth)
        log.info("device %s keys created", scenario.OTAA_NAME)

    log.info("provisioning done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
