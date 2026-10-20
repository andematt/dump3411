# Handoff: persist the full Remote ID record

Next session picks up here. Do not reopen the flight-boundary debate. Do not treat altitude trend as a non-starter.

## What the operator wants

Store every field we already decode, plus the fields we currently throw away, and send most of that over MQTT. The use case is a record complete enough to attach to a sheriff's report: the track, the altitude series, the operator position, and the packet that produced the row.

A climb, a level stretch, and a descent is a valid way to read a flight from that series. Signal loss stays the flight boundary. Remote ID does not give a reliable landed event, and this task does not add a landing classifier. Sixty seconds of silence (`--ttl`, default 60) is the end of a flight. The altitude series is what shows the shape inside that window.

Raw frames stay on disk. Decoded fields go to SQLite and to MQTT. "Most" means the decoded report fields, not a hex dump on every broker message.

## Where things are

- Fork: `https://github.com/andematt/dump3411` branch `main`. Local `main` tracks `upstream` (`ifnull/dump3411`) and is ahead of it. Push to `origin`.
- Pi: `matt@droneaware` (`192.168.1.41`). Passwordless sudo works. Dashboard `http://192.168.1.41:8754/`.
- The Pi is **not** a git checkout. The running tree is the tarball at `~/src/dump3411-feat-dual-adapter-scan`. After the code change, copy the changed files there and `sudo systemctl restart dump3411`, or replace that tree with a clone of `origin/main`. Do not overwrite `/etc/dump3411.env`.
- History is already on: `HISTORY_DB=/var/lib/dump3411/history.db`, 100 MB cap, 30 day retention, 1 s debounce, 7 day recent window. The file is on the SD card on purpose. Do not move it. Rows so far start at the enable time; the flight before that is gone.
- MQTT broker is Home Assistant Mosquitto at `192.168.1.9:1883`, user `dump3411`, prefix `dump3411`. Password is only in `/etc/dump3411.env`. Do not copy it into the repo, the handoff, or chat.
- Home Assistant entities already exist from retained discovery: `binary_sensor.dump3411_online`, `sensor.dump3411_drones_active`, `sensor.dump3411_messages`, `sensor.dump3411_last_detection`. Discovery is republished on MQTT connect (`mqtt_publisher.discovery_configs`). Do not break those unique ids.

## Already stored

SQLite `detections` (schema v1, `history.py`) and, when the tracker has them, the live row:

`ts`, `uas_id`, `id_type`, `ua_type`, `lat`, `lon`, `alt_geom_ft`, `agl_ft`, `gs`, `track`, `geom_rate`, `rssi`, `rid_source`, `self_id`, `op_lat`, `op_lon`, `operator_id`.

The live JSON/MQTT drone row also has `operator.location_type`, `rid_sources`, `seen`, `seen_pos`, `message_count`. History does not.

`dump3411/drones/<id>` is that live row, retained, latest-wins, empty payload on TTL expire. `dump3411/events/detection` is only `{uas_id, rid_source, rssi, t}` and is not retained.

## Must be stored, and sent on MQTT

Decoded today and then dropped, or never decoded. Wi-Fi parser comments some of these; BLE has its own copy of the parsers and must change too (`ble_feeder.py` and `wifi_feeder.py`). A shared decoder is already noted in `TODO.md`. Do it if it is the smaller way to keep the two paths from drifting. Do not leave BLE behind.

Location message (`0x1`), ASTM F3411, 25 bytes:

- Operational status, byte 1 bits 4-7: undeclared, ground, airborne, emergency, remote ID system failure. Wi-Fi `parse_location` documents the bits and does not read them.
- Pressure altitude, bytes 13-14, same encoding as geodetic (`uint16 × 0.5 − 1000`, unknown = −1000). Not extracted.
- Height type, byte 1 bit 2. Wi-Fi puts `height_type` (`AGL` or `Above Takeoff`) on the parse dict. `Tracker.update_location` does not take it, so `agl_ft` is stored with no label.
- Horizontal, vertical, baro, and speed accuracy, and the aircraft timestamp. Byte 19 onward. Not decoded.
- Vertical speed is already kept as `geom_rate`. Keep it.

Identity and context that the report needs and history does not have:

- Transmitter MAC.
- Wi-Fi channel, when the frame is Wi-Fi. The feeder already resolves it for the log line and the camp decision. It never reaches the tracker.
- Operator location type (`takeoff`, `live_gnss`, `fixed`). On the live row, not in SQLite.
- Raw frame bytes, SQLite only. MQTT gets the decoded fields, not the hex, unless a size check shows a typical packet is cheap enough to include. Default is disk only.

## How to ship it

- Additive columns on `detections`. Bump `SCHEMA_VERSION` and migrate an existing `/var/lib/dump3411/history.db`. Do not require a wipe. The Pi database is the one that matters.
- JSON feed and `dump3411/drones/<id>` gain the same decoded fields. Project rule: additive feed fields do not bump `schema_version`. Update `FEED.md`.
- `events/detection` should carry the position and altitude fields too, so a subscriber does not have to race the retained topic to see the point. Keep it not retained.
- Do not wait until expire to publish. The live stream stays. Expire still clears `dump3411/drones/<id>`. History rows stay, which is already the point of `history.py`.
- Debounce stays at 1 s unless a report field would be lost by latest-wins inside that second. Operational status and the altitude sample must be the values from the same moment as lat/lon, not a mix of an old fix and a new status.
- Tests: a location fixture that sets status, pressure altitude, height type, accuracies, and timestamp, asserted through the tracker row and the history insert. Cover BLE and Wi-Fi, or the shared decoder once. Existing `tests/test_frdid.py` and `tests/test_scan_plan.py` must still pass.
- Deploy to the Pi and confirm a row written after restart contains the new columns. Do not print `MQTT_PASSWORD`.

## Out of scope

- A landing classifier, including one based on altitude trend. Store the series. Do not infer touchdown in this change.
- Rebuilding the Home Assistant map. The stock map card draws entity history, not a coordinate array. That discussion is settled until this persist work lands.
- Moving the database off the SD card.
- C5 / ESP32 monitor nodes.
