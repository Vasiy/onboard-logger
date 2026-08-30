# Onboard Logger

Standalone on-board **K-Line diagnostic logger** for a motorcycle with a **Magneti Marelli
IAW 5AM (HW610)** ECU, running on a **NanoPi NEO3** (Ubuntu 24.04). On power-up the board brings
up a Wi-Fi access point with a mobile-first web UI, connects to the ECU diagnostic bus, shows live
parameters and records to `/root/k-line/`.

🔧 [Protocol reference](docs/PROTOCOL.md) · 🔍 [What the PC tools gave us](docs/REVERSE.md)

## Features

- **Wi-Fi, two modes** (exclusive, switched in Config):
  - *Access point* `nano loger` (hostapd, WPA2) + DHCP server — what you connect to on the bike.
    Auto-picks the least congested 2.4 GHz channel; `Auto`/1–13 selector + occupancy chart. The
    router address is optional and empty by default: the AP routes nowhere, so clients are told
    there is no default route.
  - *Client* — joins an existing network (wpa_supplicant), address by DHCP or set by hand. No DHCP
    server is started. If the join does not come up in 45 s the board **restores its own AP** and
    reverts the saved mode, so a wrong password can never leave it off the air.
- Web server (FastAPI) on port 80, reachable over Wi-Fi and eth0.
- **Auto-connect** to the IAW 5AM over K-Line — KWP2000/ISO 14230, 10400 8N1, **fast or 5-baud slow
  init**. Green `status_led` steady on a live link, blinking while searching.
- **Live parameters** read per-channel (`0x21 <rli>`); a **verified 5AM map** (RPM, coolant/air
  temp, throttle, injection, battery, lambda F/R, fuel trim F/R, CO/duty) plus every other readable
  `rli` as raw. Only **selected** params are polled → fewer selected = higher poll rate.
- **Two log streams** (independent toggles): decoded parameter CSV + raw frame log; auto-start on
  ECU connect, never logs before the link is up, auto-zip on close.
- **Full parameter scan** — sweep `0x00..0xFF` (addressed / both framings), auto-stop timer with a
  first-pass progress readout; writes `scan-*.ndjson` + a wide `scan-*.csv`. The tool to map new rli.
  Every probe carries its own timestamp, and **event markers** (side stand / clutch / gear / kill
  switch / free text, optionally delayed so you can reach the switch) go into the log — flip a
  switch, mark it, and the rli that moves around the marker is that status channel.
- **Web UI** tabs:
  - *Logger* — decoded/raw/scan toggles, live values + 3 s sparklines, poll rate, named channels
    first with unidentified rli collapsed. A green banner shows the connected ECU identity. A
    **read / pick** switch flips the list between big tiles of the picked channels (what you read
    from a handlebar mount) and the checkbox list; the choice is remembered per device.
  - *Firmware* — read/write the ECU image via [`denandz/5am_util`](https://github.com/denandz/5am_util);
    the Read block shows the live ECU description (saved into a GuzziDiag-style `.txt` on read);
    upload/download/diff/delete `.bin` (+`.txt`), exact-size (327680 B) write gate.
  - *K-Line logs* — checkbox list, download/delete, and a **Preview** that plots normalized decoded
    CSV (canvas, wheel-zoom / drag-pan, series toggles, value hover).
  - *Testing* — **read/clear fault codes** (SAE decode, localized descriptions, *stored vs current*
    and fault kind from the status byte), **adaptation resets** (TPS `31 21` with a throttle bar
    between the buttons, self-adaptation `30 7E 04`), **actuator tests** (`30 <id> 07/00`, pulse
    length settable 0.5–30 s, Stop button, timing owned by the board so an output is released even
    if the browser goes away), an **ECU profile reference** (65 Guzzi models → ECU → protocol) and
    the status-bitfield tables. The diagnostic session the PC tools use (`83 03 00 FF 00 FF 00` +
    `10 81`) is armed lazily on the first test — the logging path stays exactly as it was.
  - *Config* — Wi-Fi mode + credentials, Network (hostname, addresses, DHCP server or DHCP client),
    bus speed (Auto/fixed), init (fast/slow), language, and the **clock**: the board has no
    battery-backed RTC, so the first browser to open the UI after a power-up sets date, time and
    timezone (once per power-up, only if the clocks differ by more than 2 s; switch it off with a
    checkbox) plus a manual *Sync from this device* button, and **appearance** (auto / light / dark,
    a per-device choice, not board config). Non-network settings save themselves on change; network
    ones wait for *Save & apply*, with a count of pending network fields that follows the scroll.
- **8 locales** (En default, De, Es, Fr, It, Nl, Bg, Ru).
- **Field details**: a dropped Wi-Fi link shows itself (values go muted and the status says the link
  is lost, so a frozen snapshot never passes for a live one), destructive actions ask in an in-page
  dialog whose button carries the verb of the action, marker and actuator buttons are sized for
  gloves, portrait and landscape are laid out separately, and the UI installs to the Home screen as
  a standalone app.

## Hardware

| Part | What |
|---|---|
| Board | NanoPi NEO3 (RK3328), Ubuntu 24.04, kernel 6.6 |
| Wi-Fi | USB dongle Ralink RT5370 (`rt2800usb`, AP mode) |
| K-Line | USB KKL/FTDI cable (FT232R) → `/dev/kline`, Fiat 3-pin connector |
| LED | `/sys/class/leds/status_led` |

K-Line = pin 16 OBD2. On the ECU connector: 12 V = pins 4 & 17, GND = pins 10 & 34, K-Line = pin 16.
The cable contains an L9637D-class transceiver — the FTDI TXD is not wired to K-Line directly.

## Install (on the board)

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

`install.sh` installs `hostapd dnsmasq python3-venv iw rfkill`, deploys the app to
`/opt/onboard-logger` (venv), adds the `/dev/kline` udev rule, takes `wlan0` out of NetworkManager,
copies config to `/etc/onboard-logger/`, creates `/root/k-line` and enables `onboard-logger.service`.

Web UI: `http://192.168.5.1/` (also over eth0 for debugging). Check with
`systemctl status onboard-logger` / `journalctl -u onboard-logger -f`.

## Configuration

`config/config.default.json` holds the defaults; the live copy is `/etc/onboard-logger/config.json`
(the two are merged, so new keys appear after an update). Most of it is editable in the Config tab —
these keys are worth knowing:

| key | meaning |
|---|---|
| `wifi.mode` | `ap` (serve a network) or `client` (join one) |
| `wifi.ssid` / `password` / `channel` / `auto_channel` / `country` | the board's own AP |
| `wifi.client.ssid` / `password` | the network to join |
| `wifi.client.ipv4` | `dhcp` or `static` |
| `wifi.client.ip` / `prefix` / `gateway` / `dns` | manual address (`dns` is config-only, not in the UI) |
| `network.ap_ip` / `prefix` | AP address; `network.gateway` is optional, empty = clients get no default route |
| `dhcp.*` | DHCP **server**, AP mode only |
| `kline.port` / `baud` / `echo` / `init` | K-Line bus (`baud: "auto"` tries 10400/9600/15625/19200) |
| `testing.pulse_ms` | actuator pulse length, 500…30000 |
| `testing.session_init` | set `false` to stop arming the `83` + `10 81` diagnostic session before tests |
| `system.auto_time_sync` | take the clock from the first web client after a power-up (default `true`) |
| `log_dir` / `firmware_dir` / `firmware_size` / `locale` | paths, flash size gate, UI language |

## Protocol (summary)

- Frame `[0x80|len] [0x10] [0xF1] [DATA…] [CS]`, `CS = Σ & 0xFF`; short frame `[len] [DATA…] [CS]`.
- Init: fast (BREAK 25/25 ms → `81`) or 5-baud slow (address `0x33` → `55 KW1 KW2` handshake).
- Live data per-parameter: `21 <rli>` → `61 <rli> <value>` (value at offset 2, big-endian);
  keep-alive `3E` every 3 s, `82` on exit. No `10 85`/`1A` for live.
- Adaptation resets (5AM): **TPS** `31 21`, **self-adaptation** `30 7E 04`. Actuators: `30 <id> 07/00`.
- Test/adaptation services live in the session both PC tools start: `81` → `83 03 00 FF 00 FF 00`
  → `10 81` (session **0x81**, not 0x85), closed with `20` + `82`. There is no separate "test mode".

Full details, the complete parameter/actuator/DTC tables and the reverse-engineering sources are in
**[docs/PROTOCOL.md](docs/PROTOCOL.md)**. `config/params.json` holds the live map (per-parameter);
edit it to name/scale new rli discovered from a scan — no code change needed.

## Development / tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
for f in tests/*.py; do python "$f"; done      # offline tests (no hardware, no radio)
uvicorn app.main:app --reload --port 8000     # local UI run (no AP/K-Line)
```

On a dev host without sysfs/systemctl/K-Line, privileged actions degrade to no-ops — the app never
crashes.

## Structure

```
app/kline/     transport (serial + init + framing), kwp2000 (session/services), params, logger (worker), ecu_id
app/web/       state, config_mgr (network), firmware, led, system, wifi_scan
app/static/    SPA (index.html, app.js, style.css, i18n.js)
app/templates/ hostapd / dnsmasq / wpa_supplicant config templates
config/        config.default.json, params.json, ecu_id.json, actuators.json, status_maps.json,
               profiles.json (bike -> ECU -> protocol presets), dtc/
docs/          PROTOCOL.md (protocol reference), REVERSE.md (what the PC tools gave us) + .ru.md
systemd/ udev/ install.sh
```
