"""Privileged system actions: reboot / shutdown / clock."""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import rtc as rtc_mod

# One automatic sync per power-up: /run is tmpfs, so the marker disappears on a
# reboot but survives a service restart or a deploy — which is exactly the
# power-cycle boundary the feature is about.
AUTO_MARKER = Path("/run/onboard-logger/time-synced")
_auto_done_fallback = False      # dev hosts without a writable /run
# ...except when the clock is wildly out. The marker exists so that every page
# load does not nudge the clock; it must not defend a board that is hours wrong.
# That is what happened on 2026-09-10: the phone kept the page open across a
# board reboot, the websocket reconnected without a page load, nothing ever
# offered the clock, and a whole ride was written into the previous day's folder.
RESYNC_DRIFT_S = 120.0


def _run_detached(cmd: list[str]) -> dict:
    exe = shutil.which(cmd[0])
    if exe is None:
        return {"ok": False, "message": "err.no_binary"}
    try:
        # fire-and-forget: the box goes down before the request would return
        subprocess.Popen(cmd)
        return {"ok": True, "message": " ".join(cmd)}
    except OSError as exc:
        return {"ok": False, "message": str(exc)}


def reboot() -> dict:
    return _run_detached(["systemctl", "reboot"])


def shutdown() -> dict:
    return _run_detached(["systemctl", "poweroff"])


# -- clock -----------------------------------------------------------------
# Every board answers with *an* RTC and most of them have no battery behind it
# (this NEO3's rk808-rtc comes up in 2016), and there is no internet on the bike,
# so timesyncd can be running and still be wrong. The Config tab pushes the
# browser's clock instead; with a battery-backed module wired on, app/web/rtc.py
# finds it by name and the board reads that at boot. Log file names and CSV
# timestamps all come from the local clock.
def _out(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _timedatectl_props() -> dict:
    props = {}
    for line in _out(["timedatectl", "show"]).splitlines():
        k, _, v = line.partition("=")
        props[k] = v
    return props


def time_status() -> dict:
    p = _timedatectl_props()
    now = time.time()
    return {
        "epoch": now,
        "local": datetime.now().isoformat(timespec="seconds"),
        "tz": p.get("Timezone", ""),
        "ntp_active": p.get("NTP") == "yes",
        "ntp_synced": p.get("NTPSynchronized") == "yes",
    }


def rtc_to_system(dev: str) -> tuple[bool, str]:
    """Take the system clock from a battery-backed module: hwclock -s.

    Always with -f. /dev/rtc points at whichever clock the kernel registered
    first, which on this board is the PMIC's batteryless one -- a bare hwclock
    reads 2016 and looks like it worked.
    """
    node = rtc_mod.node(dev)
    if not node:
        return False, "no rtc device"
    return _run_err(["hwclock", "-s", "-f", node])


def system_to_rtc(dev: str) -> tuple[bool, str]:
    """Push the system clock into the module: hwclock -w. Same -f rule."""
    node = rtc_mod.node(dev)
    if not node:
        return False, "no rtc device"
    return _run_err(["hwclock", "-w", "-f", node])


def set_time(epoch: float, tz: str = "", rtc_dev: str = "") -> dict:
    """Set the clock from a client-supplied epoch (and optionally its timezone).

    With ``rtc_dev`` the new time is written into that module as well: on a board
    running from a hardware RTC, the sync button's job is to correct the module,
    not just the running clock it will be overwritten by at the next boot.

    Always applies (within a sanity range): the button is an explicit request and
    the browser is the only trustworthy clock on the bike. NTPSynchronized is no
    guard — systemd keeps that flag set from an earlier sync even with no server
    in reach, which would block the button exactly when it is needed. NTP is
    switched off for the set and restored afterwards, so the board still corrects
    itself the next time it really does have a network.
    """
    if shutil.which("timedatectl") is None:
        return {"ok": False, "error": "err.no_timedatectl"}
    try:
        epoch = float(epoch)
    except (TypeError, ValueError):
        return {"ok": False, "error": "err.bad_time"}
    if not (1_000_000_000 < epoch < 4_000_000_000):        # 2001..2096, sanity only
        return {"ok": False, "error": "err.bad_time"}

    st = time_status()
    applied = []
    if tz and tz != st["tz"] and "/" in tz and len(tz) < 64:
        if _run(["timedatectl", "set-timezone", tz]):
            applied.append(f"tz {tz}")

    was_ntp = st["ntp_active"]
    if was_ntp:
        _run(["timedatectl", "set-ntp", "false"])
        # timesyncd needs a moment to actually let go; set-time refuses with
        # "Automatic time synchronization is enabled" until it has
        for _ in range(20):
            if _timedatectl_props().get("NTP") != "yes":
                break
            time.sleep(0.1)
    stamp = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    ok, err = _run_err(["timedatectl", "set-time", stamp])
    if was_ntp:      # leave it enabled: it will correct us once a network exists
        _run(["timedatectl", "set-ntp", "true"])
    if ok:
        applied.append(stamp)
        if rtc_dev:
            wrote, werr = system_to_rtc(rtc_dev)
            if wrote:
                applied.append(f"rtc {rtc_dev}")
            else:
                # The running clock is right either way, so this is not a failed
                # set -- but on a board running off the module the write is the
                # half that lasts, and a green banner over a module that was not
                # written is the worst of the two answers.
                ok = False
                err = werr or err
    return {"ok": ok, "applied": applied, "error": "" if ok else "err.set_time",
            "detail": err, **time_status()}


def _run_err(cmd: list[str]) -> tuple[bool, str]:
    """Run a command, returning (ok, stderr) so failures can be shown, not guessed."""
    if shutil.which(cmd[0]) is None:
        return False, f"no {cmd[0]}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _run(cmd: list[str]) -> bool:
    return _run_err(cmd)[0]


# -- automatic sync from the first web client -------------------------------
def auto_sync_done() -> bool:
    # the file is the source of truth across service restarts; the in-memory flag
    # covers the case where /run could not be written at all (dev host)
    if _auto_done_fallback:
        return True
    try:
        return AUTO_MARKER.exists()
    except OSError:
        return False


def _mark_auto_sync() -> None:
    """Remember that this power-up already had its shot at the clock.

    The file is the record; the in-memory flag is only for boards where /run
    could not be written, so deleting the marker by hand really does re-arm the
    feature instead of being shadowed by process state.
    """
    global _auto_done_fallback
    try:
        AUTO_MARKER.parent.mkdir(parents=True, exist_ok=True)
        AUTO_MARKER.write_text(datetime.now().isoformat(timespec="seconds"))
    except OSError:
        _auto_done_fallback = True


def auto_sync(epoch: float, tz: str = "", threshold: float = 2.0,
              rtc_trusted: bool = False) -> dict:
    """Take the clock from the first browser that shows up after a power-up.

    The marker is set when the clock was written *or* deliberately left alone
    (already on time), so a board that is on time is not pestered by every page
    load. A failure — nonsense timestamp, no timedatectl — does not burn the
    shot: the next client that shows up can still fix the clock.
    """
    try:
        epoch = float(epoch)
    except (TypeError, ValueError):
        return {"ok": False, "error": "err.bad_time"}

    drift = abs(epoch - time.time())
    if auto_sync_done():
        if drift <= RESYNC_DRIFT_S:
            return {"ok": True, "skipped": "already", "drift": round(drift, 3),
                    **time_status()}
        st = time_status()
        if st["ntp_synced"]:
            # a real time server has spoken on this board; a browser that
            # disagrees by minutes is the clock more likely to be wrong
            return {"ok": True, "skipped": "ntp", "drift": round(drift, 3), **st}
        if rtc_trusted:
            # same argument, stronger: the board is running off a battery-backed
            # module, and a phone minutes out of step with it is the suspect
            return {"ok": True, "skipped": "rtc", "drift": round(drift, 3), **st}
    if drift <= threshold:
        # close enough: don't jump the clock mid-log, but do adopt the timezone
        st = time_status()
        applied = []
        if tz and tz != st["tz"] and "/" in tz and len(tz) < 64:
            if _run(["timedatectl", "set-timezone", tz]):
                applied.append(f"tz {tz}")
        _mark_auto_sync()
        return {"ok": True, "skipped": "in_sync", "drift": round(drift, 3),
                "applied": applied, **time_status()}

    res = set_time(epoch, tz)
    res["drift"] = round(drift, 3)
    if res.get("ok"):
        _mark_auto_sync()
    return res


# -- CPU frequency ----------------------------------------------------------
# The board sits in a closed case with no fan. On 2026-09-09 it idled at
# 62.5-65 C with ondemand already holding 408-816 MHz and uvicorn taking 1.9 %
# of one core -- so the heat is the SoC's baseline in that case, not the clock,
# and the first thermal trip is only 5 degrees above that idle. Capping the
# ceiling buys the margin back; it is not a fix for the enclosure.
#
# Everything here is read from the board rather than assumed: this NanoPi NEO3
# offers six governors and six frequencies, but the next board need not.
CPUFREQ = Path("/sys/devices/system/cpu/cpufreq/policy0")
THERMAL = Path("/sys/class/thermal/thermal_zone0")


def _sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _khz_list(raw: str) -> list[int]:
    return sorted({int(x) for x in raw.split() if x.isdigit()})


def cpu_status() -> dict:
    """What the kernel offers and where it stands right now.

    `headroom` is the number that matters on a hot day: how far the SoC is from
    the first passive trip, past which the kernel starts cutting the clock by
    itself and the UI has nothing to do with it.
    """
    freqs = _khz_list(_sysfs(CPUFREQ / "scaling_available_frequencies"))
    if not freqs:
        # a policy without a discrete table still names its two ends
        lo, hi = _sysfs(CPUFREQ / "cpuinfo_min_freq"), _sysfs(CPUFREQ / "cpuinfo_max_freq")
        freqs = [int(x) for x in (lo, hi) if x.isdigit()]
    out = {
        "available": bool(freqs),
        "governors": _sysfs(CPUFREQ / "scaling_available_governors").split(),
        "freqs_khz": freqs,
        "governor": _sysfs(CPUFREQ / "scaling_governor"),
        "max_khz": int(_sysfs(CPUFREQ / "scaling_max_freq") or 0),
        "cur_khz": int(_sysfs(CPUFREQ / "scaling_cur_freq") or 0),
        # the factory ceiling, to tell "nobody capped this" from "something did".
        # The thermal governor drives scaling_max_freq too, so it can sit below
        # the hardware maximum with nothing in config asking for it.
        "hw_max_khz": int(_sysfs(CPUFREQ / "cpuinfo_max_freq") or 0),
    }
    t = _sysfs(THERMAL / "temp")
    if t.lstrip("-").isdigit():
        out["temp_c"] = round(int(t) / 1000.0, 1)
    trip = _sysfs(THERMAL / "trip_point_0_temp")
    if trip.isdigit():
        out["trip_c"] = round(int(trip) / 1000.0, 1)
        if "temp_c" in out:
            out["headroom_c"] = round(out["trip_c"] - out["temp_c"], 1)
    if not out["available"]:
        out["message"] = "no cpufreq (dev host?)"
    return out


def cpu_apply(governor: str = "", max_khz: int = 0) -> dict:
    """Write the governor and the ceiling. Empty / 0 means leave that one alone.

    The service runs as root, so sysfs is written directly -- no helper binary
    and no unit of its own. A value the kernel does not offer is refused here
    rather than written and silently ignored, which is what sysfs would do.
    """
    st = cpu_status()
    if not st["available"]:
        return {"ok": False, "message": st.get("message", "no cpufreq"), **st}

    applied, errors = [], []
    if governor:
        if governor not in st["governors"]:
            errors.append(f"governor {governor} is not offered")
        else:
            try:
                (CPUFREQ / "scaling_governor").write_text(governor)
                applied.append(f"governor {governor}")
            except OSError as exc:
                errors.append(str(exc))
    if max_khz:
        if st["freqs_khz"] and max_khz not in st["freqs_khz"]:
            errors.append(f"frequency {max_khz} is not offered")
        else:
            try:
                (CPUFREQ / "scaling_max_freq").write_text(str(int(max_khz)))
                applied.append(f"max {max_khz} kHz")
            except OSError as exc:
                errors.append(str(exc))

    res = cpu_status()
    res["ok"] = not errors
    res["applied"] = applied
    if errors:
        res["message"] = "; ".join(errors)
    return res


# -- power save ------------------------------------------------------------
# A bike parked with the ignition off keeps the board retrying the link every
# two seconds, at whatever speed config asks for, until the battery gives out.
# Two steps, both counted from the last time the ECU actually answered: slow the
# CPU right down, then power off. The decision is a pure function so the tests
# can drive the whole ladder without a clock or an event loop.
def power_save_step(idle_s: float, cfg: dict, level: int, busy: bool) -> str | None:
    """What power save should do right now: "slow", "off", "restore" or nothing.

    ``level`` is what it has already done (0 = nothing, 1 = slowed down).
    ``busy`` refuses the poweroff only: slowing the CPU down leaves the UI alive
    and is always safe, while powering off under a rider reading logs in the
    garage is not.

    0 in either timer disables the step it times: ``idle_min == 0`` means the
    board never slows down (and so never reaches "off" either, since that
    requires having slowed down first); ``off_min == 0`` means it slows down
    and stays there, never powering off. Two switches, not one -- a rider who
    wants the CPU throttled but the board never unattended-off (or the other
    way around) does not need a second config key for it.
    """
    ps = (cfg.get("system", {}) or {}).get("power_save", {}) or {}
    if not ps.get("enabled"):
        return "restore" if level else None
    try:
        idle_min = float(ps.get("idle_min", 15))
        off_min = float(ps.get("off_min", 15))
    except (TypeError, ValueError):
        return "restore" if level else None
    if idle_min <= 0:
        return "restore" if level else None
    if idle_s < idle_min * 60:
        # the ECU answered again -- put the board back the way config asks
        return "restore" if level else None
    if level >= 2:
        return None     # already told to power off; saying it again buys nothing
    if level < 1:
        return "slow"
    if off_min <= 0:
        return None     # slowed down and staying there: the off step is disabled
    if idle_s >= (idle_min + off_min) * 60:
        return None if busy else "off"
    return None


def power_save_target(st: dict) -> tuple[str, int]:
    """The slowest the kernel on this board will actually agree to.

    Asked for rather than assumed: ``cpu_apply`` refuses a governor or a
    frequency the kernel does not offer, and a board that has no "powersave"
    governor should still get the frequency ceiling.
    """
    gov = "powersave" if "powersave" in (st.get("governors") or []) else ""
    freqs = st.get("freqs_khz") or []
    return gov, (min(freqs) if freqs else 0)
