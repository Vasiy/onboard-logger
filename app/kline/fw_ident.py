"""Read a firmware image's own calibration identity out of its bytes.

The IAW 5AM image carries two space-padded string fields — "Map Name 1" at
0x47FA4 and "Map Name 2" at 0x48006 — which concatenate into the calibration
code (23EC + CLGPSMD = 23ECCLGPSMD), plus a NUL-padded hardware string. The
offsets come from a TunerPro XDF and were verified against five images; a
`55 AA 33 CC` marker sits immediately before the first field and occurs only
three times in the whole image, so it is a cheap sanity check that we are
looking at the right family.

Offsets live in config/fw_layout.json, not here: another ECU family puts them
somewhere else, and that must stay a JSON edit. DEFAULT_LAYOUT is the fallback
so identification keeps working if the file is missing or malformed.

Only a ~176-byte window is ever read. /api/firmware is polled once every 1.5 s
while the Firmware tab is open, and pulling twelve 320 KB images off an SD card
at that rate would stall the board.

This is NOT the same identity as app/kline/ecu_id.py reads from the live ECU —
see that module's docstring before comparing the two.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_LAYOUT = {
    "id": "iaw5am",
    "size": 327680,
    "marker": {"offset": 0x47FA0, "hex": "55AA33CC"},
    "window": {"offset": 0x47FA0, "length": 136},
    "fields": [
        {"key": "map1", "offset": 0x47FA4, "length": 12, "pad": "space"},
        {"key": "map2", "offset": 0x48006, "length": 16, "pad": "space"},
        {"key": "hardware", "offset": 0x48016, "length": 16, "pad": "nul"},
    ],
    "code": {"join": ["map1", "map2"], "sep": ""},
}

FIELD_KEYS = ("map1", "map2", "hardware")


def _to_int(v, default=None):
    """Accept 294820 or "0x47FA4" — the XDF quotes these in hex, so must we."""
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return default


def load_layouts(path: str | Path) -> list:
    try:
        raw = json.loads(Path(path).read_text())
        layouts = [dict(x) for x in raw["layouts"] if isinstance(x, dict)]
        return layouts or [DEFAULT_LAYOUT]
    except (OSError, ValueError, KeyError, TypeError):
        return [DEFAULT_LAYOUT]


def _clean(chunk: bytes, pad: str) -> str:
    if pad == "nul":
        chunk = chunk.split(b"\x00", 1)[0]
    return "".join(chr(b) for b in chunk if 0x20 <= b < 0x7F).strip()


def _blank(reason: str, layout_id: str = "") -> dict:
    out = {k: "" for k in FIELD_KEYS}
    out.update(code="", layout=layout_id, reason=reason)
    return out


def _fields_from_window(win: bytes, base: int, layout: dict) -> dict:
    out = {k: "" for k in FIELD_KEYS}
    for f in layout.get("fields", []):
        off = _to_int(f.get("offset"))
        ln = _to_int(f.get("length"), 0)
        key = f.get("key")
        if off is None or not ln or key not in out:
            continue
        start = off - base
        # a field the window does not cover is skipped, not fatal: the rest of
        # the block still identifies the image
        if start < 0 or start + ln > len(win):
            continue
        out[key] = _clean(win[start:start + ln], str(f.get("pad", "space")))
    return out


def _marker_ok(win: bytes, base: int, layout: dict) -> bool:
    mk = layout.get("marker") or {}
    try:
        want = bytes.fromhex(str(mk.get("hex", "")))
    except ValueError:
        return True                     # no usable marker declared -> nothing to check
    if not want:
        return True
    off = _to_int(mk.get("offset"))
    if off is not None:
        start = off - base
        if 0 <= start and win[start:start + len(want)] == want:
            return True
    # declared offset missed: the marker may simply sit elsewhere in another
    # family's block, so fall back to scanning the window before giving up
    return want in win


def identify_bytes(blob: bytes, layouts=None) -> dict:
    """Identify a full image already in memory (the file path variant is preferred)."""
    layouts = layouts or [DEFAULT_LAYOUT]
    for layout in layouts:
        size = _to_int(layout.get("size"))
        if size is not None and len(blob) != size:
            continue
        win = layout.get("window") or {}
        base = _to_int(win.get("offset"), 0)
        ln = _to_int(win.get("length"), 0)
        if len(blob) < base + ln:
            return _blank("truncated", str(layout.get("id", "")))
        return _identify_window(blob[base:base + ln], base, layout)
    return _blank("size_mismatch")


def _identify_window(win: bytes, base: int, layout: dict) -> dict:
    lid = str(layout.get("id", ""))
    if not _marker_ok(win, base, layout):
        return _blank("not_5am", lid)
    out = _fields_from_window(win, base, layout)
    join = (layout.get("code") or {}).get("join") or []
    sep = str((layout.get("code") or {}).get("sep", ""))
    code = sep.join(out.get(k, "") for k in join).strip()
    out.update(code=code, layout=lid, reason="" if code else "blank")
    return out


def identify_file(path: str | Path, layouts=None) -> dict:
    """Identify an image on disk with one seek + one read of the identity window."""
    layouts = layouts or [DEFAULT_LAYOUT]
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return _blank("unreadable")

    for layout in layouts:
        want = _to_int(layout.get("size"))
        if want is not None and size != want:
            continue
        win = layout.get("window") or {}
        base = _to_int(win.get("offset"), 0)
        ln = _to_int(win.get("length"), 0)
        if size < base + ln:
            return _blank("truncated", str(layout.get("id", "")))
        try:
            with p.open("rb") as fh:
                fh.seek(base)
                chunk = fh.read(ln)
        except OSError:
            return _blank("unreadable", str(layout.get("id", "")))
        if len(chunk) < ln:
            return _blank("truncated", str(layout.get("id", "")))
        return _identify_window(chunk, base, layout)
    return _blank("size_mismatch")
