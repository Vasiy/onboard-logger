#!/bin/sh
# Re-apply the onboard-logger AP when wlan0 (re)appears after USB re-enumeration.
# Started by /etc/udev/rules.d/71-wlan-hostapd-restart.rules via
# wlan0-ap-recover.service; install.sh puts it at /usr/local/sbin/.
#
# The address is read from the live config rather than mirrored here: this
# script used to carry a hardcoded copy of network.ap_ip, which is exactly the
# kind of duplicate that drifts the first time the address is changed in the UI.
ETC=/etc/onboard-logger/config.json
AP_IP=$(python3 -c "import json;print(json.load(open('$ETC'))['network']['ap_ip'])" 2>/dev/null || echo 192.168.5.1)
PREFIX=$(python3 -c "import json;print(json.load(open('$ETC'))['network']['prefix'])" 2>/dev/null || echo 24)

# wait for wlan0 (udev fires on 'add', so usually immediate)
for i in $(seq 1 15); do ip link show wlan0 >/dev/null 2>&1 && break; sleep 1; done
ip link show wlan0 >/dev/null 2>&1 || exit 0

ip addr replace "$AP_IP/$PREFIX" dev wlan0
ip link set wlan0 up
systemctl restart hostapd
systemctl restart dnsmasq
logger -t wlan0-ap-recover "reapplied $AP_IP/$PREFIX + hostapd + dnsmasq"
