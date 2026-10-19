"""fw_codec pinned to vectors produced by the 5am_util C code (and its own verbose logs)."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kline import fw_codec as fc  # noqa: E402


def synthetic_image() -> bytes:
    return bytes(((i * 31) + (i >> 8) * 7 + 5) & 0xFF for i in range(fc.IMAGE_SIZE))


def test_calc_key_matches_the_challenges_seen_on_the_bike():
    # both pairs come from 5am_util -v logs taken on the real ECU
    assert fc.calc_key(0x718E718F) == 0xE4466927
    assert fc.calc_key(0x27882789) == 0xDA786927


def test_payload_length_and_header():
    enc = fc.build_payload(synthetic_image())
    assert len(enc) == fc.ENC_LEN == 0x4C008
    # first encrypted bytes of every image: the fixed header run through the cipher
    assert enc[:8].hex() == "da677839f65b1594"


def test_payload_and_checksum_match_the_c_reference():
    img = synthetic_image()
    assert fc.checksum16(img) == 0x9E61
    enc = fc.build_payload(img)
    assert hashlib.sha256(enc).hexdigest() == (
        "e413c067418d6daf506924b6bb4e748547f2f92c57e9205dda43ac1fed72d9e0")


def test_wrong_size_is_refused():
    for bad in (b"", b"\0" * (fc.IMAGE_SIZE - 1), b"\0" * (fc.IMAGE_SIZE + 1)):
        for fn in (fc.build_payload, fc.checksum16):
            try:
                fn(bad)
            except ValueError:
                continue
            raise AssertionError("accepted a %d byte image" % len(bad))


def test_encrypt_is_per_byte_and_position_dependent():
    assert fc.encrypt_blob(b"") == b""
    # same byte at the 8 positions of a cycle must give the 8 steps, repeating
    one = fc.encrypt_blob(b"\x00" * 16)
    assert one[:8] == one[8:]
    assert len(set(one[:8])) > 4


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
            n += 1
    print("%d passed" % n)
