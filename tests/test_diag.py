"""Offline tests for the board diagnostics log (app/web/diag.py).

No hardware and no /dev: the sysfs and /dev roots are pointed at a temp tree, so
the health snapshot is exercised without an FTDI cable or a Wi-Fi dongle.

Run directly:  python tests/test_diag.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.logger import KLineWorker  # noqa: E402
from app.web.diag import DiagLog, parse_kmsg, usb_facts  # noqa: E402
from app.web.storage import day_name  # noqa: E402
from app.web.led import Led  # noqa: E402
from app.web.state import State  # noqa: E402

PARAMS = str(Path(__file__).resolve().parent.parent / "config" / "params.json")

# the two kernel records that explained the 2026-08-26 mid-ride log splits
KMSG_HUB = b"4,1234,987654321,-;usb usb2-port1: disabled by hub (EMI?), re-enabling...\n"
KMSG_FTDI = b"6,1235,987654999,-;ftdi_sio ttyUSB0: FTDI USB Serial Device converter now disconnected from ttyUSB0\n"
KMSG_NOISE = b"6,1236,987655999,-;EXT4-fs (mmcblk0p9): re-mounted r/w\n"


def _fake_board(root: Path) -> tuple[str, str]:
    """Build a minimal /sys + /dev tree: one USB device, wlan0 up, 48.2 C."""
    sysd, devd = root / "sys", root / "dev"
    (sysd / "bus" / "usb" / "devices" / "2-1.1").mkdir(parents=True)
    (sysd / "bus" / "usb" / "devices" / "usb2").mkdir(parents=True)
    net = sysd / "class" / "net" / "wlan0"
    net.mkdir(parents=True)
    (net / "operstate").write_text("up\n")
    th = sysd / "class" / "thermal" / "thermal_zone0"
    th.mkdir(parents=True)
    (th / "temp").write_text("48200\n")
    devd.mkdir(parents=True)
    (devd / "kline").write_text("")
    (devd / "ttyUSB0").write_text("")
    return str(sysd), str(devd)


def _diag(tmp, root, **cfg):
    sysd, devd = _fake_board(Path(root))
    c = {"enabled": True, "interval_s": 1, "max_mb": 1, "keep": 3, "kmsg": False}
    c.update(cfg)
    return DiagLog(tmp, c, sys_root=sysd, dev_root=devd)


def test_kmsg_filter():
    assert "disabled by hub" in parse_kmsg(KMSG_HUB)
    assert "ttyUSB0" in parse_kmsg(KMSG_FTDI)
    assert parse_kmsg(KMSG_NOISE) is None          # not about the hardware we watch
    assert parse_kmsg(b"garbage without a header") is None


def test_event_line():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d.event("link_down", err="SerialException", up=231.4, tty=0)
        text = Path(d.current_file()).read_text()
        assert "LINK_DOWN" in text
        assert "err=SerialException" in text and "up=231.4" in text and "tty=0" in text


def test_kmsg_line_keeps_its_spaces():
    """The kernel text must stay readable — it is evidence, not a field."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d._write("KMSG", {}, raw=parse_kmsg(KMSG_HUB))
        assert "KMSG usb usb2-port1: disabled by hub (EMI?)" in Path(d.current_file()).read_text()


def test_usb_facts_resolves_the_adapter():
    """What a firmware log needs to name the cable: chip, serial, hub port."""
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        sysd, devd = root / "sys", root / "dev"
        iface = sysd / "devices" / "usb2" / "2-1" / "2-1.1" / "2-1.1:1.0"
        iface.mkdir(parents=True)
        usb = iface.parent
        for f, v in (("idVendor", "0403"), ("idProduct", "6001"),
                     ("serial", "A50285BI"), ("product", "FT232R USB UART"),
                     ("speed", "12")):
            (usb / f).write_text(v + "\n")
        (usb.parent / "product").write_text("Generic USB Hub\n")
        (iface / "latency_timer").write_text("1\n")
        drv = sysd / "bus" / "usb-serial" / "drivers" / "ftdi_sio"
        drv.mkdir(parents=True)
        (iface / "driver").symlink_to(drv)
        tty = sysd / "class" / "tty" / "ttyUSB0"
        tty.mkdir(parents=True)
        (tty / "device").symlink_to(iface)
        devd.mkdir()
        (devd / "ttyUSB0").write_text("")
        (devd / "kline").symlink_to(devd / "ttyUSB0")

        f = usb_facts(str(devd / "kline"), sysd, devd)
        assert f["tty"] == "ttyUSB0" and f["present"] == 1
        assert f["vid"] == "0403" and f["pid"] == "6001" and f["serial"] == "A50285BI"
        assert f["usbpath"] == "2-1.1"            # port 1 of the hub on bus 2
        assert f["driver"] == "ftdi_sio" and f["latency_ms"] == "1"
        assert f["upstream"] == "2-1" and f["upstream_product"] == "Generic USB Hub"


def test_usb_facts_survives_a_missing_adapter():
    with tempfile.TemporaryDirectory() as root:
        f = usb_facts(str(Path(root) / "dev" / "kline"), root, root)
        assert f["present"] == 0 and "vid" not in f


def test_log_kinds_split_rides_from_board_artefacts():
    """A flash or diagnostics log belongs to Config -> System, not to the Logs tab."""
    from app.main import KIND_GROUPS, _is_log, _log_kind
    assert _log_kind("kline-dec-20260826-190010.csv") == "decoded"
    assert _log_kind("kline-20260819-090832.raw.log") == "raw"
    assert _log_kind("scan-20260819-090832.ndjson") == "raw"
    assert _log_kind("diag-20260831-083650.log") == "diag"
    assert _log_kind("fw-reading-20260901-204455.log") == "fw"
    assert not _is_log("notes.txt")
    for k in ("decoded", "raw"):
        assert k in KIND_GROUPS["ride"] and k not in KIND_GROUPS["board"]
    for k in ("diag", "fw"):
        assert k in KIND_GROUPS["board"] and k not in KIND_GROUPS["ride"]


def test_board_log_list_is_newest_first_and_keeps_archives():
    """Config -> System asks for kind=board: diag + fw, day folders and archives."""
    import asyncio

    import app.main as m
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / day_name()).mkdir()
        files = ["diag-20260901-100000.log", "diag-20260901-090000.log.zip",
                 "fw-reading-20260901-204455.log", "kline-dec-1.csv",
                 f"{day_name()}/diag-20260902-080000.log"]
        for i, n in enumerate(files):
            (d / n).write_text("x")
            os.utime(d / n, (1000 + i, 1000 + i))
        old_cm, old_storage = m.cm, m.storage
        m.cm = type("C", (), {"load": staticmethod(lambda: {"log_dir": tmp})})()
        m.storage = None
        try:
            board = [f["name"] for f in asyncio.run(m.list_logs(kind="board"))["files"]]
            ride = [f["name"] for f in asyncio.run(m.list_logs(kind="ride"))["files"]]
            fw = [f["name"] for f in asyncio.run(m.list_logs(kind="fw"))["files"]]
        finally:
            m.cm, m.storage = old_cm, old_storage
        assert board[0] == files[4], board          # newest first, day folder included
        assert files[1] in board, "a rotated archive is still a board log"
        assert ride == ["kline-dec-1.csv"], ride
        assert fw == ["fw-reading-20260901-204455.log"], fw


def test_stop_archives_the_open_file_when_asked():
    """diag.zip_after closes the ride's file into a .zip instead of leaving it loose."""
    with tempfile.TemporaryDirectory() as tmp:
        d = DiagLog(tmp, probe=lambda: {}, cfg={"interval_s": 0.05, "zip_after": True})
        d.start()
        d.event("hello")
        d.stop()
        # the archive thread writes the .zip first and only then unlinks the
        # original, so wait on the original being gone, not on the zip appearing
        for _ in range(60):
            if not list(Path(tmp).rglob("diag-*.log")):
                break
            time.sleep(0.05)
        assert list(Path(tmp).rglob("diag-*.log.zip")), "zip_after must archive on stop"
        assert not list(Path(tmp).rglob("diag-*.log")), "the plain file is gone"


def test_health_snapshot():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root, probe=lambda: None)
        h = d.health()
        assert h["kline"] == 1 and h["tty"] == 1
        assert h["usb"] == 1                        # root hubs are not counted
        assert h["wlan0"] == "up"
        assert abs(h["temp"] - 48.2) < 0.01


def test_health_reports_missing_hardware():
    """The state the board is in right now with the cable out: absent, not crashed."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        (Path(root) / "dev" / "kline").unlink()
        (Path(root) / "sys" / "class" / "net" / "wlan0" / "operstate").unlink()
        h = d.health()
        assert h["kline"] == 0 and h["wlan0"] == "absent"


def test_probe_fields_and_failure():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d._probe = lambda: {"link": "connected", "to": 3}
        assert d.health()["link"] == "connected" and d.health()["to"] == 3
        d._probe = lambda: 1 / 0                    # a broken probe must not bite
        assert d.health()["kline"] == 1


def test_rotation_archives_and_prunes():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root, max_mb=0.05, keep=2)   # ~52 KiB per file
        for i in range(2500):
            d.event("hlt_stub", i=i, pad="x" * 40)
        for _ in range(50):        # zipping and pruning run in their own threads
            if len(list(Path(tmp).rglob("diag-*.log.zip"))) == 2:
                break
            time.sleep(0.1)
        zips = sorted(Path(tmp).rglob("diag-*.log.zip"))
        logs = sorted(Path(tmp).rglob("diag-*.log"))
        assert len(zips) >= 2, zips
        assert len(zips) <= 2, "keep=2 must prune older archives"
        assert len(logs) == 1, "exactly one open file after a rotation"
        assert Path(d.current_file()).stat().st_size < 60 * 1024


def test_disabled_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root, enabled=False)
        d.start()
        d.event("link_down", err="x")
        assert list(Path(tmp).iterdir()) == []


def test_apply_toggles_live():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root, enabled=False)
        d.apply({"enabled": True, "interval_s": 1, "max_mb": 4, "keep": 5, "kmsg": False})
        assert d.enabled and d.max_bytes == 4 * 1024 * 1024 and d.keep == 5
        d.event("x")
        assert d.current_file()
        d.apply({"enabled": False})
        assert not d.enabled
        d.event("y")                                # ignored while off
        assert "Y" not in "".join(p.read_text() for p in Path(tmp).rglob("diag-*.log"))


def test_file_lands_in_the_day_folder():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d.event("x")
        path = Path(d.current_file())
        assert path.parent.name == day_name(), path
        assert path.parent.parent == Path(tmp), "one level of date folder, no more"


def test_log_dir_may_be_a_callable():
    """The stick can be picked mid-session; the next file must follow it."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        where = [tmp]
        sysd, devd = _fake_board(Path(root))
        d = DiagLog(lambda: where[0], {"enabled": True, "interval_s": 1, "kmsg": False},
                    sys_root=sysd, dev_root=devd)
        d.event("x")
        assert Path(d.current_file()).parent.parent == Path(tmp)
        with tempfile.TemporaryDirectory() as other:
            where[0] = other
            with d._lock:
                d._close_locked(zip_it=False)
            d.event("y")
            assert Path(d.current_file()).parent.parent == Path(other)


def test_idle_stop_does_not_create_a_file():
    """stop() runs after every ride now — it must not leave a STOP-only file."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d.stop()
        d.stop()
        assert list(Path(tmp).rglob("diag-*")) == []
        d.start()
        d.event("x")
        d.stop()
        d.stop()                            # a second stop is a no-op, not a file
        assert len(list(Path(tmp).rglob("diag-*.log"))) == 1


def test_worker_reports_link_events():
    """The worker's own hooks: file open/close carry the reason for a split."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        w = KLineWorker(port="/dev/null", params_path=PARAMS, log_dir=tmp,
                        state=State(), led=Led(), diag=d)
        w._open_dec()
        w._write_decoded({"rpm": 3000})
        w._close_dec("link_lost")
        text = Path(d.current_file()).read_text()
        assert "LOG_OPEN" in text
        assert "LOG_CLOSE" in text and "rows=1" in text and "reason=link_lost" in text


def test_worker_stats_drain():
    with tempfile.TemporaryDirectory() as tmp:
        w = KLineWorker(port="/dev/null", params_path=PARAMS, log_dir=tmp,
                        state=State(), led=Led())
        w._stat = {"ok": 7, "timeout": 2, "bad_frame": 1}
        s = w.stats()
        assert s["ok"] == 7 and s["to"] == 2 and s["bad"] == 1
        assert w.stats()["ok"] == 0, "the tally is per interval, so it resets"


def test_zip_after_is_off_by_default():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        assert d.zip_after is False
        d.start()
        d.event("x")
        d.stop()
        assert len(list(Path(tmp).rglob("diag-*.log"))) == 1
        assert not list(Path(tmp).rglob("diag-*.log.zip"))


def test_apply_carries_zip_after():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
        d = _diag(tmp, root)
        d.apply({"enabled": True, "interval_s": 1, "max_mb": 2, "keep": 3,
                 "kmsg": False, "zip_after": True})
        assert d.zip_after is True


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
