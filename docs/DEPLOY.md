# Install and upgrade

**English** · [Deutsch](DEPLOY.de.md) · [Español](DEPLOY.es.md) · [Français](DEPLOY.fr.md) ·
[Italiano](DEPLOY.it.md) · [Nederlands](DEPLOY.nl.md) · [Български](DEPLOY.bg.md) ·
[Русский](DEPLOY.ru.md)

How code gets onto the board, how it is replaced afterwards, and what to do when a replacement goes
wrong.

The board is **not a repository**. It holds whatever was last copied onto it, which is why every
path below ends in a verification step, and why an undeployed change means the bike runs old code.

## The three ways in

| | when | needs | who runs it |
|---|---|---|---|
| `install.sh` | once, on a fresh board | internet on the board, root | you, on the board over ssh |
| `deploy.sh` | day-to-day development | the board reachable from the dev host | you, on the dev host |
| Config → System | no dev host at hand (garage, road) | the archive on the phone | the rider, in the browser |

They put the code in the same place and end at the same service. The difference is who carries the
files and what checks them.

---

## 1. First install (`install.sh`)

Run **as root, on the board**, from a clone of the repository:

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

This is the only step that needs internet on the board: it installs packages and builds the ECU
flasher from source.

What it does, in order:

1. **Packages** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` is there for `sfdisk`: Ubuntu splits it out of `util-linux`, and a board that has
   `wipefs` but not `sfdisk` wipes a USB stick's partition table and cannot write a new one.
2. **Copies the repository** to `/opt/onboard-logger` (excluding `.git`, `.venv`, `__pycache__`).
3. **Python venv** at `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Builds `5am_util`** (`denandz/5am_util`) into `/opt/onboard-logger/bin/5am_util` — the external
   binary the firmware read/write shells out to.
5. **Seeds the runtime config** into `/etc/onboard-logger/`: `config.json`, `params.json`,
   `ecu_id.json` — **only if absent** (`[ -f ] || cp`). See *Layered config* below.
6. **udev rule** → `/dev/kline`, **NetworkManager** told to leave `wlan0` alone, radio unblocked and
   the regulatory domain set from `wifi.country`.
7. **hostapd/dnsmasq autostart disabled** — the app brings them up itself once the config is
   rendered, so their own boot order cannot fail first.
8. **systemd unit** installed, enabled and started.

Then:

```
Web UI:  http://192.168.5.1/          (also over eth0 while debugging)
check:   systemctl status onboard-logger
         journalctl -u onboard-logger -f
```

### Where things end up

| path | what | survives an upgrade |
|---|---|---|
| `/opt/onboard-logger` | the code | replaced by it |
| `/opt/onboard-logger/.venv` | Python environment | yes — moved across, never rebuilt on the bike |
| `/opt/onboard-logger/bin/5am_util` | ECU flasher binary | yes — same |
| `/etc/onboard-logger/` | live config, seeded once | yes — never touched |
| `/root/k-line` | ride, diagnostics, firmware and update logs | yes — outside the tree |
| `/root/firmware` | ECU images | yes — outside the tree |
| `/opt/updates/` | uploaded archive + the rollback script | working area |
| `/opt/onboard-logger.old` | the previous tree, kept for rollback | created by an update |

---

## 2. Upgrade from the dev host (`./deploy.sh`)

The everyday path. Run it **from the repository root on the dev host**, not on the board:

```bash
./deploy.sh                 # test, stamp, sync, restart, verify
./deploy.sh --no-tests      # skip the offline suite
PY=/path/to/python ./deploy.sh
```

Credentials are **not** in the script: put `BOARD_HOST`, `BOARD_USER` and `BOARD_PASS` in
`.deploy.env` next to it (git-ignored, see `.deploy.env.example`) or pass them in the environment.
With no password set it uses ssh key auth.

What it does:

1. Runs the whole offline suite, `import app.main` and a syntax check of `app.js`. A failure stops
   the deploy — nothing reaches the bike.
2. Stamps `BUILD` (see *Versioning*).
3. `rsync -a --delete` of the **whole repository**, not just `app/`: `config/fw_layout.json`,
   `config/fw_catalog.json` and `config/dtc/` are read at runtime, and an `app/`-only deploy left
   the board on stale data more than once. Excluded: `.git`, `.venv`, `bin`, `__pycache__`,
   `old_logs`, `CONTEXT.md` — so the venv and the flasher binary are also protected from `--delete`.
4. Restarts `onboard-logger` and fails if the service does not come back `active`.
5. **Verifies by checksum in both directions** — the working tree against the board and the board
   against the working tree. Any difference is an error, not a warning.
6. Prints a `layered config: /etc vs repo` line naming every layered asset whose `/etc` copy differs
   from the repository's.

Board data — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — is out of reach by
construction: it all lives outside the synced directory.

### Layered config: why a `config/` edit may not take effect

`/etc/onboard-logger` **wins** over the repository copy for every layered asset (`params.json`,
`ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json`, plus `config.json`,
`selected.json`, `presets.json`), and `install.sh` only ever *seeds* them. So an edited
`config/params.json` rides along in the rsync and the board keeps running its own copy — that is how
a channel rename once shipped without taking effect.

`deploy.sh` reports the drift; syncing one is a deliberate act, and the board's copy may hold a hand
edit worth keeping:

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # back it up first
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Upgrade from the phone (Config → System)

For a board you cannot reach from a laptop. The archive is carried in by the rider; the board does
all the checking.

Why not "pull from git": the normal Wi-Fi mode is the **access point the phone joins**, so the board
has no internet, and a private remote would need a token stored on the device.

### Build the archive

On the dev host:

```bash
./release.sh                # runs the suite, then writes dist/onboard-logger-<version>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/somewhere ./release.sh
```

It packs the **tracked** files plus a generated `BUILD`, so nothing local rides along — no
`.deploy.env`, no logs, no ECU images. GitHub's own *Download ZIP* also works: the wrapper folder it
adds (`onboard-logger-main/`) is detected and stripped.

Accepted: `.tar.gz`, `.tgz`, `.tar`, `.zip`, up to 32 MB (128 MB unpacked, 5000 members).

### Apply it

Config → **System** → *Upload .tar.gz / .zip* → confirm. The panel shows the running version, a
progress bar and, behind *Log*, what the board is doing.

The order of the steps is the whole design — **nothing touches the running tree until the new one
has proven itself**:

1. **Refuse if now is a bad moment.** A firmware read/write in flight, an open ride log, or a
   running scan all block the update; so does a second update. An *armed* logging intent with the
   ignition off does not block — nothing is being written, and that is exactly when a rider updates
   the board.
2. **Unpack** into `/opt/onboard-logger.new`. Every member is validated: absolute paths, `..`, and
   anything that is not a plain file or directory (symlinks, hardlinks, devices) are refused
   outright — this runs as root next to `/opt`.
3. **Content gate.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` and `config/config.default.json` must all be present,
   and `requirements.txt` must be **byte-identical** to the installed one: `pip` needs PyPI, the bike
   has no network, and a dependency that cannot be installed leaves the service with an
   `ImportError` and port 80 dead.
4. **Test with the board's own interpreter**, against the staged tree: first `import app.main`
   (60 s), then every `tests/test_*.py` in turn (15 min for the lot). `.venv` and `bin` are lent to
   the staging tree as symlinks for this, so a failure leaves the running service untouched.
   The bar counts test files. UI tests skip themselves without `node`.
5. **Swap** — renames only, so the window in which neither tree is in place is two syscalls wide.
   `.venv` and `bin/5am_util` are *moved* into the new tree; the old tree becomes
   `/opt/onboard-logger.old`.
6. **Arm the watchdog, then restart.** `/opt/updates/rollback.sh` is scheduled with
   `systemd-run --on-active=120 --unit=onboard-logger-rollback` **before** the restart, and the
   restart itself is detached — doing it inline would kill the process that owes the browser its
   answer.
7. **Confirmation.** The new code clears `/run/onboard-logger/update-pending` at start-up. If it
   never does — it does not import, or dies during construction — the watchdog puts
   `/opt/onboard-logger.old` back, carries `.venv` and `bin` over to it, and restarts. The tree that
   failed is kept as `/opt/onboard-logger.failed`.

`/etc/onboard-logger` is not touched at any point, so settings, parameter definitions and presets
survive.

### What it leaves behind

Every operation writes `update-<ts>.log` into the day folder next to the ride logs, listing the
archive's sha256, every gate it passed, the test results and the swap. Read it in
**Config → System → Board logs** (filter *Update*), or directly:

```
GET /api/update/log.txt                      # the latest
GET /api/update/log.txt?file=<day>/<name>    # a specific one
```

### If it fails

A failure before the swap changes nothing: the staging tree is deleted and the message says which
gate refused. The common ones:

| message | meaning |
|---|---|
| a firmware operation / a ride log / a scan | wait for it to finish, then retry |
| not a readable archive | truncated download, or not an archive |
| paths outside the project | refused as unsafe — do not "fix" it, find out where it came from |
| `requirements.txt` differs | dependencies changed: this update needs `deploy.sh` and a `pip install` |
| not a complete tree | wrong archive (a partial download, or another project) |
| the new code does not import | the archive is broken — nothing was changed |
| the offline suite failed | the code is broken — nothing was changed |

A failure *after* the swap is what the watchdog is for: wait two minutes and the previous version
comes back on its own. Recovering by hand, over ssh, if it ever comes to that:

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # if the restored tree has none
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Versioning

- **`VERSION`** is tracked and bumped by hand — the release number, currently `0.2.0`.
- **`BUILD`** sits beside it, holds `<version> <describe> <branch> <date>`, is stamped by both
  `deploy.sh` and `release.sh`, and is **never committed**: a file the board generated inside its own
  tree would break `deploy.sh`'s checksum verification in both directions.

Config → System shows `BUILD` when the tree was stamped and the plain release number otherwise. A
board that has never been stamped says so rather than showing an empty line.

To cut a release: edit `VERSION`, commit it, then `./release.sh` (or `./deploy.sh`).

---

## Which one should I use?

- **Changing code during development** → `./deploy.sh`. It is the only path that verifies the board
  matches the working tree byte for byte.
- **Adding or upgrading a Python dependency** → `./deploy.sh` and then `pip install` on the board.
  The update button refuses a changed `requirements.txt` on purpose.
- **Away from the dev host** → `./release.sh` beforehand, keep the archive on the phone, upload it in
  Config → System.
- **A board that will not boot the app at all** → ssh, and the manual recovery above; if the tree
  itself is gone, `install.sh` again (it preserves `/etc/onboard-logger` and the logs).
