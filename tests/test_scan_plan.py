"""Scan-plan assignment and hopper behaviour. No hardware, no iw."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scan_plan as sp
import wifi_feeder as w


def _dual(iface, mac, channels=None):
    supported = channels
    if supported is None:
        supported = frozenset(range(1, 12)) | frozenset(sp.CHANNELS_5)
    return sp.Adapter(iface, mac, ("2.4", "5"), supported=supported)


def _only24(iface, mac):
    return sp.Adapter(iface, mac, ("2.4",), supported=frozenset(range(1, 12)))


def test_two_adapters_split_bands_and_match_21s():
    roles = sp.assign_roles([
        _dual("wlan2", "bb:00:00:00:00:02"),
        _dual("wlan1", "aa:00:00:00:00:01"),
    ])
    by_role = {item.role: item for item in roles}
    assert set(by_role) == {"2.4", "5"}
    # Lower MAC owns 2.4, so a reboot that swaps wlan numbers does not swap jobs.
    assert by_role["2.4"].iface == "wlan1"
    assert by_role["2.4"].spec.name == "C"
    assert by_role["5"].spec.name == "B"
    assert by_role["5"].spec.channels[0] == 149
    assert len(by_role["5"].spec.channels) == 21
    sweep = sp.full_sweep_s(by_role["5"].spec, sp.FAST_RETUNE_S)
    assert abs(sweep - 21.462) < 0.01, sweep
    assert "11" in sp.forecast(roles)


def test_third_adapter_halves_the_5ghz_sweep():
    roles = sp.assign_roles([
        _dual("wlan1", "aa:00:00:00:00:01"),
        _dual("wlan2", "bb:00:00:00:00:02"),
        _dual("wlan3", "cc:00:00:00:00:03"),
    ])
    five = [item for item in roles if item.role == "5"]
    assert len(five) == 2
    assert sum(len(item.spec.channels) for item in five) == 21
    sweep = sp.parallel_sweep_s(roles, "5", sp.FAST_RETUNE_S)
    assert abs(sweep - 11.242) < 0.01, sweep
    # Even split, not one radio locked to 149 and the other still walking 20.
    assert all(len(item.spec.channels) in (10, 11) for item in five)


def test_more_radios_diminish_toward_the_dwell():
    n = len(sp.CHANNELS_5)
    assert abs(sp.split_sweep_s(n, 1, 1.0, 0.022) - 21.462) < 0.01
    assert abs(sp.split_sweep_s(n, 2, 1.0, 0.022) - 11.242) < 0.01
    assert abs(sp.split_sweep_s(n, 3, 1.0, 0.022) - 7.154) < 0.01
    assert abs(sp.split_sweep_s(n, 4, 1.0, 0.022) - 6.132) < 0.01
    # 21 radios, one channel each: the dwell, not zero.
    assert abs(sp.split_sweep_s(n, 21, 1.0, 0.022) - 1.022) < 0.01


def test_single_dual_band_is_plan_a_near_two_and_a_half_minutes():
    roles = sp.assign_roles([_dual("wlan1", "aa")])
    assert len(roles) == 1
    assert roles[0].role == "both"
    assert roles[0].spec.name == "A"
    assert 6 in roles[0].spec.channels and 149 in roles[0].spec.channels
    sweep = sp.full_sweep_s(roles[0].spec, sp.FAST_RETUNE_S)
    assert 140 < sweep < 160, sweep
    text = sp.forecast(roles)
    assert "second adapter" in text


def test_single_24_only_stays_on_the_legacy_hop():
    roles = sp.assign_roles([_only24("wlan1", "aa")])
    assert roles[0].role == "legacy"
    assert roles[0].spec is None


def test_24_only_plus_dual_band_gives_5ghz_to_the_dual_band_radio():
    roles = sp.assign_roles([
        _only24("wlan1", "aa:00:00:00:00:01"),
        _dual("wlan2", "bb:00:00:00:00:02"),
    ])
    by_role = {item.role: item for item in roles}
    assert by_role["2.4"].iface == "wlan1"
    assert by_role["5"].iface == "wlan2"
    assert 149 in by_role["5"].spec.channels
    assert all(ch < 15 for ch in by_role["2.4"].spec.channels)


def test_phy_filter_drops_channels_the_radio_does_not_advertise():
    # US-style: no channel 13, no DFS. 5 GHz is U-NII-3 only.
    unii3 = frozenset({149, 153, 157, 161, 165})
    radio = sp.Adapter(
        "wlan1", "aa", ("2.4", "5"),
        supported=frozenset(range(1, 12)) | unii3,
    )
    spec = sp.assign_roles([radio])[0].spec
    assert 13 not in spec.channels
    assert 52 not in spec.channels
    assert 149 in spec.channels
    assert 165 in spec.channels


def test_onboard_and_backhaul_are_not_taken():
    roles = sp.assign_roles([
        sp.Adapter("wlan0", "00", ("2.4",), onboard=True, supported=frozenset(range(1, 12))),
        sp.Adapter("wlan1", "11", ("2.4", "5"), backhaul=True,
                   supported=frozenset(sp.CHANNELS_5)),
        _dual("wlan2", "22"),
    ])
    assert [item.iface for item in roles] == ["wlan2"]


def test_legacy_plan_uses_one_radio():
    roles = sp.assign_roles([
        _dual("wlan1", "aa"),
        _dual("wlan2", "bb"),
    ], scan_plan="legacy")
    assert len(roles) == 1
    assert roles[0].role == "legacy"
    assert "idle" in roles[0].note


def test_spare_24_only_sweeps_explore_and_a_third_is_idle():
    roles = sp.assign_roles([
        _dual("wlan1", "aa:00:00:00:00:01"),
        _only24("wlan2", "bb:00:00:00:00:02"),
        _only24("wlan3", "cc:00:00:00:00:03"),
    ])
    by_iface = {item.iface: item for item in roles}
    # The dual-band card is the 2.4 owner only if no 2.4-only card exists.
    # A 2.4-only card takes plan C; the dual-band card sweeps 5 GHz.
    assert by_iface["wlan1"].role == "5"
    assert by_iface["wlan2"].role == "2.4"
    assert by_iface["wlan3"].role == "2.4-explore"
    assert 6 not in by_iface["wlan3"].spec.channels


def test_plan_b_cycle_visits_149_then_every_explore():
    spec = sp.assign_roles([
        _only24("wlan1", "aa"),
        _dual("wlan2", "bb"),
    ])
    five = next(item.spec for item in spec if item.role == "5")
    seen = []

    def tune(_iface, channel):
        seen.append(channel)
        return True

    hopper = sp.BandHopper("wlan2", five, tune=tune, sleep=lambda _seconds: False)
    assert hopper._cycle() is False
    assert seen == list(five.channels)
    assert seen[0] == 149
    assert len(seen) == 21


def test_one_frame_arms_a_camp_and_a_social_peek_can_move_it():
    spec = sp.assign_roles([_dual("wlan1", "aa")])[0].spec
    hopper = sp.BandHopper("wlan1", spec, tune=lambda _iface, _ch: True, sleep=lambda _s: False)
    hopper.notify_detection(157)
    assert hopper._leg(157, 1.0) == "camp"

    hopper._camp_ch = 6
    hopper.notify_detection(149)
    assert hopper._peek(6) == "moved"
    assert hopper._camp_ch == 149


def test_failed_tune_does_not_dwell_or_camp():
    spec = sp.assign_roles([_dual("wlan1", "aa")])[0].spec
    slept = []
    hopper = sp.BandHopper(
        "wlan1", spec,
        tune=lambda _iface, _ch: False,
        sleep=lambda seconds: slept.append(seconds) or False,
    )
    hopper.notify_detection(6)
    assert hopper._leg(6, 4.0) == "next"
    assert slept == []


def test_radiotap_channel_and_fixed_feeder_still_constructs():
    # Channel field, bit 3, 2437 MHz = channel 6. No RSSI field.
    present = 1 << 3
    header = struct.pack("<BBHI", 0, 0, 12, present) + struct.pack("<HH", 2437, 0)
    rt_len, rssi, channel = w._parse_radiotap(header)
    assert rt_len == 12
    assert rssi is None
    assert channel == 6
    # 5745 MHz = channel 149.
    header5 = struct.pack("<BBHI", 0, 0, 12, present) + struct.pack("<HH", 5745, 0)
    assert w._parse_radiotap(header5)[2] == 149

    feeder = w.WiFiFeeder("test0", channel=6)
    assert feeder.hopper is None
    assert feeder.role == "fixed"
    feeder._heard(6)  # no hopper, must not raise

    notes = []

    class _Hop:
        current_channel = 11

        def notify_detection(self, ch):
            notes.append(ch)

    scanning = w.WiFiFeeder("wlan2", hopper=_Hop(), role="5")
    scanning._heard(None)
    assert notes == [11]
    assert scanning.status()["role"] == "5"
    assert scanning.status()["channel"] == 11


def test_max_channel_adds_12_and_13_only_when_asked():
    supported = frozenset(range(1, 15)) | frozenset(sp.CHANNELS_5)
    radio = sp.Adapter("wlan1", "aa", ("2.4", "5"), supported=supported)
    default = sp.assign_roles([radio], max_channel=11)[0].spec
    europe = sp.assign_roles([radio], max_channel=13)[0].spec
    assert 12 not in default.channels and 13 not in default.channels
    assert 12 not in europe.channels  # not in the 5 MHz explore set
    assert 13 in europe.channels


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all scan-plan tests passed")
