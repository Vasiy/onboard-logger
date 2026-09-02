"""Board-health and event log, written next to the K-Line logs.

Why it exists: on 2026-08-26 two mid-ride splits of the parameter log turned out
to be the USB hub dropping its upstream port ("usb usb2-port1: disabled by hub
(EMI?), re-enabling..."), which took the FTDI cable *and* the Wi-Fi dongle with
it — the same event killed the K-Line link and the AP. The app only knew "the
link died", and that boot's journal never reached the SD card, so the cause was
recoverable only from rsyslog's kern.log. This module puts both sides of the
story — our own link events and the kernel's USB/Wi-Fi lines — into one file in
``log_dir``, which the rider can download from the phone like any other log.

Nothing here may raise into the worker: a diagnostics log that can stop the ride
is worse than no diagnostics log, so every entry point swallows its errors.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import select
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from .storage import day_name, resolve_root

log = logging.getLogger("onboard.diag")

# Kernel lines worth keeping. The 2026-08-26 hub drop shows up as "disabled by
# hub (EMI?)" on usb2-port1 plus ftdi_sio/rt2x00 disconnects, so the filter has
# to cover the hub, both devices behind it and the controller underneath.
KMSG_RE = re.compile(
    r"usb|ftdi|ttyUSB|wlan|rt2x00|ieee80211|hub|xhci|dwc2|EMI|voltage|thermal"
    r"|usb-storage|scsi|sd \d+:|I/O error|exfat|FAT-fs",
    re.I,
)

DEFAULTS = {"enabled": True, "interval_s": 5, "max_mb": 2, "keep": 10, "kmsg": True,
            "zip_after": False}
MIN_INTERVAL_S = 1.0
MIN_MAX_MB = 0.05          # a limit below this would rotate faster than it writes


def parse_kmsg(raw: bytes) -> str | None:
    """Turn one /dev/kmsg record into its message text.

    Record layout is ``prio,seq,ts_usec,flag[,key=val];message`` followed by
    optional continuation lines that start with a space. Returns None when the
    record is malformed or not about hardware we care about.
    """
    try:
        head, _, rest = raw.partition(b";")
        if not _ or b"," not in head:
            return None
        text = rest.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not text or not KMSG_RE.search(text):
        return None
    return text


class KmsgReader:
    """Feeds hardware-related kernel lines to a callback until stopped.

    Used twice: by the always-on diagnostics log, and for the duration of a
    firmware read/write, which needs the kernel's side of the story even when
    the diagnostics log is switched off.
    """

    def __init__(self, on_line, dev_root="/dev"):
        self.on_line = on_line
        self.dev_root = Path(dev_root)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def start(self) -> bool:
        try:
            self._fd = os.open(str(self.dev_root / "kmsg"), os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = exc.__class__.__name__
            return False
        os.lseek(self._fd, 0, os.SEEK_END)   # only what happens from now on
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kmsg", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    r, _w, _x = select.select([self._fd], [], [], 0.5)
                except OSError:
                    break
                if not r:
                    continue
                try:
                    raw = os.read(self._fd, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EPIPE):
                        continue        # ring buffer overran us; keep reading
                    break
                text = parse_kmsg(raw)
                if text:
                    try:
                        self.on_line(text)
                    except Exception:
                        pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass


def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def usb_facts(port: str, sys_root="/sys", dev_root="/dev") -> dict:
    """What the kernel knows about the adapter behind ``port`` (e.g. /dev/kline).

    Reads only sysfs, so it is safe to call while the port is open by someone
    else. Returns as much as it can resolve; a missing device yields
    ``{"node": "...", "present": 0}`` rather than an exception.
    """
    sys_root, dev_root = Path(sys_root), Path(dev_root)
    node = Path(port)
    out: dict = {"node": str(node)}
    try:
        real = node.resolve()
    except OSError:
        real = node
    out["tty"] = real.name
    out["present"] = int(real.exists())
    dev = sys_root / "class" / "tty" / real.name / "device"
    try:
        iface = dev.resolve()               # e.g. .../usb2/2-1/2-1.1/2-1.1:1.0
    except OSError:
        return out
    if not iface.exists():
        return out
    try:
        out["driver"] = (iface / "driver").resolve().name   # "ftdi_sio"
    except OSError:
        pass
    usb = iface.parent                      # the USB device the interface sits on
    out["usbpath"] = usb.name               # "2-1.1" — port 1 of the hub on bus 2
    for key, fname in (("vid", "idVendor"), ("pid", "idProduct"),
                       ("serial", "serial"), ("product", "product"),
                       ("speed", "speed"), ("power_mA", "bMaxPower")):
        v = _read_file(usb / fname)
        if v:
            out[key] = v
    lat = _read_file(dev / "latency_timer")
    if lat:
        out["latency_ms"] = lat
    parent = usb.parent                     # the hub (or the root hub) above it
    if parent.name.startswith(("usb", "1-", "2-", "3-", "4-", "5-")):
        out["upstream"] = parent.name
        pr = _read_file(parent / "product")
        if pr:
            out["upstream_product"] = pr
    return out


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.1f}"
    s = str(v)
    return s.replace(" ", "_") if " " in s else s


class DiagLog:
    """Appends health snapshots and events to ``diag-<ts>.log`` in log_dir."""

    def __init__(
        self,
        log_dir,
        cfg: dict | None = None,
        probe=None,
        *,
        sys_root: str = "/sys",
        dev_root: str = "/dev",
    ):
        # a path, or a callable answering "where do I write now" — the same
        # root the ride log is using, so both land in one day folder
        self._log_root = log_dir
        self.sys_root = Path(sys_root)
        self.dev_root = Path(dev_root)
        self._probe = probe             # () -> dict of app-side fields
        self._lock = threading.Lock()
        self._fh = None
        self._path: Path | None = None
        self._bytes = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._kmsg_reader: KmsgReader | None = None
        c = {**DEFAULTS, **(cfg or {})}
        self.enabled = bool(c["enabled"])
        self.interval_s = max(MIN_INTERVAL_S, float(c["interval_s"]))
        self.max_bytes = int(max(MIN_MAX_MB, float(c["max_mb"])) * 1024 * 1024)
        self.keep = max(1, int(c["keep"]))
        self.kmsg = bool(c["kmsg"])
        # also read by FirmwareManager for its own fw-*.log files
        self.zip_after = bool(c["zip_after"])

    @property
    def log_dir(self) -> Path:
        return resolve_root(self._log_root)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if not self.enabled or self._threads:
            return
        self._stop.clear()
        self._spawn(self._health_loop, "diag-health")
        if self.kmsg:
            self._kmsg_reader = KmsgReader(
                lambda text: self._write("KMSG", {}, raw=text), self.dev_root)
            if not self._kmsg_reader.start():
                self.event("kmsg_off", err=self._kmsg_reader.error)
        self.event("start", interval=self.interval_s,
                   limit_mb=round(self.max_bytes / 1048576, 2), kmsg=int(self.kmsg))

    def _spawn(self, target, name) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        # the log is tied to a ride now, so stop() is called after every one of
        # them; without this an idle stop would open a fresh file just to write
        # STOP into it
        if not self._threads and self._fh is None:
            return
        self.event("stop")
        self._stop.set()
        if self._kmsg_reader is not None:
            self._kmsg_reader.stop()
            self._kmsg_reader = None
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        with self._lock:
            self._close_locked(zip_it=self.zip_after)

    def apply(self, cfg: dict | None) -> None:
        """Live-apply the System-tab settings (enable/disable, size limit)."""
        c = {**DEFAULTS, **(cfg or {})}
        self.zip_after = bool(c["zip_after"])
        self.interval_s = max(MIN_INTERVAL_S, float(c["interval_s"]))
        self.max_bytes = int(max(MIN_MAX_MB, float(c["max_mb"])) * 1024 * 1024)
        self.keep = max(1, int(c["keep"]))
        want = bool(c["enabled"])
        if want and not self.enabled:
            self.enabled = True
            self.kmsg = bool(c["kmsg"])
            self.start()
        elif not want and self.enabled:
            self.stop()
            self.enabled = False
        else:
            self.kmsg = bool(c["kmsg"])
            with self._lock:
                self._rotate_if_needed_locked()

    # -- writing ----------------------------------------------------------
    @staticmethod
    def render(fields: dict) -> str:
        """key=value rendering shared by the file lines and /api/diag.txt."""
        return " ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v != "")

    def event(self, kind: str, **fields) -> None:
        """Record one event. Called from the K-Line worker — never raises."""
        try:
            self._write(kind.upper(), fields)
        except Exception:   # pragma: no cover - diagnostics must not bite
            pass

    def _write(self, kind: str, fields: dict, raw: str = "") -> None:
        """``raw`` is copied verbatim (kernel text keeps its spaces); ``fields``
        are rendered as key=value, which is what the health/event lines use."""
        if not self.enabled:
            return
        rendered = self.render(fields)
        line = (datetime.now().isoformat(timespec="milliseconds") + " " + kind
                + (" " + raw if raw else "")
                + (" " + rendered if rendered else "") + "\n")
        with self._lock:
            if self._fh is None:
                self._open_locked()
            if self._fh is None:
                return
            self._fh.write(line)
            self._bytes += len(line)
            self._rotate_if_needed_locked()
        # events (not the per-tick health/kernel noise) also go to journald and
        # from there to /var/log/syslog, which survived the power cut that ate
        # the journal on 2026-08-26
        if kind not in ("HLT", "KMSG"):
            log.info("%s %s", kind, rendered)

    # -- file management --------------------------------------------------
    def _open_locked(self) -> None:
        try:
            d = self.log_dir / day_name()
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = d / f"diag-{ts}.log"
            # a rotation inside the same second must not land on the file the
            # zip thread is still reading
            n = 1
            while path.exists() or path.with_name(path.name + ".zip").exists():
                path = d / f"diag-{ts}-{n}.log"
                n += 1
            self._path = path
            self._fh = path.open("a", buffering=1)
            self._bytes = 0
        except OSError:
            self._fh, self._path = None, None

    def _close_locked(self, zip_it: bool) -> None:
        path = self._path
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
            except OSError:
                pass
        self._fh, self._path, self._bytes = None, None, 0
        if zip_it and path is not None:
            # zipping 2 MiB on this board takes a moment; the caller may be the
            # K-Line worker mid-poll, so hand it to a throwaway thread
            threading.Thread(target=self._archive, args=(path,),
                             name="diag-zip", daemon=True).start()

    def _rotate_if_needed_locked(self) -> None:
        if self._fh is not None and self._bytes >= self.max_bytes:
            self._close_locked(zip_it=True)
            self._open_locked()

    def _archive(self, path: Path) -> None:
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            zpath = path.with_name(path.name + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(path, arcname=path.name)
            path.unlink()
        except OSError:
            return
        self._prune()

    def _prune(self) -> None:
        """Keep only the newest ``keep`` archives — the SD card is small."""
        try:
            # by mtime, not by name: a rotation inside one second yields
            # "diag-<ts>-1.log.zip", which sorts *before* "diag-<ts>.log.zip"
            # rglob, not glob: the archives live in per-day folders now, and
            # a flat glob would quietly stop pruning anything at all
            zips = sorted(self.log_dir.rglob("diag-*.log.zip"),
                          key=lambda p: p.stat().st_mtime)
            for old in zips[:-self.keep]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    # -- health snapshot --------------------------------------------------
    def _read(self, path: Path) -> str:
        return _read_file(path)

    def health(self) -> dict:
        """Cheap sysfs-only board snapshot: no shell-outs, safe every second."""
        out: dict = {}
        out["kline"] = int((self.dev_root / "kline").exists())
        try:
            out["tty"] = len(list(self.dev_root.glob("ttyUSB*")))
        except OSError:
            out["tty"] = ""
        try:
            # count real devices, not the five root hubs (those are "usb1".."usb5")
            devs = self.sys_root / "bus" / "usb" / "devices"
            out["usb"] = sum(1 for p in devs.iterdir()
                             if re.match(r"^\d+-\d", p.name) and ":" not in p.name)
        except OSError:
            out["usb"] = ""
        wlan = self._read(self.sys_root / "class" / "net" / "wlan0" / "operstate")
        out["wlan0"] = wlan or "absent"
        t = self._read(self.sys_root / "class" / "thermal" / "thermal_zone0" / "temp")
        if t.lstrip("-").isdigit():
            out["temp"] = int(t) / 1000.0
        try:
            out["load"] = os.getloadavg()[0]
        except OSError:
            pass
        if self._probe is not None:
            try:
                out.update(self._probe() or {})
            except Exception:
                pass
        return out

    def _health_loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._write("HLT", self.health())
                with self._lock:      # a power cut takes the page cache with it
                    if self._fh is not None:
                        os.fsync(self._fh.fileno())
            except Exception:
                pass

    # -- readers ----------------------------------------------------------
    def current_file(self) -> str:
        return str(self._path) if self._path else ""

    def tail(self, lines: int = 200) -> str:
        """Last N lines of the open file, for /api/diag.txt."""
        p = self._path
        if p is None:
            return ""
        try:
            with self._lock:
                if self._fh is not None:
                    self._fh.flush()
            return "\n".join(p.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""
