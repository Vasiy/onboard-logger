"""Code update from an archive uploaded through the UI.

The board is not a git checkout and has no internet: its normal Wi-Fi mode is the
AP the rider's phone joins, and the private origin would need a token sitting in
/opt — the exact leak the public repository was rebuilt to remove. So an update
arrives the only way that works on a bike: as a .tar.gz/.zip the phone already
has, posted to the board, checked here, and swapped in.

The order of the steps is the whole design. Nothing touches the running tree
until the new one has proven itself:

    unpack -> content gate -> board's own interpreter imports it and runs the
    offline suite -> swap -> detached restart -> a watchdog rolls back if the
    new code never confirms it came up.

Three things live inside DEST but outside the repository. Two cannot be rebuilt
here (no network, no compiler time): ``.venv`` and ``bin/5am_util``. The third,
``addons/``, holds add-ons the rider installed and the files those are keeping —
none of which came from any release. All three are moved into the new tree, never
copied and never deleted, and the rollback script carries them back. ``/etc/onboard-logger`` is not touched at all: it wins over ``config/``
by design, so a swap of /opt must leave the rider's settings alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .storage import day_name, resolve_root

# An archive is source, not media: the repo tars to well under a megabyte, so
# these caps are three orders of magnitude of headroom and still stop a bomb.
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
MAX_MEMBERS = 5000
FREE_MARGIN = 32 * 1024 * 1024      # ... plus room for the log and the run itself
LOG_MAX = 4 * 1024 * 1024

# What has to be in the archive for it to be this project at all. A truncated
# download and a tarball of the wrong repo both die here rather than halfway
# through the swap.
REQUIRED = ("app/main.py", "app/static/app.js", "app/static/i18n.js",
            "app/static/index.html", "requirements.txt",
            "config/config.default.json")

# The new code confirms its own start-up by creating OK_MARKER; the watchdog
# reads it. Both live in /run (tmpfs), so a power cut cannot leave a stale
# confirmation behind for the next update.
RUN_DIR = Path("/run/onboard-logger")
PENDING = "update-pending"
OK = "update-ok"

# Generous on purpose: the suite is minutes of work on an SBC, and a timeout that
# fires early would fail a perfectly good update. The rider sees a bar move.
TEST_TIMEOUT = 900.0        # the whole offline suite on a slow board
IMPORT_TIMEOUT = 60.0
ROLLBACK_AFTER = 120        # seconds the new code has to say it is alive

# The watchdog cannot live in the tree it may have to replace, and it must work
# with nothing but sh: by the time it runs, the service it would ask has already
# been restarted into code that may not import.
ROLLBACK_SH = """#!/bin/sh
# Written by onboard-logger's updater before it restarted the service.
# If the new code never confirmed it came up, put the old tree back.
DEST=%(dest)s
PREV=%(prev)s
OK=%(ok)s
[ -f "$OK" ] && exit 0
[ -d "$PREV" ] || exit 0
rm -rf "$DEST.failed"
mv "$DEST" "$DEST.failed" 2>/dev/null
mv "$PREV" "$DEST" || exit 1
# .venv and bin/5am_util went with the failed tree and cannot be rebuilt offline;
# addons/ went with it too and holds files only the rider has
for keep in .venv bin addons; do
  [ -e "$DEST/$keep" ] || mv "$DEST.failed/$keep" "$DEST/$keep" 2>/dev/null
done
rm -f "%(pending)s"
systemctl restart onboard-logger
"""


class UpdateBusy(RuntimeError):
    """Refused because the board is doing something a restart would ruin."""


def confirm_boot(run_dir: Path = RUN_DIR, dest: Path | None = None,
                 etc_dir: Path | None = None) -> bool:
    """Called once from the service start-up: "the new code is running".

    Import plus a full construction of the app is what this proves — not that
    every endpoint works. That is the deliberate limit of an offline watchdog:
    the failure it exists for (a tree that will not import, leaving :80 dead and
    only ssh to fix it) is exactly the one it catches.
    """
    pending = run_dir / PENDING
    if not pending.is_file():
        return False
    try:
        info = json.loads(pending.read_text())
    except (OSError, ValueError):
        info = {}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / OK).write_text(datetime.now().isoformat(timespec="seconds"))
        pending.unlink()
    except OSError:
        return False
    if etc_dir is not None:
        _write_state(etc_dir, {**info, "confirmed": time.time()})
    return True


def _write_state(etc_dir: Path, info: dict) -> None:
    """Remember what was applied. /etc survives the swap; /opt does not."""
    try:
        etc_dir.mkdir(parents=True, exist_ok=True)
        (etc_dir / "update.json").write_text(json.dumps(info, ensure_ascii=False,
                                                        indent=1))
    except OSError:
        pass


def _read_state(etc_dir: Path) -> dict:
    try:
        return json.loads((etc_dir / "update.json").read_text())
    except (OSError, ValueError):
        return {}


def _strip_top(names: list[str]) -> str:
    """The single top-level folder to drop, or "".

    GitHub's "Download ZIP" wraps everything in ``onboard-logger-main/``; our own
    ``git archive`` does not. Both have to work, so the wrapper is detected
    rather than assumed.
    """
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    # a lone top-level *file* is not a wrapper folder
    return top + "/" if any(n.startswith(top + "/") for n in names) else ""


def _rel(name: str, top: str) -> str | None:
    """Member path relative to the tree root, or None when it is not safe.

    Zip slip is a real risk here and not a theoretical one: this runs as root and
    writes next to /opt. Anything absolute, anything climbing out with ``..`` and
    anything that is not a plain file or directory is refused outright.
    """
    n = name.replace("\\", "/").strip()
    if not n or n.startswith("/") or ":" in n.split("/", 1)[0]:
        return None
    if top and not n.startswith(top):
        return None
    n = n[len(top):] if top else n
    parts = [p for p in n.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


class UpdateManager:
    """Applies an uploaded archive to the tree the service runs from.

    Shaped like FirmwareManager on purpose — the same status()/log/thread
    contract, so the UI polls it the same way and a failed operation leaves the
    same kind of file behind.
    """

    def __init__(self, dest, state, log_dir, py=None, fwm_getter=None, diag=None,
                 etc_dir=Path("/etc/onboard-logger"), run_dir=RUN_DIR,
                 work_dir=Path("/opt/updates"), service="onboard-logger"):
        # resolved: the staging and previous trees are named as siblings of this
        # one, and a relative path has no sibling to name
        self.dest = Path(dest).resolve()
        self.state = state
        self._log_root = log_dir            # callable or path, like the writers
        self.py = Path(py) if py else self.dest / ".venv" / "bin" / "python"
        self.fwm_getter = fwm_getter or (lambda: None)
        self.diag = diag
        self.etc_dir = Path(etc_dir)
        self.run_dir = Path(run_dir)
        self.work_dir = Path(work_dir)
        self.service = service
        self._lock = threading.Lock()
        self.op = "idle"        # idle | unpacking | testing | swapping | restarting
        self.last_op = ""
        self.result = ""        # "" | ok | error
        self.progress = ""
        self.percent = -1.0
        self.done = 0
        self.total = 0
        self.log: list[str] = []
        self.current = ""       # archive file name
        self._thread: threading.Thread | None = None
        self._vlog = None
        self._vlog_path: Path | None = None
        self._vlog_bytes = 0

    # -- introspection -----------------------------------------------------
    @property
    def log_dir(self) -> Path:
        return resolve_root(self._log_root)

    @property
    def stage(self) -> Path:
        return self.dest.with_name(self.dest.name + ".new")

    @property
    def prev(self) -> Path:
        return self.dest.with_name(self.dest.name + ".old")

    def version(self) -> dict:
        """What the board is running.

        VERSION is the tracked release number (hand-bumped). BUILD is stamped
        beside it by deploy.sh / release.sh — the same number plus the commit and
        the date — and is never committed: a file the board generated inside DEST
        would break deploy.sh's checksum verification in both directions. This
        endpoint feeds Config → System's `#updVersion`, and a rider reads that as
        "what firmware am I on" — not a debugging aid, so it names the release
        number alone. The commit, branch and stamp date that BUILD also carries
        stay out of it; they exist for `deploy.sh`'s own checksum story and the
        terminal, not for the phone.
        """
        text = _release_version(self.dest)
        st = _read_state(self.etc_dir)
        return {
            "version": text,
            "applied": st.get("applied", 0),
            "archive": st.get("archive", ""),
            "sha256": st.get("sha256", ""),
            "rolled_back": bool(st.get("rolled_back")),
            "can_rollback": self.prev.is_dir(),
        }

    def status(self) -> dict:
        with self._lock:
            return {
                "op": self.op,
                "last_op": self.last_op,
                "result": self.result,
                "progress": self.progress,
                # nested like /api/firmware: a flat "total" collides with the
                # one _disk_free() puts in the same response
                "prog": {"percent": self.percent, "done": self.done,
                         "total": self.total},
                "current": self.current,
                "log": self.log[-50:],
                "log_file": self._vlog_path.name if self._vlog_path else "",
            }

    # -- the gate ----------------------------------------------------------
    def busy_reason(self) -> str:
        """i18n key of what a restart would ruin right now, or "".

        A restart in the middle of a flash bricks the ECU; in the middle of a
        ride it cuts the log. Both are cheap to wait out and expensive to undo.
        """
        fwm = self.fwm_getter()
        if fwm is not None and getattr(fwm, "op", "idle") != "idle":
            return "err.update_fw_busy"
        snap = self.state.snapshot() if self.state is not None else {}
        # an *open file*, not the armed intent: logging armed with the ignition
        # off writes nothing, and that is when a rider updates the board
        if snap.get("log_decoded_file") or snap.get("log_raw_file"):
            return "err.update_logging"
        if snap.get("scan_on"):
            return "err.update_scanning"
        with self._lock:
            if self.op != "idle":
                return "err.update_busy"
        return ""

    # -- start -------------------------------------------------------------
    def start(self, archive: Path) -> None:
        reason = self.busy_reason()
        if reason:
            raise UpdateBusy(reason)
        with self._lock:
            if self.op != "idle":
                raise UpdateBusy("err.update_busy")
            self.op = self.last_op = "unpacking"
            self.result = ""
            self.progress = ""
            self.percent, self.done, self.total = -1.0, 0, 0
            self.log = []
            self.current = Path(archive).name
        self._thread = threading.Thread(target=self._apply, args=(Path(archive),),
                                        name="update", daemon=True)
        self._thread.start()

    # -- worker thread -----------------------------------------------------
    def _append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 300:
                self.log = self.log[-300:]
            self.progress = line
        self._v("UPD", line)

    def _set_op(self, op: str) -> None:
        with self._lock:
            self.op = self.last_op = op

    def _finish(self, result: str, msg: str) -> None:
        self._append("[=] " + msg)
        with self._lock:
            self.result = result
            self.op = "idle"
        self._vclose()

    def _fail(self, key: str, detail: str = "") -> None:
        """Failures are i18n keys, like every other error the UI shows."""
        self._append("[!] " + key + ((": " + detail) if detail else ""))
        self._finish("error", key)

    # -- operation log -----------------------------------------------------
    def _vopen(self) -> None:
        try:
            d = self.log_dir / day_name()
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._vlog_path = d / f"update-{ts}.log"
            self._vlog = self._vlog_path.open("a", buffering=1)
            self._vlog_bytes = 0
        except OSError:
            self._vlog, self._vlog_path = None, None

    def _v(self, tag: str, text: str) -> None:
        fh = self._vlog
        if fh is None or self._vlog_bytes >= LOG_MAX:
            return
        try:
            line = f"{datetime.now().isoformat(timespec='milliseconds')} {tag} {text}\n"
            fh.write(line)
            self._vlog_bytes += len(line)
        except OSError:
            pass

    def _vclose(self) -> None:
        fh, self._vlog = self._vlog, None
        if fh is None:
            return
        try:
            fh.flush()
            os.fsync(fh.fileno())   # a swap is followed by a restart, maybe a reboot
            fh.close()
        except OSError:
            pass

    # -- shell -------------------------------------------------------------
    def _run_cmd(self, cmd: list[str], cwd: Path | None = None,
                 timeout: float = 60.0) -> tuple[int, str]:
        """Blocking shell-out. Tests replace this with a recorder, so every
        privileged or slow step in this module has to go through it."""
        exe = shutil.which(cmd[0]) or cmd[0]
        try:
            r = subprocess.run([exe, *cmd[1:]], cwd=str(cwd) if cwd else None,
                               capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout:.0f}s"
        except OSError as exc:
            return 127, str(exc)

    def _spawn_detached(self, cmd: list[str]) -> bool:
        """Fire and forget — used for anything that outlives this process."""
        exe = shutil.which(cmd[0])
        if exe is None:
            self._v("UPD", "no %s (dev host?)" % cmd[0])
            return False
        try:
            subprocess.Popen([exe, *cmd[1:]])
            return True
        except OSError as exc:
            self._v("UPD", "spawn failed: %s" % exc)
            return False

    # -- steps -------------------------------------------------------------
    def _unpack(self, archive: Path) -> str:
        """Archive -> self.stage. Returns an i18n key on failure, "" on success."""
        size = archive.stat().st_size
        if size > MAX_ARCHIVE:
            return "err.update_too_big"
        self._v("UPD", "archive %s size=%d sha256=%s"
                % (archive.name, size, _sha256(archive)[:16]))
        try:
            if zipfile.is_zipfile(archive):
                return self._unpack_zip(archive)
            return self._unpack_tar(archive)
        except (tarfile.TarError, zipfile.BadZipFile, EOFError):
            return "err.update_bad_archive"

    def _prepare_stage(self) -> None:
        shutil.rmtree(self.stage, ignore_errors=True)
        self.stage.mkdir(parents=True)

    def _write_member(self, rel: str, data: bytes, mode: int = 0) -> None:
        p = self.stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mode & 0o111:            # keep the executable bit on deploy.sh & co
            p.chmod(0o755)

    def _unpack_zip(self, archive: Path) -> str:
        with zipfile.ZipFile(archive) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_MEMBERS:
                return "err.update_bad_archive"
            top = _strip_top([i.filename for i in z.infolist()])
            total = sum(i.file_size for i in infos)
            if total > MAX_UNPACKED:
                return "err.update_too_big"
            if (key := self._space(total)):
                return key
            self._prepare_stage()
            for i in infos:
                rel = _rel(i.filename, top)
                if rel is None:
                    self._v("UPD", "rejected member " + i.filename)
                    return "err.update_unsafe_member"
                mode = (i.external_attr >> 16) & 0o777
                self._write_member(rel, z.read(i), mode)
        return ""

    def _unpack_tar(self, archive: Path) -> str:
        with tarfile.open(archive, "r:*") as t:
            members = t.getmembers()
            if len(members) > MAX_MEMBERS:
                return "err.update_bad_archive"
            top = _strip_top([m.name for m in members])
            files = [m for m in members if m.isreg()]
            total = sum(m.size for m in files)
            if total > MAX_UNPACKED:
                return "err.update_too_big"
            if (key := self._space(total)):
                return key
            for m in members:
                # symlinks, hardlinks, devices and fifos have no business in a
                # source tree and every business in an exploit
                if not (m.isreg() or m.isdir()):
                    self._v("UPD", "rejected member %s (type %r)" % (m.name, m.type))
                    return "err.update_unsafe_member"
                if _rel(m.name, top) is None:
                    self._v("UPD", "rejected member " + m.name)
                    return "err.update_unsafe_member"
            self._prepare_stage()
            for m in files:
                fh = t.extractfile(m)
                if fh is None:
                    continue
                self._write_member(_rel(m.name, top), fh.read(), m.mode)
        return ""

    def _space(self, need: int) -> str:
        try:
            free = shutil.disk_usage(self.dest.parent).free
        except OSError:
            return ""
        return "" if free > need + FREE_MARGIN else "err.update_no_space"

    def _content_gate(self) -> str:
        for rel in REQUIRED:
            if not (self.stage / rel).is_file():
                self._v("UPD", "missing " + rel)
                return "err.update_incomplete"
        # A changed requirements.txt cannot be honoured here: pip needs PyPI and
        # the bike has no network. Letting it through would leave the service
        # with an ImportError, :80 dead, and only ssh to fix it.
        old = self.dest / "requirements.txt"
        if old.is_file() and old.read_bytes() != (self.stage / "requirements.txt").read_bytes():
            return "err.update_deps_changed"
        return ""

    def _link_runtime(self) -> None:
        """Lend .venv and bin to the staged tree for the test run.

        Symlinks, not a move: if the tests fail, the running service must still
        have its interpreter and its flasher exactly where they were.
        """
        for name in (".venv", "bin", "addons"):
            src = self.dest / name
            dst = self.stage / name
            if src.exists() and not dst.exists():
                try:
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    pass

    def _test(self) -> str:
        self._set_op("testing")
        py = str(self.py)
        if not Path(py).exists():
            self._append("[*] no interpreter at %s -- tests skipped" % py)
            return ""
        # The import is the gate that matters: it is the failure that takes the
        # web UI down and leaves no way in but ssh.
        self._append("[*] import app.main")
        rc, out = self._run_cmd([py, "-c", "import app.main"], cwd=self.stage,
                                timeout=IMPORT_TIMEOUT)
        self._v("TEST", "import rc=%d %s" % (rc, out.strip()[-2000:]))
        if rc != 0:
            self._append((out.strip().splitlines() or [""])[-1][:200])
            return "err.update_import_failed"
        tests = sorted((self.stage / "tests").glob("test_*.py"))
        with self._lock:
            self.total, self.done, self.percent = len(tests), 0, 0.0
        deadline = time.monotonic() + TEST_TIMEOUT
        for i, t in enumerate(tests, 1):
            left = deadline - time.monotonic()
            if left <= 0:
                return "err.update_tests_failed"
            rc, out = self._run_cmd([py, str(t.relative_to(self.stage))],
                                    cwd=self.stage, timeout=left)
            self._v("TEST", "%s rc=%d" % (t.name, rc))
            with self._lock:
                self.done = i
                self.percent = round(i * 100.0 / max(1, len(tests)), 1)
            if rc != 0:
                self._v("TEST", out.strip()[-4000:])
                self._append("[!] %s: %s" % (t.name,
                             (out.strip().splitlines() or [""])[-1][:200]))
                return "err.update_tests_failed"
        self._append("[+] tests passed: %d" % len(tests))
        return ""

    def _swap(self) -> str:
        """Move the runtime bits across, then exchange the two trees.

        Renames only — no copy anywhere, so the window in which neither tree is
        in place is two syscalls wide.
        """
        self._set_op("swapping")
        for name in (".venv", "bin", "addons"):
            link = self.stage / name
            if link.is_symlink():
                link.unlink()
            src = self.dest / name
            if src.exists():
                shutil.move(str(src), str(link))
        shutil.rmtree(self.prev, ignore_errors=True)
        try:
            os.rename(self.dest, self.prev)
            os.rename(self.stage, self.dest)
        except OSError as exc:
            self._v("UPD", "swap failed: %s" % exc)
            # put back whatever we moved, so a failed swap is not a dead board
            if not self.dest.exists() and self.prev.is_dir():
                try:
                    os.rename(self.prev, self.dest)
                except OSError:
                    pass
            return "err.update_swap_failed"
        return ""

    def _arm_rollback(self, info: dict) -> None:
        """Schedule the watchdog *before* the restart that might not come back."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / OK).unlink(missing_ok=True)
            (self.run_dir / PENDING).write_text(json.dumps(info, ensure_ascii=False))
        except OSError as exc:
            self._v("UPD", "marker failed: %s" % exc)
            return
        script = self.work_dir / "rollback.sh"
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            script.write_text(ROLLBACK_SH % {
                "dest": self.dest, "prev": self.prev,
                "ok": self.run_dir / OK, "pending": self.run_dir / PENDING})
            script.chmod(0o755)
        except OSError as exc:
            self._v("UPD", "watchdog script failed: %s" % exc)
            return
        if not self._spawn_detached(["systemd-run", "--on-active=%d" % ROLLBACK_AFTER,
                                     "--unit=onboard-logger-rollback",
                                     "/bin/sh", str(script)]):
            self._append("[*] the rollback watchdog is unavailable")

    def _restart(self) -> None:
        self._set_op("restarting")
        # Restarting from inside the request's own process kills the answer, so
        # the restart is always somebody else's job, a moment from now.
        if not self._spawn_detached(["systemd-run", "--on-active=2",
                                     "systemctl", "restart", self.service]):
            self._spawn_detached(["sh", "-c",
                                  "sleep 2; systemctl restart " + self.service])

    # -- the operation -----------------------------------------------------
    def _apply(self, archive: Path) -> None:
        self._vopen()
        self._v("UPD", "start dest=%s stage=%s" % (self.dest, self.stage))
        if self.diag is not None:
            try:
                self.diag.event("update_start", archive=archive.name)
            except Exception:      # noqa: BLE001 - diagnostics never take a ride down
                pass
        try:
            self._append("[*] unpacking " + archive.name)
            key = self._unpack(archive)
            if not key:
                key = self._content_gate()
            if key:
                shutil.rmtree(self.stage, ignore_errors=True)
                self._fail(key)
                return
            self._link_runtime()
            if (key := self._test()):
                shutil.rmtree(self.stage, ignore_errors=True)
                self._fail(key)
                return
            info = {"applied": time.time(), "archive": archive.name,
                    "sha256": _sha256(archive)[:32],
                    "version": _tree_version(self.stage)}
            if (key := self._swap()):
                self._fail(key)
                return
            _write_state(self.etc_dir, info)
            self._append("[+] tree swapped, previous: " + self.prev.name)
            self._arm_rollback(info)
            self._finish("ok", "restarting")
            self._restart()
        except Exception as exc:   # noqa: BLE001 - never leave op stuck at work
            self._v("UPD", "exception %s: %s" % (type(exc).__name__, exc))
            shutil.rmtree(self.stage, ignore_errors=True)
            self._fail("err.update_failed", str(exc))
        finally:
            try:
                archive.unlink(missing_ok=True)   # never keep an unpacked upload
            except OSError:
                pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _tree_version(root: Path) -> str:
    """BUILD (number + commit + branch + date) if the tree was stamped, else
    VERSION. Full detail, for the rollback record and the terminal -- never
    handed to the UI as-is; see `_release_version()` for that."""
    for name in ("BUILD", "VERSION"):
        try:
            text = (root / name).read_text().strip()
        except OSError:
            continue
        if text:
            return text
    return ""


def _release_version(root: Path) -> str:
    """Just the release number -- BUILD's first field, or the whole of VERSION.
    What Config → System shows: a rider reads it as "what firmware am I on",
    and a commit hash on screen answers a question nobody there is asking."""
    full = _tree_version(root)
    return full.split()[0] if full else ""
