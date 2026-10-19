"""Hex dump in the layout 5am_util prints, so our traces read like its logs.

Format only (derived from the util's output, see THIRD_PARTY.md): 16 bytes per line,
an extra space after every 8, then `|  <ascii> `.
"""
from __future__ import annotations


def hexdump(data: bytes) -> list[str]:
    lines: list[str] = []
    cur, text = "", []
    total = len(data)
    for i, b in enumerate(data):
        cur += "%02X " % b
        text.append(chr(b) if 0x20 <= b <= 0x7E else ".")
        n = i + 1
        if n % 8 and n != total:
            continue
        cur += " "
        if n % 16 == 0:
            lines.append(cur + "|  " + "".join(text) + " ")
            cur, text = "", []
        elif n == total:
            if n % 16 <= 8:
                cur += " "
            cur += "   " * (16 - n % 16)
            lines.append(cur + "|  " + "".join(text) + " ")
    return lines
