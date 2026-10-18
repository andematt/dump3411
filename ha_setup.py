#!/usr/bin/env python3
"""Publish Home Assistant MQTT discovery for dump3411.

Catch-all for the one-time broker side of setup, and for putting the
discovery configs back after a broker loses its retained messages. The
running service does the same publish on every MQTT connect, so this is
only required when you want it done before the next restart, or from
another machine that has the env file.

It does not create the broker user or write /etc/dump3411.env. Those
stay outside the repo because they hold a password.

Usage:
    sudo python3 ha_setup.py
    sudo python3 ha_setup.py --env-file /etc/dump3411.env
    python3 ha_setup.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Fill os.environ from a KEY=VALUE file. Existing variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish dump3411 Home Assistant MQTT discovery configs",
    )
    parser.add_argument(
        "--env-file", default="/etc/dump3411.env",
        help="KEY=VALUE file to load (default: /etc/dump3411.env). "
             "Variables already set in the environment are kept.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the discovery topics and payloads. Do not connect.",
    )
    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    from mqtt_publisher import discovery_configs, discovery_enabled

    prefix = os.environ.get("MQTT_TOPIC_PREFIX", "dump3411").strip("/") or "dump3411"
    discovery_prefix = (
        os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/")
        or "homeassistant"
    )
    configs = discovery_configs(prefix, discovery_prefix)

    if not discovery_enabled():
        print("MQTT_DISCOVERY is off — nothing to publish.", file=sys.stderr)
        sys.exit(0)

    print(f"prefix:    {prefix}")
    print(f"discovery: {discovery_prefix}")
    for topic, body in configs:
        print(f"  {topic}")
        if args.dry_run:
            print(f"    {json.dumps(body, separators=(',', ':'))}")

    if args.dry_run:
        return

    broker = os.environ.get("MQTT_BROKER", "").strip()
    if not broker:
        print(
            "MQTT_BROKER is not set. Write /etc/dump3411.env first "
            "(MQTT_BROKER, MQTT_USER, MQTT_PASSWORD), then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    from mqtt_publisher import MqttPublisher, _make_client

    host, port = MqttPublisher._parse_broker(broker)
    client = _make_client(f"dump3411-ha-setup-{os.uname().nodename}")
    user = os.environ.get("MQTT_USER") or None
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASSWORD") or "")
    client.connect(host, port, keepalive=30)
    client.loop_start()
    import time
    time.sleep(0.5)
    for topic, body in configs:
        info = client.publish(
            topic, json.dumps(body, separators=(",", ":")), qos=1, retain=True,
        )
        info.wait_for_publish(5)
    client.loop_stop()
    client.disconnect()
    print("discovery published (retained). Does not touch dump3411/online. "
          "The service republishes the same configs every time it connects.")


if __name__ == "__main__":
    main()
