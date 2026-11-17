import pytest

from telemetry import regions


def test_known_regions():
    eu = regions.get("eu868")
    assert (eu.uplink_mhz, eu.datr) == (868.1, "SF7BW125")
    assert (eu.rx2_freq_hz, eu.rx2_dr) == (869_525_000, 0)
    au = regions.get("au915_0")
    # Sub-band 1 starts at 915.2 MHz; the RX2 of AU915 is 923.3 MHz at DR8.
    assert (au.uplink_mhz, au.rx2_freq_hz, au.rx2_dr) == (915.2, 923_300_000, 8)
    assert au.common_name == "AU915"


def test_unknown_region_is_refused():
    with pytest.raises(regions.UnknownRegionError, match="eu868"):
        regions.get("us915_0")


def test_uplink_channel_is_inside_the_chirpstack_region_file():
    import pathlib
    import re

    for region in regions.REGIONS.values():
        text = pathlib.Path(f"configuration/chirpstack/region_{region.id}.toml").read_text()
        freqs = {int(f) for f in re.findall(r"frequency=(\d+)", text)}
        assert round(region.uplink_mhz * 1_000_000) in freqs
