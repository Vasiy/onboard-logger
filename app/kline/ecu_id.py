"""Build a GuzziDiag-style ECU description from the ReadEcuIdentification data.

This module handles the *live* identity: the 0x1A 0x80 response the ECU gives
before a download, sliced into the labelled fields (Drawing / Hardware /
Omologation / Software / Tester / Date) and rendered in the same plain-text
format (CRLF) GuzziDiag saves next to a dump.

The firmware image carries an identity of its own — a calibration code assembled
from two string fields near 0x47FA4 — read by `app/kline/fw_ident.py`. The two
are related but are NOT the same namespace: on a Morini both spell 23ECCLGPSMD,
while on a Ducati the live Drawing is an OEM part number (96520610B) and the
image holds a calibration mnemonic. Never compare one against the other directly.

Drawing (0:11) and Hardware (11:22) are reliable across IAW5AM units; the rest
are best-guess offsets and can be tuned in config/ecu_id.json without code.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# (label, offset, length) in the payload after the SID echo + option byte.
DEFAULT_FIELDS = [
    ("Drawing", 0, 11),
    ("Hardware", 11, 11),
    ("Omologation", 22, 12),
    ("Software", 34, 12),
    ("Tester", 46, 10),
    ("Date", 56, 10),
]


def load_fields(path: str | Path):
    try:
        raw = json.loads(Path(path).read_text())
        return [(f["label"], int(f["offset"]), int(f["length"])) for f in raw["fields"]]
    except (OSError, ValueError, KeyError):
        return DEFAULT_FIELDS


def _clean(chunk: bytes) -> str:
    # keep printable ASCII (incl. spaces GuzziDiag preserves), drop nulls/binary
    return "".join(chr(b) for b in chunk if 0x20 <= b < 0x7F)


def _field_value(label: str, raw: bytes, off: int, ln: int, hw: str) -> str:
    if label.lower() == "date":
        return datetime.now().strftime("%Y-%m-%d")   # dump/read date = today
    val = _clean(raw[off:off + ln]).strip()
    if label.lower() == "hardware" and hw and "HW" not in val:
        val = hw  # guaranteed from the regex-parsed HW version
    return val


def parse_fields(raw: bytes, hw: str = "", fields=DEFAULT_FIELDS) -> dict:
    """Slice the ECU-id payload into a {label: value} dict (for the UI banner)."""
    out: dict[str, str] = {}
    for label, off, ln in fields:
        val = _field_value(label, raw, off, ln, hw)
        if val:
            out[label] = val
    return out


def describe(raw: bytes, hw: str = "", fields=DEFAULT_FIELDS, extra=None) -> str:
    """Render the ECU-id payload into GuzziDiag's labelled text block (Date = today).

    `extra` appends further `Label: value` lines (bike brand/model, what the image
    bytes say) *after* the configured fields, so the file still opens with Drawing:
    and still ends in CRLF — GuzziDiag's format, and what tests/test_firmware.py
    guards. Empty values are dropped, so an unidentified image yields the old file
    byte for byte.
    """
    lines = [f"{label}: {_field_value(label, raw, off, ln, hw)}"
             for label, off, ln in fields]
    lines += [f"{label}: {value}" for label, value in (extra or []) if value]
    return "\r\n".join(lines) + "\r\n"


def parse_desc(text: str) -> dict:
    """Inverse of describe(): read a .bin.txt sidecar back into {label: value}.

    Needed because the sidecar is the only place a dump's identity survives once
    the ECU is gone — comparing it against the image is what catches a firmware
    labelled with someone else's calibration.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip():
            out.setdefault(label.strip(), value.strip())
    return out
