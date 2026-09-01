"""Offline tests for the board diagnostics log (app/web/diag.py).

No hardware and no /dev: the sysfs and /dev roots are pointed at a temp tree, so
the health snapshot is exercised without an FTDI cable or a Wi-Fi dongle.

Run directly:  python tests/test_diag.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.logger import KLineWorker  # noqa: E402
from app.web.diag import DiagLog, parse_kmsg, usb_facts  # noqa: E402
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


def test_firmware_logs_stay_out_of_the_ride_log_list():
    """A flash log belongs to Config -> System, not to the Logs tab."""
    from app.main import _is_ride_log, _log_kind
    assert _is_ride_log("kline-dec-20260826-190010.csv")
    assert _is_ride_log("kline-20260819-090832.raw.log")
    assert _is_ride_log("diag-20260831-083650.log")
    assert _is_ride_log("scan-20260819-090832.ndjson")
    assert not _is_ride_log("fw-reading-20260901-204455.log")
    assert not _is_ride_log("notes.txt")
    assert _log_kind("diag-x.log") == "diag" and _log_kind("a.csv") == "decoded"


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
            if len(list(Path(tmp).glob("diag-*.log.zip"))) == 2:
                break
            time.sleep(0.1)
        zips = sorted(Path(tmp).glob("diag-*.log.zip"))
        logs = sorted(Path(tmp).glob("diag-*.log"))
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
        assert "Y" not in "".join(p.read_text() for p in Path(tmp).glob("diag-*.log"))


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


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
