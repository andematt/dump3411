# dump3411

A local-only Remote ID detector for Linux. Picks up the mandatory drone identification broadcasts on BLE and Wi-Fi Beacon, decodes them per ASTM F3411, prints them to the system journal, and serves a status dashboard plus a JSON feed on the LAN. Nothing leaves the host.

Spiritual sibling of [dump1090](https://github.com/flightaware/dump1090) (ADS-B at 1090 MHz) and dump978 (UAT at 978 MHz). The `3411` is the ASTM standard number — Remote ID isn't tied to a single frequency the way ADS-B or UAT are, so a spec number is the more honest identifier.

## Set up a fresh Pi

This is the path that reproduces the running node: a newly flashed Raspberry Pi, USB monitor-mode adapters, the service starting on boot, and — if you want it — Home Assistant. Clone `main` of this fork.

Confirmed on a Raspberry Pi 4 (3 GB) running Raspberry Pi OS Trixie, with two Alfa AWUS036ACS adapters and the Pi's built-in Wi-Fi used only for SSH.

### 1. Flash the card

Use Raspberry Pi Imager. Install **Raspberry Pi OS Lite (64-bit)**. That image is Trixie. Bookworm's kernel does not ship `rtw88_8821au`, so an AWUS036ACS shows up in `lsusb` and never gets an interface.

In Imager's customisation, before you write the card:

- Set a hostname, a user, and a password.
- Enable SSH.
- Set the Wi-Fi country. This is also the regulatory domain. If you skip it, 5 GHz channels are marked disabled and the sweep drops them.
- If the Pi will join the LAN on its built-in Wi-Fi, enter that network here. Ethernet needs nothing extra.

Do not configure a USB adapter as the management network. dump3411 will refuse to put the interface that carries the default route into monitor mode, and it will refuse the Pi's onboard radio (`brcmfmac`). One of those two has to stay up for SSH.

### 2. Plug in hardware, then boot

- Power, and either Ethernet or the onboard Wi-Fi from the step above.
- The USB monitor adapters. Two Alfa AWUS036ACS is the setup this sweep is written for: one covers 2.4 GHz, the other walks 5 GHz.
- A powered USB hub if you are adding a third adapter, or if an adapter enumerates and then disappears. A Pi 4's ports share a 1.2 A budget. Two ACS adapters have run on the onboard ports; three or four should not.

Boot and SSH in as the user you set in Imager.

### 3. Confirm the radios before installing

```bash
cat /etc/os-release          # VERSION_CODENAME=trixie
lsusb                        # Realtek 8812AU/8821AU, one line per ACS
ip -br link                  # wlan0 onboard; wlan1/wlan2 (or wlx*) for the USB radios
sudo iw reg get              # country US (or yours), not 00
```

`iw` is not on the Lite image until step 4. If you need the country check first:

```bash
sudo apt update
sudo apt install -y iw
sudo iw reg get
```

If the country is unset, set it so it survives reboot, then reboot before scanning:

```bash
sudo raspi-config            # Localisation Options → WLAN Country
```

`sudo iw reg set US` works until the next reboot. It is not a substitute for raspi-config.

Each ACS should have a `wlan` or `wlx` interface. The driver is `rtw88_8821au`:

```bash
basename $(readlink /sys/class/net/wlan1/device/driver)
```

An adapter in `lsusb` with no interface is power, cable, or the wrong OS — not a dump3411 setting.

### 4. Install

Lite does not always include `git`.

```bash
sudo apt install -y git
git clone https://github.com/andematt/dump3411.git
cd dump3411
sudo ./install.sh
```

`install.sh` is idempotent. It:

1. Installs `python3-bleak`, `iw`, `rfkill`, and `bluez`.
2. Asks whether to install `python3-paho-mqtt`. Answer `y` if you will connect Home Assistant. Answer `n` and the detector still runs; MQTT stays off.
3. Checks that a USB Wi-Fi interface exists (anything `wlan*` / `wlx*` except `wlan0`).
4. Rewrites the systemd unit's path to this checkout and installs it.
5. Enables and starts `dump3411`.

The unit runs `--wifi-iface auto`. A second adapter plugged in later is picked up on restart. No unit edit.

The service is enabled, so it starts again after a reboot. Adapter names can swap; roles are assigned by MAC, so the same radio keeps 5 GHz.

### 5. Check that it is scanning

```bash
systemctl is-enabled dump3411          # enabled
systemctl is-active dump3411           # active
journalctl -u dump3411 -n 80 --no-pager
```

You want a plan line, then a measured sweep. On two ACS adapters that looks like:

```
Not using wlan0: it is the Pi onboard radio
Wi-Fi plan: wlan2 plan C role=2.4 ... ; wlan1 plan B role=5 21 ch ~21s
wlan2 plan C: 9s ch6 + 1 of 4 explore @ 1s — full sweep 40.1s including 14ms retune
wlan1 plan B: 1s ch149 + all 20 explore @ 1s — full sweep 21.5s including 22ms retune
```

The retune times are measured on that boot. A line that says `Not using wlanX: it is carrying the network connection` means that adapter is your SSH link and will not scan.

A `[org.bluez.Error.NotReady]` line in the first second is normal. `set_monitor_mode` runs `rfkill unblock all`, which also kicks Bluetooth, and the BLE scanner retries until the controller is back.

Dashboard, from any LAN host:

```bash
echo "http://$(hostname -I | awk '{print $1}'):8754/"
```

The radios table on that page is the same assignment the journal printed. No drones and an IDLE pill means the radios are up and the air is empty.

### 6. Optional: keep the journal across reboots

The journal is volatile by default. This drop-in caps it at 50 MB:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp journald-dump3411.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
```

Detection history in SQLite is a separate option. See **Persistent detection history** below. It is off until you set `HISTORY_DB`.

### Manual install

`install.sh` is the path above. The equivalent by hand, from a checkout of `main`:

```bash
sudo apt install -y python3-bleak iw rfkill bluez
# edit the ExecStart path in dump3411.service if the checkout is not /home/pi/dump3411
# leave --wifi-iface auto
sudo cp dump3411.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dump3411
```

## Home Assistant connection

Optional. The detector does not need a broker. This is how the Pi that is already scanning publishes into an existing Home Assistant Mosquitto broker and shows up as entities.

Home Assistant must already be on the LAN, with:

- The **Mosquitto broker** app installed, started, and set to start on boot. Port **1883** is the plaintext listener.
- The **MQTT** integration loaded. Discovery is on by default (`discovery_prefix: homeassistant`). Leave it on. dump3411 does not create entities any other way.

### 1. Add a broker login

In the Mosquitto app configuration, add a local login used only by this Pi. A separate user is better than reusing one that belongs to another device.

- Username: `dump3411`
- Password: one you generate. Do not commit it, and do not reuse another add-on's password.

Save, then restart the Mosquitto app. A login change does not apply until that restart. Other MQTT clients drop for a few seconds while it restarts.

Find the broker address from the Pi:

```bash
getent hosts homeassistant.local
```

Use that IP if mDNS is flaky. The port is 1883.

### 2. Point dump3411 at the broker

If you answered `n` to paho-mqtt during install:

```bash
sudo apt install -y python3-paho-mqtt
```

Write the env file the unit already loads (`EnvironmentFile=-/etc/dump3411.env`). Replace the broker address and the password:

```bash
sudo tee /etc/dump3411.env >/dev/null <<'EOF'
MQTT_BROKER=192.168.1.9:1883
MQTT_TOPIC_PREFIX=dump3411
MQTT_USER=dump3411
MQTT_PASSWORD=choose-a-password
EOF
sudo chmod 600 /etc/dump3411.env
sudo systemctl restart dump3411
```

`chmod 600` matters. The unit runs as root and is the only thing that should read the password.

The same values work as flags if you are not using the unit: `--mqtt-broker`, `--mqtt-topic-prefix`, `--mqtt-user`, `--mqtt-password`.

### 3. Confirm the publish

```bash
journalctl -u dump3411 -n 40 --no-pager | grep -E 'MQTT|discovery'
```

A good connect looks like:

```
connecting to MQTT broker 192.168.1.9:1883
MQTT connected to 192.168.1.9:1883
published 4 Home Assistant discovery configs under homeassistant/
```

The service writes those four retained discovery configs on every connect. After a broker restart that kept its data directory, they are still there. After a wipe of that directory, the next time dump3411 connects it writes them again.

`sudo python3 ha_setup.py` publishes the same configs without restarting the detector, and it does not touch `dump3411/online`. It reads `/etc/dump3411.env`. It does not create the broker user. `python3 ha_setup.py --dry-run` prints the topics and does not connect. `MQTT_DISCOVERY=false` in the env file turns discovery off.

### 4. What shows up in Home Assistant

Discovery creates one device and four entities. The state is a slice of a larger payload. The full drone row is on the broker, not in these four states.

| Entity | Topic | State |
|---|---|---|
| `binary_sensor.dump3411_online` | `dump3411/online` | `online` or `offline` |
| `sensor.dump3411_drones_active` | `dump3411/status` | drones in memory |
| `sensor.dump3411_messages` | `dump3411/status` | decoded messages since process start |
| `sensor.dump3411_last_detection` | `dump3411/events/detection` | latest UAS ID. Attributes are `rid_source`, `rssi`, `t`. `unknown` until a drone is heard |

`dump3411/status` (retained, every 5 s) also carries uptime, last-seen age, per-source counts, and CPU temp. `dump3411/drones/<id>` (retained) is one drone row in the same shape as `/data/remoteid.json`: identity, lat/lon, altitude, AGL, speed, track, climb rate, RSSI, transports, self-ID, operator block. An empty retained payload is published when that drone expires. `dump3411/events/detection` is one small event per decode and is not retained.

Units in those payloads are imperial, matching FEED.md.

### 5. What survives a reboot

| | Pi reboot | Home Assistant or Mosquitto reboot |
|---|---|---|
| The service | starts. The unit is enabled. | unrelated |
| Entity registrations | stay | stay, if the retained discovery configs are still on the broker |
| Online | `offline` on a clean stop, then `online` when it connects | restored from the broker, then refreshed when the Pi reconnects |
| Drones active, message count | reset to 0. The tracker is memory only unless `HISTORY_DB` is set | restored from the retained status message, then overwritten by the live Pi within 5 s |
| Last detection entity | keeps the last value Home Assistant already has. The event topic is not retained | not restored from the broker. Unknown until the next beacon |
| Full drone rows on `dump3411/drones/<id>` | old retained rows stay on the broker. A rebooted Pi does not clear ids it no longer has in memory | kept across a normal Mosquitto restart. The add-on persists retained messages on its data directory. Gone if that directory is wiped |
| Discovery configs | stay on the broker | same as the drone rows. dump3411 publishes them again on the next connect |
| Individual detection events | gone | gone |

A power cut does not run the clean stop. The broker's last-will sets `dump3411/online` to `offline`. It does not clear drone topics.


## What you'll see

When a Remote-ID-compliant drone broadcasts in range, the journal logs each decoded message. All three ASTM F3411 transports are decoded — BLE, Wi-Fi Beacon, and Wi-Fi NAN — with the BLE lines split by Bluetooth generation (`[BLE]` legacy advertising vs `[BLE5]` long-range/extended, classified from the wire format since a Message Pack cannot fit a legacy advertisement):

```
[BLE]           MAC=...  RSSI=-62dBm  Type=Basic ID         UAS-ID=1581F...
[BLE5]          MAC=...  RSSI=-68dBm  Type=Location/Vector  lat=40.7128 lon=-74.0060
[WiFi-Beacon]   MAC=...  RSSI=-71dBm  Type=Location/Vector  lat=40.7128 lon=-74.0060
[WiFi-NAN]      MAC=...  RSSI=-68dBm  Type=Basic ID         UAS-ID=...
```

France's national Direct Remote ID beacons (vendor element `6A:5C:35`, a TLV format that isn't ASTM F3411) are decoded too. Each beacon carries the identity and position in one line:

```
[WiFi-FR]       MAC=...  RSSI=-70dBm  UAS-ID=1581F...  lat=48.85837 lon=2.29448
```

The dashboard at `http://<host>:8754/` shows a live status pill, per-transport message counters, CPU temperature, and a table of currently tracked drones with clickable Google Maps links for both the drone position and the operator location. A header toggle switches the display between imperial (`ft·kt·°F`) and metric (`m·m/s·°C`); the wire feed stays imperial regardless.

If nothing's in range, the dashboard sits at IDLE and the journal stays quiet. That's expected — Remote ID is only broadcast by drones registered after September 2023, so coverage is sparse. See **[TESTING.md](./TESTING.md)** for how to put a transmitter on the air and confirm the whole path works.

## Screenshots

Dashboard with a single live drone — live drones table on top, Recent detections (last N days, configurable) below, both with clickable UAS-IDs that open the map view when history is enabled.

![Dashboard](./docs/screenshots/dashboard.png)

Map view at `/map?uas_id=…` — operator location pinned, drone polyline traced from history, each point clickable for the per-message detail (timestamp, altitude, speed, RSSI, transport).

![Map view](./docs/screenshots/map.png)

## Hardware

Confirmed on a Raspberry Pi Zero W with an Alfa AWUS036NEH adapter. Should work on **any Linux host** that has:

- **A Bluetooth adapter** for BLE Remote ID. Built-in is fine.
- **A USB Wi-Fi adapter that supports monitor mode** for Wi-Fi Beacon Remote ID. The Alfa AWUS036NEH (RT3070) is 2.4 GHz only, which is what the original flat hop was written for. Most built-in Wi-Fi cards can't reliably enter monitor mode, so a dedicated USB adapter is the easy path.
- **A dual-band adapter** if you want 5 GHz. The Alfa AWUS036ACS (RTL8811AU) changes channel in about 20 ms, which is what makes a 21-channel 5 GHz sweep take about 21 seconds instead of a minute of deaf retunes. Its driver (`rtw88_8821au`) is in newer kernels — Raspberry Pi OS Trixie has it; Bookworm often does not, in which case the adapter shows up in `lsusb` and never gets an interface. A Panda AC600-class MT7610U card is the fallback on those kernels. It works; it retunes slower, and the startup log says by how much.

On low-powered single-board hosts a powered USB hub is strongly recommended — one adapter can brown out a Pi Zero, and two or three will. A Pi 4's USB ports share one 1.2 A budget and one bus.

### Two adapters, and more than two

A monitor radio hears one channel at a time. Compliant aircraft beacon on channel 6 or 149 about once a second, and may transmit anywhere else only by going to at least 5 Hz, so the sweep sits on 6 and 149 and spends one second on each other channel. The 5 GHz list is 21 channels (149, 153–165, and the DFS channels 52–144). U-NII-1 is left out: it is indoor and low power, and a compliant beacon stays on 149 no matter where the video link is.

| Radios | What they do | 5 GHz sweep |
|---|---|---|
| 1 dual-band | Channel 6 most of the time, channel 149 every cycle, one other channel per cycle | ~2.5 min |
| 2 (one on 2.4, one sweeping 5) | The 2.4 radio owns channel 6; the other walks all 21 | **~21 s** |
| 3 | The extra radio takes half of the 5 GHz list | ~11 s |
| 4 | Three-way split of 5 GHz | ~7 s |
| 5 | Four-way split | ~6 s |

Two ACS adapters were measured at about 22 ms per retune, which is the ~21 s row. One radio, and three or more, are estimates only. Those configurations remain untested. The figures assume a ~20 ms retune and a 1 s dwell. The startup log prints the cycle the plugged-in radios actually run.

On paper a third adapter cuts the 5 GHz hole from ~21 s to ~11 s, and keeps sweeping its half while the other radio holds a detection. A fourth still helps. A fifth barely does: the dwell itself is a second. Past two adapters the limit is usually the host — USB current, one shared bus, and Python reading every management frame — not the plan. A Pi 4 or 5 with a powered hub is the machine to try three or four on. That has not been done yet.

A 2.4-only spare is not given 5 GHz work it cannot do. It sweeps the other 2.4 channels while its partner sits on channel 6. Further 2.4-only cards are left idle; that band is already small.

Hearing a drone holds the radio on that channel until it goes quiet (about six seconds of silence on 6 or 149, about three anywhere else). The 5 GHz radio in a pair may hold indefinitely, because its partner never leaves 2.4 uncovered for more than one explore visit. A single radio breaks the hold about every 10 seconds to look at the other social channel and the next explore channel, then goes back.

## Software

- Linux with **systemd** (the included `dump3411.service` requires it; `run-offline.sh` works without).
- **Python 3.10+** (uses PEP 604 `int | None` syntax).
- Tested on Raspberry Pi OS Bookworm with a 2.4 GHz adapter. The AWUS036ACS needs a newer kernel (Raspberry Pi OS Trixie ships its driver; Bookworm usually does not). Any modern systemd distro with Python 3.10+ should work too (Debian 12, Ubuntu 22.04+, Fedora, …).
- Architecture-agnostic — runs on ARMv6, ARMv7, AArch64, and x86_64.

## Configuration

`install.sh` rewrites the unit's `ExecStart` path to the checkout you ran it from. The checked-in template still says `/home/pi/dump3411` and `--wifi-iface auto`, and the feed listens on `0.0.0.0:8754`. Re-run `sudo ./install.sh` after moving the checkout. Do not hand-edit the installed unit to pin `wlan1` — `auto` is what assigns the radios.

`dump3411.py --help` shows the full CLI:

```
--ble-adapter HCI    HCI adapter for BLE scan          (default: hci0)
--wifi-iface IFACE   monitor interface; repeatable, or 'auto' (default: $WIFI_IFACES or wlan1)
--scan-plan PLAN     auto | legacy                     (default: auto)
--channel-dwell SEC  seconds per channel, legacy hop    (default: 0.2)
--channel CH         stay on one WiFi channel           (default: scan plan)
--max-channel N      highest 2.4 GHz channel, 11-13     (default: 11)
--serve HOST:PORT    serve /data/remoteid.json + /      (omit for journal-only)
--ttl SECS           drop drones after N s of silence   (default: 60)
--verbose            log every decoded message type
```

Outside North America, Wi-Fi channels 12 and 13 are legal and a drone's own Wi-Fi (and its beacon Remote ID) can use them. `--max-channel 13` adds them to the 2.4 plan. The adapter can only tune them when the kernel's regulatory domain allows, so set the Wi-Fi country first (`sudo iw reg set FR`, or the WLAN country in `raspi-config`; `iw reg get` shows the current one). If a channel is refused, dump3411 logs a warning and drops it rather than silently staying on 11. 5 GHz channels are gated the same way: whatever `iw` does not advertise is not visited.

`--scan-plan legacy` is the old flat hop of channels 1 to `--max-channel` at `--channel-dwell` seconds, on one radio. A single 2.4-only adapter still does that by default, because it cannot see 5 GHz and the flat hop is what that hardware was tested with. `--channel` pins one named interface and does not combine with `auto`.

## Status dashboard

`http://<host>:8754/` is a single self-contained HTML page (no CDN, no build step). It polls `/status` and `/data/remoteid.json` every 1.5 s and shows:

- Service health pill (ACTIVE / IDLE / OFFLINE)
- Top tiles: uptime, last-beacon age, drones active, total messages, CPU temp
- Per-transport message counters with last-seen timestamps (BLE split into Bluetooth 4 legacy and Bluetooth 5 long-range/extended, so a radio stack that can't receive BT5 is visible at a glance)
- Wi-Fi radios: which adapter is on which plan, the channel it is on now, and how long a full sweep of its list takes
- Table of currently tracked drones — UAS-ID, type, **drone + operator coordinates as Google Maps links**, altitude, AGL, ground speed, track, RSSI, transport (every transport the drone was heard on, most recent first — previously-heard transports dimmed), age
- Unit toggle (per-browser, persists in `localStorage`)

## JSON feed

`GET http://<host>:8754/data/remoteid.json` returns the current tracker snapshot. The wire format is locked by **[FEED.md](./FEED.md)** — see that file if you're integrating a consumer (e.g. [`ha-airspace`](https://github.com/ifnull/ha-airspace), the Home Assistant airspace integration).

Quick check from any LAN host:

```bash
curl -s http://<host>:8754/data/remoteid.json | python3 -m json.tool
```

`GET /status` is also available — operational health (uptime, last beacon, CPU temp, per-source counters, and the running `version`). Useful for Home Assistant binary sensors and uptime monitors.

## MQTT publisher

dump3411 can publish detections to an MQTT broker so consumers react without polling the JSON feed. Optional — only active when `MQTT_BROKER` is set. The Home Assistant steps, including the env file and the entities that appear, are in **Home Assistant connection** above.

Topic layout (under the configured prefix, default `dump3411`):

| Topic | QoS | Retained | Content |
|---|:--:|:--:|---|
| `<prefix>/online` | 1 | yes | `"online"` on connect; LWT publishes `"offline"` if dump3411 dies |
| `<prefix>/status` | 1 | yes | `GET /status` JSON, refreshed every ~5 s |
| `<prefix>/drones/<uas_id>` | 1 | yes | per-drone state (same shape as one row of `drones[]` in the JSON feed), latest-wins, debounced 1 Hz |
| `<prefix>/drones/<uas_id>` | 1 | yes | **empty payload** when a drone TTL-evicts — subscribers see the removal |
| `<prefix>/events/detection` | 0 | no | one publish per decoded message: `{uas_id, rid_source, rssi, t}` (`rid_source` is `ble5` for Bluetooth 5 long-range advertisements) |

Units mirror FEED.md (imperial). The retained per-drone topic means a fresh HA restart immediately sees the current airspace. The `events/detection` stream is for automations that want to react on every beacon.

Quick LAN-side check after configuring:

```bash
mosquitto_sub -h mqtt.lan -t 'dump3411/#' -v
```

## Persistent detection history (optional)

When `--history-db PATH` is configured (or `HISTORY_DB=` in the env file), dump3411 logs every decoded snapshot to a SQLite database. Disabled by default — Pi SD cards are mortal.

```bash
# /etc/dump3411.env
HISTORY_DB=/var/lib/dump3411/history.db
HISTORY_MAX_MB=100           # default 100; rotates oldest rows when over
HISTORY_RETENTION_DAYS=30    # default 30; drops rows older than this
HISTORY_DEBOUNCE_S=1.0       # default 1.0; per-drone min write spacing
HISTORY_RECENT_DAYS=7        # default 7;   dashboard Recent table lookback
```

`sudo mkdir -p /var/lib/dump3411 && sudo chown root:root /var/lib/dump3411` before first start. Restart the service after editing the env file.

When history is enabled:

- Every UAS-ID in the dashboard becomes a clickable link to **`/map?uas_id=<ID>`** — a self-contained Leaflet page showing the operator location (blue marker) and the drone's full track (red polyline with per-point markers, click each for timestamp, altitude, AGL, speed, track, RSSI, transport).
- The dashboard gets a **Recent detections** table below the live Drones section, listing UAS-IDs seen in the configured window (default last 7 days; set `HISTORY_RECENT_DAYS=`) with type, description, first/last seen, message count, and a `● live` badge next to anything currently in the live tracker. Polled on a 30 s cadence — much slower than the 1.5 s live data. Same hyperlink behavior: click a UAS-ID to open its map.
- **`GET /history.json?uas_id=<ID>&since=<epoch>&until=<epoch>`** returns the same data as JSON for one drone. `since`/`until` are optional.
- **`GET /history/recent.json?since=<epoch>&limit=<N>`** lists recently-seen drones (defaults: configured `HISTORY_RECENT_DAYS` window — 7 days out of the box — and 50 most recent). Response includes `window_seconds` and `window_label` for clients that want to render the lookback.
- **`GET /status`** gains a `history_enabled: true` flag plus `history: { rows, drones, size_bytes, earliest_ts, latest_ts }`.

When history is **not** configured, `/map` and `/history.json` return 404 and the dashboard renders UAS-IDs as plain text — no UI surprises.

The map view loads Leaflet from a CDN and OSM tiles from OpenStreetMap (so this *one* page requires internet to render at all — tiles do); the rest of dump3411 stays fully offline.

## Logs

The detector writes no log file of its own; under systemd its output goes to the journal:

```bash
journalctl -u dump3411 -f                            # live tail
journalctl -u dump3411 --since "today"               # by date
journalctl -u dump3411 --since "2026-06-01 09:00"    # from a time
```

Persistence is handled by the journald drop-in installed in the Quickstart.

## Standalone single-radio mode

The per-radio scripts run on their own — handy when debugging one radio in isolation:

```bash
sudo python3 ble_feeder.py  --adapter hci0 --verbose
sudo python3 wifi_feeder.py --iface wlan1  --verbose
```

These do not serve the feed and do not update the shared tracker; only `dump3411.py` does. Use them only for diagnosing one radio's capture path.

## Notes

- Run as root: raw sockets, monitor mode, and Bluetooth all require it.
- Remote ID is only broadcast by drones registered after Sept 2023. Seeing zero detections usually just means nothing compliant is in range.
- The Wi-Fi capture parses every 802.11 management frame in Python. On single-core ARMv6 hosts (Pi Zero W) that's heavy and may drop packets under busy 2.4 GHz — fine for detection, not lossless. More-powerful hosts will keep up easily.
- The CPU-temp tile reads `/sys/class/thermal/thermal_zone0/temp`; on hosts without that file the dashboard shows `–`.

## Changelog

Release notes live in [CHANGELOG.md](./CHANGELOG.md).

## Credit & license

Inspired by the [DroneAware Node feeders](https://droneaware.io/). If you are looking for a user friendly experience, take a look at that project. Released under the [MIT License](./LICENSE).
