"""Firmware read/write on the IAW 5AM ECU via the denandz/5am_util binary.

5am_util is the proven reference flasher (C, builds cleanly on aarch64, targets
exactly the IAW5AMHW610). We shell out to it rather than reimplement the flash
sequence (a wrong reimplementation risks bricking the ECU).

    read :  5am_util -o <out.bin> -i /dev/kline
    write:  5am_util -w <fw.bin>  -i /dev/kline   (prompts once; we feed newline)

The K-Line logger worker holds /dev/kline, so before running we ask it to
release the port (request_pause) and resume it afterwards.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from ..kline.ecu_id import DEFAULT_FIELDS, describe
from .diag import KmsgReader, usb_facts
from .storage import day_name, resolve_root

# A flash that fails leaves nothing behind except what we wrote down, so the
# per-operation log keeps everything: the util's own -v output, the kernel's USB
# lines, and a sample of the port state while the transfer runs.
FW_LOG_MAX = 16 * 1024 * 1024
FW_SAMPLE_S = 1.0          # port/USB poll while an operation is in flight
FW_HEARTBEAT_S = 15.0      # ... and a line every so often even when nothing moves

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 5am_util exits 0 even when it failed ("ERROR: ioctl: Bad file descriptor" with
# no adapter attached, main.c:494), so the exit code alone must never decide the
# result — its own error lines and the size of what came out do.
UTIL_ERR_RE = re.compile(r"\[!\]|\berror\b|\bfailed\b|timeout", re.I)


class FirmwareBlocked(RuntimeError):
    """A write the guard refused. Carries the verdict so the UI can explain it."""

    def __init__(self, verdict: dict):
        super().__init__(verdict.get("reason", "blocked"))
        self.verdict = verdict


class FirmwareManager:
    def __init__(self, worker_getter, util_path, fw_dir, port: str, state, ecu_fields=None,
                 guard=None, describe_image=None, log_dir=None, diag=None,
                 sys_root="/sys", dev_root="/dev", fw_size=0):
        self.worker_getter = worker_getter          # callable -> KLineWorker | None
        self.util_path = Path(util_path)
        self.fw_dir = Path(fw_dir)
        self.port = port
        self.state = state
        self.ecu_fields = ecu_fields or DEFAULT_FIELDS
        # guard(name) -> verdict dict; describe_image(name) -> extra sidecar lines.
        # Both are injected so this module stays free of config/catalog plumbing.
        self.guard = guard
        self.describe_image = describe_image
        # verbose per-operation log: the same day folder as the ride logs, so it
        # is listed and downloadable next to them. Asked again per operation,
        # because the destination may be a USB stick that came and went.
        self._log_root = log_dir or fw_dir
        self.diag = diag
        self.sys_root, self.dev_root = sys_root, dev_root
        self.fw_size = int(fw_size or 0)   # expected image size, 0 = unknown
        self._util_err = ""                # first failure line the util printed
        self._vlog = None                  # open file handle for the current op
        self._vlog_path: Path | None = None
        self._vlog_bytes = 0
        self._kmsg: KmsgReader | None = None
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._ui_verbose = False
        self.last_read = ""     # name of the last successful read, for the rename hint
        self._pending_note = ""  # one log line to emit when the next op starts
        self._lock = threading.Lock()
        self.op = "idle"        # idle | reading | writing
        self.result = ""        # "" | ok | error | cancelled
        self.progress = ""
        self.log: list[str] = []
        self.current = ""
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    # -- introspection -----------------------------------------------------
    def available(self) -> bool:
        return self.util_path.is_file()

    def status(self) -> dict:
        with self._lock:
            return {
                "op": self.op,
                "result": self.result,
                "progress": self.progress,
                "current": self.current,
                "log": self.log[-50:],
                "available": self.available(),
                "port": self.port,
                "log_file": self._vlog_path.name if self._vlog_path else "",
            }

    # -- start operations --------------------------------------------------
    def start_read(self, name: str, verbose: bool = False) -> None:
        out = self.fw_dir / name
        # -v is always on: its output goes to the per-operation file, which is
        # the only place a failed read can be explained from afterwards. The
        # checkbox decides whether the UI list shows every line or milestones.
        cmd = [str(self.util_path), "-o", str(out), "-i", self.port, "-v"]
        self._start("reading", name, cmd, confirm=False, ui_verbose=verbose)

    def start_write(self, name: str, verbose: bool = False) -> None:
        path = self.fw_dir / name
        if not path.is_file():
            raise FileNotFoundError(name)
        # The endpoint checks this too (so it can answer 400 instead of 500), but the
        # gate lives here because this is the only road to `5am_util -w`: a direct API
        # call, a future batch endpoint or a REPL all have to come through it.
        if self.guard is not None:
            verdict = self.guard(name)
            if verdict.get("level") == "block":
                raise FirmwareBlocked(verdict)
            if verdict.get("overridden"):
                self._pending_note = "[!] guard overridden: " + str(verdict.get("reason", ""))
        cmd = [str(self.util_path), "-w", str(path), "-i", self.port, "-v"]
        self._start("writing", name, cmd, confirm=True, ui_verbose=verbose)

    def _start(self, op: str, name: str, cmd: list[str], confirm: bool,
               ui_verbose: bool = False) -> None:
        with self._lock:
            if self.op != "idle":
                raise RuntimeError("busy")
            if not self.available():
                raise RuntimeError("5am_util не установлен")
            self.op = op
            self.result = ""
            self.progress = ""
            self.log = []
            self.current = name
            self._ui_verbose = bool(ui_verbose)
            self._util_err = ""
        note = getattr(self, "_pending_note", "")
        if note:
            self._pending_note = ""
            self._append(note)
        self._thread = threading.Thread(target=self._run, args=(cmd, confirm, op), daemon=True)
        self._thread.start()

    # -- worker thread -----------------------------------------------------
    def _append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 500:
                self.log = self.log[-500:]
            self.progress = line

    # -- verbose per-operation log ----------------------------------------
    UI_KEEP = ("[", "err", "fail", "timeout", "invalid", "abort", "%")

    @property
    def log_dir(self) -> Path:
        return resolve_root(self._log_root)

    def _vopen(self, op: str, name: str) -> None:
        try:
            d = self.log_dir / day_name()
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._vlog_path = d / f"fw-{op}-{ts}.log"
            self._vlog = self._vlog_path.open("a", buffering=1)
            self._vlog_bytes = 0
        except OSError:
            self._vlog, self._vlog_path = None, None

    def _v(self, tag: str, text: str) -> None:
        """One line into the operation file. Never raises into the flash path."""
        fh = self._vlog
        if fh is None:
            return
        try:
            if self._vlog_bytes >= FW_LOG_MAX:
                return
            line = f"{datetime.now().isoformat(timespec='milliseconds')} {tag} {text}\n"
            fh.write(line)
            self._vlog_bytes += len(line)
            if self._vlog_bytes >= FW_LOG_MAX:
                fh.write("... truncated: operation log hit its size cap\n")
        except OSError:
            pass

    def _vclose(self) -> None:
        fh, self._vlog = self._vlog, None
        if fh is None:
            return
        try:
            fh.flush()
            os.fsync(fh.fileno())     # a failed flash is often followed by a reboot
            fh.close()
        except OSError:
            pass
        # one switch in Config -> System archives both board log kinds
        if self.diag is not None and getattr(self.diag, "zip_after", False):
            self._varchive(self._vlog_path)

    def _varchive(self, path: Path | None) -> None:
        """Zip a finished operation log. Best-effort: an unarchived file is a
        cosmetic problem, an exception here would be a lost flash result."""
        if path is None:
            return
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return
            zpath = path.with_name(path.name + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(path, arcname=path.name)
            path.unlink()
            self._vlog_path = zpath      # name the file that now exists
        except OSError:
            pass

    def _usb_line(self) -> str:
        facts = usb_facts(self.port, self.sys_root, self.dev_root)
        return " ".join(f"{k}={v}" for k, v in facts.items())

    def _port_state(self) -> tuple:
        """The few things whose change explains a mid-transfer failure."""
        dev = Path(self.dev_root)
        try:
            ttys = sorted(p.name for p in dev.glob("ttyUSB*"))
        except OSError:
            ttys = []
        return (Path(self.port).exists(), tuple(ttys))

    def _sample_loop(self) -> None:
        prev, last = None, 0.0
        while not self._sampler_stop.wait(FW_SAMPLE_S):
            now = time.monotonic()
            cur = self._port_state()
            if cur != prev or now - last >= FW_HEARTBEAT_S:
                present, ttys = cur
                self._v("PORT", f"present={int(present)} tty={','.join(ttys) or '-'}")
                if prev is not None and cur[0] != prev[0]:
                    # the node vanishing mid-transfer *is* the failure cause
                    self._v("PORT", "!! %s %s" % (self.port,
                            "disappeared" if not present else "came back"))
                    self._append("[!] порт %s %s" % (self.port,
                                 "исчез" if not present else "вернулся"))
                prev, last = cur, now

    # what is worth quoting out of the ring buffer: the adapter and anything that
    # took it away. Plain enumeration noise from boot explains nothing.
    DMESG_RE = re.compile(
        r"ftdi|ttyUSB|disconnect|disabled by hub|over-?current|reset .*device|"
        r"new (full|high|low)-speed|error", re.I)

    def _dmesg_tail(self, keep: int = 20) -> list[str]:
        """Kernel ring buffer as a safety net: catches what the reader missed."""
        exe = shutil.which("dmesg")
        if not exe:
            return []
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        hits = [ln for ln in out.splitlines() if self.DMESG_RE.search(ln)]
        return hits[-keep:] if hits else ["(ring buffer holds nothing about the adapter)"]

    def _verdict(self, rc: int, writing: bool) -> str:
        """Empty when the operation really succeeded, else why it did not."""
        if rc != 0:
            return f"код возврата {rc}" + (f": {self._util_err}" if self._util_err else "")
        if self._util_err:
            return self._util_err          # rc lied; the util said what went wrong
        if writing or not self.current:
            return ""
        out = self.fw_dir / self.current   # a read must leave a full image behind
        if not out.is_file():
            return f"файл {self.current} не создан"
        size = out.stat().st_size
        if self.fw_size and size != self.fw_size:
            return f"размер {size} != {self.fw_size}"
        if not size:
            return "прочитано 0 байт"
        return ""

    def _image_facts(self, name: str) -> str:
        path = self.fw_dir / name
        try:
            data = path.read_bytes()
        except OSError:
            return "missing"
        return (f"size={len(data)} sha256={hashlib.sha256(data).hexdigest()[:16]} "
                f"path={path}")

    def _run(self, cmd: list[str], confirm: bool, op: str = "") -> None:
        self.fw_dir.mkdir(parents=True, exist_ok=True)
        worker = self.worker_getter()
        paused = False
        prev_logging = None
        started = time.monotonic()
        self._vopen(op or self.op, self.current)
        snap = self.state.snapshot()
        self._v("FW", f"start op={op or self.op} name={self.current} util={self.util_path}")
        self._v("FW", "cmd " + " ".join(cmd))
        self._v("FW", "image " + self._image_facts(self.current))
        self._v("FW", "ecu id=%s hw=%s status=%s"
                % (snap.get("ecu_id", "") or "-", snap.get("ecu_hw", "") or "-",
                   snap.get("status", "")))
        self._v("USB", self._usb_line())
        try:
            st = os.statvfs(self.fw_dir)
            self._v("FW", "disk free_mb=%d" % (st.f_bavail * st.f_frsize // 1048576))
        except OSError:
            pass
        if self.diag is not None:
            self.diag.event("fw_start", op=op or self.op, name=self.current,
                            log=self._vlog_path.name if self._vlog_path else "")
        # the kernel's own account of the adapter for the length of the operation,
        # regardless of whether the diagnostics log is switched on
        self._kmsg = KmsgReader(lambda t: self._v("KMSG", t), self.dev_root)
        if not self._kmsg.start():
            self._v("FW", "kmsg unavailable: " + self._kmsg.error)
            self._kmsg = None
        self._sampler_stop.clear()
        self._sampler = threading.Thread(target=self._sample_loop, name="fw-sampler",
                                         daemon=True)
        self._sampler.start()
        try:
            if worker is not None:
                # force both K-Line log streams OFF for the whole flash, remember
                # the prior intent to restore afterwards
                prev_logging = worker.logging_state()
                worker.set_logging_decoded(False)
                worker.set_logging_raw(False)
                self._append("[*] запись логов K-Line отключена на время операции")
                paused = worker.request_pause(timeout=10.0)
                self._v("FW", "worker paused=%d prev_logging=%s"
                        % (int(paused), prev_logging))
                if not paused:
                    self._finish("error", "не удалось освободить порт K-Line")
                    return
                self._v("USB", self._usb_line())   # after the port was released
            self._append("[*] " + " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
            )
            if confirm:  # writer waits on one keypress before flashing
                try:
                    self._proc.stdin.write("\n")
                    self._proc.stdin.flush()
                except OSError:
                    pass
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            # 5am_util uses \r for progress spinners; normalize to lines
            for raw in self._proc.stdout:
                for part in raw.replace("\r", "\n").split("\n"):
                    p = ANSI_RE.sub("", part).strip()
                    if not p:
                        continue
                    self._v("UTIL", p)
                    if not self._util_err and UTIL_ERR_RE.search(p):
                        self._util_err = p
                    # -v is always on now, so an unchecked box shows milestones
                    # and anything that smells like a failure, not every byte
                    if self._ui_verbose or any(w in p.lower() for w in self.UI_KEEP):
                        self._append(p)
            rc = self._proc.wait()
            self._v("FW", "exit rc=%d elapsed=%.1fs" % (rc, time.monotonic() - started))
            failure = self._verdict(rc, confirm)
            if self.result == "cancelled":
                self._finish("cancelled", "отменено")
            elif failure:
                self._v("FW", "verdict " + failure)
                self._finish("error", failure)
            else:
                if not confirm:  # a read -> save the GuzziDiag-style description
                    self._write_desc(self.current)
                    self.last_read = self.current
                self._finish("ok", "готово")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self._v("FW", "exception %s: %s" % (type(exc).__name__, exc))
            self._finish("error", str(exc))
        finally:
            self._proc = None
            self._sampler_stop.set()
            if self._sampler is not None:
                self._sampler.join(timeout=2.0)
                self._sampler = None
            if self._kmsg is not None:
                self._kmsg.stop()
                self._kmsg = None
            self._v("USB", self._usb_line())
            out = self.fw_dir / self.current
            if out.is_file():
                self._v("FW", "file %s size=%d" % (out.name, out.stat().st_size))
            if self.result != "ok":
                # the reader only sees what happened after it started; the ring
                # buffer also holds whatever led up to the operation
                for ln in self._dmesg_tail():
                    self._v("DMESG", ln)
            self._v("FW", "end result=%s elapsed=%.1fs"
                    % (self.result or "?", time.monotonic() - started))
            if self.diag is not None:
                self.diag.event("fw_end", op=op or "", name=self.current,
                                result=self.result or "?",
                                elapsed=round(time.monotonic() - started, 1),
                                log=self._vlog_path.name if self._vlog_path else "")
            self._vclose()
            if worker is not None:
                if prev_logging is not None:  # restore prior logging intent
                    worker.set_logging_decoded(prev_logging[0])
                    worker.set_logging_raw(prev_logging[1])
                if paused:
                    worker.resume()

    def _write_desc(self, name: str) -> None:
        """Save <name>.txt: the ECU identification plus what the image bytes say."""
        snap = self.state.snapshot()
        raw_hex = snap.get("ecu_id_raw", "")
        extra = self.describe_image(name) if self.describe_image else []
        # No live identity and nothing readable in the image -> nothing worth writing
        if not raw_hex and not extra:
            return
        try:
            raw = bytes.fromhex(raw_hex) if raw_hex else b""
        except ValueError:
            raw = b""
        try:
            text = describe(raw, snap.get("ecu_hw", ""), self.ecu_fields, extra)
            (self.fw_dir / (name + ".txt")).write_bytes(text.encode("ascii", "replace"))
            self._append("[+] описание ECU сохранено: " + name + ".txt")
        except OSError:
            pass

    def _finish(self, result: str, msg: str) -> None:
        self._append("[=] " + msg)
        with self._lock:
            if self.result != "cancelled":
                self.result = result
            self.op = "idle"

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            self.result = "cancelled"
        proc.terminate()
        return True
