#!/usr/bin/env bash
# Provision a Raspberry Pi-compatible board for onboard-logger.
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

log "udev rules -> /dev/kline, USB power, wlan0 AP recovery"
cp "$DEST/udev/99-kline.rules"             /etc/udev/rules.d/99-kline.rules
cp "$DEST/udev/70-usb-keep-powered.rules"  /etc/udev/rules.d/70-usb-keep-powered.rules
cp "$DEST/udev/71-wlan-hostapd-restart.rules" /etc/udev/rules.d/71-wlan-hostapd-restart.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true
udevadm trigger --subsystem-match=usb --action=add || true

# The dwc2 root hub is already up by the time udev's rules land, so the trigger
# above is what actually stops it deadlocking on this boot too.
for c in /sys/bus/usb/devices/usb*/power/control; do echo on >"$c" 2>/dev/null || true; done

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

# ...but ConfigManager re-enables hostapd whenever the AP mode is applied, so
# the unit does come back. The drop-in is what keeps that harmless: no wlan0,
# no unit — instead of a 30 s wait-for-wlan0 inside multi-user.target and an
# unbounded restart loop behind it. See systemd/hostapd-override.conf.
log "hostapd drop-in + wlan0 AP recovery"
mkdir -p /etc/systemd/system/hostapd.service.d
cp "$DEST/systemd/hostapd-override.conf" /etc/systemd/system/hostapd.service.d/override.conf
cp "$DEST/systemd/wlan0-ap-recover.service" /etc/systemd/system/wlan0-ap-recover.service
install -m 0755 "$DEST/systemd/wlan0-ap-recover.sh" /usr/local/sbin/wlan0-ap-recover.sh

log "SD card: one copy of the log, not four"
# The journal's cap comes down — the stock 256 MB is a quarter of a gigabyte of
# rotation the board has no use for.
mkdir -p /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/onboard-logger.conf <<'EOF'
[Journal]
SystemMaxUse=64M
RuntimeMaxUse=32M
Compress=yes
SyncIntervalSec=5m
EOF

# rsyslog stays, but only for our own events. The stock 50-default.conf routes
# *.* into /var/log/syslog — every line written to the card a second time. What
# that duplication was actually protecting is narrower than it looks: DiagLog
# mirrors its link events to journald, and a plain-text copy of *those* survived
# a power cut that left the binary journal unreadable. So keep the plain-text
# copy, drop the firehose.
cp "$DEST/rsyslog/10-onboard-logger.conf" /etc/rsyslog.d/10-onboard-logger.conf
cp "$DEST/rsyslog/logrotate-onboard-logger" /etc/logrotate.d/onboard-logger
sed -i 's|^\(\*\.\*;auth,authpriv\.none.*\)$|#\1  # onboard-logger: see 10-onboard-logger.conf|' \
    /etc/rsyslog.d/50-default.conf 2>/dev/null || true
systemctl restart rsyslog 2>/dev/null || true
systemctl restart systemd-journald 2>/dev/null || true

log "page cache: shorten the window a cut ignition can lose"
# The bike cuts power at the ignition switch — a clean shutdown is the exception,
# not the rule — and the stock 30 s writeback delay is 30 s of ride log sitting
# in RAM. The extra write traffic this costs is far below what disabling rsyslog
# just saved.
cat >/etc/sysctl.d/90-onboard-logger.conf <<'EOF'
vm.dirty_expire_centisecs = 500
vm.dirty_writeback_centisecs = 250
EOF
sysctl -q --system 2>/dev/null || true

log "trimming the stock image"
# None of these have anything to do with a K-Line logger, and between them they
# cost about ten seconds of every boot. apt's timers matter twice over: the bike
# has no internet, so they can only fail, and they fail slowly.
systemctl set-default multi-user.target 2>/dev/null || true
systemctl disable --now ModemManager bluetooth brcm_patchram_plus \
                        NetworkManager-wait-online motd-news 2>/dev/null || true
systemctl disable apt-daily.timer apt-daily-upgrade.timer motd-news.timer \
                  dpkg-db-backup.timer ua-timer.timer 2>/dev/null || true
systemctl mask hdmi-on.service 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true
# ifupdown alongside NetworkManager is a second network stack racing the first
# every time the Wi-Fi mode changes — but only disable it when it is not the one
# holding the wired link this board is reached over.
if ! grep -qE '^\s*(auto|iface)\s+e' /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null; then
  systemctl disable networking 2>/dev/null || true
fi

log "systemd unit"
cp "$DEST/systemd/onboard-logger.service" /etc/systemd/system/onboard-logger.service
systemctl daemon-reload
systemctl enable onboard-logger.service
systemctl restart onboard-logger.service

AP_IP="$(python3 -c "import json;print(json.load(open('$ETC/config.json'))['network']['ap_ip'])" 2>/dev/null || echo 192.168.5.1)"
log "done. Web UI: http://${AP_IP}/  (also on eth0 for dev)"
log "check:  systemctl status onboard-logger  ·  journalctl -u onboard-logger -f"
