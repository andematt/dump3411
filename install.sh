#!/usr/bin/env bash
#
# dump3411 install helper.
#
# Checks that a USB Wi-Fi adapter is present, rewrites the systemd unit's
# ExecStart path to point at this checkout, installs the apt dependencies,
# copies the unit into /etc/systemd/system, and enables + starts the service.
# The unit uses --wifi-iface auto, so a second adapter is picked up on restart
# without editing the unit.
#
# Idempotent — re-run safely after a `git pull` to roll out config changes.
#
# Usage:
#   sudo ./install.sh

set -euo pipefail


# --- preflight ---------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root (apt + systemctl)." >&2
    echo "Try: sudo ./install.sh" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — dump3411's unit needs systemd." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/dump3411.service"
SERVICE_DEST="/etc/systemd/system/dump3411.service"
SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "dump3411.service not found at $SERVICE_SRC" >&2
    echo "Run this from the dump3411 repo checkout." >&2
    exit 1
fi

echo "=== dump3411 install ==="
echo "Repo path : $SCRIPT_DIR"
echo


# --- 1) apt dependencies -----------------------------------------------------

echo "[1/5] Installing apt dependencies (python3-bleak, iw, rfkill, bluez)..."
DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    python3-bleak iw rfkill bluez >/dev/null

echo "      Optional: python3-paho-mqtt for the MQTT publisher."
read -rp "      Install paho-mqtt now? [y/N]: " ans
if [[ "${ans,,}" == "y" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-paho-mqtt >/dev/null
    INSTALLED_MQTT=1
else
    INSTALLED_MQTT=0
fi
echo


# --- 2) confirm a USB Wi-Fi adapter is present --------------------------------

echo "[2/5] Checking for a USB Wi-Fi adapter..."
# wlan0 is usually the Pi's built-in radio, which cannot do monitor mode and
# is the management link. Runtime discovery (--wifi-iface auto) makes the
# same call from the driver and the default route, so this is only a
# presence check.
mapfile -t IFACES < <(
    ip -o link show 2>/dev/null \
        | awk -F': ' '{print $2}' \
        | awk -F'@' '{print $1}' \
        | grep -E '^(wlan|wlx)' \
        | grep -v '^wlan0$' \
    || true
)

if [[ ${#IFACES[@]} -eq 0 ]]; then
    echo "      No USB Wi-Fi interface found (looked for wlan*/wlx* except wlan0)." >&2
    echo "      Plug in a monitor-mode adapter and re-run." >&2
    exit 1
fi

echo "      Found: ${IFACES[*]}"
echo "      The service uses --wifi-iface auto: every USB monitor adapter"
echo "      except the one carrying your network connection."
if [[ ${#IFACES[@]} -ge 2 ]]; then
    echo "      ${#IFACES[@]} adapters: one covers 2.4 GHz, the rest split 5 GHz."
else
    echo "      One adapter. A second dual-band adapter cuts the 5 GHz sweep"
    echo "      from about 2.5 minutes to about 21 seconds. Plug it in and restart."
fi
echo


# --- 3) rewrite the unit -----------------------------------------------------

echo "[3/5] Generating systemd unit..."
# Substitute the ExecStart path from the checked-in template. The pristine
# template lives in $SERVICE_SRC, so this is safe to re-run without ever
# modifying the repo copy. --wifi-iface auto stays as written.
sed \
    -e "s|/home/pi/dump3411/dump3411.py|$SCRIPT_DIR/dump3411.py|g" \
    "$SERVICE_SRC" > "$SERVICE_TMP"
echo "      ExecStart -> $SCRIPT_DIR/dump3411.py"
echo "      Interface -> auto"
echo


# --- 4) install + reload -----------------------------------------------------

echo "[4/5] Installing unit and reloading systemd..."
install -m 644 "$SERVICE_TMP" "$SERVICE_DEST"
systemctl daemon-reload
echo


# --- 5) enable + start -------------------------------------------------------

echo "[5/5] Enabling and starting dump3411..."
systemctl enable --now dump3411 >/dev/null
sleep 2

echo
echo "=== status ==="
systemctl --no-pager status dump3411 | head -8 || true
echo

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<host>")"
echo "Dashboard : http://${LAN_IP}:8754/"
echo "JSON feed : http://${LAN_IP}:8754/data/remoteid.json"
echo "Live tail : sudo journalctl -u dump3411 -f"
echo

if [[ "$INSTALLED_MQTT" == "1" ]]; then
    echo "MQTT is installed — to enable publishing, create /etc/dump3411.env with:"
    echo "    MQTT_BROKER=mqtt.lan:1883"
    echo "    MQTT_USER=dump3411"
    echo "    MQTT_PASSWORD=…"
    echo "Then: sudo systemctl restart dump3411"
    echo
fi

echo "To enable persistent detection history, add to /etc/dump3411.env:"
echo "    HISTORY_DB=/var/lib/dump3411/history.db"
echo "Then: sudo mkdir -p /var/lib/dump3411 && sudo systemctl restart dump3411"
echo
echo "Done."
