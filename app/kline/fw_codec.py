"""Image encoding and security-access key for Marelli IAW5AM firmware writes.

The constants and the byte transform below follow denandz/5am_util (main.c,
BSD 3-Clause, Copyright (c) 2018 DoI); see THIRD_PARTY.md. tests/test_fw_codec.py
pins every function to vectors produced by that C code.
"""
from __future__ import annotations

IMAGE_SIZE = 327680
PAYLOAD_START = 0x4000                 # the first 16 KiB (boot block) is never written
ENC_LEN = IMAGE_SIZE - 0x3FF8          # 0x4C008: 8-byte header + 0x4C000 of image
CHECKSUM_SPAN = 0x4BFFE
ENC_HEADER = bytes((0xC2, 0x07, 0x16, 0x33, 0x6F, 0xEB, 0xB0, 0x1D))

# (addend, rotate-right bits, invert after rotating) per position in an 8-byte cycle.
# Step 7 is the odd one out: 0x66 - b, then rotate right by 4.
_STEPS = (
    (0x88, 1, True),
    (0xC7, 1, False),
    (0x26, 3, True),
    (0xA5, 5, True),
    (0x6C, 2, False),
    (0xEB, 6, False),
    (0x0A, 6, True),
)


def _ror8(b: int, n: int) -> int:
    return ((b >> n) | (b << (8 - n))) & 0xFF


def encrypt_blob(data: bytes) -> bytes:
    out = bytearray(data)
    for i, b in enumerate(out):
        k = i % 8
        if k == 7:
            b = _ror8((0x66 - b) & 0xFF, 4)
        else:
            add, rot, inv = _STEPS[k]
            b = _ror8((b + add) & 0xFF, rot)
            if inv:
                b ^= 0xFF
        out[i] = b
    return bytes(out)


def checksum16(image: bytes) -> int:
    """Sum of image[0x4000 : 0x4000+0x4BFFE] mod 2**16, sent with the program routine."""
    if len(image) != IMAGE_SIZE:
        raise ValueError("image must be %d bytes" % IMAGE_SIZE)
    return sum(image[PAYLOAD_START:PAYLOAD_START + CHECKSUM_SPAN]) & 0xFFFF


def build_payload(image: bytes) -> bytes:
    """What goes over the wire in the 0x36 blocks: header + the image past the boot block, encoded."""
    if len(image) != IMAGE_SIZE:
        raise ValueError("image must be %d bytes" % IMAGE_SIZE)
    raw = ENC_HEADER + image[PAYLOAD_START:PAYLOAD_START + ENC_LEN - len(ENC_HEADER)]
    return encrypt_blob(raw)


def calc_key(challenge: int) -> int:
    """27 02 response for the 27 01 seed; the ECU's check is this fixed arithmetic."""
    low = challenge & 0xFFFF
    high = (challenge >> 16) & 0xFFFF
    swapped = ((low >> 8) | (low << 8)) & 0xFFFF
    b1 = (swapped // 0xA1) & 0xFF
    b2 = (high % 0xC8) & 0xFF
    return (b1 << 24) | (b2 << 16) | (0x69 << 8) | 0x27
