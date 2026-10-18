#!/usr/bin/env python3
"""
dump3411 WiFi detector — offline Remote ID capture.

Puts one or more USB monitor-mode adapters into monitor mode and listens
for Remote ID. A 2.4-only card (Alfa AWUS036NEH, RT3070) still hops
channels 1-11. A dual-band card sweeps 2.4 and 5 GHz, spending most of
its time on the social channels (6 and 149). Two or more cards split the
work: one covers 2.4 GHz, the rest divide 5 GHz. ``--channel`` still pins
a single radio.
No network connection, no token, no data sharing.

Supports:
  - Wi-Fi Beacon transport (vendor IE, OUI FA:0B:BC, type 0x0D)  [F3411-19/22a]
  - Wi-Fi NAN transport detection (action frames, OUI 50:6F:9A)  [F3411-22a]
  - French Direct Remote ID beacons (vendor IE, OUI 6A:5C:35, type 0x01, TLV)

Uses raw AF_PACKET sockets (stdlib only — no scapy dependency).

Usage:
    sudo python3 wifi_feeder.py --iface wlan1 [--channel CH] [--verbose]
    sudo python3 wifi_feeder.py --iface auto          # every USB adapter

Requirements:
    sudo apt install iw rfkill
"""

import argparse
import hashlib
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # avoid hard import — feeder must run standalone
    from tracker import Tracker

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dump3411.wifi")


# -- Constants -----------------------------------------------------------------

# Vendor-specific IE OUI for ASTM F3411 Wi-Fi Beacon transport
ASTM_OUI      = bytes([0xFA, 0x0B, 0xBC])
ASTM_OUI_TYPE = 0x0D  # Remote ID app code

# French Direct Remote ID ("signalement électronique", arrêté of 27 December
# 2019). Its own vendor IE and a TLV payload, not ODID: see parse_frdid.
FRDID_OUI      = bytes([0x6A, 0x5C, 0x35])
FRDID_OUI_TYPE = 0x01

# Parrot SA's OUI. Some Remote ID beacons carry the ASTM layout under it:
# OUI(3) + type(1) + send counter(1) + ODID Message Pack. The layout comes
# from Sky-Spy's receiver, not from a Parrot capture. Parrot uses this OUI
# for other vendor IEs too, so it only counts when a well-formed pack
# follows (see _is_message_pack). Unrelated to the French scheme's own OUI.
PARROT_OUI = bytes([0x90, 0x3A, 0xE6])

# Wi-Fi Alliance NAN OUI (action frames)
NAN_OUI      = bytes([0x50, 0x6F, 0x9A])
NAN_OUI_TYPE = 0x13  # NAN

# ASTM F3411-22a Open Drone ID NAN Service ID
# First 6 bytes of SHA-256("org.opendroneid.remoteid")
# Consumer NAN (Apple AirDrop, Google Nearby Share, etc.) will never match this.
ODID_NAN_SERVICE_ID = hashlib.sha256(b"org.opendroneid.remoteid").digest()[:6]

# 2.4 GHz channels (RT3070 is 2.4 GHz only)
CHANNELS_24 = list(range(1, 12))  # 1-11 (US band)

# Channels 12-13 are legal in most of the world outside North America
# (Europe, Japan, much of Asia), so a drone's own Wi-Fi, and its beacon
# Remote ID with it, can sit there. Opt-in via --max-channel because the
# adapter can only tune them when the kernel's regulatory domain allows.
# Channel 14 (Japan, 802.11b only) is left out: drones don't use it.
MAX_CHANNEL_CHOICES = (11, 12, 13)


def channels_up_to(max_channel: int) -> list[int]:
    """2.4 GHz hop list 1..max_channel."""
    return list(range(1, max_channel + 1))

MSG_TYPE = {
    0x0: "Basic ID",
    0x1: "Location/Vector",
    0x2: "Authentication",
    0x3: "Self ID",
    0x4: "System",
    0x5: "Operator ID",
    0xF: "Message Pack",
}

ID_TYPE = {
    0: "None",
    1: "Serial Number (ANSI/CTA-2063-A)",
    2: "CAA Assigned",
    3: "UTM Assigned",
    4: "Specific Session ID",
}

UA_TYPE = {
    0: "None",
    1: "Aeroplane",
    2: "Helicopter/Multirotor",
    3: "Gyroplane",
    4: "Hybrid Lift",
    5: "Ornithopter",
    6: "Glider",
    7: "Kite",
    8: "Free Balloon",
    9: "Captive Balloon",
    10: "Airship",
    11: "Free Fall/Parachute",
    12: "Rocket",
    13: "Tethered Powered Aircraft",
    14: "Ground Obstacle",
    255: "Other",
}

# System message byte 1, bits 0-1: what the lat/lon in that message actually
# represents. "takeoff" means the drone's own launch point, not a live
# operator position — conflating the two under one "operator" label is what
# makes coordinates look like they randomly land in the drone vs. operator
# column when a transmitter mixes location types across messages.
OPERATOR_LOCATION_TYPE = {
    0: "takeoff",
    1: "live_gnss",
    2: "fixed",
}


# -- Remote ID Decoder ---------------------------------------------------------

def parse_basic_id(data: bytes) -> dict:
    if len(data) < 25:
        return {}
    id_type = (data[1] >> 4) & 0x0F
    ua_type = data[1] & 0x0F
    uas_id  = data[2:22].rstrip(b'\x00').decode('ascii', errors='replace')
    return {
        "id_type":     ID_TYPE.get(id_type, f"Unknown({id_type})"),
        "ua_type":     UA_TYPE.get(ua_type, f"Unknown({ua_type})"),
        "id_type_raw": id_type,         # raw enum — consumed by Tracker.update_basic_id
        "ua_type_raw": ua_type,
        "uas_id":      uas_id,
    }


def parse_location(data: bytes) -> dict:
    """Parse a Location/Vector message (msg type 0x1) per ASTM F3411 (25 bytes).

      byte 0:        msg_type<<4 | version    (caller has consumed)
      byte 1:        status flags
                       bit 0:    Speed Multiplier (0 = ×0.25 m/s, 1 = ×0.75 m/s + 63.75)
                       bit 1:    E/W Direction Segment (0 = 0-179°, 1 = 180-359°)
                       bit 2:    Height Type (0 = Above Takeoff, 1 = AGL)
                       bit 3:    reserved
                       bits 4-7: Operational Status
      byte 2:        Direction (uint8, 0-179°; add 180° if segment bit set)
      byte 3:        Speed Horizontal (uint8 × 0.25, or × 0.75 + 63.75 m/s)
      byte 4:        Speed Vertical (int8 × 0.5 m/s)
      bytes 5-8:     Latitude (int32 × 1e-7°)
      bytes 9-12:    Longitude (int32 × 1e-7°)
      bytes 13-14:   Pressure Altitude  (uint16 × 0.5 − 1000 m)
      bytes 15-16:   Geodetic Altitude  (uint16 × 0.5 − 1000 m)
      bytes 17-18:   Height (AGL/takeoff) (uint16 × 0.5 − 1000 m)
      byte 19+:      accuracies, timestamp, reserved — not decoded
    """
    if len(data) < 25:
        return {}

    speed_mult  = data[1] & 0x01
    dir_segment = (data[1] >> 1) & 0x01
    height_type = (data[1] >> 2) & 0x01

    heading = float(data[2]) + (180.0 if dir_segment else 0.0)
    # Above 255 * 0.25 = 63.75 m/s the encoding switches to 0.75 m/s steps
    # offset by 63.75, so 254.25 m/s still fits in a uint8.
    speed   = data[3] * 0.75 + 63.75 if speed_mult else data[3] * 0.25
    vspeed  = struct.unpack_from('<b', data, 4)[0] * 0.5

    lat = struct.unpack_from('<i', data, 5)[0] * 1e-7
    lon = struct.unpack_from('<i', data, 9)[0] * 1e-7

    # Reject placeholder GPS broadcast before lock (e.g. DJI firmware transmits
    # lat>90 or lon>180 as a sentinel until GPS acquires).
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return {}

    geodetic_alt = struct.unpack_from('<H', data, 15)[0] * 0.5 - 1000.0
    height_m     = struct.unpack_from('<H', data, 17)[0] * 0.5 - 1000.0

    result = {
        "latitude":       round(lat, 7),
        "longitude":      round(lon, 7),
        "ground_speed":   round(speed, 2),
        "vertical_speed": round(vspeed, 2),
        "heading":        round(heading, 1),
        "height_type":    "AGL" if height_type == 1 else "Above Takeoff",
    }
    # uint16 value 0x0000 decodes to −1000.0 m, the ODID "unknown" sentinel.
    if geodetic_alt > -1000.0:
        result["altitude_geo"] = round(geodetic_alt, 1)
    if height_m > -1000.0:
        result["height_agl"] = round(height_m, 1)
    return result


def parse_system_msg(data: bytes) -> dict:
    """Parse a System message (msg type 0x4) per ASTM F3411 (25 bytes).

      byte 0:        msg_type<<4 | version    (caller has consumed)
      byte 1:        classification + operator location source
      bytes 2-5:     Operator Latitude  (int32 × 1e-7°)
      bytes 6-9:     Operator Longitude (int32 × 1e-7°)
      bytes 10-11:   Flight Area Count (uint16)
      byte 12:       Flight Area Radius (uint8 × 10 m)
      bytes 13-14:   Flight Area Ceiling (uint16 × 0.5 − 1000 m)
      bytes 15-16:   Flight Area Floor   (uint16 × 0.5 − 1000 m)
      byte 17:       UA Classification (EU category / class)
      bytes 18-19:   Operator Altitude / takeoff geo (uint16 × 0.5 − 1000 m)
      bytes 20+:     timestamp, reserved — not decoded
    """
    if len(data) < 20:
        return {}
    loc_type    = data[1] & 0x03
    op_lat      = struct.unpack_from('<i', data,  2)[0] * 1e-7
    op_lon      = struct.unpack_from('<i', data,  6)[0] * 1e-7
    area_count  = struct.unpack_from('<H', data, 10)[0]
    area_radius = data[12] * 10
    alt_takeoff = struct.unpack_from('<H', data, 18)[0] * 0.5 - 1000.0

    result: dict = {
        "area_count":    area_count,
        "area_radius_m": area_radius,
    }
    # Spec ranges: lat ∈ [-90, 90], lon ∈ [-180, 180]. Out-of-range values are
    # placeholder/sentinel — keep them out of the feed rather than emit 152° N.
    if abs(op_lat) <= 90.0 and abs(op_lon) <= 180.0:
        result["operator_lat"]           = round(op_lat, 7)
        result["operator_lon"]           = round(op_lon, 7)
        result["operator_location_type"] = OPERATOR_LOCATION_TYPE.get(loc_type, "reserved")
    # -1000.0 m is the ODID "unknown" altitude sentinel.
    if alt_takeoff > -1000.0:
        result["alt_takeoff_geo"] = round(alt_takeoff, 1)
    return result


def parse_operator_id(data: bytes) -> dict:
    if len(data) < 22:
        return {}
    return {
        "operator_id_type": data[1],
        "operator_id":      data[2:22].rstrip(b'\x00').decode('ascii', errors='replace'),
    }


def parse_self_id(data: bytes) -> dict:
    """Parse a Self-ID message (msg type 0x3) per ASTM F3411 (25 bytes).

      byte 0:        msg_type<<4 | version    (caller has consumed)
      byte 1:        Description Type (0 = text, 1 = emergency, 2 = extended)
      bytes 2-24:    Description (23 bytes ASCII, null-padded)

    Free-text "purpose of flight" string. Common examples: "Search and
    Rescue", "Photography Lesson", "Flight Training", "Pipeline Survey".
    """
    if len(data) < 25:
        return {}
    return {
        "description_type": data[1],
        "description":      data[2:25].rstrip(b'\x00').decode('ascii', errors='replace'),
    }


def parse_message_pack(data: bytes) -> list:
    if len(data) < 3:
        return []
    msg_size  = data[1]
    msg_count = data[2]
    messages  = []
    for i in range(msg_count):
        offset = 3 + i * msg_size
        if offset + msg_size > len(data):
            break
        messages.append(data[offset: offset + msg_size])
    return messages


def decode_rid_message(raw_bytes: bytes) -> dict | None:
    if len(raw_bytes) < 2:
        return None
    msg_type  = (raw_bytes[0] >> 4) & 0x0F
    type_name = MSG_TYPE.get(msg_type, f"Unknown(0x{msg_type:X})")
    result    = {"message_type": type_name, "raw_hex": raw_bytes.hex().upper()}
    if msg_type == 0x0:
        result.update(parse_basic_id(raw_bytes))
    elif msg_type == 0x1:
        result.update(parse_location(raw_bytes))
    elif msg_type == 0x3:
        result.update(parse_self_id(raw_bytes))
    elif msg_type == 0x4:
        result.update(parse_system_msg(raw_bytes))
    elif msg_type == 0x5:
        result.update(parse_operator_id(raw_bytes))
    elif msg_type == 0xF:
        sub_msgs = parse_message_pack(raw_bytes)
        result["messages"] = [m for m in (decode_rid_message(s) for s in sub_msgs) if m]
    return result


# -- Raw 802.11 Frame Parsers --------------------------------------------------
# Replaces scapy — uses stdlib socket + struct only.

def _freq_to_channel(freq_mhz: int) -> int | None:
    """Wi-Fi center frequency (MHz) to channel number. None if unknown."""
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2412) // 5 + 1
    if freq_mhz == 2484:
        return 14
    if 5180 <= freq_mhz <= 5885:
        return (freq_mhz - 5000) // 5
    return None


def _parse_radiotap(data: bytes) -> tuple[int, int | None, int | None]:
    """
    Parse RadioTap header (IEEE 802.11-2020 Annex I).
    Returns (header_length, rssi_dbm_or_None, channel_or_None).

    Fields are walked in present-bitmap order with natural alignment relative
    to the start of the header. Only fields needed to reach dBm Signal (bit 5)
    are decoded; the rest are skipped by size. The channel comes from the
    frequency the frame was actually received on, which is what a camp decision
    has to trust — the hopper's idea of the current channel can be a retune behind.
    """
    if len(data) < 8:
        return len(data), None, None

    rt_len  = struct.unpack_from('<H', data, 2)[0]
    present = struct.unpack_from('<I', data, 4)[0]

    rssi    = None
    channel = None
    offset  = 8  # first field starts after the fixed 8-byte header

    # Bit 31 (EXT): chipsets like Atheros AR9271 chain additional present words
    # before field data begins. Read each word and check its own bit 31 — do not
    # re-check the first word, which never changes. `present` (first word) is
    # preserved for field parsing since standard bits 0–28 live only there.
    ext_word = present
    while ext_word & (1 << 31):
        if offset + 4 > len(data):
            return rt_len, None, None
        ext_word = struct.unpack_from('<I', data, offset)[0]
        offset += 4

    # Bit 0: TSFT — uint64, align 8
    if present & (1 << 0):
        offset = (offset + 7) & ~7
        offset += 8
    # Bit 1: Flags — uint8
    if present & (1 << 1):
        offset += 1
    # Bit 2: Rate — uint8
    if present & (1 << 2):
        offset += 1
    # Bit 3: Channel — uint16 freq + uint16 flags, align 2
    if present & (1 << 3):
        offset = (offset + 1) & ~1
        if offset + 2 <= len(data):
            freq_mhz = struct.unpack_from('<H', data, offset)[0]
            channel = _freq_to_channel(freq_mhz)
        offset += 4
    # Bit 4: FHSS — uint8 hop_set + uint8 hop_pattern
    if present & (1 << 4):
        offset += 2
    # Bit 5: dBm Antenna Signal — int8
    if present & (1 << 5):
        if offset < len(data):
            rssi = struct.unpack_from('b', data, offset)[0]
        offset += 1

    return rt_len, rssi, channel


def _mac_str(b: bytes) -> str:
    return ':'.join(f'{x:02x}' for x in b)


def _parse_dot11_mgmt(data: bytes) -> tuple[int, str, int] | None:
    """
    Parse an 802.11 management frame MAC header.

    Returns (subtype, addr2_mac_str, body_offset) or None if not a mgmt frame.
    addr2 is the transmitter (Source Address).
    Management frames have a fixed 24-byte MAC header.
    """
    if len(data) < 24:
        return None
    fc0 = data[0]
    frame_type    = (fc0 >> 2) & 0x3
    frame_subtype = (fc0 >> 4) & 0xF
    if frame_type != 0:          # 0 = management
        return None
    addr2 = _mac_str(data[10:16])
    return frame_subtype, addr2, 24


def _is_message_pack(data: bytes) -> bool:
    """True if data starts with a structurally valid ODID Message Pack:
    type 0xF, 25-byte messages, 1-9 of them, all present."""
    if len(data) < 3 or (data[0] >> 4) != 0xF or data[1] != 25:
        return False
    count = data[2]
    return 1 <= count <= 9 and len(data) >= 3 + count * 25


def _extract_beacon_rid(body: bytes) -> bytes | None:
    """
    Walk 802.11 beacon Information Elements looking for the vendor-specific
    ASTM F3411 Remote ID payload (OUI FA:0B:BC, type 0x0D).

    Beacon frame body layout (after 24-byte MAC header):
      Fixed parameters: 8 (timestamp) + 2 (beacon interval) + 2 (capability) = 12 bytes
      Then: IE chain — tag(1) + length(1) + value(length)

    The vendor IE itself (after OUI + vendor_type) is laid out per the
    OpenDroneID Wi-Fi Beacon transport:
        OUI(3) + vendor_type=0x0D(1) + send_counter(1) + ODID_message(25..)
    Note the **send counter** byte sitting between vendor_type and the ODID
    message. Skipping only OUI+type (4 bytes) leaves the counter at the start
    of the returned payload, which the decoder then misreads as the ODID
    msg_type byte — every downstream field shifts by one and the uas_id grows
    leading control bytes. Strip the counter too.

    The same layout under Parrot's OUI (90:3A:E6) is accepted when a valid
    Message Pack follows; its type byte isn't checked, since its value
    isn't documented.

    Returns the ODID message (single message or Message Pack) or None.
    """
    offset = 12  # skip fixed parameters
    while offset + 2 <= len(body):
        tag_id  = body[offset]
        tag_len = body[offset + 1]
        end     = offset + 2 + tag_len
        if end > len(body):
            break
        if tag_id == 221:  # Vendor Specific IE
            info = body[offset + 2: end]
            if len(info) >= 6 and info[:3] == ASTM_OUI and info[3] == ASTM_OUI_TYPE:
                return info[5:]   # OUI(3) + vendor_type(1) + counter(1)
            if len(info) >= 6 and info[:3] == PARROT_OUI and _is_message_pack(info[5:]):
                return info[5:]
        offset = end
    return None


def _extract_beacon_frdid(body: bytes) -> bytes | None:
    """
    Walk beacon IEs for the French Direct Remote ID vendor IE (OUI 6A:5C:35,
    type 0x01) and return its TLV payload. Unlike the ASTM element there is
    no send counter: the TLVs start straight after OUI(3) + type(1).
    """
    offset = 12  # skip fixed parameters
    while offset + 2 <= len(body):
        tag_id  = body[offset]
        tag_len = body[offset + 1]
        end     = offset + 2 + tag_len
        if end > len(body):
            break
        if tag_id == 221:
            info = body[offset + 2: end]
            if len(info) > 4 and info[:3] == FRDID_OUI and info[3] == FRDID_OUI_TYPE:
                return info[4:]
        offset = end
    return None


def parse_frdid(data: bytes) -> dict | None:
    """
    Decode a French Direct Remote ID TLV payload.

    Each field is type(1) + length(1) + value, **big-endian** (ODID is
    little-endian). Types, per opendroneid-core-c's frdid_build:

      0x01  protocol version (uint8, 1)
      0x02  French ID (30 ASCII bytes: maker trigram + model + serial)
      0x03  ANSI/CTA-2063 serial number (ASCII, variable length)
      0x04  latitude          (int32 × 1e-5°)
      0x05  longitude         (int32 × 1e-5°)
      0x06  altitude, MSL     (int16, m)
      0x07  height above takeoff (int16, m)
      0x08  takeoff latitude  (int32 × 1e-5°)
      0x09  takeoff longitude (int32 × 1e-5°)
      0x0A  horizontal speed  (uint8, m/s)
      0x0B  true course       (uint16, degrees 0-359)

    Senders omit fields they don't have, so every key is optional. Returns
    None when a TLV overruns the payload or nothing identifies the drone,
    since a beacon we can't attribute is no use to the tracker.
    """
    fields: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            return None
        t, n = data[offset], data[offset + 1]
        if offset + 2 + n > len(data):
            return None
        fields[t] = data[offset + 2: offset + 2 + n]
        offset += 2 + n

    def text(t: int) -> str:
        return fields.get(t, b"").split(b"\0", 1)[0].decode("ascii", errors="replace").strip()

    def num(t: int, fmt: str):
        v = fields.get(t)
        return struct.unpack(fmt, v)[0] if v is not None and len(v) == struct.calcsize(fmt) else None

    result: dict = {}
    french_id, serial = text(0x02), text(0x03)
    if french_id:
        result["french_id"] = french_id
    if serial:
        result["serial"] = serial
    if not result:
        return None

    lat, lon = num(0x04, ">i"), num(0x05, ">i")
    if lat is not None and lon is not None and abs(lat) <= 9_000_000 and abs(lon) <= 18_000_000:
        result["latitude"]  = round(lat * 1e-5, 5)
        result["longitude"] = round(lon * 1e-5, 5)
    alt, height = num(0x06, ">h"), num(0x07, ">h")
    if alt is not None:
        result["altitude_geo"] = float(alt)
    if height is not None:
        result["height_agl"] = float(height)
    speed, course = num(0x0A, ">B"), num(0x0B, ">H")
    if speed is not None:
        result["ground_speed"] = float(speed)
    if course is not None and course < 360:
        result["heading"] = float(course)
    to_lat, to_lon = num(0x08, ">i"), num(0x09, ">i")
    if to_lat is not None and to_lon is not None and abs(to_lat) <= 9_000_000 and abs(to_lon) <= 18_000_000:
        result["takeoff_lat"] = round(to_lat * 1e-5, 5)
        result["takeoff_lon"] = round(to_lon * 1e-5, 5)
    return result


def _is_nan_action(body: bytes) -> bool:
    """
    Detect Wi-Fi NAN action frames carrying ODID (ASTM F3411-22a).
    Requires the ODID NAN Service ID to be present in the frame body, which
    filters out consumer NAN traffic (Apple AirDrop/Handoff, Google Nearby
    Share) that uses the same OUI/type but a different Service ID.
    """
    if not (
        len(body) >= 6 and
        body[0] == 4 and           # Category: Public Action
        body[2:5] == NAN_OUI and
        body[5] == NAN_OUI_TYPE
    ):
        return False
    return ODID_NAN_SERVICE_ID in body[:64]


def _extract_nan_odid(body: bytes) -> bytes | None:
    """
    Pull the ODID payload out of a NAN Service Discovery Frame body.

    NAN Public Action Frame body layout:
        Category(1)=0x04 + Action(1)=0x09 + OUI(3) + OUI_Type(1)=0x13
        followed by NAN attributes.

    Each NAN attribute:
        AttrID(1) + Length(2, little-endian) + Body(Length)

    We walk attributes looking for the Service Descriptor Attribute (ID=3)
    whose Service ID matches the ODID Service ID hash; from its body we
    parse out the Service Info bytes that carry the ODID message.

    Some implementations stick an extra OUI-Subtype byte between OUI_Type
    and the first NAN attribute; try both offsets to be tolerant.
    """
    for start in (6, 7):
        payload = _walk_nan_attrs(body, start)
        if payload is not None:
            return payload
    return None


def _walk_nan_attrs(body: bytes, offset: int) -> bytes | None:
    while offset + 3 <= len(body):
        attr_id  = body[offset]
        attr_len = struct.unpack_from('<H', body, offset + 1)[0]
        attr_end = offset + 3 + attr_len
        if attr_end > len(body):
            return None
        if attr_id == 0x03:                 # Service Descriptor Attribute
            payload = _parse_nan_sda(body[offset + 3: attr_end])
            if payload is not None:
                return payload
        offset = attr_end
    return None


def _parse_nan_sda(sda: bytes) -> bytes | None:
    """
    Parse a NAN Service Descriptor Attribute body.

    SDA layout:
        Service ID (6)        — match against ODID_NAN_SERVICE_ID
        Instance ID (1)
        Requestor Instance ID (1)
        Service Control (1)   — bit field describing which optional fields follow
        Binding Bitmap (2)    — present if Service Control bit 6
        Matching Filter       — 1-byte length + N bytes, if bit 2
        Service Response Flt  — 1-byte length + N bytes, if bit 3
        Service Info          — 1-byte length + N bytes, if bit 4 (REQUIRED for ODID)

    Inside the Service Info, ODID Wi-Fi NAN mirrors BLE/Beacon: the first byte
    is a send counter, followed by the ODID message (single message or pack).
    We strip the counter and return the message bytes for ``decode_rid_message``.
    """
    if len(sda) < 9 or sda[:6] != ODID_NAN_SERVICE_ID:
        return None
    service_control = sda[8]
    offset = 9
    # Binding Bitmap (2 bytes) — present when bit 6 set.
    if service_control & (1 << 6):
        offset += 2
    # Matching Filter — present when bit 2 set; 1-byte length + body.
    if service_control & (1 << 2):
        if offset >= len(sda):
            return None
        offset += 1 + sda[offset]
    # Service Response Filter — present when bit 3 set; 1-byte length + body.
    if service_control & (1 << 3):
        if offset >= len(sda):
            return None
        offset += 1 + sda[offset]
    # Service Info — bit 4 must be set for ODID to be carried at all.
    if not (service_control & (1 << 4)) or offset >= len(sda):
        return None
    sil = sda[offset]
    offset += 1
    if sil < 2 or offset + sil > len(sda):
        return None
    # service_info[0] is the send counter; the rest is the ODID message.
    return sda[offset + 1: offset + sil]


# -- Monitor Mode --------------------------------------------------------------

_NM_CONF = "/etc/NetworkManager/conf.d/dump3411.conf"
_nm_lock = threading.Lock()


def _get_backhaul_iface() -> str | None:
    """Return the interface currently carrying the default route."""
    try:
        r = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        parts = r.stdout.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return None


def _get_iface_mac(iface: str) -> str | None:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except Exception:
        return None


def _nm_macs_in(text: str) -> set[str]:
    return {match.group(1).lower() for match in re.finditer(r"mac:([0-9a-f:]{17})", text, re.I)}


def _set_nm_unmanaged(mac: str | list[str]):
    """
    Tell NetworkManager to leave monitor-mode adapters alone. If NM manages
    an interface it fights monitor mode, causing zero packet capture.

    Merges with any MACs already in the file so a second radio does not
    wipe the first. ``mac`` may be one address or a list.
    """
    incoming = [mac] if isinstance(mac, str) else list(mac)
    with _nm_lock:
        try:
            with open(_NM_CONF) as f:
                existing = _nm_macs_in(f.read())
        except OSError:
            existing = set()
        macs = existing | {item.lower() for item in incoming if item}
        if not macs:
            return
        nm_body = (
            "# dump3411 — keep NetworkManager off monitor-mode adapters.\n"
            "[keyfile]\n"
            "unmanaged-devices=" + ";".join(f"mac:{item}" for item in sorted(macs)) + "\n"
        )
        try:
            os.makedirs(os.path.dirname(_NM_CONF), exist_ok=True)
            with open(_NM_CONF, "w") as f:
                f.write(nm_body)
            log.info("[Monitor] NetworkManager set to ignore %s", ", ".join(sorted(macs)))
        except Exception as e:
            log.warning(f"[Monitor] Could not update NM config: {e}")


def _ensure_monitor_safe(iface: str):
    """
    Refuse to monitor-mode the active backhaul interface. If the interface is
    NM-managed but not the backhaul, release it so monitor mode sticks.
    """
    backhaul = _get_backhaul_iface()
    if backhaul and iface == backhaul:
        log.error(f"Refusing to monitor {iface} — it is your active network interface.")
        log.error("Plug the USB adapter into a different interface and re-run.")
        sys.exit(1)

    try:
        r = subprocess.run(
            ["nmcli", "-g", "GENERAL.STATE", "device", "show", iface],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if "unmanaged" not in r.stdout.lower():
            mac = _get_iface_mac(iface)
            log.warning(f"[Monitor] {iface} is NetworkManager-managed — releasing it.")
            subprocess.run(
                ["nmcli", "device", "set", iface, "managed", "no"],
                capture_output=True, check=False,
            )
            if mac:
                _set_nm_unmanaged(mac)
    except Exception:
        pass


def set_monitor_mode(iface: str):
    """Bring interface up in monitor mode."""
    _ensure_monitor_safe(iface)
    log.info(f"Setting {iface} to monitor mode...")
    subprocess.run(["rfkill", "unblock", "all"], check=False, capture_output=True)
    subprocess.run(["ip", "link", "set", iface, "down"], check=True)
    subprocess.run(["iw", "dev", iface, "set", "type", "monitor"], check=True)
    subprocess.run(["ip", "link", "set", iface, "up"], check=True)
    log.info(f"{iface} is now in monitor mode")


def restore_managed_mode(iface: str):
    """Restore interface to managed mode on exit."""
    log.info(f"Restoring {iface} to managed mode...")
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False)
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"], check=False)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False)
    except Exception as e:
        log.warning(f"Could not restore managed mode: {e}")


def set_channel(iface: str, channel: int) -> bool:
    """Tune the monitor interface to ``channel`` (2.4 or 5 GHz).

    Returns False if iw refused, e.g. channel 12-13 under a regulatory domain
    that doesn't allow them, or a 5 GHz channel on a 2.4-only card. The
    adapter then stays on its previous channel.
    """
    r = subprocess.run(
        ["iw", "dev", iface, "set", "channel", str(channel)],
        check=False, capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.debug(f"iw set channel {channel} failed: {r.stderr.strip()}")
    return r.returncode == 0


REGDOM_HINT = ("the regulatory domain probably doesn't allow it; check `iw reg get` "
               "and set the Wi-Fi country, e.g. `sudo iw reg set FR` or the WLAN "
               "country in raspi-config")


# -- Channel Hopper ------------------------------------------------------------

class ChannelHopper(threading.Thread):
    """Cycles through 2.4 GHz channels at a fixed dwell time."""

    def __init__(self, iface: str, channels: list, dwell: float):
        super().__init__(daemon=True)
        self.iface    = iface
        self.channels = channels
        self.dwell    = dwell
        self.current_channel = channels[0] if channels else None
        self.plan_name = "legacy"
        self._halt    = threading.Event()  # not _stop: that name shadows Thread._stop() and breaks join()

    def run(self):
        log.info(f"Channel hopper started: {self.channels} @ {self.dwell}s dwell")
        while not self._halt.is_set():
            for ch in list(self.channels):
                if self._halt.is_set():
                    break
                if not set_channel(self.iface, ch) and ch > CHANNELS_24[-1]:
                    # Without this the hop would silently dwell twice as long
                    # on channel 11 and look like 12-13 were covered.
                    log.warning(f"Channel {ch} rejected by {self.iface}; dropping it "
                                f"from the hop list: {REGDOM_HINT}")
                    self.channels.remove(ch)
                    continue
                self.current_channel = ch
                time.sleep(self.dwell)

    def notify_detection(self, channel: int | None) -> None:
        """Flat hop ignores detection feedback. Present so capture can always call it."""

    def stop(self):
        self._halt.set()


# -- WiFi Detector -------------------------------------------------------------

# Sentinel: None is a valid hopper (fixed channel), so the default has to be
# something else or an explicit BandHopper could not be told apart from "build
# the legacy hopper".
_DEFAULT_HOPPER = object()


class WiFiFeeder:
    def __init__(self, iface: str, verbose: bool = False, channel_dwell: float = 0.2,
                 channel: int | None = None, tracker: "Tracker | None" = None,
                 max_channel: int = CHANNELS_24[-1], hopper=_DEFAULT_HOPPER,
                 role: str | None = None):
        self.iface     = iface
        self.verbose   = verbose
        self.tracker   = tracker      # optional; when set, decoded messages also feed it
        self.channel   = channel      # fixed channel; None = hop
        self.count     = 0
        self.nan_count = 0
        self.fr_count  = 0
        self._stop     = threading.Event()  # for orderly shutdown from another thread
        if hopper is _DEFAULT_HOPPER:
            self.hopper = None if channel is not None else \
                ChannelHopper(iface, channels_up_to(max_channel), channel_dwell)
            self.role = role or ("fixed" if channel is not None else "legacy")
        else:
            self.hopper = hopper
            self.role = role or ("fixed" if hopper is None else "scan")

    def _tuned_channel(self) -> int | None:
        if self.hopper is not None:
            return getattr(self.hopper, "current_channel", None)
        return self.channel

    def _heard(self, channel: int | None) -> None:
        """A decoded Remote ID frame landed. The hopper may camp on it."""
        hopper = self.hopper
        if hopper is None or not hasattr(hopper, "notify_detection"):
            return
        hopper.notify_detection(channel if channel is not None else self._tuned_channel())

    def status(self) -> dict:
        """A few integers for /status. No scanning, no locks worth taking."""
        hopper = self.hopper
        channel = self._tuned_channel()
        camp = getattr(hopper, "camp_channel", None) if hopper is not None else None
        plan = getattr(hopper, "plan_name", self.role) if hopper is not None else self.role
        sweep = getattr(hopper, "sweep_s", None) if hopper is not None else None
        targets = None
        if hopper is not None:
            targets = getattr(hopper, "target_channels", None)
            if targets is None and hasattr(hopper, "channels"):
                targets = hopper.channels
        return {
            "iface":    self.iface,
            "role":     self.role,
            "plan":     plan,
            "channel":  channel,
            "camping":  camp,
            "channels": None if targets is None else len(list(targets)),
            "sweep_s":  None if sweep is None else round(float(sweep), 1),
        }

    def _on_packet(self, data: bytes):
        rt_len, rssi, channel = _parse_radiotap(data)
        if rt_len >= len(data):
            return
        if channel is None:
            channel = self._tuned_channel()

        mac_data = data[rt_len:]
        header = _parse_dot11_mgmt(mac_data)
        if header is None:
            return

        subtype, addr2, body_offset = header
        body = mac_data[body_offset:]

        # ---- Wi-Fi Beacon Remote ID (subtype 8) ----
        if subtype == 8:
            rid_payload = _extract_beacon_rid(body)
            if rid_payload is None:
                frdid = _extract_beacon_frdid(body)
                if frdid is not None:
                    self._on_frdid(addr2, frdid, rssi, channel)
                return

            decoded = decode_rid_message(rid_payload)
            if decoded is None:
                return

            if decoded.get("message_type") == "Message Pack":
                sub_messages = decoded.get("messages", [])
            else:
                sub_messages = [decoded]

            # Process Basic ID first within a pack so the tracker's MAC →
            # uas_id mapping is in place when Location / System / Operator-ID
            # arrive in the same beacon.
            sub_messages = sorted(
                sub_messages,
                key=lambda m: 0 if m.get("message_type") == "Basic ID" else 1,
            )

            for msg in sub_messages:
                mtype = msg.get("message_type", "?")
                # Drop Location/Vector messages with no valid GPS fix.
                if mtype == "Location/Vector" and "latitude" not in msg:
                    continue
                self.count += 1

                # Feed the per-drone tracker if one was injected.
                if self.tracker is not None:
                    self._update_tracker(addr2, mtype, msg, rssi)

                if self.verbose or mtype in ("Basic ID", "Location/Vector", "Self ID"):
                    uas_id = msg.get("uas_id", "")
                    lat    = msg.get("latitude")
                    lon    = msg.get("longitude")
                    desc   = msg.get("description")
                    if uas_id:
                        detail = f"UAS-ID={uas_id}"
                    elif lat is not None:
                        detail = f"lat={lat} lon={lon}"
                    elif desc:
                        detail = f'Description="{desc}"'
                    else:
                        detail = ""
                    log.info(
                        f"[WiFi-Beacon] ch{channel}  MAC={addr2}  RSSI={rssi}dBm  "
                        f"Type={mtype}  {detail}"
                    )
            self._heard(channel)
            return

        # ---- Wi-Fi NAN Remote ID (subtype 13 — action frame) ----
        if subtype == 13 and _is_nan_action(body):
            self.nan_count += 1

            rid_payload = _extract_nan_odid(body)
            if rid_payload is None:
                # NAN Service ID matched but the SDA didn't parse to an ODID
                # payload. Always log this so that when the first real-world
                # NAN-broadcasting drone shows up and our spec interpretation
                # is slightly off, we have hex evidence to fix the parser.
                log.warning(
                    f"[WiFi-NAN] ch{channel}  MAC={addr2}  RSSI={rssi}dBm  "
                    f"matched NAN Service ID but SDA didn't parse; "
                    f"body[:80]={body.hex().upper()[:160]}"
                )
                return

            decoded = decode_rid_message(rid_payload)
            if decoded is None:
                return

            if decoded.get("message_type") == "Message Pack":
                sub_messages = decoded.get("messages", [])
            else:
                sub_messages = [decoded]

            # Same Basic-ID-first ordering as the beacon branch — the tracker
            # needs the MAC → uas_id mapping in place before Location/System
            # arrive in the same pack.
            sub_messages = sorted(
                sub_messages,
                key=lambda m: 0 if m.get("message_type") == "Basic ID" else 1,
            )

            for msg in sub_messages:
                mtype = msg.get("message_type", "?")
                if mtype == "Location/Vector" and "latitude" not in msg:
                    continue
                self.count += 1

                if self.tracker is not None:
                    self._update_tracker(addr2, mtype, msg, rssi,
                                         rid_source="wifi_nan")

                if self.verbose or mtype in ("Basic ID", "Location/Vector", "Self ID"):
                    uas_id = msg.get("uas_id", "")
                    lat    = msg.get("latitude")
                    lon    = msg.get("longitude")
                    desc   = msg.get("description")
                    if uas_id:
                        detail = f"UAS-ID={uas_id}"
                    elif lat is not None:
                        detail = f"lat={lat} lon={lon}"
                    elif desc:
                        detail = f'Description="{desc}"'
                    else:
                        detail = ""
                    log.info(
                        f"[WiFi-NAN] ch{channel}  MAC={addr2}  RSSI={rssi}dBm  "
                        f"Type={mtype}  {detail}"
                    )
            self._heard(channel)

    def _on_frdid(self, mac: str, payload: bytes, rssi, channel: int | None = None) -> None:
        """Handle a French Direct Remote ID beacon.

        One beacon carries identity, position and takeoff point together, so
        it's split into the ODID-shaped Basic ID / Location / System dicts
        _update_tracker already routes. Basic ID goes first so the MAC →
        uas_id mapping exists for the rest. The takeoff point fills the
        operator block as location type ``takeoff``, the same convention as
        an ODID System message that reports takeoff rather than a live fix.
        """
        fr = parse_frdid(payload)
        if fr is None:
            log.debug(f"[WiFi-FR] MAC={mac}  unparseable TLV payload: {payload.hex()}")
            return
        self.fr_count += 1

        # Prefer the ANSI/CTA-2063 serial: it's the same identifier an ODID
        # Basic ID would carry, so it lines up with FAA lookups and other
        # receivers. The French ID is the fallback.
        serial = fr.get("serial")
        messages = [("Basic ID", {
            "uas_id":      serial or fr["french_id"],
            "id_type_raw": 1 if serial else 0,
            # No UA type in the format. None leaves it unset, where 0 would
            # write "none" and block a later ODID Basic ID from filling it.
            "ua_type_raw": None,
        })]
        if "latitude" in fr:
            messages.append(("Location/Vector", fr))
        if "takeoff_lat" in fr:
            messages.append(("System", {
                "operator_lat":           fr["takeoff_lat"],
                "operator_lon":           fr["takeoff_lon"],
                "operator_location_type": "takeoff",
            }))

        if self.tracker is not None:
            for mtype, msg in messages:
                self._update_tracker(mac, mtype, msg, rssi, rid_source="wifi_beacon_fr")
        self._heard(channel)

        position = f"lat={fr['latitude']} lon={fr['longitude']}" if "latitude" in fr else "no position"
        log.info(
            f"[WiFi-FR] ch{channel}  MAC={mac}  RSSI={rssi}dBm  "
            f"UAS-ID={messages[0][1]['uas_id']}  {position}"
        )

    def _update_tracker(self, mac: str, mtype: str, msg: dict, rssi,
                        rid_source: str = "wifi_beacon") -> None:
        """Route a decoded sub-message to the appropriate Tracker.update_*.

        ``rid_source`` defaults to ``wifi_beacon`` so the existing beacon
        callsite stays unchanged; the NAN branch passes ``wifi_nan``.
        """
        if mtype == "Basic ID":
            self.tracker.update_basic_id(
                mac=mac, uas_id=msg.get("uas_id", ""),
                id_type_raw=msg.get("id_type_raw", 0),
                ua_type_raw=msg.get("ua_type_raw", 0),
                rssi=rssi, rid_source=rid_source,
            )
        elif mtype == "Location/Vector" and "latitude" in msg:
            self.tracker.update_location(
                mac=mac,
                lat=msg["latitude"], lon=msg["longitude"],
                alt_geo_m=msg.get("altitude_geo"),
                height_agl_m=msg.get("height_agl"),
                gs_mps=msg.get("ground_speed"),
                heading_deg=msg.get("heading"),
                vspeed_mps=msg.get("vertical_speed"),
                rssi=rssi, rid_source=rid_source,
            )
        elif mtype == "System":
            self.tracker.update_system(
                mac=mac,
                op_lat=msg.get("operator_lat"),
                op_lon=msg.get("operator_lon"),
                op_location_type=msg.get("operator_location_type"),
                alt_takeoff_m=msg.get("alt_takeoff_geo"),
                rssi=rssi, rid_source=rid_source,
            )
        elif mtype == "Operator ID":
            self.tracker.update_operator_id(
                mac=mac, operator_id=msg.get("operator_id", ""),
                rssi=rssi, rid_source=rid_source,
            )
        elif mtype == "Self ID":
            self.tracker.update_self_id(
                mac=mac, description=msg.get("description", ""),
                rssi=rssi, rid_source=rid_source,
            )

    def _heartbeat_loop(self):
        while not self._stop.wait(60):
            plan = getattr(self.hopper, "plan_name", self.role) if self.hopper else self.role
            log.info(
                f"[Heartbeat] {self.iface} plan={plan} ch{self._tuned_channel()}  "
                f"Beacon RID={self.count}  NAN={self.nan_count}  FR={self.fr_count}"
            )

    def stop(self) -> None:
        """Signal run() to exit; restore_managed_mode() runs in the finally."""
        self._stop.set()

    def run(self):
        try:
            self._capture()
        except Exception:
            log.exception("Wi-Fi feeder %s crashed", self.iface)

    def _capture(self):
        log.info(f"dump3411 WiFi detector — {self.iface} ({self.role})")

        set_monitor_mode(self.iface)
        if self.hopper is not None:
            channels = getattr(self.hopper, "channels", None)
            if channels is not None:
                log.info(f"Channels: {channels}")
            self.hopper.start()
        else:
            log.info(f"Fixed channel: {self.channel} (hopping disabled)")
            if not set_channel(self.iface, self.channel):
                hint = f": {REGDOM_HINT}" if self.channel > CHANNELS_24[-1] else ""
                log.error(f"Could not tune {self.iface} to channel {self.channel}{hint}")

        log.info("Scanning for Remote ID beacon frames (ASTM F3411)...")

        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        sock.bind((self.iface, 0))
        sock.settimeout(1.0)

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(65535)
                    self._on_packet(data)
                except socket.timeout:
                    continue
        except KeyboardInterrupt:
            log.info("Stopped.")
        finally:
            sock.close()
            if self.hopper is not None:
                self.hopper.stop()
            restore_managed_mode(self.iface)
            log.info(f"[Summary] Beacon RID={self.count}  NAN frames={self.nan_count}  FR beacons={self.fr_count}")


def _handle_sigterm(signum, frame):
    """systemctl stop sends SIGTERM — raise so run()'s finally restores the adapter."""
    raise KeyboardInterrupt


# -- Adapter discovery ---------------------------------------------------------

def _is_onboard(iface: str) -> bool:
    """Pi onboard Wi-Fi (brcmfmac, or an SDIO/MMC bus). Never put that in monitor mode."""
    try:
        driver = os.path.basename(os.readlink(f"/sys/class/net/{iface}/device/driver"))
    except OSError:
        driver = ""
    if driver == "brcmfmac":
        return True
    try:
        bus = os.path.basename(os.readlink(f"/sys/class/net/{iface}/device/subsystem"))
    except OSError:
        bus = ""
    return bus in ("sdio", "mmc")


def _phy_info(iface: str) -> str | None:
    try:
        info = subprocess.run(
            ["iw", "dev", iface, "info"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    phy = None
    for line in info.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("wiphy "):
            phy = stripped.split()[-1]
            break
    if phy is None:
        return None
    try:
        phyinfo = subprocess.run(
            ["iw", "phy", f"phy{phy}", "info"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if phyinfo.returncode != 0 and not phyinfo.stdout:
        return None
    return phyinfo.stdout


def _channels_and_bands(iw_info: str) -> tuple[frozenset[int], tuple[str, ...]]:
    """Enabled channel numbers, plus band capability (disabled channels still count
    as a band — the question there is what the silicon can do)."""
    channels: set[int] = set()
    bands: set[str] = set()
    for line in iw_info.splitlines():
        match = re.search(r"(\d{4})(?:\.\d+)?\s+MHz\s+\[(\d+)\]", line)
        if not match:
            continue
        freq = int(match.group(1))
        if 2400 <= freq <= 2500:
            bands.add("2.4")
        elif 5000 <= freq <= 5900:
            bands.add("5")
        if "disabled" in line:
            continue
        channels.add(int(match.group(2)))
    return frozenset(channels), tuple(sorted(bands))


def probe_adapter(iface: str, *, backhaul: str | None = None):
    """Build a scan_plan.Adapter from sysfs + iw. Probe failure leaves
    ``probed=False`` so the caller can still try the interface the user named."""
    from scan_plan import Adapter

    mac = _get_iface_mac(iface) or ""
    onboard = _is_onboard(iface)
    is_backhaul = bool(backhaul and iface == backhaul)
    info = _phy_info(iface)
    if info is None:
        log.warning("Could not read PHY info for %s — band detection unavailable", iface)
        return Adapter(
            iface=iface, mac=mac, monitor=True, onboard=onboard,
            backhaul=is_backhaul, supported=None, probed=False,
        )
    channels, bands = _channels_and_bands(info)
    monitor = bool(re.search(r"^\s*\*\s+monitor\s*$", info, re.MULTILINE))
    return Adapter(
        iface=iface, mac=mac, bands=bands, monitor=monitor, onboard=onboard,
        backhaul=is_backhaul, supported=channels, probed=True,
    )


def enumerate_adapters() -> list:
    backhaul = _get_backhaul_iface()
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return []
    ifaces = sorted(name for name in names if name.startswith(("wlan", "wlx")))
    return [probe_adapter(iface, backhaul=backhaul) for iface in ifaces]


def resolve_adapters(ifaces: list[str]) -> list:
    if len(ifaces) == 1 and ifaces[0] == "auto":
        return enumerate_adapters()
    backhaul = _get_backhaul_iface()
    return [probe_adapter(iface, backhaul=backhaul) for iface in ifaces]


def _skip_reason(adapter) -> str | None:
    if adapter.onboard:
        return "it is the Pi onboard radio"
    if adapter.backhaul:
        return "it is carrying the network connection"
    if not adapter.monitor:
        return "it does not advertise monitor mode"
    return None


def build_feeders(
    ifaces: list[str],
    *,
    verbose: bool = False,
    channel: int | None = None,
    channel_dwell: float = 0.2,
    max_channel: int = CHANNELS_24[-1],
    tracker=None,
    scan_plan: str = "auto",
) -> list[WiFiFeeder]:
    """One WiFiFeeder per radio the scan plan assigns.

    ``ifaces`` is interface names, or ``["auto"]`` to take every USB monitor
    adapter that is not the backhaul. Raises RuntimeError when nothing usable
    is plugged in, ValueError when ``--channel`` is combined with more than
    one radio.
    """
    from scan_plan import BandHopper, assign_roles, describe, forecast

    if channel is not None:
        if len(ifaces) != 1 or ifaces[0] == "auto":
            raise ValueError("--channel pins one radio; pass a single --wifi-iface")
        return [WiFiFeeder(
            iface=ifaces[0], verbose=verbose, channel=channel,
            max_channel=max_channel, tracker=tracker,
        )]

    adapters = resolve_adapters(ifaces)
    for adapter in adapters:
        reason = _skip_reason(adapter)
        if reason:
            log.info("Not using %s: %s", adapter.iface, reason)
    assignments = assign_roles(adapters, scan_plan=scan_plan, max_channel=max_channel)
    active = [item for item in assignments if item.role != "unused"]
    if not active:
        raise RuntimeError(
            "No monitor-capable USB Wi-Fi adapter found. Plug one in "
            "(not the interface carrying your network) and retry."
        )
    for item in assignments:
        if item.role == "unused" and item.note:
            log.info("Leaving %s unused: %s", item.iface, item.note)
        elif item.note and item.role == "legacy":
            log.info("%s: %s", item.iface, item.note)
    log.info("Wi-Fi plan: %s", describe(assignments))
    note = forecast(assignments)
    if note:
        log.info(note)

    macs = [item.mac for item in active if item.mac]
    if macs:
        _set_nm_unmanaged(macs)

    feeders: list[WiFiFeeder] = []
    for item in active:
        if item.spec is not None:
            hopper = BandHopper(item.iface, item.spec, tune=set_channel)
            feeders.append(WiFiFeeder(
                iface=item.iface, verbose=verbose, tracker=tracker,
                hopper=hopper, role=item.role,
            ))
        else:
            feeders.append(WiFiFeeder(
                iface=item.iface, verbose=verbose, tracker=tracker,
                channel_dwell=channel_dwell, max_channel=max_channel,
                role="legacy",
            ))
    return feeders


def _serve_feeders(feeders: list[WiFiFeeder]) -> None:
    """Block until the feeders stop. One radio runs on this thread so SIGTERM
    still lands in its finally; several radios each get a thread."""
    if len(feeders) == 1:
        feeders[0].run()
        return
    threads = []
    for feeder in feeders:
        thread = threading.Thread(
            target=feeder.run, daemon=True, name=f"wifi-{feeder.iface}",
        )
        thread.start()
        threads.append(thread)
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        for feeder in feeders:
            feeder.stop()
        time.sleep(1.5)


def main():
    parser = argparse.ArgumentParser(
        description="dump3411 WiFi Remote ID detector (offline)"
    )
    parser.add_argument(
        "--iface", action="append", default=None,
        help="Monitor-mode interface. Repeat for each adapter, or pass 'auto' "
             "to use every USB monitor adapter except the one carrying your "
             "network (default: wlan1)",
    )
    parser.add_argument(
        "--scan-plan", choices=["auto", "legacy"], default="auto",
        help="auto: social-weighted sweep, splitting bands across adapters. "
             "legacy: flat 2.4 GHz hop on one radio (default: auto)",
    )
    parser.add_argument(
        "--channel-dwell", type=float, default=0.2,
        help="Seconds per channel in the legacy hop (default: 0.2). Ignored by "
             "the social-weighted plans."
    )
    parser.add_argument(
        "--channel", type=int, default=None, metavar="CH",
        help="Stay on a single fixed channel (1 to --max-channel) instead of hopping"
    )
    parser.add_argument(
        "--max-channel", type=int, default=CHANNELS_24[-1], choices=MAX_CHANNEL_CHOICES,
        help="Highest 2.4 GHz channel to use (default: 11). 12-13 are legal outside "
             "North America and need a matching regulatory domain."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every decoded message and NAN frame"
    )
    args = parser.parse_args()

    if args.channel is not None and not 1 <= args.channel <= args.max_channel:
        parser.error(f"--channel must be 1-{args.max_channel} (raise --max-channel for 12-13)")

    signal.signal(signal.SIGTERM, _handle_sigterm)

    ifaces = args.iface or ["wlan1"]
    try:
        feeders = build_feeders(
            ifaces,
            verbose=args.verbose,
            channel_dwell=args.channel_dwell,
            channel=args.channel,
            max_channel=args.max_channel,
            scan_plan=args.scan_plan,
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    _serve_feeders(feeders)


if __name__ == "__main__":
    main()
