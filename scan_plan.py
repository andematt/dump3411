"""Social-weighted Wi-Fi channel plans for ASTM F3411 Remote ID.

A monitor-mode radio hears one channel at a time and is deaf while it
retunes. Compliant aircraft beacon on channel 6 (2.4 GHz) or 149 (5 GHz)
about once a second, and may leave those channels only by raising the rate
to at least 5 Hz. Long visits belong on 6 and 149; a one-second visit is
enough everywhere else. A decoded Remote ID frame camps the radio on that
channel until the aircraft goes quiet.

This is an independent scheduler. It is aimed at the same sweep times a
pair of fast USB adapters can actually achieve — about 21 seconds for the
whole 5 GHz list on a dedicated radio, about two and a half minutes when
one radio has to cover both bands — without sharing another project's source.

Planning functions are pure. ``BandHopper`` is the thread that tunes a live
interface; pass ``tune`` and ``sleep`` to test it without hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("dump3411.scan")

SOCIAL_24 = 6
SOCIAL_5 = 149

# 21 channels: U-NII-3 plus the DFS range 52-144. U-NII-1 is indoor and low
# power, and a compliant beacon stays on 149 regardless of where the video
# link sits. U-NII-4 is uncommon enough to leave out until a radio that
# advertises it shows up in the field.
CHANNELS_5 = (
    149, 153, 157, 161, 165,
    52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
)

# 5 MHz steps, not only the non-overlapping trio. Some aircraft (Parrot's
# published table, and at least one ANAFI heard in the wild) sit on 5.
# 13 is legal outside North America; the PHY filter drops it when the
# regulatory domain does.
EXPLORE_24 = (1, 5, 9, 11, 13)

# Single-radio cycle: 4 s on ch6, 1 s on ch149, 1 s on one other channel.
# With ~25 explore slots and a ~20 ms retune that is about 2.5 minutes
# before every 5 GHz channel has been visited — the cost of keeping ch6,
# where almost all 2.4 GHz beacons are, from being starved.
DWELL_SOCIAL_24 = 4.0
# Dedicated 2.4 radio. ch6 is 1 Hz, so holding it is cheap; one second in
# ten is given to the rest of 2.4 so the 5 GHz radio never has to cross
# bands to cover it.
DWELL_SOCIAL_24_DEDICATED = 9.0
DWELL_OTHER = 1.0

# Six missed 1 Hz beacons, or about three seconds of a >=5 Hz off-social
# beacon, before a camp is called lost.
SILENCE_SOCIAL_S = 6.0
SILENCE_OTHER_S = 3.0
# How often a radio that is the only one covering more than one channel
# breaks a camp to look elsewhere. A 5 GHz radio with a partner on ch6
# does not break its camp.
RELEASE_S = 10.0

# Planning estimate only. A fast USB adapter (Alfa AWUS036ACS class) retunes
# in about this long; a slow one can take a second. The hopper measures the
# real radio and logs that cycle instead.
FAST_RETUNE_S = 0.022


@dataclass(frozen=True)
class Adapter:
    """One Wi-Fi interface, as reported by sysfs + ``iw``.

    ``supported is None`` means the PHY could not be queried. An empty set
    means the query succeeded and advertised nothing tunable.
    """

    iface: str
    mac: str = ""
    bands: tuple[str, ...] = ()
    monitor: bool = True
    onboard: bool = False
    backhaul: bool = False
    supported: frozenset[int] | None = None
    probed: bool = True


@dataclass(frozen=True)
class ScanSpec:
    """What one radio should do. ``explore_mode`` is ``one`` (rotate a single
    explore channel per cycle) or ``all`` (visit every explore channel every
    cycle)."""

    name: str
    social: tuple[tuple[int, float], ...]
    explore: tuple[int, ...]
    explore_mode: str
    camp_release: bool
    dwell_explore: float = DWELL_OTHER
    trigger_frames: int = 1
    trigger_window_s: float = 2.0
    silence_social_s: float = SILENCE_SOCIAL_S
    silence_other_s: float = SILENCE_OTHER_S
    release_s: float = RELEASE_S
    social_ids: tuple[int, ...] = (SOCIAL_24, SOCIAL_5)

    @property
    def channels(self) -> tuple[int, ...]:
        return tuple(ch for ch, _ in self.social) + tuple(self.explore)


@dataclass
class Assignment:
    iface: str
    role: str
    spec: ScanSpec | None = None
    mac: str = ""
    note: str = ""
    channels: tuple[int, ...] = field(default_factory=tuple)


def supports_5(adapter: Adapter) -> bool:
    if not adapter.probed:
        return True
    if "5" in adapter.bands:
        return True
    return bool(adapter.supported) and SOCIAL_5 in adapter.supported


def supports_24(adapter: Adapter) -> bool:
    if not adapter.probed:
        return True
    if "2.4" in adapter.bands:
        return True
    if adapter.supported and any(1 <= ch <= 14 for ch in adapter.supported):
        return True
    return False


def _keep(channel: int, supported: frozenset[int] | None) -> bool:
    if not channel:
        return False
    if supported is None:
        return True
    return channel in supported


def explore_24(max_channel: int, supported: frozenset[int] | None) -> tuple[int, ...]:
    """2.4 explore list. 12-13 follow ``--max-channel``; the PHY has the last word."""
    out = []
    for ch in EXPLORE_24:
        if ch > max_channel or ch == SOCIAL_24:
            continue
        if _keep(ch, supported):
            out.append(ch)
    return tuple(out)


def channels_5(supported: frozenset[int] | None) -> tuple[int, ...]:
    return tuple(ch for ch in CHANNELS_5 if _keep(ch, supported))


def _spec_a(supported: frozenset[int] | None, max_channel: int) -> ScanSpec:
    social = []
    if _keep(SOCIAL_24, supported):
        social.append((SOCIAL_24, DWELL_SOCIAL_24))
    if _keep(SOCIAL_5, supported):
        social.append((SOCIAL_5, DWELL_OTHER))
    explore = tuple(ch for ch in channels_5(supported) if ch != SOCIAL_5)
    explore += explore_24(max_channel, supported)
    return ScanSpec("A", tuple(social), explore, "one", camp_release=True)


def _spec_c(supported: frozenset[int] | None, max_channel: int) -> ScanSpec:
    social = ((SOCIAL_24, DWELL_SOCIAL_24_DEDICATED),) if _keep(SOCIAL_24, supported) else ()
    return ScanSpec(
        "C", social, explore_24(max_channel, supported), "one", camp_release=True,
    )


def _spec_e(supported: frozenset[int] | None, max_channel: int) -> ScanSpec:
    """Spare 2.4-only radio: sweep the non-social 2.4 channels. ch6 stays with plan C."""
    return ScanSpec(
        "E", (), explore_24(max_channel, supported), "all", camp_release=True,
    )


def _spec_slice(slice_channels: list[int]) -> ScanSpec:
    """One 5 GHz radio's share. 149, if present, is visited every cycle; the
    rest of the slice is visited every cycle too. No camp release — a partner
    owns channel 6, and any sibling 5 GHz radio keeps sweeping its own slice."""
    social = []
    explore = []
    for ch in slice_channels:
        if ch == SOCIAL_5:
            social.append((SOCIAL_5, DWELL_OTHER))
        else:
            explore.append(ch)
    return ScanSpec("B", tuple(social), tuple(explore), "all", camp_release=False)


def _union_5(radios: list[Adapter]) -> list[int]:
    """5 GHz channels at least one of ``radios`` can tune, in CHANNELS_5 order."""
    out = []
    for ch in CHANNELS_5:
        if any(_keep(ch, radio.supported) for radio in radios):
            out.append(ch)
    return out


def _partition(channels: list[int], radios: list[Adapter]) -> dict[str, list[int]]:
    """Spread channels across radios. A channel goes to the supporting radio
    with the smallest share so far, so a 2.4-only card never receives 149 and
    two identical adapters split the list in half."""
    load: dict[str, list[int]] = {radio.iface: [] for radio in radios}
    for ch in channels:
        capable = [radio for radio in radios if _keep(ch, radio.supported)]
        if not capable:
            continue
        pick = min(capable, key=lambda radio: (len(load[radio.iface]), radio.iface))
        load[pick.iface].append(ch)
    return load


def usable_adapters(adapters: list[Adapter]) -> list[Adapter]:
    """Monitor-capable USB adapters that are not the Pi's onboard radio and
    not the interface carrying the default route. Ordered by MAC so a reboot
    that renames wlan1/wlan2 does not swap roles."""
    kept = [
        adapter for adapter in adapters
        if adapter.monitor and not adapter.onboard and not adapter.backhaul
    ]
    return sorted(kept, key=lambda adapter: (adapter.mac.lower(), adapter.iface))


def assign_roles(
    adapters: list[Adapter],
    *,
    scan_plan: str = "auto",
    max_channel: int = 11,
) -> list[Assignment]:
    """Decide which radio does what.

    ``auto`` with one dual-band radio uses plan A (both bands, ch6-heavy).
    ``auto`` with one 2.4-only radio keeps the legacy flat hop, so an
    RT3070 install does not change. Two or more radios put one on 2.4
    (plan C) and split 5 GHz across every remaining dual-band radio.
    A 2.4-only spare sweeps the rest of 2.4 instead of sitting idle.

    ``legacy`` forces the flat hop on the first usable radio and leaves
    the others unused.
    """
    usable = usable_adapters(adapters)
    if not usable:
        return []

    if scan_plan == "legacy":
        first = usable[0]
        notes = ""
        if len(usable) > 1:
            notes = "legacy hop uses one radio; extra adapters are idle"
        return [Assignment(first.iface, "legacy", mac=first.mac, note=notes)]

    if len(usable) == 1:
        only = usable[0]
        if supports_5(only):
            spec = _spec_a(only.supported, max_channel)
            return [Assignment(only.iface, "both", spec, only.mac,
                               channels=spec.channels)]
        return [Assignment(only.iface, "legacy", mac=only.mac,
                           note="2.4-only adapter — 5 GHz is invisible")]

    # Prefer a 2.4-only card for the 2.4 role so a dual-band card is free
    # to sweep 5 GHz. Two identical dual-band cards: MAC order, which is
    # stable and arbitrary.
    can_24 = [adapter for adapter in usable if supports_24(adapter)]
    only_24 = [adapter for adapter in can_24 if not supports_5(adapter)]
    owner = (only_24 or can_24 or [None])[0]
    rest = [adapter for adapter in usable if adapter is not owner]
    five = [adapter for adapter in rest if supports_5(adapter)]
    spare_24 = [adapter for adapter in rest if not supports_5(adapter) and supports_24(adapter)]

    assignments: list[Assignment] = []
    if owner is not None:
        spec = _spec_c(owner.supported, max_channel)
        assignments.append(Assignment(
            owner.iface, "2.4", spec, owner.mac, channels=spec.channels,
        ))
    else:
        log.warning("No 2.4 GHz adapter — channel 6 will not be covered")

    if five:
        parts = _partition(_union_5(five), five)
        for radio in five:
            slice_channels = parts[radio.iface]
            if not slice_channels:
                assignments.append(Assignment(
                    radio.iface, "unused", mac=radio.mac,
                    note="no 5 GHz channel left for this radio",
                ))
                continue
            spec = _spec_slice(slice_channels)
            assignments.append(Assignment(
                radio.iface, "5", spec, radio.mac, channels=spec.channels,
            ))
    elif owner is not None:
        log.warning("No 5 GHz adapter — aircraft beaconing only on 5 GHz will be missed")

    if spare_24:
        spare = spare_24[0]
        spec = _spec_e(spare.supported, max_channel)
        assignments.append(Assignment(
            spare.iface, "2.4-explore", spec, spare.mac, channels=spec.channels,
            note="extra 2.4-only radio sweeping non-social 2.4 channels",
        ))
        for extra in spare_24[1:]:
            assignments.append(Assignment(
                extra.iface, "unused", mac=extra.mac,
                note="2.4 explore is already covered",
            ))
    return assignments


def full_sweep_s(spec: ScanSpec, retune_s: float) -> float:
    """Seconds until every channel in ``spec`` has been visited once.

    Retune is paid on every leg: the radio is deaf while it tunes, and the
    dwell starts only after the tune returns.
    """
    n_social = len(spec.social)
    social_dwell = sum(dwell for _, dwell in spec.social)
    if spec.explore_mode == "all" or not spec.explore:
        n_legs = n_social + len(spec.explore)
        dwell = social_dwell + len(spec.explore) * spec.dwell_explore
        return dwell + n_legs * retune_s
    cycle = social_dwell + spec.dwell_explore
    legs = n_social + 1
    return len(spec.explore) * (cycle + legs * retune_s)


def parallel_sweep_s(assignments: list[Assignment], role: str, retune_s: float) -> float | None:
    """Wall time for every radio of ``role`` to finish its own list. They
    run together, so the slowest share is the sweep."""
    specs = [item.spec for item in assignments if item.role == role and item.spec is not None]
    if not specs:
        return None
    return max(full_sweep_s(spec, retune_s) for spec in specs)


def split_sweep_s(n_channels: int, n_radios: int, dwell_s: float, retune_s: float) -> float:
    """Full-sweep time if ``n_channels`` were split evenly across ``n_radios``."""
    if n_channels < 1 or n_radios < 1:
        return 0.0
    largest = (n_channels + n_radios - 1) // n_radios
    return largest * (dwell_s + retune_s)


def forecast(assignments: list[Assignment], retune_s: float = FAST_RETUNE_S) -> str:
    """One startup paragraph: what this set of radios will do, and what
    another 5 GHz radio would buy. Returns "" when there is nothing to say."""
    five = [item for item in assignments if item.role == "5" and item.spec is not None]
    both = [item for item in assignments if item.role == "both" and item.spec is not None]
    if not five and not both:
        return ""

    if both and not five:
        spec = both[0].spec
        assert spec is not None
        n5 = sum(1 for ch in spec.channels if ch in CHANNELS_5)
        single = full_sweep_s(spec, retune_s)
        dedicated = split_sweep_s(len(CHANNELS_5), 1, DWELL_OTHER, retune_s)
        return (
            f"One dual-band radio covers both bands in {single:.0f}s "
            f"({n5} of the 5 GHz channels, ch6 every cycle) at {retune_s * 1000:.0f}ms retune. "
            f"A second adapter sweeping 5 GHz alone would finish that band in "
            f"~{dedicated:.0f}s instead."
        )

    n_channels = len({ch for item in five for ch in (item.spec.channels if item.spec else ())})
    now = parallel_sweep_s(assignments, "5", retune_s) or 0.0
    extras = []
    for k in range(len(five) + 1, len(five) + 4):
        extras.append(f"{k}×5 GHz ~{split_sweep_s(n_channels, k, DWELL_OTHER, retune_s):.0f}s")
    return (
        f"5 GHz sweep: {n_channels} channels across {len(five)} radio(s), "
        f"~{now:.0f}s at {retune_s * 1000:.0f}ms retune. "
        f"Splitting further: {', '.join(extras)}. "
        f"The floor is the {DWELL_OTHER:.0f}s dwell — off-social beacons are at least 5 Hz, "
        f"so a shorter visit starts missing weak ones — and a Pi usually runs out of "
        f"USB power and CPU before that floor matters."
    )


def describe(assignments: list[Assignment], retune_s: float = FAST_RETUNE_S) -> str:
    parts = []
    for item in assignments:
        if item.role == "unused":
            parts.append(f"{item.iface} unused ({item.note})")
            continue
        if item.spec is None:
            extra = f" ({item.note})" if item.note else ""
            parts.append(f"{item.iface} legacy 2.4 hop{extra}")
            continue
        sweep = full_sweep_s(item.spec, retune_s)
        parts.append(
            f"{item.iface} plan {item.spec.name} role={item.role} "
            f"{len(item.spec.channels)} ch ~{sweep:.0f}s"
        )
    return "; ".join(parts)


class BandHopper(threading.Thread):
    """Tune ``iface`` through a ``ScanSpec``. Camp when Remote ID is heard.

    ``tune(iface, channel) -> bool`` and ``sleep(seconds) -> bool`` (True
    means stop) are injectable so tests can run a cycle without ``iw`` or
    real time. ``notify`` is called from the capture thread.
    """

    POLL_S = 0.5

    def __init__(self, iface: str, spec: ScanSpec, tune, sleep=None):
        super().__init__(daemon=True, name=f"hop-{iface}")
        self.iface = iface
        self.spec = spec
        self.plan_name = spec.name
        self._tune = tune
        self._sleep = sleep or self._wait
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._hits: dict[int, list[float]] = {}
        self._last_frame: dict[int, float] = {}
        self._camp_ch: int | None = None
        self._explore_i = 0
        self._release_i = 0
        self.retune_s = 0.0
        self.sweep_s = full_sweep_s(spec, FAST_RETUNE_S)
        first = spec.channels[0] if spec.channels else None
        self.current_channel = first
        self.target_channels = list(spec.channels)

    @property
    def camp_channel(self) -> int | None:
        return self._camp_ch

    def _wait(self, seconds: float) -> bool:
        return self._halt.wait(timeout=seconds)

    def stop(self) -> None:
        self._halt.set()

    def notify_detection(self, channel: int | None) -> None:
        now = time.monotonic()
        if channel is None:
            channel = self.current_channel
        if channel is None:
            return
        with self._lock:
            self._last_frame[channel] = now
            window = self.spec.trigger_window_s
            hits = [stamp for stamp in self._hits.get(channel, ()) if now - stamp <= window]
            hits.append(now)
            self._hits[channel] = hits

    def _armed(self, channel: int) -> bool:
        now = time.monotonic()
        window = self.spec.trigger_window_s
        with self._lock:
            hits = [stamp for stamp in self._hits.get(channel, ()) if now - stamp <= window]
            self._hits[channel] = hits
        return len(hits) >= self.spec.trigger_frames

    def _last(self, channel: int) -> float:
        with self._lock:
            return self._last_frame.get(channel, 0.0)

    def _silence_for(self, channel: int) -> float:
        if channel in self.spec.social_ids:
            return self.spec.silence_social_s
        return self.spec.silence_other_s

    def _set(self, channel: int) -> bool:
        ok = self._tune(self.iface, channel)
        if ok:
            self.current_channel = channel
        else:
            log.warning("Could not tune %s to channel %s", self.iface, channel)
        return bool(ok)

    def _measure_retune(self) -> float:
        pool = list(self.spec.channels)
        if len(pool) < 2 or not self.iface:
            return 0.0
        first, second = pool[0], pool[1]
        samples = []
        try:
            for _ in range(3):
                self._tune(self.iface, first)
                started = time.monotonic()
                self._tune(self.iface, second)
                samples.append(time.monotonic() - started)
        except Exception as exc:
            log.warning("Could not measure retune on %s: %s", self.iface, exc)
            return 0.0
        samples.sort()
        return samples[len(samples) // 2]

    def run(self) -> None:
        if not self.spec.channels:
            log.error("%s has an empty scan plan — not scanning", self.iface)
            return
        self.retune_s = self._measure_retune()
        self.sweep_s = full_sweep_s(self.spec, self.retune_s)
        log.info(
            "%s plan %s: %s — full sweep %.1fs including %.0fms retune",
            self.iface, self.spec.name, _plan_phrase(self.spec),
            self.sweep_s, self.retune_s * 1000,
        )
        shortest = min(
            [dwell for _, dwell in self.spec.social] + [self.spec.dwell_explore]
        )
        if self.retune_s > 0.25 * shortest:
            log.warning(
                "%s takes %.0fms to change channel, %.0f%% of the shortest dwell. "
                "A faster adapter shortens the sweep; lengthening the dwell does not.",
                self.iface, self.retune_s * 1000, 100 * self.retune_s / shortest,
            )
        while not self._halt.is_set():
            if self._camp_ch is None:
                if self._cycle():
                    return
            elif self._hold():
                return

    def _leg(self, channel: int, dwell: float) -> str:
        if not self._set(channel):
            return "next"
        if self._sleep(dwell):
            return "stop"
        if self._armed(channel):
            return "camp"
        return "next"

    def _cycle(self) -> bool:
        legs = list(self.spec.social)
        if self.spec.explore:
            if self.spec.explore_mode == "one":
                channel = self.spec.explore[self._explore_i % len(self.spec.explore)]
                self._explore_i += 1
                legs.append((channel, self.spec.dwell_explore))
            else:
                legs.extend((channel, self.spec.dwell_explore) for channel in self.spec.explore)
        if not legs:
            log.warning("%s has no channels to scan — idling", self.iface)
            return self._sleep(5.0)
        for channel, dwell in legs:
            result = self._leg(channel, dwell)
            if result == "stop":
                return True
            if result == "camp":
                self._camp_ch = channel
                log.info("Camp: %s holding ch%s", self.iface, channel)
                return False
        return False

    def _hold(self) -> bool:
        channel = self._camp_ch
        if channel is None:
            return False
        if not self._set(channel):
            self._camp_ch = None
            return False
        last = self._last(channel)
        silence = self._silence_for(channel)
        away = 0.0
        held_since = time.monotonic()
        while not self._halt.is_set():
            if self._sleep(self.POLL_S):
                return True
            seen = self._last(channel)
            if seen > last:
                last = seen
                away = 0.0
            if (time.monotonic() - last) - away > silence:
                log.info("Camp: %s ch%s quiet for %.0fs — resuming", self.iface, channel, silence)
                self._camp_ch = None
                return False
            if self.spec.camp_release and (time.monotonic() - held_since) >= self.spec.release_s:
                left = time.monotonic()
                peeked = self._peek(channel)
                if peeked == "stop":
                    return True
                if peeked == "moved":
                    # Caller loops and holds the new channel. Retuning here
                    # would undo the move.
                    return False
                away += time.monotonic() - left
                self._set(channel)
                held_since = time.monotonic()
        return True

    def _peek(self, camp_channel: int) -> str:
        """Look at another social channel and one explore channel.

        Returns "stop", "moved" (camp followed a live social channel), or
        "back". An explore peek is decoded by the capture thread, but the
        radio returns to the channel it already knows is carrying traffic
        unless that peek was itself a social channel with a live aircraft.
        """
        others = [(ch, dwell) for ch, dwell in self.spec.social if ch != camp_channel]
        if others:
            other, dwell = others[self._release_i % len(others)]
            self._release_i += 1
            result = self._leg(other, dwell)
            if result == "stop":
                return "stop"
            if result == "camp":
                log.info("Camp: %s moving to ch%s", self.iface, other)
                self._camp_ch = other
                return "moved"
        if self.spec.explore:
            explore = self.spec.explore[self._explore_i % len(self.spec.explore)]
            self._explore_i += 1
            if explore != camp_channel and self._leg(explore, self.spec.dwell_explore) == "stop":
                return "stop"
        return "back"


def _plan_phrase(spec: ScanSpec) -> str:
    social = " + ".join(f"{dwell:.0f}s ch{ch}" for ch, dwell in spec.social)
    if not spec.explore:
        return social or "no channels"
    if spec.explore_mode == "one":
        explore = f"1 of {len(spec.explore)} explore @ {spec.dwell_explore:.0f}s"
    else:
        explore = f"all {len(spec.explore)} explore @ {spec.dwell_explore:.0f}s"
    return " + ".join(part for part in (social, explore) if part)
