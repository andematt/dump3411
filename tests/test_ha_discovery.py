"""Home Assistant discovery documents. No broker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mqtt_publisher as m


def test_discovery_topics_match_the_live_entities():
    configs = dict(m.discovery_configs("dump3411", "homeassistant", hostname="droneaware"))
    assert set(configs) == {
        "homeassistant/binary_sensor/dump3411/online/config",
        "homeassistant/sensor/dump3411/drones_active/config",
        "homeassistant/sensor/dump3411/messages/config",
        "homeassistant/sensor/dump3411/last_detection/config",
    }
    assert configs["homeassistant/sensor/dump3411/messages/config"]["unique_id"] == \
        "dump3411_messages_total"
    assert configs["homeassistant/sensor/dump3411/messages/config"]["value_template"] == \
        "{{ value_json.messages_total }}"
    online = configs["homeassistant/binary_sensor/dump3411/online/config"]
    assert online["device"]["identifiers"] == ["dump3411-droneaware"]
    assert online["state_topic"] == "dump3411/online"
    last = configs["homeassistant/sensor/dump3411/last_detection/config"]
    assert last["state_topic"] == "dump3411/events/detection"
    assert "json_attributes_topic" in last


def test_discovery_can_be_disabled(monkeypatch=None):
    # The runner below is not pytest, so set the env directly and restore it.
    old = os_environ_set("MQTT_DISCOVERY", "off")
    try:
        assert m.discovery_enabled() is False
    finally:
        os_environ_set("MQTT_DISCOVERY", old)


def os_environ_set(key, value):
    import os
    previous = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    return previous


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all discovery tests passed")
