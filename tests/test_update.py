"""Updating the board from an uploaded archive.

The gates are the point of this feature, so most of what is checked here is a
refusal: an archive that would escape the tree, one that changes dependencies
the bike cannot install, one whose tests fail — and, when the swap does happen,
that .venv and bin/5am_util survive it and the watchdog can put the old tree
back with them.
"""

import io
import json
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import update as U  # noqa: E402
from app.web.state import State  # noqa: E402
from app.web.update import UpdateBusy, UpdateManager, confirm_boot  # noqa: E402

TREE = {
    "app/main.py": "print('main')\n",
    "app/static/app.js": "// app\n",
    "app/static/i18n.js": "// i18n\n",
    "app/static/index.html": "<html></html>\n",
    "requirements.txt": "fastapi\nuvicorn\n",
    "config/config.default.json": '{"log_dir": "/root/k-line"}\n',
}


def _make_tree(root: Path, files=None, venv=True) -> Path:
    for rel, text in (files or TREE).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if venv:
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (root / "bin").mkdir(exist_ok=True)
        (root / "bin" / "5am_util").write_text("ELF")
    return root


def _tar(path: Path, files: dict, top: str = "", extra=None) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo((top + rel) if top else rel)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        for info in (extra or []):
            t.addfile(info)
    return path


def _zip(path: Path, files: dict, top: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr((top + rel) if top else rel, text)
    return path


def _mgr(tmp: Path, state=None, py=None, fwm=None) -> UpdateManager:
    dest = _make_tree(tmp / "opt" / "onboard-logger")
    m = UpdateManager(dest=dest, state=state or State(), log_dir=tmp / "logs",
                      py=py or (tmp / "nope"), fwm_getter=lambda: fwm,
                      etc_dir=tmp / "etc", run_dir=tmp / "run",
                      work_dir=tmp / "opt" / "updates")
    m.spawned = []
    m._spawn_detached = lambda cmd: (m.spawned.append(cmd), True)[1]
    return m


def _apply(m: UpdateManager, archive: Path) -> dict:
    """Run the operation inline — no thread, so the assertions see the result."""
    m._apply(archive)
    return m.status()


# -- unpacking -------------------------------------------------------------
def test_github_zip_wrapper_folder_is_stripped():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        arc = _zip(tmp / "a.zip", TREE, top="onboard-logger-main/")
        assert m._unpack(arc) == ""
        assert (m.stage / "app" / "main.py").is_file()   # not .../onboard-logger-main/app
        assert not (m.stage / "onboard-logger-main").exists()


def test_tar_without_a_wrapper_folder_also_works():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        assert m._unpack(_tar(tmp / "a.tar.gz", TREE)) == ""
        assert (m.stage / "requirements.txt").is_file()


def test_member_escaping_the_tree_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        arc = _zip(tmp / "bad.zip", {**TREE, "../../etc/passwd": "root::0:0\n"})
        assert m._unpack(arc) == "err.update_unsafe_member"
        assert not (tmp / "etc" / "passwd").exists()


def test_absolute_member_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        arc = _zip(tmp / "bad.zip", {**TREE, "/etc/shadow": "x\n"})
        assert m._unpack(arc) == "err.update_unsafe_member"


def test_symlink_member_is_refused():
    """A source tree has no symlinks; an exploit very much does."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        link = tarfile.TarInfo("app/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
        arc = _tar(tmp / "bad.tar.gz", TREE, extra=[link])
        assert m._unpack(arc) == "err.update_unsafe_member"


def test_a_file_that_is_not_an_archive_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        junk = tmp / "x.tar.gz"
        junk.write_bytes(b"not an archive at all")
        assert m._unpack(junk) == "err.update_bad_archive"


# -- content gate ----------------------------------------------------------
def test_incomplete_archive_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        short = {k: v for k, v in TREE.items() if k != "app/main.py"}
        assert m._unpack(_tar(tmp / "a.tar.gz", short)) == ""
        assert m._content_gate() == "err.update_incomplete"


def test_changed_requirements_are_refused():
    """pip needs PyPI; the bike has no network. Letting this through would leave
    the service with an ImportError and :80 dead."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp)
        changed = {**TREE, "requirements.txt": "fastapi\nuvicorn\npandas\n"}
        assert m._unpack(_tar(tmp / "a.tar.gz", changed)) == ""
        assert m._content_gate() == "err.update_deps_changed"


# -- the busy gate ---------------------------------------------------------
class _Fwm:
    def __init__(self, op):
        self.op = op


def test_refused_while_the_flasher_runs():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, fwm=_Fwm("writing"))
        assert m.busy_reason() == "err.update_fw_busy"
        try:
            m.start(tmp / "a.tar.gz")
        except UpdateBusy as e:
            assert str(e) == "err.update_fw_busy"
        else:
            raise AssertionError("a write in flight must block an update")


def test_refused_while_a_log_is_open():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        st = State()
        st.set_decoded_file("06-09-2026/ride.csv", 10)
        m = _mgr(tmp, state=st)
        assert m.busy_reason() == "err.update_logging"


def test_armed_logging_with_no_open_file_does_not_block():
    """Armed with the ignition off writes nothing — and that is exactly when a
    rider stands next to the bike and updates it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        st = State()
        st.set_decoded_armed(True)
        assert _mgr(Path(tmp), state=st).busy_reason() == ""


def test_refused_while_scanning():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        st = State()
        st.set_scan(True, 1, 2, "scan.csv")
        m = _mgr(tmp, state=st)
        assert m.busy_reason() == "err.update_scanning"


def test_idle_board_is_not_busy():
    with tempfile.TemporaryDirectory() as tmp:
        assert _mgr(Path(tmp)).busy_reason() == ""


# -- tests, swap, rollback -------------------------------------------------
def _py_stub(tmp: Path, rc_map: dict) -> Path:
    """A fake interpreter: exits per argument, so a test run can be scripted."""
    p = tmp / "py"
    body = ["#!/bin/sh"]
    for needle, rc in rc_map.items():
        body.append(f'case "$*" in *{needle}*) echo "{needle} says {rc}"; exit {rc};; esac')
    body.append("exit 0")
    p.write_text("\n".join(body) + "\n")
    p.chmod(0o755)
    return p


def test_a_tree_that_does_not_import_never_reaches_the_swap():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {"import app.main": 1}))
        st = _apply(m, _tar(tmp / "a.tar.gz", TREE))
        assert st["result"] == "error"
        assert st["progress"].endswith("err.update_import_failed")
        assert not m.stage.exists()                     # staging cleaned up
        assert (m.dest / "app" / "main.py").is_file()   # running tree untouched
        assert not m.prev.exists()                      # nothing was swapped


def test_a_failing_offline_suite_never_reaches_the_swap():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files = {**TREE, "tests/test_x.py": "raise SystemExit(1)\n"}
        m = _mgr(tmp, py=_py_stub(tmp, {"test_x.py": 1}))
        st = _apply(m, _tar(tmp / "a.tar.gz", files))
        assert st["result"] == "error"
        assert not m.prev.exists()
        assert (m.dest / "bin" / "5am_util").is_file()


def test_a_good_archive_swaps_and_keeps_venv_and_util():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files = {**TREE, "app/main.py": "print('new')\n", "VERSION": "v2\n",
                 "tests/test_x.py": "print('ok')\n"}
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        st = _apply(m, _tar(tmp / "a.tar.gz", files))
        assert st["result"] == "ok", st["log"]
        assert (m.dest / "app" / "main.py").read_text() == "print('new')\n"
        # the two things that live in DEST, outside git, and cannot be rebuilt here
        assert (m.dest / "bin" / "5am_util").read_text() == "ELF"
        assert (m.dest / ".venv" / "bin" / "python").is_file()
        assert not (m.dest / ".venv").is_symlink()      # moved, not left as a link
        assert (m.prev / "app" / "main.py").read_text() == "print('main')\n"
        assert m.version()["version"] == "v2"
        assert json.loads((tmp / "etc" / "update.json").read_text())["version"] == "v2"


def test_an_installed_addon_and_its_files_cross_the_swap():
    """addons/ is the third thing inside DEST that no release ever ships: the
    add-on was installed on the board and the files under it are the rider's."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        store = m.dest / "addons" / "maps" / "data"
        store.mkdir(parents=True)
        (store / "shared.xdf").write_text("<XDFFORMAT/>")
        (m.dest / "addons" / "maps" / "addon.json").write_text('{"name":"maps"}')

        st = _apply(m, _tar(tmp / "a.tar.gz", {**TREE, "VERSION": "v2\n"}))
        assert st["result"] == "ok", st["log"]
        assert (m.dest / "addons" / "maps" / "data" / "shared.xdf").read_text() == "<XDFFORMAT/>"
        assert not (m.dest / "addons").is_symlink()     # moved, not left as a link
        assert not (m.prev / "addons").exists()


def test_a_stamped_build_outranks_the_bare_release_number():
    """VERSION is the tracked number; BUILD adds the commit, the branch and the
    date and is what deploy.sh / release.sh leave in an installed tree -- the
    full string `_tree_version()` returns for the rollback record and the
    terminal."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "t").mkdir()
        (tmp / "t" / "VERSION").write_text("0.2.0\n")
        assert U._tree_version(tmp / "t") == "0.2.0"
        (tmp / "t" / "BUILD").write_text("0.2.0 66f5b2d main 2026-09-06 12:33\n")
        assert U._tree_version(tmp / "t") == "0.2.0 66f5b2d main 2026-09-06 12:33"


def test_the_ui_never_sees_the_commit_behind_a_release():
    """Config -> System shows what a rider reads as "what firmware am I on" --
    the release number alone. A stamped BUILD still carries the commit, branch
    and date, but `.version()` -- what /api/update hands #updVersion -- trims to
    just the first field, whether or not the tree was stamped."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        files = {**TREE, "VERSION": "0.2.0\n",
                 "BUILD": "0.2.0 66f5b2d main 2026-09-06 12:33\n"}
        assert _apply(m, _tar(tmp / "a.tar.gz", files))["result"] == "ok"
        assert m.version()["version"] == "0.2.0"
        assert "66f5b2d" not in m.version()["version"]
        (m.dest / "BUILD").unlink()          # a bare source tree, never stamped
        assert m.version()["version"] == "0.2.0"


def test_the_swap_arms_the_watchdog_and_restarts_detached():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        _apply(m, _tar(tmp / "a.tar.gz", TREE))
        assert (tmp / "run" / U.PENDING).is_file()
        assert not (tmp / "run" / U.OK).exists()
        joined = [" ".join(c) for c in m.spawned]
        assert any("rollback.sh" in c and "systemd-run" in c for c in joined), joined
        # the restart must outlive this process, or the answer never goes out
        assert any("restart onboard-logger" in c for c in joined), joined
        assert all("systemd-run" in c or c.startswith("sh -c") for c in joined)


def test_the_uploaded_archive_is_never_kept():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        arc = _tar(tmp / "a.tar.gz", TREE)
        _apply(m, arc)
        assert not arc.exists()


def test_startup_confirmation_clears_the_pending_marker():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run, etc = tmp / "run", tmp / "etc"
        run.mkdir()
        (run / U.PENDING).write_text(json.dumps({"version": "v2"}))
        assert confirm_boot(run, tmp / "dest", etc) is True
        assert (run / U.OK).is_file()
        assert not (run / U.PENDING).exists()
        assert json.loads((etc / "update.json").read_text())["confirmed"] > 0
        assert confirm_boot(run, tmp / "dest", etc) is False   # nothing pending now


def test_the_rollback_script_restores_the_old_tree_with_its_venv():
    """The watchdog runs with nothing but sh, after the service was restarted
    into code that may not import — so it is exercised as a real script."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        _apply(m, _tar(tmp / "a.tar.gz", {**TREE, "app/main.py": "print('new')\n"}))
        script = tmp / "opt" / "updates" / "rollback.sh"
        assert script.is_file()
        # no OK marker was ever written -> the new code never came up
        body = script.read_text().replace("systemctl restart onboard-logger", "true")
        (tmp / "rb.sh").write_text(body)
        assert os.system("/bin/sh %s" % (tmp / "rb.sh")) == 0
        assert (m.dest / "app" / "main.py").read_text() == "print('main')\n"
        assert (m.dest / "bin" / "5am_util").read_text() == "ELF"   # carried back
        assert (m.dest / ".venv" / "bin" / "python").is_file()
        assert not (tmp / "run" / U.PENDING).exists()


def test_the_rollback_script_does_nothing_once_the_new_code_confirmed():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = _mgr(tmp, py=_py_stub(tmp, {}))
        _apply(m, _tar(tmp / "a.tar.gz", {**TREE, "app/main.py": "print('new')\n"}))
        confirm_boot(tmp / "run", m.dest, tmp / "etc")
        script = tmp / "opt" / "updates" / "rollback.sh"
        body = script.read_text().replace("systemctl restart onboard-logger", "true")
        (tmp / "rb.sh").write_text(body)
        assert os.system("/bin/sh %s" % (tmp / "rb.sh")) == 0
        assert (m.dest / "app" / "main.py").read_text() == "print('new')\n"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
