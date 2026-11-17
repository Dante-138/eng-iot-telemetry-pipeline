"""Radio parameters of the regions the bench can run in.

The region id is the one of the ChirpStack region file (``configuration/chirpstack``). It also
prefixes the gateway MQTT topics. The simulator takes the uplink frequency and data rate from
here, and the provisioning takes the RX2 parameters, so one variable (``REGION``) switches the
whole bench.

AU915 is split in 8 sub-bands of 8 channels (125 kHz, 200 kHz apart) plus one 500 kHz channel.
``au915_0`` is sub-band 1: channels 0 to 7 (915.2 to 916.6 MHz) plus channel 64 (915.9 MHz).
Uplink 915.2 MHz is channel 0 of that sub-band. SF7BW125 is DR5 in AU915 (DR0 is SF12BW125).
Source: LoRaWAN Regional Parameters (RP002-1.0.x), AU915-928 section, and the ChirpStack
region files in this repository.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    id: str
    common_name: str  # name of the ChirpStack common.Region enum
    uplink_mhz: float
    datr: str
    rx2_freq_hz: int
    rx2_dr: int


REGIONS: dict[str, Region] = {
    "eu868": Region("eu868", "EU868", 868.1, "SF7BW125", 869_525_000, 0),
    "au915_0": Region("au915_0", "AU915", 915.2, "SF7BW125", 923_300_000, 8),
}


class UnknownRegionError(ValueError):
    pass


def get(region_id: str) -> Region:
    try:
        return REGIONS[region_id]
    except KeyError:
        raise UnknownRegionError(
            f"unknown region {region_id!r}; choose one of {', '.join(sorted(REGIONS))}"
        ) from None
