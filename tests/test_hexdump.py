"""hexdump pinned to lines the board's fw-writing logs actually contain."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kline.hexdump import hexdump  # noqa: E402


def test_short_line_matches_the_util_log():
    got = hexdump(bytes.fromhex("8110F18103"))
    assert [ln.rstrip() for ln in got] == [
        "81 10 F1 81 03                                    |  ....."]


def test_twelve_bytes_get_the_gap_after_eight():
    got = hexdump(bytes.fromhex("8110F1810383F110C1EF8FC3"))
    assert [ln.rstrip() for ln in got] == [
        "81 10 F1 81 03 83 F1 10  C1 EF 8F C3              |  ............"]


def test_multi_line_with_ascii():
    data = bytes.fromhex("BF F1 10 5A 80 32 33 45 43 43 4C 47 50 53 4D 45 49 41 57 35 41 4D 48 57 36 31 30 26".replace(" ", ""))
    got = [ln.rstrip() for ln in hexdump(data)]
    assert got == [
        "BF F1 10 5A 80 32 33 45  43 43 4C 47 50 53 4D 45  |  ...Z.23ECCLGPSME",
        "49 41 57 35 41 4D 48 57  36 31 30 26              |  IAW5AMHW610&",
    ]


def test_exact_multiple_of_sixteen_and_empty():
    assert len(hexdump(bytes(range(32)))) == 2
    assert hexdump(b"") == []


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
            n += 1
    print("%d passed" % n)
