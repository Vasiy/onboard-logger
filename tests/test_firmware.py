"""Firmware op forces both K-Line log streams off, then restores them, and
writes a verbose per-operation log that explains a failure afterwards."""

import os
import stat
import sys
import tempfile
import zipfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.ecu_id import DEFAULT_FIELDS, describe, parse_desc  # noqa: E402
from app.web.firmware import FirmwareBlocked, FirmwareManager  # noqa: E402
from app.web.state import State  # noqa: E402


class FakeWorker:
    def __init__(self, dec=True, raw=True):
        self._dec, self._raw = dec, raw
        self.calls = []

    def logging_state(self):
        return (self._dec, self._raw)

    def set_logging_decoded(self, on):
        self.calls.append(("dec", on)); self._dec = on

    def set_logging_raw(self, on):
        self.calls.append(("raw", on)); self._raw = on

    def request_pause(self, timeout=10.0):
        self.calls.append(("pause",)); return True

    def resume(self):
        self.calls.append(("resume",))


def test_write_desc_from_ecu_id():
    with tempfile.TemporaryDirectory() as tmp:
        st = State()
        st.set_ecu_id_raw(bytes(range(60)).hex())  # mostly non-printable -> blanks
        st.set_ecu_hw("IAW5AMHW610")
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", st)
        fm._write_desc("x.bin")
        p = Path(tmp) / "x.bin.txt"
        assert p.exists()
        raw = p.read_bytes()
        assert raw.startswith(b"Drawing:")
        assert b"Hardware: IAW5AMHW610" in raw   # hw falls back to the regex-parsed value
        assert raw.endswith(b"\r\n")             # GuzziDiag-style CRLF preserved on disk


def test_write_desc_skips_without_id():
    with tempfile.TemporaryDirectory() as tmp:
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State())
        fm._write_desc("y.bin")
        assert not (Path(tmp) / "y.bin.txt").exists()  # no ECU id -> no file


def test_desc_without_extra_is_unchanged():
    raw = bytes(range(60))
    text = describe(raw, "IAW5AMHW610", DEFAULT_FIELDS)
    assert text == describe(raw, "IAW5AMHW610", DEFAULT_FIELDS, extra=None)
    assert text == describe(raw, "IAW5AMHW610", DEFAULT_FIELDS, extra=[])
    assert "Brand" not in text          # nothing new leaks into an old-style dump


def test_desc_extra_lines_keep_the_guzzidiag_shape():
    text = describe(bytes(range(60)), "IAW5AMHW610", DEFAULT_FIELDS,
                    extra=[("Brand", "Moto Morini"), ("Model", ""), ("Image", "23ECCLGPSMD")])
    assert text.startswith("Drawing:") and text.endswith("\r\n")
    assert "Brand: Moto Morini" in text
    assert "Model:" not in text          # empty values are dropped, as in parse_fields
    assert text.index("Drawing:") < text.index("Brand:")   # appended, never prepended


def test_parse_desc_round_trips():
    text = describe(bytes(range(60)), "IAW5AMHW610", DEFAULT_FIELDS,
                    extra=[("Brand", "Moto Morini"), ("Model", "Granpasso 1200")])
    got = parse_desc(text)
    assert got["Hardware"] == "IAW5AMHW610"
    assert got["Brand"] == "Moto Morini" and got["Model"] == "Granpasso 1200"


def test_write_desc_records_the_bike():
    with tempfile.TemporaryDirectory() as tmp:
        st = State()
        st.set_ecu_id_raw(bytes(range(60)).hex())
        st.set_ecu_hw("IAW5AMHW610")
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", st,
                             describe_image=lambda n: [("Brand", "Moto Morini"),
                                                       ("Model", "Granpasso 1200"),
                                                       ("Image", "23ECCLGPSMD (5AM X0000)"),
                                                       ("Catalog", "verified")])
        fm._write_desc("x.bin")
        raw = (Path(tmp) / "x.bin.txt").read_bytes()
        assert raw.startswith(b"Drawing:") and raw.endswith(b"\r\n")
        assert b"Brand: Moto Morini" in raw and b"Image: 23ECCLGPSMD (5AM X0000)" in raw


def test_write_desc_falls_back_to_the_image_when_the_ecu_is_gone():
    with tempfile.TemporaryDirectory() as tmp:
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             describe_image=lambda n: [("Image", "23ECCLGPSMC")])
        fm._write_desc("z.bin")
        raw = (Path(tmp) / "z.bin.txt").read_bytes()
        assert b"Image: 23ECCLGPSMC" in raw   # a dump is still worth a passport


def test_guard_blocks_below_the_http_layer():
    """A direct start_write() must not be able to skip the check."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "other.bin").write_bytes(b"\x00" * 16)
        verdict = {"level": "block", "reason": "model_mismatch"}
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             guard=lambda name: verdict)
        try:
            fm.start_write("other.bin")
            raise AssertionError("start_write should have refused")
        except FirmwareBlocked as e:
            assert e.verdict["reason"] == "model_mismatch"
        assert fm.status()["op"] == "idle"    # nothing was started


def test_overridden_write_leaves_a_trace_in_the_operation_log():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "other.bin").write_bytes(b"\x00" * 16)
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             guard=lambda name: {"level": "warn", "reason": "model_mismatch",
                                                 "overridden": True})
        fm.start_write("other.bin")
        fm._thread.join(timeout=5)
        assert any("guard overridden: model_mismatch" in ln for ln in fm.status()["log"])


def _run_true(worker, tmp):
    fm = FirmwareManager(
        worker_getter=lambda: worker, util_path="/usr/bin/true",
        fw_dir=tmp, port="/dev/kline", state=State(),
    )
    fm._run(["/usr/bin/true"], confirm=False)  # synchronous
    return fm


def test_logs_forced_off_then_restored():
    with tempfile.TemporaryDirectory() as tmp:
        w = FakeWorker(dec=True, raw=True)
        fm = _run_true(w, tmp)
        c = w.calls
        # both streams disabled BEFORE the port is taken
        assert ("dec", False) in c and ("raw", False) in c
        assert c.index(("dec", False)) < c.index(("pause",))
        assert c.index(("raw", False)) < c.index(("pause",))
        # restored after, and resumed
        assert w._dec is True and w._raw is True
        assert ("resume",) in c
        assert c.index(("resume",)) > c.index(("pause",))
        assert fm.status()["result"] == "ok"


def test_restore_respects_prior_state():
    with tempfile.TemporaryDirectory() as tmp:
        w = FakeWorker(dec=True, raw=False)   # raw was off before
        _run_true(w, tmp)
        assert w._dec is True and w._raw is False  # exact prior intent restored


# -- verbose per-operation log --------------------------------------------
def _fake_util(tmp: Path, body: str, rc: int = 0) -> str:
    """A stand-in for 5am_util: prints what we tell it, exits with ``rc``."""
    p = tmp / "fake_util.sh"
    p.write_text("#!/bin/sh\n" + body + f"\nexit {rc}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _fake_dev(root: Path) -> str:
    dev = root / "dev"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "kline").write_text("")
    (dev / "ttyUSB0").write_text("")
    return str(dev)


def _run_util(tmp, body, rc=0, verbose=False, op="read", fw_size=0, diag=None):
    """Start a real (fake) subprocess through the manager and wait for it."""
    tmp = Path(tmp)
    logs = tmp / "logs"
    dev = _fake_dev(tmp)
    fm = FirmwareManager(
        worker_getter=lambda: None, util_path=_fake_util(tmp, body, rc),
        fw_dir=str(tmp), port=str(Path(dev) / "kline"), state=State(),
        log_dir=str(logs), dev_root=dev, sys_root=str(tmp / "sys"), fw_size=fw_size,
        diag=diag,
    )
    if op == "write":
        (tmp / "img.bin").write_bytes(b"\x5a" * 4096)
        fm.start_write("img.bin", verbose=verbose)
    else:
        fm.start_read("out.bin", verbose=verbose)
    fm._thread.join(timeout=20)
    return fm, logs


def test_finished_operation_log_is_archived_when_asked():
    """One switch in Config -> System archives both board log kinds."""
    with tempfile.TemporaryDirectory() as tmp:
        diag = type("D", (), {"zip_after": True, "event": staticmethod(lambda *a, **k: None)})()
        fm, logs = _run_util(tmp, 'printf "x" > "$2"', diag=diag)
        assert fm.status()["result"] == "ok"
        assert not list(logs.rglob("fw-*.log")), "the plain file is replaced by its archive"
        zips = list(logs.rglob("fw-*.log.zip"))
        assert len(zips) == 1, zips
        with zipfile.ZipFile(zips[0]) as z:
            assert "FW start op=reading" in z.read(z.namelist()[0]).decode()
        assert fm._vlog_path == zips[0], "the manager names the file that exists"


def test_verbose_log_records_the_whole_operation():
    with tempfile.TemporaryDirectory() as tmp:
        fm, logs = _run_util(
            tmp, 'echo "[+] reading block 1"; printf "x" > "$2"; echo "[+] done"')
        assert fm.status()["result"] == "ok"
        files = list(logs.rglob("fw-reading-*.log"))
        assert len(files) == 1, files
        text = files[0].read_text()
        assert "FW start op=reading name=out.bin" in text
        assert "FW cmd " in text and " -v" in text      # -v is always passed
        assert "USB node=" in text                      # adapter facts from sysfs
        assert "UTIL [+] reading block 1" in text
        assert "FW exit rc=0" in text and "FW end result=ok" in text
        assert fm.status()["log_file"] == files[0].name


def test_failure_reason_is_in_the_file_and_the_ui():
    with tempfile.TemporaryDirectory() as tmp:
        fm, logs = _run_util(tmp, 'echo "chatter"; echo "ERROR: no reply from ECU"',
                             rc=3, op="write")
        assert fm.status()["result"] == "error"
        text = next(iter(logs.rglob("fw-writing-*.log"))).read_text()
        assert "UTIL ERROR: no reply from ECU" in text
        assert "FW exit rc=3" in text and "FW end result=error" in text
        assert "FW image size=4096 sha256=" in text     # what was about to be flashed
        # the failing line reaches the UI list even with the verbose box unticked
        assert any("no reply from ECU" in ln for ln in fm.status()["log"])
        assert not any(ln == "chatter" for ln in fm.status()["log"])


def test_zero_exit_with_an_error_line_is_still_a_failure():
    """5am_util exits 0 after "ERROR: ioctl: Bad file descriptor" (main.c:494),
    which used to be reported as a successful read."""
    with tempfile.TemporaryDirectory() as tmp:
        fm, logs = _run_util(
            tmp, 'printf "\\033[31m[!] ERROR: \\033[0mioctl: Bad file descriptor\\n"',
            rc=0)
        assert fm.status()["result"] == "error"
        assert "ioctl: Bad file descriptor" in fm.status()["progress"]
        text = next(iter(logs.rglob("fw-reading-*.log"))).read_text()
        assert "\x1b[" not in text          # ANSI colour stripped before logging
        assert "FW verdict " in text


def test_short_read_is_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        body = 'printf "%s" "half" > "$2"; echo "[+] done"'   # $2 == the -o path
        fm, _logs = _run_util(tmp, body, fw_size=327680)
        assert fm.status()["result"] == "error"
        assert "!= 327680" in fm.status()["progress"]


def test_full_read_passes_the_size_check():
    with tempfile.TemporaryDirectory() as tmp:
        body = 'dd if=/dev/zero of="$2" bs=1024 count=4 2>/dev/null; echo "[+] done"'
        fm, _logs = _run_util(tmp, body, fw_size=4096)
        assert fm.status()["result"] == "ok", fm.status()["progress"]


def test_missing_output_is_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        fm, _logs = _run_util(tmp, 'echo "[+] pretending"')
        assert fm.status()["result"] == "error"
        assert "не создан" in fm.status()["progress"]


def test_verbose_box_streams_everything_to_the_ui():
    with tempfile.TemporaryDirectory() as tmp:
        fm, _logs = _run_util(tmp, 'echo "chatter"; printf "x" > "$2"', verbose=True)
        assert any(ln == "chatter" for ln in fm.status()["log"])


def test_port_state_tracks_the_node():
    with tempfile.TemporaryDirectory() as tmp:
        fm, _logs = _run_util(tmp, 'printf "x" > "$2"')
        assert fm._port_state()[0] is True
        Path(fm.port).unlink()
        assert fm._port_state()[0] is False       # a vanished adapter is visible


def test_operation_log_has_a_size_cap():
    with tempfile.TemporaryDirectory() as tmp:
        fm, logs = _run_util(tmp, 'printf "x" > "$2"')
        fm._vlog = (logs / "cap.log").open("a", buffering=1)
        fm._vlog_bytes = 0
        try:
            import app.web.firmware as fwmod
            cap, fwmod.FW_LOG_MAX = fwmod.FW_LOG_MAX, 200
            for i in range(200):
                fm._v("UTIL", "x" * 50)
        finally:
            fwmod.FW_LOG_MAX = cap
            fm._vclose()
        assert (logs / "cap.log").stat().st_size < 1000
        assert "truncated" in (logs / "cap.log").read_text()


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
