"""Offline tests for the clock helpers: manual set + one auto sync per power-up.

Nothing is executed: ``system._run`` is replaced by a recorder and the marker
lives in a temp dir, so the tests assert on the commands that *would* run.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import system  # noqa: E402


def _fake(tmp, tz="Europe/Sofia", ntp=False):
    """Point the module at a temp marker and record commands instead of running."""
    system.AUTO_MARKER = Path(tmp) / "time-synced"
    system._auto_done_fallback = False
    system.shutil.which = lambda name: "/usr/bin/" + name   # dev host has no timedatectl
    system.ran = []
    system._run = lambda cmd: (system.ran.append(" ".join(cmd)), True)[1]
    system._run_err = lambda cmd: (system.ran.append(" ".join(cmd)), (True, ""))[1]
    system.time_status = lambda: {
        "epoch": time.time(), "local": "now", "tz": tz,
        "ntp_active": ntp, "ntp_synced": False,
    }
    system._timedatectl_props = lambda: {"NTP": "yes" if ntp else "no"}


def test_auto_sync_sets_clock_when_drifted():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp)
        res = system.auto_sync(time.time() + 3600, "Europe/Sofia")
        assert res["ok"] and res.get("skipped") is None
        assert any("set-time" in c for c in system.ran)
        assert system.auto_sync_done() is True          # this power-up is spent


def test_auto_sync_runs_once_per_power_up():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp)
        system.auto_sync(time.time() + 3600)
        system.ran.clear()
        res = system.auto_sync(time.time() + 7200)      # a second tab shows up
        assert res["skipped"] == "already"
        assert system.ran == []                         # clock left alone


def test_auto_sync_skips_when_already_on_time():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp)
        res = system.auto_sync(time.time() + 0.4, "Europe/Sofia")
        assert res["skipped"] == "in_sync" and res["ok"]
        assert not any("set-time" in c for c in system.ran)   # no mid-log jump
        assert system.auto_sync_done() is True                # but the shot is used


def test_auto_sync_adopts_timezone_even_when_in_sync():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp, tz="UTC")
        res = system.auto_sync(time.time(), "Europe/Sofia")
        assert res["skipped"] == "in_sync"
        assert "timedatectl set-timezone Europe/Sofia" in system.ran


def test_auto_sync_rejects_nonsense_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp)
        assert system.auto_sync(42)["error"] == "err.bad_time"
        assert system.auto_sync("tomorrow")["error"] == "err.bad_time"
        assert system.ran == []
        # a bad client must not use up the one sync this power-up gets
        assert system.auto_sync_done() is False
        assert system.auto_sync(time.time() + 3600)["ok"] is True


def test_manual_set_time_restores_ntp():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp, ntp=True)
        res = system.set_time(time.time() + 60, "Europe/Sofia")
        assert res["ok"]
        assert "timedatectl set-ntp false" in system.ran      # timesyncd must let go
        assert "timedatectl set-ntp true" in system.ran       # and is handed back
        assert system.ran.index("timedatectl set-ntp false") < \
               next(i for i, c in enumerate(system.ran) if "set-time" in c)


def test_marker_survives_without_writable_run():
    with tempfile.TemporaryDirectory() as tmp:
        _fake(tmp)
        system.AUTO_MARKER = Path("/proc/definitely/not/writable/marker")
        system.auto_sync(time.time() + 3600)
        assert system.auto_sync_done() is True    # in-memory fallback carries it


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
