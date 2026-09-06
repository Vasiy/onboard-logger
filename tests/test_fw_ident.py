"""Reading a firmware image's calibration identity out of its own bytes.

Images are synthesised from the layout's own offsets, so changing an offset moves
the fixture and the code together instead of silently passing.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline import fw_ident  # noqa: E402

LAYOUT = fw_ident.DEFAULT_LAYOUT
OFF = {f["key"]: (f["offset"], f["length"]) for f in LAYOUT["fields"]}
SIZE = LAYOUT["size"]


def _image(map1="23EC", map2="CLGPSMD", hw="5AM X0000", marker=True, size=SIZE):
    """A synthetic 5AM image: marker + the three identity fields, rest zeroed."""
    blob = bytearray(size)
    if marker:
        mk = LAYOUT["marker"]
        blob[mk["offset"]:mk["offset"] + 4] = bytes.fromhex(mk["hex"])
    for key, text in (("map1", map1), ("map2", map2)):
        off, ln = OFF[key]
        blob[off:off + ln] = text.encode().ljust(ln, b" ")   # space-padded on the real ECU
    off, ln = OFF["hardware"]
    blob[off:off + ln] = hw.encode().ljust(ln, b"\x00")      # this one is NUL-padded
    return bytes(blob)


def _write(tmp, blob, name="fw.bin"):
    p = Path(tmp) / name
    p.write_bytes(blob)
    return p


def test_code_is_the_two_map_names_joined():
    with tempfile.TemporaryDirectory() as tmp:
        r = fw_ident.identify_file(_write(tmp, _image()))
        assert r["code"] == "23ECCLGPSMD"
        assert r["map1"] == "23EC" and r["map2"] == "CLGPSMD"
        assert r["reason"] == "" and r["layout"] == "iaw5am"


def test_padding_is_stripped_per_field_rule():
    with tempfile.TemporaryDirectory() as tmp:
        r = fw_ident.identify_file(_write(tmp, _image(hw="5AM X0000")))
        assert r["hardware"] == "5AM X0000"      # NUL padding cut, not rendered
        r2 = fw_ident.identify_file(_write(tmp, _image(map2="CLGPSMB"), "b.bin"))
        assert r2["code"] == "23ECCLGPSMB"       # trailing spaces gone


def test_revisions_read_distinctly():
    with tempfile.TemporaryDirectory() as tmp:
        codes = set()
        for rev in "BCD":
            p = _write(tmp, _image(map2="CLGPSM" + rev), f"{rev}.bin")
            codes.add(fw_ident.identify_file(p)["code"])
        assert codes == {"23ECCLGPSMB", "23ECCLGPSMC", "23ECCLGPSMD"}


def test_truncated_file_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "short.bin"
        p.write_bytes(b"\x00" * 100)
        r = fw_ident.identify_file(p)
        assert r["code"] == "" and r["reason"] == "size_mismatch"
        # right size but the window falls off the end -> truncated
        tiny = dict(LAYOUT, size=64)
        assert fw_ident.identify_bytes(b"\x00" * 64, [tiny])["reason"] == "truncated"


def test_missing_marker_is_not_this_family():
    with tempfile.TemporaryDirectory() as tmp:
        r = fw_ident.identify_file(_write(tmp, _image(marker=False)))
        assert r["code"] == "" and r["reason"] == "not_5am"


def test_marker_elsewhere_in_the_window_still_counts():
    # a wrong marker offset in a hand-written layout must degrade, not fail
    blob = bytearray(_image(marker=False))
    win = LAYOUT["window"]
    blob[win["offset"] + 60:win["offset"] + 64] = bytes.fromhex(LAYOUT["marker"]["hex"])
    assert fw_ident.identify_bytes(bytes(blob))["code"] == "23ECCLGPSMD"


def test_blank_fields_report_blank():
    with tempfile.TemporaryDirectory() as tmp:
        r = fw_ident.identify_file(_write(tmp, _image(map1="", map2="")))
        assert r["code"] == "" and r["reason"] == "blank"


def test_unreadable_path():
    r = fw_ident.identify_file("/nonexistent/nowhere.bin")
    assert r["code"] == "" and r["reason"] == "unreadable"


def test_layout_file_failures_fall_back_to_the_builtin():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.json"
        bad.write_text("{{{")
        assert fw_ident.load_layouts(bad) == [fw_ident.DEFAULT_LAYOUT]
        assert fw_ident.load_layouts(Path(tmp) / "missing.json") == [fw_ident.DEFAULT_LAYOUT]
        empty = Path(tmp) / "empty.json"
        empty.write_text('{"layouts": []}')
        assert fw_ident.load_layouts(empty) == [fw_ident.DEFAULT_LAYOUT]


def test_shipped_layout_matches_the_builtin_one():
    repo = Path(__file__).resolve().parent.parent / "config" / "fw_layout.json"
    got = fw_ident.load_layouts(repo)[0]
    assert got["id"] == fw_ident.DEFAULT_LAYOUT["id"]
    # hex strings in JSON must resolve to the same numbers as the Python fallback
    assert fw_ident._to_int(got["marker"]["offset"]) == LAYOUT["marker"]["offset"]
    for f in got["fields"]:
        want = dict((x["key"], x) for x in LAYOUT["fields"])[f["key"]]
        assert fw_ident._to_int(f["offset"]) == want["offset"]
        assert fw_ident._to_int(f["length"]) == want["length"]


def test_hex_and_int_offsets_agree():
    hexed = dict(LAYOUT)
    hexed["fields"] = [dict(f, offset=hex(f["offset"])) for f in LAYOUT["fields"]]
    blob = _image()
    assert fw_ident.identify_bytes(blob, [hexed]) == fw_ident.identify_bytes(blob, [LAYOUT])


def test_field_outside_the_window_is_skipped_not_fatal():
    lay = dict(LAYOUT)
    lay["fields"] = LAYOUT["fields"] + [{"key": "map1", "offset": 0, "length": 4}]
    # the bogus map1 sits outside the window, so the real one must survive
    assert fw_ident.identify_bytes(_image(), [lay])["code"] == "23ECCLGPSMD"


def test_only_the_identity_window_is_read():
    """Perf guarantee: /api/firmware polls every 1.5 s — never pull 320 KB per file."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, _image())
        calls = []
        orig = Path.open

        def spy(self, *a, **k):
            fh = orig(self, *a, **k)

            class Wrapped:
                def __enter__(inner):
                    return inner

                def __exit__(inner, *exc):
                    fh.close()

                def seek(inner, n):
                    calls.append(("seek", n)); return fh.seek(n)

                def read(inner, n=-1):
                    calls.append(("read", n)); return fh.read(n)

            return Wrapped()

        Path.open = spy
        try:
            assert fw_ident.identify_file(p)["code"] == "23ECCLGPSMD"
        finally:
            Path.open = orig
        assert [c[0] for c in calls] == ["seek", "read"], calls
        assert calls[1][1] <= 256, f"read {calls[1][1]} B, expected the window only"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
