"""K-Line worker thread: connect, poll live-data, decode, log, drive the LED.

Runs in its own thread because pyserial is blocking. Communicates with the web
layer only through the thread-safe ``State`` object; the web layer broadcasts
snapshots to WebSocket clients on its own asyncio task.
"""

from __future__ import annotations

import json
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .ecu_id import DEFAULT_FIELDS, describe, parse_fields
from .params import ParamMap
from .kwp2000 import KWP2000Session, NegativeResponse
from .transport import ChecksumError, KLineError, KLineTimeout, KLineTransport

MAX_LOG_BYTES = 64 * 1024 * 1024  # roll the ndjson file at 64 MiB

# Candidate K-Line rates tried (in order) when baud == "auto". 10400 is the
# standard IAW 5AM / KWP2000 rate and comes first.
AUTO_BAUDS = [10400, 9600, 15625, 19200]


def _friendly_error(exc: Exception) -> str:
    """Map noisy serial/OS errors to a localizable status code (else raw text)."""
    s = str(exc)
    if isinstance(exc, FileNotFoundError) or "No such file" in s or "Errno 2" in s:
        return "err.no_adapter"
    if isinstance(exc, PermissionError) or "Permission denied" in s or "Errno 13" in s:
        return "err.port_denied"
    if isinstance(exc, NegativeResponse):
        return "err.ecu_rejected"
    if isinstance(exc, KLineTimeout) or "before timeout" in s:
        return "err.no_response"
    if isinstance(exc, ChecksumError) or "checksum" in s:
        return "err.bad_frame"
    return s


class KLineWorker(threading.Thread):
    def __init__(
        self,
        *,
        port: str,
        params_path: str,
        log_dir: str = "/root/k-line",
        state,
        led,
        log_decoded_default: bool = True,
        log_raw_default: bool = False,
        zip_after: bool = False,
        echo: bool = True,
        baud="auto",
        init="fast",
        ecu_fields=None,
        reconnect_delay: float = 2.0,
        session_init: bool = True,
        diag=None,
    ):
        super().__init__(name="kline-worker", daemon=True)
        self.diag = diag            # DiagLog or None; every call is best-effort
        self.port = port
        self._ecu_fields = ecu_fields or DEFAULT_FIELDS
        self.params_path = params_path
        self.log_dir = Path(log_dir)
        self.state = state
        self.led = led
        self.echo = echo
        self._init_slow = str(init).lower() == "slow"   # fast-init (default) vs 5-baud slow init
        self.reconnect_delay = reconnect_delay

        self._auto_baud = str(baud).lower() == "auto"
        self._bauds = AUTO_BAUDS if self._auto_baud else [int(baud)]
        self._baud_idx = 0

        self._stop_event = threading.Event()
        # pause handshake so an exclusive port user (firmware flasher) can take
        # over /dev/kline: request_pause() sets _pause_req and blocks until the
        # worker has released the port and set _paused_ack.
        self._pause_req = threading.Event()
        self._paused_ack = threading.Event()
        self._reconnect = threading.Event()   # kline settings changed -> re-link
        self._lock = threading.Lock()
        # two independent log streams: decoded parameters (CSV) + raw frames
        self._want_dec = bool(log_decoded_default)
        self._want_raw = bool(log_raw_default)
        self._zip_after = bool(zip_after)
        self._dec_fh = None
        self._dec_path: Path | None = None
        self._dec_bytes = 0
        self._dec_cols: list[str] = []      # columns of the open decoded file
        self._dec_restart = False           # selection changed -> roll the file
        self._raw_fh = None
        self._raw_path: Path | None = None
        self._raw_bytes = 0

        # rli-scan (bus sweep) state: probe 0x21 <rli> across a range, log raw
        self._scan_on = False
        self._scan_start = 0x00
        self._scan_end = 0xFF
        self._scan_fmt = 2            # 2 addressed, 0 short header, "both" = try both
        self._scan_fh = None
        self._scan_csv = None
        self._scan_path: Path | None = None
        self._scan_dur = 0            # auto-stop after N seconds (0 = run until stopped)
        self._scan_deadline = 0.0     # monotonic deadline when scanning
        self._pre_dec = False         # log intent before scan, restored on stop
        self._pre_raw = False

        # one-shot Testing commands (DTC, resets, actuators) run on the worker
        # thread using the live session; run_command() hands off via this handshake.
        self._cmd_name: str | None = None
        self._cmd_arg = None
        self._cmd_resp: dict | None = None
        self._cmd_done = threading.Event()
        self._cmd_lock = threading.Lock()
        self._session_init = bool(session_init)  # arm 0x83 + 0x10 81 before tests

        # actuator test in progress: the ON frame is sent by the command, the OFF
        # frame by the poll loop at the deadline, so polling never stops for the
        # length of the pulse and a lost UI can't leave an output energized.
        self._act_lid: int | None = None
        self._act_key = ""
        self._act_until = 0.0        # monotonic deadline
        self._act_session: KWP2000Session | None = None

        # per-request outcome tally, drained by the diagnostics health tick: a
        # line that is starting to fail shows up here long before it drops
        self._stat: dict[str, int] = {}
        self._dec_rows = 0           # rows in the open decoded file
        self._link_at = 0.0          # monotonic time the current link came up
        self._last_fail = ""         # last connect failure, to log it only once

        self.pmap = ParamMap.load(params_path)
        # decoded log columns = selected parameters. Default: only the named
        # channels (p.default); unidentified rli start unticked.
        self._selected = [p.key for p in self.pmap.params if p.default]
        self.state.set_catalog(self.pmap.catalog(), self._selected)
        self.state.set_decoded_armed(self._want_dec)
        self.state.set_raw_armed(self._want_raw)

    # -- public API (web thread) ------------------------------------------
    def set_logging_decoded(self, on: bool) -> None:
        if on and self._scan_on:   # logging and scan are mutually exclusive
            self.stop_scan()
        with self._lock:
            self._want_dec = bool(on)
        self.state.set_decoded_armed(self._want_dec)

    def set_logging_raw(self, on: bool) -> None:
        if on and self._scan_on:
            self.stop_scan()
        with self._lock:
            self._want_raw = bool(on)
        self.state.set_raw_armed(self._want_raw)

    def logging_state(self) -> tuple[bool, bool]:
        """Current (decoded, raw) logging intent — used to save/restore around a flash."""
        with self._lock:
            return self._want_dec, self._want_raw

    def set_selected(self, keys: list[str]) -> None:
        """Set which parameters are logged to the decoded CSV. Changing the set
        while a decoded log is open rolls it to a fresh file (new column header)
        so the CSV stays consistent."""
        valid = {p.key for p in self.pmap.params}
        sel = [k for k in keys if k in valid]
        with self._lock:
            self._selected = sel
            if self._dec_fh is not None:
                self._dec_restart = True

    def set_zip_after(self, on: bool) -> None:
        self._zip_after = bool(on)

    def stop(self) -> None:
        self._stop_event.set()

    def request_pause(self, timeout: float = 10.0) -> bool:
        """Ask the worker to release /dev/kline; block until it has (or timeout)."""
        self._pause_req.set()
        return self._paused_ack.wait(timeout)

    def resume(self) -> None:
        self._pause_req.clear()

    def apply_kline(self, baud, echo=None, init=None) -> None:
        """Apply K-Line settings (bus speed / echo / init mode) live — no reboot.
        Drops the current link so the next attempt uses the new settings."""
        self._auto_baud = str(baud).lower() == "auto"
        self._bauds = AUTO_BAUDS if self._auto_baud else [int(baud)]
        self._baud_idx = 0
        if echo is not None:
            self.echo = bool(echo)
        if init is not None:
            self._init_slow = str(init).lower() == "slow"
        self._reconnect.set()

    def reload_params(self) -> None:
        pmap = ParamMap.load(self.params_path)
        sel = [p.key for p in pmap.params if p.default]
        with self._lock:
            self.pmap = pmap
            self._selected = sel
        self.state.set_catalog(pmap.catalog(), sel)

    def start_scan(self, start: int = 0x00, end: int = 0xFF, fmt="both",
                   duration_s: int = 0) -> None:
        """Enter rli-scan mode: sweep 0x21 <rli> across [start..end], logging every
        request/response to scan-*.ndjson (+ a wide CSV of 16-bit values per sweep).
        This is the calibration capture — run it on the bike with the engine idling
        and blipping the throttle so channels can be identified from the data.

        ``duration_s`` > 0 auto-stops the scan after that many seconds and restores
        the log toggles to their pre-scan state."""
        with self._lock:
            self._scan_start = max(0, min(0xFF, int(start)))
            self._scan_end = max(self._scan_start, min(0xFF, int(end)))
            self._scan_fmt = fmt if fmt == "both" else int(fmt)
            self._scan_dur = max(0, int(duration_s))
            self._scan_on = True
            # scan owns /dev/kline while it runs -> disarm both log streams so they
            # neither fight the scan nor silently resume when it stops. Remember the
            # prior intent to restore it when the scan ends.
            self._pre_dec, self._pre_raw = self._want_dec, self._want_raw
            self._want_dec = False
            self._want_raw = False
        self.state.set_decoded_armed(False)
        self.state.set_raw_armed(False)
        self.state.set_scan(True, 0, 0, "", self._scan_dur or -1)  # intent before link
        self._reconnect.set()

    def stop_scan(self) -> None:
        with self._lock:
            self._scan_on = False
            self._want_dec, self._want_raw = self._pre_dec, self._pre_raw  # restore logs
        self.state.set_scan(False, 0, 0, "")
        self.state.set_decoded_armed(self._want_dec)
        self.state.set_raw_armed(self._want_raw)
        self._reconnect.set()

    def mark_event(self, label: str) -> dict:
        """Stamp a labelled marker into the running scan log (web thread).

        Identifying the status channels (side stand, clutch, gear, kill switch,
        engine state) means correlating an rli that changes exactly when a switch
        is flipped. Pressing "mark" at the moment of the flip writes the marker
        into scan-*.ndjson and the wide CSV, so the sweep before/after the marker
        can be diffed afterwards instead of guessing from timestamps.
        """
        text = str(label)[:64]
        with self._lock:
            fh, csv = self._scan_fh, self._scan_csv
        if fh is None:
            return {"ok": False, "error": "err.no_scan"}
        wall = time.time()
        try:
            fh.write(json.dumps({"t": wall, "event": text}, separators=(",", ":")) + "\n")
            if csv is not None:   # keep the CSV row count aligned with the ndjson
                csv.write(datetime.now().isoformat(timespec="milliseconds")
                          + ",# " + text.replace(",", " ") + "\n")
        except OSError as e:
            return {"ok": False, "error": _friendly_error(e)}
        return {"ok": True, "event": text, "t": wall}

    def scan_state(self) -> dict:
        with self._lock:
            return {
                "on": self._scan_on, "start": self._scan_start,
                "end": self._scan_end, "fmt": self._scan_fmt,
            }

    def run_command(self, name: str, arg=None, timeout: float = 6.0) -> dict:
        """Execute a one-shot KWP command (Testing tab) on the worker thread with
        the live session. Needs an active poll loop (connected, not scanning)."""
        with self._cmd_lock:
            self._cmd_resp = None
            self._cmd_done.clear()
            self._cmd_arg = arg
            self._cmd_name = name
            if not self._cmd_done.wait(timeout):
                self._cmd_name = None
                return {"ok": False, "error": "err.no_response"}
            return self._cmd_resp or {"ok": False, "error": "err.no_response"}

    def _exec_command(self, session: KWP2000Session) -> None:
        name = self._cmd_name
        arg = self._cmd_arg
        try:
            if name == "actuator_stop":
                r = {"ok": True, "stopped": self._act_off(session)}
            else:
                # every Testing command runs in the diagnostic session the PC tools
                # start; armed on first use, never at connect (see enter_test_mode)
                mode = self._arm_test_mode(session)
                if name == "read_dtc":
                    r = session.read_dtc(); r["ok"] = True
                elif name == "clear_dtc":
                    r = session.clear_dtc(); r["ok"] = True
                elif name == "reset_tps":
                    r = session.reset_tps(); r["ok"] = True
                elif name == "reset_adaptation":
                    r = session.reset_adaptation(); r["ok"] = True
                elif name == "actuator":
                    r = self._act_on(session, int(arg[0]), float(arg[1]), str(arg[2]))
                else:
                    r = {"ok": False, "error": "unknown command"}
                if mode is not None:
                    r["mode"] = mode
        except NegativeResponse as e:
            r = {"ok": False, "error": "err.ecu_rejected", "nrc": e.code, "detail": str(e)}
            self._act_clear()
        except (KLineError, OSError) as e:
            r = {"ok": False, "error": _friendly_error(e)}
            self._act_clear()
        self._cmd_resp = r
        self._cmd_name = None
        self._cmd_done.set()

    def _arm_test_mode(self, session: KWP2000Session) -> dict | None:
        """Lazily start the diagnostic session (0x83 timing + 0x10 81) — see
        KWP2000Session.enter_test_mode. Disabled by config testing.session_init."""
        if not self._session_init:
            return None
        mode = session.enter_test_mode()
        self.state.set_test_mode(session.test_mode and mode.get("ok", False),
                                 mode.get("detail", ""))
        return mode

    # -- actuator tests (worker thread) -----------------------------------
    def _act_on(self, session: KWP2000Session, lid: int, secs: float, key: str) -> dict:
        """Energize one actuator and hand the timing to the poll loop."""
        self._act_off(session)           # only one actuator at a time
        r = session.actuator_set(lid, True)
        self._act_lid = lid
        self._act_key = key
        self._act_until = time.monotonic() + secs
        self._act_session = session
        self.state.set_actuator(lid, key, time.time() + secs)
        r.update({"ok": True, "seconds": round(secs, 1)})
        return r

    def _act_off(self, session: KWP2000Session | None) -> bool:
        """Release the active actuator. Safe to call when none is active."""
        lid = self._act_lid
        if lid is None:
            return False
        self._act_clear()
        if session is not None:
            try:
                session.actuator_set(lid, False)
            except (KLineError, OSError):
                return False
        return True

    def _act_clear(self) -> None:
        self._act_lid = None
        self._act_key = ""
        self._act_until = 0.0
        self._act_session = None
        self.state.set_actuator(None, "", 0.0)

    def _act_tick(self, session: KWP2000Session) -> None:
        """Poll-loop hook: drop the output once its deadline has passed."""
        if self._act_lid is not None and time.monotonic() >= self._act_until:
            self._act_off(session)

    def actuator_active(self) -> bool:
        return self._act_lid is not None

    # -- diagnostics ------------------------------------------------------
    def _diag(self, kind: str, **fields) -> None:
        """Best-effort note into the board diagnostics log (may be disabled)."""
        if self.diag is not None:
            self.diag.event(kind, **fields)

    def stats(self) -> dict:
        """Drain the per-request tally for the diagnostics health tick.

        A line going bad (loose K-Line ground, a dying adapter) raises `to`/`bad`
        for a while before the link drops outright — worth seeing in the log.
        """
        s, self._stat = self._stat, {}
        return {
            "ok": s.get("ok", 0), "to": s.get("timeout", 0),
            "nrc": s.get("nrc", 0), "bad": s.get("bad_frame", 0),
            "rows": self._dec_rows if self._dec_fh is not None else "",
        }

    # -- logging file management (worker thread) --------------------------
    def _reconcile_logging(self) -> None:
        if self._want_dec and self._dec_fh is None:
            self._open_dec()
        elif not self._want_dec and self._dec_fh is not None:
            self._close_dec("disarmed")
        if self._want_raw and self._raw_fh is None:
            self._open_raw()
        elif not self._want_raw and self._raw_fh is not None:
            self._close_raw("disarmed")

    def _open_dec(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._dec_path = self.log_dir / f"kline-dec-{ts}.csv"
        self._dec_fh = self._dec_path.open("a", buffering=1)
        # guzzidiag-style CSV: time + one column per *selected* parameter
        with self._lock:
            self._dec_cols = list(self._selected)
        header = "time," + ",".join(self._dec_cols) + "\n"
        self._dec_fh.write(header)
        self._dec_bytes = len(header)
        self._dec_rows = 0
        self.state.set_decoded_file(str(self._dec_path), 0)
        self._diag("log_open", file=self._dec_path.name, cols=len(self._dec_cols))

    def _close_dec(self, reason: str = "link_lost") -> None:
        path = self._dec_path
        if self._dec_fh is not None:
            try:
                self._dec_fh.flush()
                self._dec_fh.close()
            finally:
                self._dec_fh = None
            self._maybe_zip(path)
            self._diag("log_close", file=path.name, rows=self._dec_rows, reason=reason)
        self.state.set_decoded_file("", 0)

    def _open_raw(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._raw_path = self.log_dir / f"kline-{ts}.raw.log"
        self._raw_fh = self._raw_path.open("a", buffering=1)
        self._raw_bytes = 0
        self.state.set_raw_file(str(self._raw_path), 0)

    def _close_raw(self, reason: str = "link_lost") -> None:
        path = self._raw_path
        if self._raw_fh is not None:
            try:
                self._raw_fh.flush()
                self._raw_fh.close()
            finally:
                self._raw_fh = None
            self._maybe_zip(path)
            self._diag("raw_close", file=path.name, reason=reason)
        self.state.set_raw_file("", 0)

    def _maybe_zip(self, path: Path | None) -> None:
        """Archive a just-closed log to <name>.zip and drop the original."""
        if not (self._zip_after and path and path.exists() and path.stat().st_size > 0):
            return
        try:
            zpath = path.with_name(path.name + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(path, arcname=path.name)
            path.unlink()
        except OSError:
            pass

    def _close_all_logs(self, reason: str = "link_lost") -> None:
        self._close_dec(reason)
        self._close_raw(reason)
        self._close_scan()

    # -- rli-scan files ---------------------------------------------------
    def _open_scan(self, start: int, end: int) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._scan_path = self.log_dir / f"scan-{ts}.ndjson"
        self._scan_fh = self._scan_path.open("a", buffering=1)
        # wide CSV alongside: time + one r<HH> column per rli in range (16-bit value)
        csv_path = self.log_dir / f"scan-{ts}.csv"
        self._scan_csv = csv_path.open("a", buffering=1)
        cols = ["r%02X" % r for r in range(start, end + 1)]
        self._scan_csv.write("time," + ",".join(cols) + "\n")

    def _close_scan(self) -> None:
        path = self._scan_path
        for fh in (self._scan_fh, self._scan_csv):
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except OSError:
                    pass
        self._scan_fh = self._scan_csv = None
        if path is not None:
            self._maybe_zip(path)
        self._scan_path = None

    def _roll_if_needed(self) -> None:
        if self._dec_fh is not None and self._dec_bytes >= MAX_LOG_BYTES:
            self._close_dec("size")
            self._open_dec()
        if self._raw_fh is not None and self._raw_bytes >= MAX_LOG_BYTES:
            self._close_raw("size")
            self._open_raw()

    def _write_decoded(self, values: dict) -> None:
        if self._dec_fh is None:
            return
        iso = datetime.now().isoformat(timespec="milliseconds")
        row = iso + "," + ",".join(
            "" if values.get(k) is None else str(values.get(k)) for k in self._dec_cols
        ) + "\n"
        self._dec_fh.write(row)
        self._dec_bytes += len(row)
        self._dec_rows += 1
        self.state.inc_decoded_records()

    def _write_raw(self, rec: dict) -> None:
        if self._raw_fh is None:
            return
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        self._raw_fh.write(line)
        self._raw_bytes += len(line)
        self.state.inc_raw_records()

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        while not self._stop_event.is_set():
            self._reconnect.clear()  # consume any pending live re-link request
            # yield the port to a firmware flash if requested
            if self._pause_req.is_set():
                self._close_all_logs()
                self.state.set_status("firmware", "")
                self.led.searching()
                self._paused_ack.set()
                while self._pause_req.is_set() and not self._stop_event.is_set():
                    self._stop_event.wait(0.2)
                self._paused_ack.clear()
                continue
            baud = self._bauds[self._baud_idx]
            self.state.set_bus_baud(baud)
            transport = KLineTransport(self.port, baud=baud, echo=self.echo)
            connected = False
            clean = False
            session = None
            err: Exception | None = None
            self.state.set_test_mode(False, "")
            try:
                self.led.searching()
                self.state.set_status("searching", "")  # UI localizes the label
                transport.open()
                session = KWP2000Session(transport)
                ecu_id = session.connect(slow=self._init_slow)
                connected = True
                self.state.set_ecu_id(ecu_id)
                self.state.set_ecu_hw(session.ecu_hw)
                self.state.set_ecu_id_raw(session.ecu_id_raw.hex())
                self.state.set_ecu_id_blocks(
                    {"%02X" % o: b.hex() for o, b in session.ecu_id_blocks.items()})
                self.state.set_ecu_fields(
                    parse_fields(session.ecu_id_raw, session.ecu_hw, self._ecu_fields))
                self.state.set_ecu_desc(
                    describe(session.ecu_id_raw, session.ecu_hw, self._ecu_fields))
                self.led.connected()
                self.state.set_status("connected", ecu_id or "")
                self._link_at = time.monotonic()
                self._last_fail = ""
                self._diag("link_up", baud=baud,
                           init="slow" if self._init_slow else "fast",
                           ecu=(ecu_id or "?").split()[0] if ecu_id else "?")
                with self._lock:
                    scan = self._scan_on
                if scan:
                    self._scan_loop(session)
                else:
                    self.state.set_scan(False, 0, 0, "")
                    self._poll_loop(session)
                clean = True            # loop exited on request, link still alive
            except (KLineError, OSError) as exc:
                err = exc
                # a bike parked with the ignition off retries forever: log the
                # first failure of a kind, not every 2 s attempt
                if not connected and _friendly_error(exc) != self._last_fail:
                    self._last_fail = _friendly_error(exc)
                    self._diag("link_fail", err=type(exc).__name__,
                               detail=str(exc)[:100], baud=baud,
                               tty=int(Path(self.port).exists()))
                self.state.set_status("error", _friendly_error(exc))
                self.state.set_ecu_fields({})   # drop the ECU banner on disconnect
                self.state.set_ecu_desc("")
                self.led.searching()
            finally:
                self._act_clear()       # nothing may stay "energized" in the UI
                if session is not None and clean:
                    session.end_link()  # 0x20 + 0x82, like both PC tools (best effort)
                self.state.set_test_mode(False, "")
                transport.close()
                if connected:
                    # the split of a ride's log into several files starts here:
                    # whatever killed the link also ends the file
                    self._diag("link_down",
                               err=type(err).__name__ if err else "clean",
                               detail=(str(err)[:100] if err else ""),
                               up=round(time.monotonic() - self._link_at, 1),
                               tty=int(Path(self.port).exists()))
                self._close_all_logs()  # never keep logging once the link ends
            # rotate to the next candidate rate only if the initial link failed
            if not connected and self._auto_baud:
                self._baud_idx = (self._baud_idx + 1) % len(self._bauds)
            if self._stop_event.is_set():
                break
            time.sleep(self.reconnect_delay)
        self._close_all_logs()
        self.led.off()

    def _poll_loop(self, session: KWP2000Session) -> None:
        try:
            self._poll_loop_inner(session)
        finally:
            # leaving the loop for any reason (pause, re-link, error, shutdown)
            # must release an energized actuator
            self._act_off(session)

    def _poll_loop_inner(self, session: KWP2000Session) -> None:
        with self._lock:
            pmap = self.pmap
        interval = pmap.poll_interval_ms / 1000.0
        timeout = pmap.poll_timeout_ms / 1000.0
        while not self._stop_event.is_set():
            if self._pause_req.is_set() or self._reconnect.is_set():
                return  # release the port; run() handles pause / live re-link
            if self._cmd_name is not None:      # one-shot Testing command
                self._exec_command(session)
            self._act_tick(session)             # release an actuator whose time is up
            t0 = time.monotonic()

            # per-parameter poll: each value is its own 0x21 <rli> request. Only the
            # SELECTED params are polled -> fewer ticks = higher poll rate. Selection
            # can change live, so it is re-read each cycle.
            with self._lock:
                sel = set(self._selected)
            to_poll = [p for p in pmap.params if p.key in sel]
            values: dict = {}
            probes: list[dict] = []
            cache: dict[tuple[int, bool], dict] = {}   # dedupe params sharing an rli
            got_any = False
            for p in to_poll:
                if self._reconnect.is_set() or self._pause_req.is_set():
                    return
                key = (p.rli, p.with_addr)
                r = cache.get(key)
                if r is None:
                    r = session.read_local(p.rli, with_addr=p.with_addr, timeout=timeout)
                    cache[key] = r
                    probes.append(r)
                    self._stat[r["status"]] = self._stat.get(r["status"], 0) + 1
                if r["status"] == "ok":
                    got_any = True
                    values[p.key] = p.decode(bytes.fromhex(r["data"]))
                else:
                    values[p.key] = None

            # polled something but nobody answered -> verify the link is still up
            # (TesterPresent raises on a dead bus, bubbling to run() to relink).
            # Nothing selected -> the keepalive below holds the session open.
            if to_poll and not got_any:
                session.tester_present()

            self.state.update_values(values)

            # the link is proven live -> only now open/manage the log files
            if self._dec_restart:  # parameter selection changed -> roll decoded file
                self._dec_restart = False
                if self._dec_fh is not None:
                    self._close_dec("selection")
                    self._open_dec()
            self._reconcile_logging()
            self._roll_if_needed()
            self._write_decoded(values)
            if self._raw_fh is not None:
                wall = time.time()
                for r in probes:
                    self._write_raw({
                        "t": wall, "rli": r["rli"], "fmt": 2 if r["with_addr"] else 0,
                        "st": r["status"], "tx": r["tx"], "rx": r["rx"],
                    })

            session.keepalive_if_idle(1.0)

            elapsed = time.monotonic() - t0
            if elapsed < interval:
                self._stop_event.wait(interval - elapsed)

    def _scan_loop(self, session: KWP2000Session) -> None:
        """Sweep 0x21 <rli> across the configured range, logging every exchange to
        scan-*.ndjson (+ a wide CSV). Repeats continuously so values are captured
        over time (idle vs. throttle) for channel identification."""
        with self._lock:
            start, end, fmt = self._scan_start, self._scan_end, self._scan_fmt
            timeout = self.pmap.poll_timeout_ms / 1000.0
        modes = [True, False] if fmt == "both" else [fmt != 0]
        self._close_all_logs()          # scan owns the files while it runs
        self._open_scan(start, end)
        self._scan_deadline = (time.monotonic() + self._scan_dur) if self._scan_dur else 0.0
        rem0 = self._scan_dur if self._scan_dur else -1
        self.state.set_scan(True, 0, 0, str(self._scan_path or ""), rem0)
        sweep = 0
        alive_mode: dict[int, bool] = {}   # rli -> framing that answered (found on sweep 0)
        while not self._stop_event.is_set():
            if self._pause_req.is_set() or self._reconnect.is_set():
                return
            with self._lock:
                if not self._scan_on:
                    return
            sweep_t0 = time.time()
            row: dict[int, int | None] = {}
            first = sweep == 0 or not alive_mode   # full range until something answers
            # sweep 0 probes the whole range (find live rli); later sweeps hit only
            # the live ones in their working framing, so repeats are fast enough to
            # capture how values move with rpm / throttle.
            total = end - start + 1
            rlis = range(start, end + 1) if first else sorted(alive_mode)
            for rli in rlis:
                if self._stop_event.is_set() or self._reconnect.is_set():
                    return
                if first and (rli & 3) == 0:   # publish sweep-0 progress (rli position)
                    self.state.set_scan_progress(rli - start, total)
                try_modes = modes if first else [alive_mode[rli]]
                best = None
                for with_addr in try_modes:
                    r = session.read_local(rli, with_addr=with_addr, timeout=timeout)
                    if self._scan_fh is not None:
                        # timestamp per probe, not per sweep: a marker can then be
                        # matched to the individual read (~tens of ms) instead of to
                        # a whole sweep (seconds) when hunting status channels
                        self._write_scan({
                            "t": time.time(), "sweep": sweep, "sweep_t": sweep_t0,
                            "rli": rli, "fmt": 2 if with_addr else 0,
                            "st": r["status"], "nrc": r["nrc"],
                            "tx": r["tx"], "rx": r["rx"], "v": r["val"],
                        })
                    if r["status"] == "ok" and best is None:
                        best = r["val"]
                        alive_mode.setdefault(rli, with_addr)
                row[rli] = best
            if self._scan_csv is not None:
                iso = datetime.now().isoformat(timespec="milliseconds")
                cells = ["" if row.get(r) is None else str(row[r])
                         for r in range(start, end + 1)]
                self._scan_csv.write(iso + "," + ",".join(cells) + "\n")
            sweep += 1
            self.state.set_scan_progress(total, total)   # sweep finished -> 100 %
            rem = max(0, int(self._scan_deadline - time.monotonic())) if self._scan_deadline else -1
            self.state.set_scan(True, sweep, len(alive_mode), str(self._scan_path or ""), rem)
            if self._scan_deadline and time.monotonic() >= self._scan_deadline:
                self.stop_scan()        # auto-stop: restores log toggles, exits loop
                return

    def _write_scan(self, rec: dict) -> None:
        if self._scan_fh is None:
            return
        self._scan_fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.state.inc_raw_records()
