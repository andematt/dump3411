#!/usr/bin/env python3
"""
French Direct Remote ID beacon decoding, checked against the reference encoder.

FRAMES are 802.11 beacon frames built by opendroneid-core-c's
frdid_wifi_build_beacon_frame (commit 6484f26) from the inputs in EXPECTED;
gen_frdid.c alongside this file regenerates them. Only the beacon timestamp
(bytes 24-31) changes between runs.

Run with `python3 tests/test_frdid.py`, or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import wifi_feeder as w  # noqa: E402
from tracker import Tracker  # noqa: E402

M_TO_FT   = 3.28084
MPS_TO_KT = 1.943844

# The encoder rounds with `value * 1e5 + 0.5` and then truncates, which is
# off by up to 1.5 wire LSB (1e-5°, about 1.6 m) for negative coordinates.
# The decoder reads the wire exactly; the tolerance absorbs the encoder.
LATLON_TOL = 1.51e-5

FRAMES = {
    "full": (
        "80000000ffffffffffff0211223344550211223344550000c7dc07427e00000064002004"
        "000846522d44524f4e4501018cdd646a5c3501010101021e414243303030303030303030"
        "3030303030303030303030303030303030310314313538314635464a4432313233343536"
        "373839300404004a8d4d050400038048060200980702002d0804004a8d28090400038018"
        "0a010c0b02010f"
    ),
    "south_west": (
        "80000000ffffffffffff0211223344550211223344550000fbdc07427e00000064002004"
        "000846522d44524f4e4501018cdd4e6a5c3501010101021e58595a313233414243444546"
        "4748494a4b4c4d4e4f5051525354555657580404ffcc56b30504ff94dc270602fff40702"
        "00030804ffcc569d0904ff94dbe10a01000b020167"
    ),
    "id_only": (
        "80000000ffffffffffff021122334455021122334455000007dd07427e00000064002004"
        "000846522d44524f4e4501018cdd276a5c3501010101021e414243303030303030303030"
        "303030303030303030303030303030303032"
    ),
    "serial_only_fast": (
        "80000000ffffffffffff02112233445502112233445500000edd07427e00000064002004"
        "000846522d44524f4e4501018cdd3e6a5c3501010101030e53455249414c2d4f4e4c592d"
        "3737040400456d700504000802c8060203840702007808040045698809040007fee00a01"
        "640b020000"
    ),
}

EXPECTED = {
    "full": dict(id="1581F5FJD21234567890", id_type="serial",
                 lat=48.8583701, lon=2.2944813, alt_m=152, agl_m=45, gs_mps=12, track=271,
                 takeoff=(48.858, 2.294)),
    "south_west": dict(id="XYZ123ABCDEFGHIJKLMNOPQRSTUVWX", id_type="unknown",
                       lat=-33.8567844, lon=-70.2153, alt_m=-12, agl_m=3, gs_mps=0, track=359,
                       takeoff=(-33.857, -70.216)),
    "id_only": dict(id="ABC000000000000000000000000002", id_type="unknown"),
    "serial_only_fast": dict(id="SERIAL-ONLY-77", id_type="serial",
                             lat=45.5, lon=5.25, alt_m=900, agl_m=120, gs_mps=100, track=0,
                             takeoff=(45.49, 5.24)),
}

RADIOTAP = bytes([0, 0, 8, 0, 0, 0, 0, 0])  # minimal header, no RSSI field


def _decode(name: str) -> dict:
    """Feed one frame through WiFiFeeder._on_packet into a fresh Tracker."""
    tracker = Tracker()
    feeder = w.WiFiFeeder("test0", channel=6, tracker=tracker)
    feeder._on_packet(RADIOTAP + bytes.fromhex("".join(FRAMES[name])))
    drones = tracker.snapshot()["drones"]
    assert len(drones) == 1, f"{name}: {len(drones)} drones"
    return drones[0]


def test_reference_frames_decode_to_encoder_inputs():
    for name, e in EXPECTED.items():
        d = _decode(name)
        assert d["id"] == e["id"], (name, d["id"])
        assert d["id_type"] == e["id_type"], (name, d["id_type"])
        assert d["rid_source"] == "wifi_beacon_fr", (name, d["rid_source"])
        assert d.get("ua_type") is None, (name, d.get("ua_type"))
        if "lat" not in e:
            assert "lat" not in d and "operator" not in d, (name, d)
            continue
        assert abs(d["lat"] - e["lat"]) <= LATLON_TOL, (name, d["lat"])
        assert abs(d["lon"] - e["lon"]) <= LATLON_TOL, (name, d["lon"])
        assert abs(d["alt_geom_ft"] - e["alt_m"] * M_TO_FT) < 0.2, (name, d["alt_geom_ft"])
        assert abs(d["agl_ft"] - e["agl_m"] * M_TO_FT) < 0.2, (name, d["agl_ft"])
        assert abs(d["gs"] - e["gs_mps"] * MPS_TO_KT) < 0.2, (name, d["gs"])
        assert d["track"] == e["track"], (name, d["track"])
        op = d["operator"]
        assert abs(op["lat"] - e["takeoff"][0]) <= LATLON_TOL, (name, op)
        assert abs(op["lon"] - e["takeoff"][1]) <= LATLON_TOL, (name, op)
        assert op["location_type"] == "takeoff", (name, op)


def test_later_odid_basic_id_can_fill_ua_type():
    """The French format has no UA type, so it must not claim one."""
    tracker = Tracker()
    feeder = w.WiFiFeeder("test0", channel=6, tracker=tracker)
    feeder._on_packet(RADIOTAP + bytes.fromhex("".join(FRAMES["full"])))
    tracker.update_basic_id(mac="02:aa:bb:cc:dd:ee", uas_id="1581F5FJD21234567890",
                            id_type_raw=1, ua_type_raw=2, rssi=None, rid_source="wifi_beacon")
    assert tracker.snapshot()["drones"][0]["ua_type"] == "multirotor"


def test_malformed_payloads_rejected():
    truncated = bytes([0x01, 0x01, 0x01, 0x02, 30]) + b"ABC"
    assert w.parse_frdid(truncated) is None
    no_identity = bytes([0x01, 0x01, 0x01, 0x04, 0x04, 0, 0, 0, 1])
    assert w.parse_frdid(no_identity) is None
    assert w.parse_frdid(b"") is None


def test_astm_element_not_taken_as_french():
    body = bytes(12) + bytes([221, 9]) + w.ASTM_OUI + bytes([0x0D, 7, 0x02, 0, 0, 0])
    assert w._extract_beacon_frdid(body) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all French DRI tests passed")
