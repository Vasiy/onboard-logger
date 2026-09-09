"""Privileged system actions: reboot / shutdown / clock."""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

# One automatic sync per power-up: /run is tmpfs, so the marker disappears on a
# reboot but survives a service restart or a deploy — which is exactly the
# power-cycle boundary the feature is about.
AUTO_MARKER = Path("/run/onboard-logger/time-synced")
_auto_done_fallback = False      # dev hosts without a writable /run


def _run_detached(cmd: list[str]) -> dict:
    exe = shutil.which(cmd[0])
    if exe is None:
        return {"ok": False, "message": f"no {cmd[0]} (dev host?)"}
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
# The board has an RTC but no internet on the bike, so timesyncd can be running
# and still be wrong. The Config tab can push the browser's clock instead — log
# file names and CSV timestamps come from the local clock.
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


def set_time(epoch: float, tz: str = "") -> dict:
    """Set the clock from a client-supplied epoch (and optionally its timezone).

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


def auto_sync(epoch: float, tz: str = "", threshold: float = 2.0) -> dict:
    """Take the clock from the first browser that shows up after a power-up.

    The marker is set when the clock was written *or* deliberately left alone
    (already on time), so a board that is on time is not pestered by every page
    load. A failure — nonsense timestamp, no timedatectl — does not burn the
    shot: the next client that shows up can still fix the clock.
    """
    if auto_sync_done():
        return {"ok": True, "skipped": "already", **time_status()}
    try:
        epoch = float(epoch)
    except (TypeError, ValueError):
        return {"ok": False, "error": "err.bad_time"}

    drift = abs(epoch - time.time())
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
