"""Finding the clock that knows the date, on a board carrying two.

The tree below is this NanoPi NEO3 verbatim, read off it on 2026-09-19:

    rtc0  "rk808-rtc rk808-rtc.4.auto"  i2c-1 1-0018  hctosys=1  -> 2016-01-21
    rtc1  "rtc-ds1307 0-0068"           i2c-0 0-0068  hctosys=0  -> 2026-09-19
    /dev/rtc -> rtc0

Both are on i2c, which is the whole reason the driver name decides and the bus
does not: "external means i2c" would pick the PMIC's batteryless clock, and the
board would keep coming up in 2016 while the UI claimed it had a hardware RTC.

Run directly:  python tests/test_rtc.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web import rtc  # noqa: E402
from app.web import system  # noqa: E402
from app.web.config_mgr import ConfigError, ConfigManager  # noqa: E402

DEFAULTS = json.loads((ROOT / "config" / "config.default.json").read_text())


def _tree(tmp, entries):
    """Build a /sys/class/rtc stand-in. `entries` is [(dev, name, bus_path, hctosys)]."""
    root = Path(tmp) / "sys" / "class" / "rtc"
    root.mkdir(parents=True)
    for dev, name, bus_path, hctosys in entries:
        d = root / dev
        d.mkdir()
        (d / "name").write_text(name + "\n")
        (d / "hctosys").write_text(("1" if hctosys else "0") + "\n")
        real = Path(tmp) / bus_path.lstrip("/")
        real.mkdir(parents=True, exist_ok=True)
        (d / "device").symlink_to(real)
    rtc.RTC_ROOT = root
    rtc.DEV_ROOT = Path(tmp) / "dev"
    return root


NEO3 = [
    ("rtc0", "rk808-rtc rk808-rtc.4.auto",
     "sys/devices/platform/ff160000.i2c/i2c-1/1-0018/rk808-rtc.4.auto", True),
    ("rtc1", "rtc-ds1307 0-0068",
     "sys/devices/platform/ff150000.i2c/i2c-0/0-0068", False),
]


def test_the_module_wins_over_the_pmic_clock_on_the_same_bus():
    with tempfile.TemporaryDirectory() as tmp:
        _tree(tmp, NEO3)
        found = {d["dev"]: d for d in rtc.devices()}
        assert found["rtc0"]["name"] == "rk808-rtc" and found["rtc0"]["bus"] == "i2c-1"
        assert found["rtc1"]["name"] == "rtc-ds1307" and found["rtc1"]["bus"] == "i2c-0"
        assert found["rtc0"]["external"] is False and found["rtc0"]["onboard"] is True
        assert found["rtc1"]["external"] is True
        # hctosys belongs to the wrong one, and is reported rather than believed
        assert found["rtc0"]["hctosys"] is True and found["rtc1"]["hctosys"] is False
        assert rtc.pick({})["dev"] == "rtc1"
        st = rtc.status({})
        assert st["available"] and st["selected"] == "rtc1" and st["bus"] == "i2c-0"


def test_a_board_with_only_a_soc_clock_offers_nothing():
    """Every board has *an* RTC. Offering the choice would mean offering a liar."""
    with tempfile.TemporaryDirectory() as tmp:
        _tree(tmp, [NEO3[0]])
        assert rtc.pick({}) is None
        assert rtc.status({})["available"] is False


def test_an_unlisted_chip_is_listed_but_not_trusted_until_it_is_named():
    """A name whitelist fails closed on a module nobody has heard of, so config
    can name one -- and that override wins over the whitelist, not beside it."""
    with tempfile.TemporaryDirectory() as tmp:
        _tree(tmp, [NEO3[0],
                    ("rtc1", "rtc-brandnew 0-0051",
                     "sys/devices/platform/ff150000.i2c/i2c-0/0-0051", False)])
        assert rtc.pick({}) is None, "an unknown driver is not trusted on its own"
        assert [d["dev"] for d in rtc.devices()] == ["rtc0", "rtc1"], "but it is listed"
        cfg = {"system": {"rtc": {"device": "rtc1"}}}
        assert rtc.pick(cfg)["name"] == "rtc-brandnew"
        # and a name that is not there resolves to nothing rather than to rtc0
        assert rtc.pick({"system": {"rtc": {"device": "rtc9"}}}) is None


def test_a_dev_host_has_no_rtc_tree_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        rtc.RTC_ROOT = Path(tmp) / "nothing" / "here"
        assert rtc.devices() == []
        assert rtc.status({})["available"] is False


def test_the_device_node_is_always_named_and_never_guessed():
    """/dev/rtc points at rtc0 on this board -- the clock that says 2016. A bare
    hwclock works on the bench and silently reads the wrong chip on the bike."""
    with tempfile.TemporaryDirectory() as tmp:
        rtc.DEV_ROOT = Path(tmp) / "dev"
        assert rtc.node("rtc1").endswith("/dev/rtc1")
        for bad in ("", "rtc", "../../dev/sda", "rtc1; reboot", "/dev/rtc1"):
            assert rtc.node(bad) == "", bad


def test_every_hwclock_call_carries_the_device():
    ran = []
    real = system._run_err
    system._run_err = lambda cmd: (ran.append(" ".join(cmd)), (True, ""))[1]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rtc.DEV_ROOT = Path(tmp) / "dev"
            assert system.rtc_to_system("rtc1")[0] is True
            assert system.system_to_rtc("rtc1")[0] is True
            assert system.rtc_to_system("nonsense") == (False, "no rtc device")
        assert len(ran) == 2, ran
        assert ran[0].startswith("hwclock -s -f ") and ran[0].endswith("/dev/rtc1"), ran[0]
        assert ran[1].startswith("hwclock -w -f ") and ran[1].endswith("/dev/rtc1"), ran[1]
        for cmd in ran:
            assert "-f" in cmd.split(), "a bare hwclock follows /dev/rtc: " + cmd
    finally:
        system._run_err = real


def test_the_source_switch_defaults_to_the_browser():
    assert rtc.source({}) == "web"
    assert rtc.source(DEFAULTS) == "web"
    assert rtc.source({"system": {"rtc": {"source": "rtc"}}}) == "rtc"
    # a hand-edited value that means nothing must not silently mean "rtc"
    assert rtc.source({"system": {"rtc": {"source": "hardware"}}}) == "web"


def test_the_source_and_device_are_validated():
    cm = ConfigManager.__new__(ConfigManager)
    cm.validate(json.loads(json.dumps(DEFAULTS)))
    for key, bad, expect in (("source", "hardware", "cfgerr.rtc_source"),
                             ("device", "../../dev/sda", "cfgerr.rtc_device"),
                             ("device", "rtc1; reboot", "cfgerr.rtc_device")):
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg["system"]["rtc"][key] = bad
        try:
            cm.validate(cfg)
        except ConfigError as exc:
            assert exc.args[0] == expect, exc.args
        else:
            raise AssertionError(f"{key}={bad!r} passed validation")
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["system"]["rtc"]["device"] = "rtc1"       # naming one is allowed
    cm.validate(cfg)


def test_a_late_probing_module_is_not_there_when_the_service_starts():
    """Read off this board's kernel ring on 2026-09-19:

        [ 2.205915] rk808-rtc: registered as rtc0
        [14.835851] rtc-ds1307 0-0068: registered as rtc1

    The unit is only ``After=network.target``, so at a cold boot the module is
    twelve seconds away when the lifespan runs. A single look finds nothing,
    ``_rtc_target()`` answers "" and the boot read is skipped **without a word**
    -- the board stays in 2016, which is the failure the feature exists to fix.
    So the startup waits for it. This pins the shape of the problem: the same
    tree answers differently before and after the probe.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _tree(tmp, [NEO3[0]])                     # 2.2 s in: only the PMIC clock
        assert rtc.pick({}) is None
        # 14.8 s in: the driver registers and the same call now finds the module
        dev, name, bus, _ = NEO3[1]
        d = rtc.RTC_ROOT / dev
        d.mkdir()
        (d / "name").write_text(name + "\n")
        (d / "hctosys").write_text("0\n")
        real = Path(tmp) / bus.lstrip("/")
        real.mkdir(parents=True, exist_ok=True)
        (d / "device").symlink_to(real)
        assert rtc.pick({})["dev"] == "rtc1", "the wait has something to wait for"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
