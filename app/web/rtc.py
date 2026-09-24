"""Finding the battery-backed clock, and telling it apart from the one that lies.

This board carries two RTCs and only one of them knows what year it is:

    rtc0  "rk808-rtc"    i2c-1 1-0018  hctosys=1  -> 2016-01-21
    rtc1  "rtc-ds1307"   i2c-0 0-0068  hctosys=0  -> 2026-09-19

``rtc0`` is the PMIC's own clock, has no battery behind it, and is what the
kernel restored the system time from at boot -- which is why the board comes up
in 2016. It is also what ``/dev/rtc``, a bare ``hwclock`` and ``timedatectl``
all read. The DS module that actually holds the date is ``rtc1``.

So: **the driver name decides, the bus is only context.** Both of these sit on
i2c, so a rule of "external means i2c" would trust the PMIC. A name on neither
list is reported but not trusted, and ``system.rtc.device`` in config is how a
module this file has not heard of gets used anyway.

Nothing here raises: a dev host has no /sys/class/rtc at all, and the UI simply
hides the block.
"""

from __future__ import annotations

import re
from pathlib import Path

RTC_ROOT = Path("/sys/class/rtc")        # module-level so tests can point them
DEV_ROOT = Path("/dev")                  # at a fake tree

# Battery-backed modules someone wires on deliberately. rtc-ds1307 is one driver
# for the whole DS1307/1337/1338/3231 family, which is why DS3231 is not listed
# separately -- the kernel calls it "rtc-ds1307".
EXTERNAL = {
    "rtc-ds1307", "rtc-ds3232", "rtc-ds1672", "rtc-pcf8563", "rtc-pcf8523",
    "rtc-pcf85063", "rtc-hym8563", "rtc-mcp7941x", "rtc-m41t80", "rtc-rx8025",
    "rtc-abx80x", "rtc-bq32k",
}
# Clocks that come with the SoC or the PMIC. None of them has a battery, and
# every one of them will happily answer.
ONBOARD = {
    "rk808-rtc", "rk805-rtc", "rk809-rtc", "rk817-rtc", "rk818-rtc",
    "snvs_rtc", "rtc-pl031", "ftrtc010", "sun6i-rtc", "sun4i-rtc", "mtk-rtc",
    "bcm2835", "rtc-efi", "rtc_cmos",
}

_BUS = re.compile(r"^(i2c-\d+|spi\d+(?:\.\d+)?)$")
_DEV = re.compile(r"^rtc\d+$")


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _bus_of(dev_dir: Path) -> str:
    """The i2c/spi bus the chip hangs off, read out of the device symlink."""
    try:
        target = (dev_dir / "device").resolve()
    except OSError:
        return ""
    for part in reversed(target.parts):
        if _BUS.match(part):
            return part
    return ""


def devices() -> list[dict]:
    """Every RTC the kernel offers, newest-looking facts first. Never raises."""
    out = []
    try:
        entries = sorted(p for p in RTC_ROOT.iterdir() if _DEV.match(p.name))
    except OSError:
        return out
    for p in entries:
        # the name file is "<driver> <device id>", e.g. "rtc-ds1307 0-0068"
        parts = _read(p / "name").split()
        driver = parts[0] if parts else ""
        out.append({
            "dev": p.name,
            "name": driver,
            "id": parts[1] if len(parts) > 1 else "",
            "bus": _bus_of(p),
            "hctosys": _read(p / "hctosys") == "1",
            "external": driver in EXTERNAL,
            "onboard": driver in ONBOARD,
        })
    return out


def pick(cfg: dict | None = None, found: list[dict] | None = None) -> dict | None:
    """The clock to believe: the one config names, else the first trusted one.

    A hand-named device wins even when this file does not recognise its driver
    -- that is the whole point of the override. It still has to exist.
    """
    devs = devices() if found is None else found
    want = str(((cfg or {}).get("system", {}) or {}).get("rtc", {}).get("device", "") or "")
    if want:
        return next((d for d in devs if d["dev"] == want), None)
    return next((d for d in devs if d["external"]), None)


def source(cfg: dict | None = None) -> str:
    """"rtc" or "web" -- where the board's clock comes from. Default: the web."""
    s = str(((cfg or {}).get("system", {}) or {}).get("rtc", {}).get("source", "web") or "web")
    return s if s in ("rtc", "web") else "web"


def node(dev: str) -> str:
    """/dev path for a device name, or "" if it is not one.

    Every hwclock call in this project passes this as ``-f``. A bare ``hwclock``
    follows /dev/rtc, which on this board points at the PMIC clock: it works on
    the bench and silently reads 2016 on the bike.
    """
    return str(DEV_ROOT / dev) if _DEV.match(dev or "") else ""


def status(cfg: dict | None = None) -> dict:
    """What the UI needs: the list, the choice, and whether to show the block."""
    found = devices()
    chosen = pick(cfg, found)
    return {
        "devices": found,
        "selected": chosen["dev"] if chosen else "",
        "name": chosen["name"] if chosen else "",
        "bus": chosen["bus"] if chosen else "",
        "trusted": bool(chosen),
        "source": source(cfg),
        "available": bool(chosen),
    }
