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

import subprocess
import threading
from pathlib import Path

from ..kline.ecu_id import DEFAULT_FIELDS, describe


class FirmwareBlocked(RuntimeError):
    """A write the guard refused. Carries the verdict so the UI can explain it."""

    def __init__(self, verdict: dict):
        super().__init__(verdict.get("reason", "blocked"))
        self.verdict = verdict


class FirmwareManager:
    def __init__(self, worker_getter, util_path, fw_dir, port: str, state, ecu_fields=None,
                 guard=None, describe_image=None):
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
            }

    # -- start operations --------------------------------------------------
    def start_read(self, name: str, verbose: bool = False) -> None:
        out = self.fw_dir / name
        cmd = [str(self.util_path), "-o", str(out), "-i", self.port]
        if verbose:
            cmd.append("-v")
        self._start("reading", name, cmd, confirm=False)

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
        cmd = [str(self.util_path), "-w", str(path), "-i", self.port]
        if verbose:
            cmd.append("-v")
        self._start("writing", name, cmd, confirm=True)

    def _start(self, op: str, name: str, cmd: list[str], confirm: bool) -> None:
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
        note = getattr(self, "_pending_note", "")
        if note:
            self._pending_note = ""
            self._append(note)
        self._thread = threading.Thread(target=self._run, args=(cmd, confirm), daemon=True)
        self._thread.start()

    # -- worker thread -----------------------------------------------------
    def _append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 500:
                self.log = self.log[-500:]
            self.progress = line

    def _run(self, cmd: list[str], confirm: bool) -> None:
        self.fw_dir.mkdir(parents=True, exist_ok=True)
        worker = self.worker_getter()
        paused = False
        prev_logging = None
        try:
            if worker is not None:
                # force both K-Line log streams OFF for the whole flash, remember
                # the prior intent to restore afterwards
                prev_logging = worker.logging_state()
                worker.set_logging_decoded(False)
                worker.set_logging_raw(False)
                self._append("[*] запись логов K-Line отключена на время операции")
                paused = worker.request_pause(timeout=10.0)
                if not paused:
                    self._finish("error", "не удалось освободить порт K-Line")
                    return
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
                    p = part.strip()
                    if p:
                        self._append(p)
            rc = self._proc.wait()
            if rc == 0:
                if not confirm:  # a read -> save the GuzziDiag-style description
                    self._write_desc(self.current)
                    self.last_read = self.current
                self._finish("ok", "готово")
            elif self.result == "cancelled":
                self._finish("cancelled", "отменено")
            else:
                self._finish("error", f"код возврата {rc}")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self._finish("error", str(exc))
        finally:
            self._proc = None
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
