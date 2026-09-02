#!/usr/bin/env bash
# Provision the NanoPi NEO3 for onboard-logger.
# Run as root on the board:  sudo ./install.sh
#
# First-time setup only. To ship code changes afterwards use ./deploy.sh from the
# dev host: it syncs the whole repo (not just app/), restarts the service and
# verifies by checksum that the board really matches the working tree.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/onboard-logger
ETC=/etc/onboard-logger
IFACE=wlan0

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

log "apt: installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
# wpasupplicant + dhcpcd-base are the client-mode half of the Wi-Fi switch.
# Formatting a USB stick needs three more packages: exfatprogs and dosfstools
# for mkfs.exfat / mkfs.vfat, and `fdisk` for sfdisk — Ubuntu splits sfdisk out
# of util-linux, so wipefs being present says nothing about it, and a run that
# wipes before discovering that leaves a stick with no partition table at all.
apt-get install -y hostapd dnsmasq wpasupplicant dhcpcd-base \
  python3-venv python3-pip usbutils rfkill iw rsync \
  exfatprogs dosfstools fdisk \
  build-essential git

log "copying app -> ${DEST}"
mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.ndjson' \
  "$SRC"/ "$DEST"/

log "python venv + deps"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --upgrade pip >/dev/null
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"

log "build 5am_util (firmware read/write, denandz/5am_util)"
SRC5AM=/opt/5am_util-src
if [ -d "$SRC5AM/.git" ]; then
  git -C "$SRC5AM" pull --ff-only || true
else
  rm -rf "$SRC5AM"
  git clone --depth 1 https://github.com/denandz/5am_util "$SRC5AM"
fi
make -C "$SRC5AM" clean >/dev/null 2>&1 || true
make -C "$SRC5AM"
mkdir -p "$DEST/bin"
cp "$SRC5AM/5am_util" "$DEST/bin/5am_util"

log "runtime config -> ${ETC} (preserving existing)"
mkdir -p "$ETC" /root/k-line /root/firmware /media
[ -f "$ETC/config.json" ] || cp "$DEST/config/config.default.json" "$ETC/config.json"
[ -f "$ETC/params.json" ] || cp "$DEST/config/params.json" "$ETC/params.json"
[ -f "$ETC/ecu_id.json" ] || cp "$DEST/config/ecu_id.json" "$ETC/ecu_id.json"

log "udev rule -> /dev/kline"
cp "$DEST/udev/99-kline.rules" /etc/udev/rules.d/99-kline.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true

log "NetworkManager: leave ${IFACE} unmanaged"
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/onboard-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:${IFACE}
EOF
nmcli device set "$IFACE" managed no 2>/dev/null || true
systemctl reload NetworkManager 2>/dev/null || true

log "radio: unblock + regulatory domain"
rfkill unblock wlan || true
COUNTRY="$(python3 -c "import json;print(json.load(open('$ETC/config.json'))['wifi']['country'].upper())" 2>/dev/null || echo DE)"
iw reg set "$COUNTRY" || true

log "hostapd/dnsmasq: managed by our service (not auto-started)"
systemctl unmask hostapd 2>/dev/null || true
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' >/etc/default/hostapd
# Our app brings hostapd/dnsmasq up at startup, so disable their own autostart
# to avoid boot-order failures (they would start before the conf is rendered).
systemctl disable hostapd dnsmasq 2>/dev/null || true

log "systemd unit"
cp "$DEST/systemd/onboard-logger.service" /etc/systemd/system/onboard-logger.service
systemctl daemon-reload
systemctl enable onboard-logger.service
systemctl restart onboard-logger.service

AP_IP="$(python3 -c "import json;print(json.load(open('$ETC/config.json'))['network']['ap_ip'])" 2>/dev/null || echo 192.168.5.1)"
log "done. Web UI: http://${AP_IP}/  (also on eth0 for dev)"
log "check:  systemctl status onboard-logger  ·  journalctl -u onboard-logger -f"
