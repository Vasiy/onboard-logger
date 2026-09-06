"""Quick 2.4 GHz survey to pick the least-congested AP channel before hostapd.

Runs `iw dev <iface> scan` (interface must be up and NOT in AP mode — so the
caller stops hostapd first), tallies every detected BSS onto the non-overlapping
candidate channels (1/6/11 by default) weighted by signal power and 20 MHz
spectral overlap, and returns the quietest candidate.

Scoring is a pure function (`best_channel_from_scan`) so it is unit-tested
without a radio; the IO wrapper (`pick_best_channel`) degrades to (None, ...)
on any failure so the caller can fall back to the configured channel.
"""

from __future__ import annotations

import re
import shutil
import subprocess

DEFAULT_CANDIDATES = (1, 6, 11)


def freq_to_channel(mhz: int) -> int | None:
    """2.4 GHz only (5 GHz APs are ignored as non-candidates)."""
    if 2412 <= mhz <= 2472:
        return (mhz - 2407) // 5
    if mhz == 2484:
        return 14
    return None


def _mw(dbm: float) -> float:
    return 10.0 ** (dbm / 10.0)


def best_channel_from_scan(
    aps: list[tuple[int, float]], candidates=DEFAULT_CANDIDATES
) -> tuple[int, dict]:
    """aps = list of (channel, signal_dBm). Returns (best_channel, per-channel info).

    A 20 MHz AP on channel ``c`` overlaps candidate ``k`` when |c-k| <= 4; its
    contribution is scaled 1.0 (co-channel) down to 0.2 (4 apart) and weighted by
    linear signal power, so a strong nearby AP hurts more than a weak distant one.
    """
    scores = {k: 0.0 for k in candidates}
    counts = {k: 0 for k in candidates}
    for ch, sig in aps:
        for k in candidates:
            d = abs(ch - k)
            if d <= 4:
                scores[k] += (1.0 - d / 5.0) * _mw(sig)
                if d == 0:
                    counts[k] += 1
    # pick min score; break ties by fewer co-channel APs, then lower channel
    best = min(candidates, key=lambda k: (round(scores[k], 6), counts[k], k))
    info = {
        "candidates": {
            k: {"score": round(scores[k], 4), "cochannel": counts[k]} for k in candidates
        },
        "aps_seen": len(aps),
        "chosen": best,
    }
    return best, info


def channel_survey(aps: list[tuple[int, float]], display_channels=range(1, 14)) -> list[dict]:
    """Per-channel occupancy for the UI chart across 2.4 GHz channels 1..13.

    ``load`` is the same overlap-weighted signal-power metric used for picking,
    computed for every display channel; ``count`` is co-channel AP count. The
    client normalizes ``load`` for bar heights.
    """
    out = []
    for ch in display_channels:
        load = 0.0
        count = 0
        for c, sig in aps:
            d = abs(c - ch)
            if d <= 4:
                load += (1.0 - d / 5.0) * _mw(sig)
            if d == 0:
                count += 1
        out.append({"channel": ch, "count": count, "load": round(load, 5)})
    return out


_BSS_RE = re.compile(r"^BSS ", re.M)
_FREQ_RE = re.compile(r"^\s*freq:\s*(\d+)", re.M)
_SIG_RE = re.compile(r"^\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", re.M)


def parse_iw_scan(output: str) -> list[tuple[int, float]]:
    aps: list[tuple[int, float]] = []
    # split into per-BSS blocks
    blocks = _BSS_RE.split(output)
    for blk in blocks:
        fm = _FREQ_RE.search(blk)
        if not fm:
            continue
        ch = freq_to_channel(int(fm.group(1)))
        if ch is None:
            continue
        sm = _SIG_RE.search(blk)
        sig = float(sm.group(1)) if sm else -90.0
        aps.append((ch, sig))
    return aps


_SSID_RE = re.compile(r"^[^\S\n]*SSID:[^\S\n]*(.*)$", re.M)
_PRIV_RE = re.compile(r"^\s*capability:.*Privacy", re.M)


def parse_iw_networks(output: str) -> list[dict]:
    """Named networks from an `iw scan`, best signal first.

    Same blocks parse_iw_scan walks, but keeping the identity of each BSS so the
    Config tab can offer a pick-list for client mode. Hidden SSIDs are dropped
    (they cannot be joined by name) and duplicate BSSIDs of one SSID collapse to
    the strongest one.
    """
    best: dict[str, dict] = {}
    for blk in _BSS_RE.split(output):
        fm = _FREQ_RE.search(blk)
        sm = _SSID_RE.search(blk)
        if not fm or not sm:
            continue
        ssid = sm.group(1).strip()
        if not ssid:
            continue                       # hidden network
        sig = float(_SIG_RE.search(blk).group(1)) if _SIG_RE.search(blk) else -90.0
        freq = int(fm.group(1))
        net = {
            "ssid": ssid,
            "signal": sig,
            "freq": freq,
            # 0 for 5 GHz: the AP side is 2.4-only, but a client may still join one
            "channel": freq_to_channel(freq) or 0,
            "secured": bool(_PRIV_RE.search(blk)),
        }
        if ssid not in best or sig > best[ssid]["signal"]:
            best[ssid] = net
    return sorted(best.values(), key=lambda n: -n["signal"])


def scan_networks(iface: str = "wlan0") -> tuple[list[dict], str]:
    """Scan for joinable networks. Returns (networks, error)."""
    if shutil.which("iw") is None:
        return [], "iw not available"
    r = subprocess.run(["iw", "dev", iface, "scan"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return [], (r.stderr or r.stdout).strip() or "scan failed"
    return parse_iw_networks(r.stdout), ""


def pick_best_channel(
    iface: str = "wlan0", candidates=DEFAULT_CANDIDATES, retries: int = 2
) -> tuple[int | None, dict]:
    """Bring the interface up, scan, and choose the quietest candidate channel.

    Returns (channel, info). channel is None if scanning is impossible; info
    always carries an "error"/"aps_seen" so the caller can log what happened.
    """
    if shutil.which("iw") is None or shutil.which("ip") is None:
        return None, {"error": "iw/ip not available"}
    # ensure the radio is in station/managed mode — scanning fails in AP mode
    subprocess.run(["ip", "link", "set", iface, "down"], capture_output=True, text=True)
    subprocess.run(["iw", "dev", iface, "set", "type", "managed"], capture_output=True, text=True)
    subprocess.run(["ip", "link", "set", iface, "up"], capture_output=True, text=True)
    last_err = ""
    for _ in range(max(1, retries)):
        r = subprocess.run(
            ["iw", "dev", iface, "scan"], capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            aps = parse_iw_scan(r.stdout)
            ch, info = best_channel_from_scan(aps, candidates)
            info["survey"] = channel_survey(aps)
            return ch, info
        last_err = (r.stderr or r.stdout).strip()
    return None, {"error": last_err or "scan failed", "aps_seen": 0}
