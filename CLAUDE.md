# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

`HANDOFF.md` (Russian) is the living project-state document: what is verified on the real ECU, what
is deployed but untested, the reverse-engineering findings behind every command byte, and the board's
SSH/deploy details. Read it before touching protocol code, and update it when hardware facts change.
`docs/PROTOCOL.md` is the stable protocol reference; `docs/REVERSE.md` records what the PC tools
(GuzziDiag / IAWDiag / JPDiag) gave us and what could not be recovered.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# offline test suite — plain scripts, no pytest runner needed, no hardware/radio (153 tests)
# test_ui_*.py shell out to tests/ui_*.js (app.js in a Node vm); they skip without node
for f in tests/*.py; do .venv/bin/python "$f"; done
.venv/bin/python tests/test_kline.py          # a single file (prints "ok <name>" per test)

.venv/bin/python -c "import app.main"                                        # import smoke test
node -e "new Function(require('fs').readFileSync('app/static/app.js','utf8'))"  # JS syntax check
.venv/bin/uvicorn app.main:app --reload --port 8000                          # local UI, no AP/K-Line
```

i18n parity is a hard invariant — all 8 locales must have identical key sets (currently 324 keys ×8):

```bash
node -e "const fs=require('fs');global.window={};eval(fs.readFileSync('app/static/i18n.js','utf8'));
const L=window.I18N,en=Object.keys(L.en);
for(const k of Object.keys(L))console.log(k,Object.keys(L[k]).length,'missing:',en.filter(x=>!(x in L[k])).length)"
```

Deploy is `./deploy.sh` from the dev host. **Always deploy after changing anything the board
runs** — the board is not a git checkout, it only ever holds what was last rsynced, so an unsynced
change means the bike runs old code. The script syncs the **whole repo**, not just `app/`
(`config/fw_layout.json`, `config/fw_catalog.json` and `config/dtc/` live outside it, and an
app/-only deploy left the board on stale data more than once), restarts the service, then verifies
by checksum in both directions and fails if anything still differs. It runs the offline suite first;
`--no-tests` skips that, `BOARD_HOST`/`PY` override the target and the interpreter. `install.sh` is
first-time provisioning on the board itself (packages, venv, builds `5am_util`, udev, systemd).

## Architecture

**One thread owns the serial port.** `KLineWorker` (`app/kline/logger.py`) is the only code that
talks to `/dev/kline`. HTTP handlers never open the port: Testing-tab commands are queued with
`worker.run_command(name, arg)` and executed by `_exec_command()` inside the worker's poll loop with
the live `KWP2000Session`. The worker also owns actuator timing (`_act_on` arms a deadline, the poll
loop's `_act_tick` releases the output), so an energized actuator switches off even if the browser
disappears. Scanning and logging are mutually exclusive by design.

**State is a single snapshot.** `app/web/state.py` holds everything the UI shows; `/api/state` and
the `/ws` push (every 0.2 s) both serve `state.snapshot()`. Add a field there and the UI sees it
without a new endpoint.

**Config is layered and dev-host tolerant.** `ConfigManager` deep-merges
`/etc/onboard-logger/config.json` over `config/config.default.json`, so new default keys appear
after an update. `_cfg_file()` in `main.py` does the same fallback for `params.json`, `ecu_id.json`,
`actuators.json`, `status_maps.json`, `profiles.json` — `/etc` wins, repo `config/` is the fallback
used on a dev machine. Every privileged action (`systemctl`, `ip`, sysfs LED, `timedatectl`,
`hostnamectl`) degrades to a captured message instead of raising; tests rely on this by replacing
`_run` with a recorder, so keep new shell-outs behind those helpers.

**Network changes are diffed, not reapplied.** `plan()` compares old vs new config and restarts only
what changed; a language change must never drop a live Wi-Fi link (`tests/test_config_net.py`
guards this). Client mode has a 45 s watchdog — if the join does not come up, `ConfigManager` puts
the AP back and `_after_network()` rewrites `wifi.mode="ap"` in config.json so a reboot stays
reachable. **`apply_network` must never be awaited inline on the event loop**: hostapd's pre-start
waits up to 30 s for `wlan0`, which previously made port 80 unreachable for the whole window — it
runs via `asyncio.to_thread` in a startup task.

**Config-tab save semantics:** non-network fields autosave on change; `wifi.*`, `network.*`,
`dhcp.*`, `hostname` are marked dirty and only go out on *Save & apply* (`NET_FIELD` in `app.js`).
Settings that must not trigger `apply_network` get their own endpoint (see
`/api/testing/settings`, `/api/system/time`).

**Firmware images identify themselves.** `config/fw_layout.json` says where the calibration code
sits inside a `.bin` (Map Name 1 @0x47FA4 + Map Name 2 @0x48006 → `23ECCLGPSMD`), `app/kline/fw_ident.py`
reads *only* that ~176-byte window — `/api/firmware` polls every 1.5 s, so nothing may pull 320 KB per
file. `config/fw_catalog.json` maps a code prefix to a bike; unlike every other config asset it is
**layered, not replaced** (`fw_catalog.load_catalog`), because `_cfg_file()` semantics would erase the
seeded entries the first time the UI names a bike. The write guard (`app/web/fw_guard.py`) compares
catalog-resolved brand+model, never the raw strings: the live `Drawing` and the image code are
different namespaces (a Ducati answers `96520610B` live while its image holds a mnemonic). It lives in
`FirmwareManager.start_write()` so no API path can skip it. Catalog entries carrying `"guard": false`
(the imported TuneECU tune lists) name a bike in the UI but never resolve a side for the guard — their
model text is free-form, so one motorcycle appears as `1098`, `1098S` and `Superbike 1098 (USA)`, and
letting that decide would block legitimate writes.

**The board keeps its own diagnostics log.** `app/web/diag.py` writes `diag-<ts>.log` into
`log_dir` alongside the ride logs: the worker's link events (`link_up`/`link_down` with the
exception and how long the link had been up, `log_open`/`log_close` with the reason a file ended),
a sysfs health snapshot every `diag.interval_s`, and a filtered tail of `/dev/kmsg`. The kernel
half is the point: a ride's log splitting into several files turned out to be the USB hub dropping
its port ("disabled by hub (EMI?)"), which killed the FTDI cable and the Wi-Fi dongle at once, and
only the kernel ever says so — `journalctl -k` is empty on this board and that boot's journal never
reached the SD card. It rotates at `diag.max_mb` (archive to `.zip`, keep `diag.keep`), is toggled
from Config → System, and is read with `GET /api/diag.txt`. Every call from the worker is
best-effort: diagnostics must never take the ride down.

**Parameters are data, not code.** `config/params.json` defines each channel (`rli`, `fmt`, `offset`,
`length`, `endian`, `signed`, `scale`, `bias`, `recip`, `digits`, `map`/`map_type`). Naming a newly
discovered rli, changing a scale, or adding a status decoder is a JSON edit — no Python change.
Only *selected* params are polled, so poll rate is inversely proportional to selection size.

**Protocol invariants** (all reverse-engineered and documented in `docs/PROTOCOL.md`):
live data is `21 <rli>` → `61 <rli> <value>`, value at **offset 2**, big-endian; there is no
`10 85`/`1A` in the live path. The diagnostic session the PC tools use (`83 03 00 FF 00 FF 00` +
`10 81`, session **0x81**) is armed **lazily on the first Testing command**, never at connect — the
logging path must stay byte-identical to what is verified on the bike. `testing.session_init: false`
is the escape hatch.

**A firmware read/write leaves a verbose file behind.** `FirmwareManager` always passes `-v` to
`5am_util` (the checkbox now only decides whether the UI list shows every line or just milestones
and anything failure-shaped) and writes `fw-<op>-<ts>.log` into `log_dir`: the command, the image
size + sha256, the ECU identity, `usb_facts()` from sysfs (chip, serial, `2-1.1` hub port, driver,
latency timer) before and after, every util line, the kernel's USB lines for the length of the
operation via `KmsgReader` — started even when the diagnostics log is off — a 1 Hz port sampler
that says out loud when `/dev/kline` disappears mid-transfer, and on failure a `dmesg` tail. Read
Read it with `GET /api/firmware/log.txt?file=<name>`; the files are listed in **Config → System**
(`GET /api/firmware/logs`) and deliberately excluded from the Logs tab — `_is_ride_log()` in
`main.py` is the single place that decides what belongs there.

**The exit code of `5am_util` does not decide the result.** With no adapter attached it prints
`ERROR: ioctl: Bad file descriptor` (main.c:494) and still exits 0, so a failed read was reported
as a success. `_verdict()` fails the operation when the util printed an error line (ANSI stripped
first), when a read left no file, or when the file is not `firmware_size` bytes — and the reason,
not "код возврата 0", is what the UI shows.

**Firmware writes are size-gated.** `firmware_size` (327680 B) is checked before any write; an image
of the wrong size bricks the ECU. Read/write shell out to the external `5am_util` binary at
`/opt/onboard-logger/bin/5am_util`, which is absent on a dev host.

## Conventions

- **No native `alert()` / `confirm()`** — `toast(msg, kind)` and `await confirmDialog(text, {danger,
  okLabel})` in `app.js` replace them; `okLabel` carries the verb of the action. Long board commands
  go through `withBusy(btn, fn)`. Anything interpolated into `innerHTML` that comes from the board
  (file names, ECU identity) goes through `esc()`.
- **Two device-local preferences live in `localStorage`, not in config**: the Logger's read/pick
  mode (`paramMode`) and the appearance choice (`theme` → `data-theme` on `<html>`). They are
  per-phone, so they must never travel to `/api/config`.
- **Layout is responsive in two directions**: `@media (max-width: 560px)` for narrow portrait and
  `@media (max-height: 500px)` for landscape phones, plus horizontal `env(safe-area-inset-*)` on
  every full-bleed block. A frozen snapshot must always look frozen — see `setStale()`.
- **All user-visible strings live in `app/static/i18n.js`**, keyed and referenced through `t(key)`
  or `data-i18n` attributes in `index.html`; live-parameter names use `param.<key>`. English is the
  fallback. Never hardcode display text in `app.js` or the HTML.
- Backend error strings returned to the UI are **i18n keys** (`err.no_response`, `err.ecu_rejected`),
  not prose — `_friendly_error()` maps exceptions to them.
- The SPA is dependency-free vanilla JS/CSS (no build step); `main.py` sends `Cache-Control:
  no-cache` for `/` and `/static/*` so a redeploy is picked up without a hard reload.
- Comments explain *why* (a hardware quirk, a reverse-engineered constant, a past bug), not *what* —
  match that density.
- Commits only when asked; the working style is a feature branch merged into `main`.
