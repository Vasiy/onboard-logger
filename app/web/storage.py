"""Where the ride logs go: the board's own SD card, or a USB stick.

Why it exists: ``log_dir`` used to be a single string read once at startup by the
K-Line worker and by DiagLog, and re-read per request by ``/api/logs`` — three
consumers that could disagree the moment the setting changed. This module is the
single authority on the answer to "where do I write right now", and it hands that
answer out as a *callable* so a destination switch takes effect at the next file
open instead of at the next service restart.

The SD card is the fallback, always: if the stick the rider picked is not there
when the log opens, or is yanked mid-ride, writing continues into internal memory
and the UI says so. A ride must never be lost because a connector let go — the
board already loses USB devices to a flaky hub (see HANDOFF.md, 2026-08-26).

Nothing here may raise into the worker; the shell-outs degrade to a message the
way ``system.py`` does, so a dev host with no ``lsblk`` simply reports no devices.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("onboard.storage")

DEFAULTS = {"dest": "internal", "usb_uuid": "", "mount_point": "/media/usb0",
            "auto_mount": True, "poll_s": 3}
MIN_POLL_S = 1.0

# Log files are grouped into one folder per calendar day, in the rider's own
# reading order. It does not sort lexicographically — every consumer that orders
# these folders must go through parse_day().
DAY_FMT = "%d-%m-%Y"
DAY_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")

# Filesystems we are willing to open a ride log on. FAT and exFAT are here
# because they are what a stick shared with Windows/macOS will be formatted as.
USABLE_FS = {"exfat", "vfat", "fat", "fat32", "msdos", "ext2", "ext3", "ext4", "ntfs"}

# A device carrying any of these is the board itself. Never offer it as a log
# target, and never, under any circumstance, format it.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware", "/usr", "/var", "/home", "/etc"}

# mkfs name and extra flags per filesystem the UI may ask for. exFAT is the
# default: no 4 GB file ceiling, and Windows/macOS/Linux all mount it.
MKFS = {
    "exfat": (["mkfs.exfat", "-n"], 15),
    "vfat": (["mkfs.vfat", "-F", "32", "-n"], 11),
}

# MBR partition type 07 — what both exFAT and NTFS sticks ship with, and what
# Windows expects to find on a removable drive.
PART_TYPE = "7"

# Everything format() will need before it is allowed to touch the device. On
# Ubuntu these are three separate packages — sfdisk is in `fdisk`, not in the
# util-linux base — so a board can have wipefs and still not be able to finish
# the job. Checking afterwards is what left a stick with no partition table at
# all on 2026-08-31.
PARTITION_TOOLS = ["wipefs", "sfdisk"]


def day_name(when: datetime | date | None = None) -> str:
    """The folder name for a day: 31-08-2026."""
    return (when or datetime.now()).strftime(DAY_FMT)


def parse_day(name: str) -> date | None:
    """The date behind a folder name, or None if it is not one of ours."""
    if not DAY_RE.match(name or ""):
        return None
    try:
        return datetime.strptime(name, DAY_FMT).date()
    except ValueError:
        return None


def resolve_root(src) -> Path:
    """Accept a path or a callable returning one.

    The writers hold ``StorageManager.active_root`` itself, so every file open
    re-asks where to write and a destination change needs no restart.
    """
    return Path(src() if callable(src) else src)


def _out(cmd: list[str]) -> str:
    """Read-only shell-out; empty string on any failure (dev host, no lsblk)."""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _run(cmd: list[str], stdin_text: str = "", timeout: float = 20) -> tuple[bool, str]:
    """Run a command, returning (ok, message) — never raises.

    Deliberately not ``system._run_err``: partitioning feeds a table on stdin,
    and mkfs on a large stick needs a budget far past that helper's 10 s. Same
    contract otherwise, and the single seam the tests replace with a recorder.
    """
    if shutil.which(cmd[0]) is None:
        return False, f"нет {cmd[0]}"
    try:
        r = subprocess.run(cmd, input=stdin_text or None, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _fs_label(raw: str, limit: int) -> str:
    """A label both mkfs and a Windows box will accept."""
    keep = "".join(c for c in (raw or "").upper() if c.isalnum() or c in "-_")
    return (keep or "LOGS")[:limit]


def _part_dev(disk: str, n: int = 1) -> str:
    """/dev/sda -> /dev/sda1, but /dev/mmcblk0 -> /dev/mmcblk0p1."""
    return f"{disk}p{n}" if disk[-1:].isdigit() else f"{disk}{n}"


class StorageManager:
    """Enumerates USB storage, mounts it, and answers ``active_root()``."""

    def __init__(
        self,
        internal_dir,
        cfg: dict | None = None,
        on_change=None,
        *,
        sys_root: str = "/sys",
        proc_root: str = "/proc",
    ):
        self.internal_dir = Path(internal_dir)
        self._on_change = on_change     # (status) -> None, when the answer moves
        self._last: tuple | None = None
        self.sys_root = Path(sys_root)
        self.proc_root = Path(proc_root)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.apply(cfg)

    def apply(self, cfg: dict | None) -> None:
        """Adopt the storage block of the config (also used at construction)."""
        c = {**DEFAULTS, **(cfg or {})}
        self.dest = "usb" if str(c["dest"]) == "usb" else "internal"
        self.usb_uuid = str(c["usb_uuid"] or "")
        self.mount_point = Path(str(c["mount_point"] or DEFAULTS["mount_point"]))
        self.auto_mount = bool(c["auto_mount"])
        self.poll_s = max(MIN_POLL_S, float(c["poll_s"]))

    # -- where do I write right now ---------------------------------------
    def usb_root(self) -> Path:
        """The log folder on the stick: the internal folder's name, at its root."""
        return self.mount_point / self.internal_dir.name

    def active_root(self) -> Path:
        return self._resolve()[0]

    def fallback(self) -> str:
        return self._resolve()[1]

    def _resolve(self) -> tuple[Path, str]:
        """(root, fallback-reason). The SD card is the answer whenever the USB
        destination cannot be honoured — silently losing a ride is not an option."""
        if self.dest != "usb":
            return self.internal_dir, ""
        if self.is_mounted() and os.access(self.mount_point, os.W_OK):
            return self.usb_root(), ""
        return self.internal_dir, "usb_missing"

    def is_mounted(self) -> bool:
        want = str(self.mount_point)
        for _dev, mnt, _fs in self._mounts():
            if mnt == want:
                return True
        return False

    def _mounts(self) -> list[tuple[str, str, str]]:
        """(device, mountpoint, fstype) from /proc/mounts; empty off-board."""
        out = []
        try:
            text = (self.proc_root / "mounts").read_text()
        except OSError:
            return out
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                # mountpoints are octal-escaped in /proc/mounts
                out.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
        return out

    # -- enumeration -------------------------------------------------------
    def _lsblk(self) -> list[dict]:
        txt = _out(["lsblk", "-J", "-b", "-o",
                    "NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,RM,TYPE,MODEL,VENDOR"])
        if not txt.strip():
            return []
        try:
            return json.loads(txt).get("blockdevices") or []
        except (ValueError, AttributeError, TypeError):
            return []

    def _is_usb(self, name: str, rm: bool) -> bool:
        """Trust sysfs over the removable flag: some sticks report rm=0."""
        try:
            target = os.path.realpath(self.sys_root / "block" / name)
        except OSError:
            return bool(rm)
        return "/usb" in target or bool(rm)

    def devices(self) -> list[dict]:
        """USB disks with their partitions, as the Config tab shows them.

        Anything carrying a system mountpoint is dropped here rather than
        flagged: a device that is never in the list can never be picked by
        name, which is what makes format() safe.
        """
        active = str(self.active_root())
        out = []
        for d in self._lsblk():
            if (d.get("type") or "") != "disk":
                continue
            name = d.get("name") or ""
            dev = d.get("path") or (f"/dev/{name}" if name else "")
            if not dev or not self._is_usb(name, bool(d.get("rm"))):
                continue
            kids = d.get("children") or [d]
            if any((k.get("mountpoint") or "") in SYSTEM_MOUNTS for k in kids):
                continue
            parts = []
            for k in kids:
                pdev = k.get("path") or f"/dev/{k.get('name') or ''}"
                fs = (k.get("fstype") or "").lower()
                mnt = k.get("mountpoint") or ""
                parts.append({
                    "dev": pdev, "fs": fs, "label": k.get("label") or "",
                    "uuid": k.get("uuid") or "", "size": int(k.get("size") or 0),
                    "mount": mnt,
                    "usable": fs in USABLE_FS,
                    "reason": "" if fs in USABLE_FS else ("no_fs" if not fs else "unknown_fs"),
                    "active": bool(mnt) and active.startswith(mnt),
                })
            out.append({
                "dev": dev, "size": int(d.get("size") or 0),
                "model": (d.get("model") or "").strip(),
                "vendor": (d.get("vendor") or "").strip(),
                "parts": parts,
            })
        return out

    def _find_part(self, uuid: str = "", dev: str = "") -> dict | None:
        """Re-enumerate and match. A client-supplied device string is never
        acted on directly — if it is not in the list we just built, it does not
        exist as far as this module is concerned."""
        for d in self.devices():
            for p in d["parts"]:
                if uuid and p["uuid"] == uuid:
                    return p
                if dev and p["dev"] == dev:
                    return p
        return None

    def _find_disk(self, dev: str) -> dict | None:
        for d in self.devices():
            if d["dev"] == dev:
                return d
        return None

    # -- mounting ----------------------------------------------------------
    def mount(self, dev: str = "", uuid: str = "") -> dict:
        part = self._find_part(uuid=uuid, dev=dev)
        if part is None:
            return {"ok": False, "error": "err.storage_unknown_dev"}
        if not part["usable"]:
            return {"ok": False, "error": "err.storage_unusable", "reason": part["reason"]}
        if part["mount"] == str(self.mount_point):
            return {"ok": True, "mount": part["mount"], "dev": part["dev"]}
        try:
            self.mount_point.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "err.storage_mount", "detail": str(exc)}
        opts = "rw,noatime"
        if part["fs"] in ("vfat", "msdos", "fat", "fat32"):
            # flush: the rider can pull the stick at any moment, so do not let
            # the page cache sit on a ride that is already written. vfat only —
            # the exFAT driver rejects the whole mount over it ("exfat: Unknown
            # parameter 'flush'"), which is not a warning but a failure.
            opts += ",flush"
        ok, msg = _run(["mount", "-o", opts, part["dev"], str(self.mount_point)])
        if not ok:
            return {"ok": False, "error": "err.storage_mount", "detail": msg}
        return {"ok": True, "mount": str(self.mount_point), "dev": part["dev"],
                "uuid": part["uuid"]}

    def unmount(self) -> dict:
        """Safe eject: flush first, then let go."""
        if not self.is_mounted():
            return {"ok": True, "skipped": "not_mounted"}
        _run(["sync"])
        ok, msg = _run(["umount", str(self.mount_point)])
        if not ok:
            return {"ok": False, "error": "err.storage_busy", "detail": msg}
        return {"ok": True}

    # -- destination -------------------------------------------------------
    def select(self, dest: str, uuid: str = "", dev: str = "") -> dict:
        """Pick where logs go. Persisting the choice is the caller's job."""
        if dest not in ("internal", "usb"):
            return {"ok": False, "error": "err.storage_bad_dest"}
        if dest == "internal":
            self.dest = "internal"
            return {"ok": True, **self.status()}
        part = self._find_part(uuid=uuid, dev=dev)
        if part is None:
            return {"ok": False, "error": "err.storage_unknown_dev"}
        res = self.mount(dev=part["dev"])
        if not res.get("ok"):
            return res
        self.dest = "usb"
        self.usb_uuid = part["uuid"] or self.usb_uuid
        try:
            self.usb_root().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "err.storage_mount", "detail": str(exc)}
        return {"ok": True, **self.status()}

    # -- formatting --------------------------------------------------------
    def format(self, dev: str, fs: str = "exfat", label: str = "LOGS") -> dict:
        """Wipe a stick and lay down one FAT32/exFAT partition. Irreversible.

        Every guard is here rather than in the endpoint: no API path may reach
        mkfs without passing them.
        """
        if fs not in MKFS:
            return {"ok": False, "error": "err.storage_bad_fs"}
        disk = self._find_disk(dev)
        if disk is None:
            # not enumerated => not removable USB, or it carries a system mount
            return {"ok": False, "error": "err.storage_unknown_dev"}
        if any(p["mount"] in SYSTEM_MOUNTS for p in disk["parts"]):
            return {"ok": False, "error": "err.storage_system_dev"}
        internal = str(self.internal_dir.resolve()) if self.internal_dir.exists() else ""
        if internal and any(p["mount"] and internal.startswith(p["mount"])
                            for p in disk["parts"]):
            return {"ok": False, "error": "err.storage_system_dev"}

        # Feasibility before destruction: wipefs alone is enough to erase the
        # partition table, so a run that cannot reach mkfs must not begin.
        argv, limit = MKFS[fs]
        missing = [c for c in PARTITION_TOOLS if shutil.which(c) is None]
        if shutil.which(argv[0]) is None:
            if not missing:
                # only the filesystem tool is absent — the UI can offer the other one
                return {"ok": False, "error": "err.storage_no_mkfs", "detail": argv[0],
                        "missing": [argv[0]]}
            missing = missing + [argv[0]]
        if missing:
            return {"ok": False, "error": "err.storage_no_tools",
                    "missing": missing, "detail": ", ".join(missing)}

        steps: list[str] = []
        if self.is_mounted():
            self.unmount()
        for p in disk["parts"]:
            if p["mount"]:
                _run(["umount", p["dev"]])

        name = _fs_label(label, limit)
        for cmd, stdin_text in (
            (["wipefs", "-a", disk["dev"]], ""),
            # one partition spanning the device; sfdisk takes the table on stdin
            (["sfdisk", "--label", "dos", disk["dev"]], f"label: dos\n,,{PART_TYPE}\n"),
        ):
            ok, msg = _run(cmd, stdin_text, timeout=60)
            if not ok:
                return {"ok": False, "error": "err.storage_format", "step": cmd[0],
                        "detail": msg, "applied": steps}
            steps.append(cmd[0])

        _run(["partprobe", disk["dev"]], timeout=30)   # best effort; udev usually wins
        part = _part_dev(disk["dev"])
        ok, msg = _run([*argv, name, part], timeout=300)
        if not ok:
            return {"ok": False, "error": "err.storage_format", "step": argv[0],
                    "detail": msg, "applied": steps}
        steps.append(argv[0])
        self._settle(disk["dev"], part)
        return {"ok": True, "dev": part, "fs": fs, "label": name, "applied": steps}

    def _settle(self, disk: str, part: str, timeout: float = 6.0) -> bool:
        """Wait until the kernel can name the filesystem that was just written.

        Without this the enumeration in the very same response still reports the
        stick as "no filesystem", so a successful format renders as an unusable
        drive until the rider hits refresh.
        """
        _run(["partprobe", disk], timeout=30)
        _run(["udevadm", "settle"], timeout=15)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            p = self._find_part(dev=part)
            if p and p["fs"]:
                return True
            time.sleep(0.25)
        return False

    # -- automatic re-attach ------------------------------------------------
    def tick(self) -> None:
        """Re-mount the remembered stick when it shows up again.

        Only the stick the rider already chose (by UUID) is picked up, and the
        config is never rewritten from here: a plug/unplug cycle must not cost
        an SD-card write, and a stranger's stick must not hijack the ride log.
        """
        try:
            if (self.auto_mount and self.dest == "usb" and self.usb_uuid
                    and not self.is_mounted()
                    and self._find_part(uuid=self.usb_uuid) is not None):
                self.mount(uuid=self.usb_uuid)
            # tell the UI when the answer moved on its own — a stick that was
            # pulled changes nothing in the config, only in the world
            cur = self._resolve()
            if cur != self._last:
                self._last = cur
                if self._on_change is not None:
                    self._on_change(self.status(with_devices=False))
        except Exception:       # pragma: no cover - never take the ride down
            pass

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="storage", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.tick()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)

    # -- reporting ---------------------------------------------------------
    def status(self, with_devices: bool = True) -> dict:
        root, fallback = self._resolve()
        st = {
            "dest": self.dest,
            "root": str(root),
            "is_usb": root != self.internal_dir,
            "fallback": fallback,
            "mount_point": str(self.mount_point),
            "usb_uuid": self.usb_uuid,
            "mounted": self.is_mounted(),
        }
        if with_devices:
            st["devices"] = self.devices()
        return st
