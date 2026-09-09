"""Name validation and zip bundling, plus the two GET download endpoints.

The point of the module is that these guards can be exercised without importing
app.main — which builds the FastAPI app and reads /etc. The endpoint tests at
the bottom do import it, the way tests/test_logs_api.py does.
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.web.archive import (flat_entries, resolve_in, safe_name, split_names,
                             zip_entries)

passed = failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print("ok   " + name)
    except Exception as e:                                  # noqa: BLE001
        failed += 1
        print("FAIL %s: %s" % (name, e))


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="archive-"))


# ---------- names ----------
def t_safe_name_refuses_dot_dot():
    """The hole this module was extracted to close.

    Path("..").name is "..", and "." is in the allowed charset, so the old rule
    let it straight through and handed the caller the parent directory. Every
    call site happened to guard with is_file(), but that is the caller's luck,
    not the rule's doing.
    """
    for bad in ("..", "../..", "a/..", ".", "", "../etc/passwd/..", "\\..",):
        try:
            got = safe_name(bad)
            raise AssertionError("accepted %r as %r" % (bad, got))
        except ValueError:
            pass


def t_safe_name_keeps_the_basename():
    assert safe_name("dump.bin") == "dump.bin"
    assert safe_name("a/b/dump.bin") == "dump.bin"
    assert safe_name("23ECCLGPSMD.bin.txt") == "23ECCLGPSMD.bin.txt"
    # a trailing slash is just a directory-ish spelling of the same basename
    assert safe_name("slash/") == "slash"
    for bad in ("space name.bin", "semi;colon", "quote'", "pipe|"):
        try:
            safe_name(bad)
            raise AssertionError("accepted " + bad)
        except ValueError:
            pass


def t_split_names():
    assert split_names("a.bin,b.bin") == ["a.bin", "b.bin"]
    assert split_names("a.bin,,b.bin,") == ["a.bin", "b.bin"]
    assert split_names("") == [] and split_names(None) == []


# ---------- resolving ----------
def t_resolve_in_stays_in_the_directory():
    d = tmpdir()
    try:
        (d / "one.bin").write_bytes(b"1")
        (d.parent / "outside.bin").write_bytes(b"x")
        assert resolve_in(d, "one.bin").name == "one.bin"
        assert resolve_in(d, "..") is None
        assert resolve_in(d, "../outside.bin") is None
        assert resolve_in(d, "missing.bin") is None
        # a directory is not a file
        (d / "sub").mkdir()
        assert resolve_in(d, "sub") is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
        (d.parent / "outside.bin").unlink(missing_ok=True)


def t_resolve_in_refuses_a_symlink_pointing_out():
    d = tmpdir()
    try:
        target = d.parent / ("target-%s.bin" % d.name)
        target.write_bytes(b"secret-ish")
        os.symlink(target, d / "link.bin")
        # safe_name is happy with the name; the parent check is what refuses it
        assert resolve_in(d, "link.bin") is None
        target.unlink()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- bundling ----------
def names_in(blob):
    return sorted(zipfile.ZipFile(io.BytesIO(blob)).namelist())


def t_zip_entries_bundles_and_dedupes():
    d = tmpdir()
    try:
        (d / "a.bin").write_bytes(b"aaa")
        (d / "b.bin").write_bytes(b"bbb")
        blob = zip_entries([("a.bin", d / "a.bin"), ("b.bin", d / "b.bin"),
                            ("a.bin", d / "a.bin")])
        assert names_in(blob) == ["a.bin", "b.bin"]
        z = zipfile.ZipFile(io.BytesIO(blob))
        assert z.read("a.bin") == b"aaa"
        # a missing path is skipped rather than raising
        assert names_in(zip_entries([("gone.bin", d / "gone.bin")])) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_flat_entries_pulls_in_the_description():
    """A dump is worth little without its notes, so the .txt rides along and
    the download round-trips back through the uploader unchanged."""
    d = tmpdir()
    try:
        (d / "stock.bin").write_bytes(b"\x00" * 8)
        (d / "stock.bin.txt").write_text("Drawing: 23ECCLGPSMD\n")
        (d / "other.bin").write_bytes(b"\x01" * 8)
        entries = flat_entries(d, ["stock.bin", "other.bin"], sidecars=(".txt",))
        assert [a for a, _ in entries] == ["stock.bin", "stock.bin.txt", "other.bin"]
        assert names_in(zip_entries(entries)) == ["other.bin", "stock.bin", "stock.bin.txt"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_flat_entries_skips_what_it_cannot_have():
    d = tmpdir()
    try:
        (d / "a.bin").write_bytes(b"a")
        entries = flat_entries(d, ["a.bin", "..", "../etc/passwd", "missing.bin", "a.bin"])
        assert [a for a, _ in entries] == ["a.bin"], entries
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- the endpoints ----------
import asyncio                                              # noqa: E402
from app import main                                        # noqa: E402
from app.web.storage import StorageManager, day_name        # noqa: E402


def body_of(resp):
    return resp.body


def t_firmware_download_one_and_many():
    d = tmpdir()
    try:
        (d / "stock.bin").write_bytes(b"\x00" * 16)
        (d / "stock.bin.txt").write_text("notes")
        (d / "tuned.bin").write_bytes(b"\x01" * 16)

        class FakeFwm:
            fw_dir = d

        old = main.fwm
        main.fwm = FakeFwm()
        try:
            one = main.firmware_download_many(names="stock.bin")
            assert one.path.name == "stock.bin", one
            assert one.filename == "stock.bin"

            many = main.firmware_download_many(names="stock.bin,tuned.bin")
            assert names_in(body_of(many)) == ["stock.bin", "stock.bin.txt", "tuned.bin"]
            assert "attachment" in many.headers["content-disposition"]

            assert main.firmware_download_many(names="").status_code == 400
            assert main.firmware_download_many(names="..").status_code == 404
            assert main.firmware_download_many(names="gone.bin,also-gone.bin").status_code == 404
        finally:
            main.fwm = old
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_logs_download_keeps_the_day_folder():
    d = tmpdir()
    try:
        day = day_name()
        (d / day).mkdir()
        (d / day / "kline-dec-1.csv").write_text("time,rpm\n")
        (d / day / "kline-dec-2.csv").write_text("time,rpm\n")
        (d / "loose.csv").write_text("old layout\n")            # pre-folder file
        old_storage = main.storage
        main.storage = StorageManager(str(d), {})
        try:
            resp = main.logs_download_get(
                names="%s/kline-dec-1.csv,%s/kline-dec-2.csv,loose.csv" % (day, day))
            got = names_in(body_of(resp))
            assert got == sorted(["%s/kline-dec-1.csv" % day,
                                  "%s/kline-dec-2.csv" % day, "loose.csv"]), got
            assert resp.headers["content-disposition"].endswith('"k-line.log.zip"')

            # the POST is the same bundle
            post = main.logs_download({"names": ["loose.csv"], "zipname": "pick"})
            assert names_in(body_of(post)) == ["loose.csv"]
            assert post.headers["content-disposition"].endswith('"pick.zip"')

            # nothing resolvable is a 404, not an empty zip
            assert main.logs_download_get(names="../../etc/passwd").status_code == 404
            assert main.logs_download_get(names="").status_code == 404
        finally:
            main.storage = old_storage
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_a_hostile_zipname_cannot_escape_the_header():
    d = tmpdir()
    try:
        (d / "a.csv").write_text("x\n")
        old_storage = main.storage
        main.storage = StorageManager(str(d), {})
        try:
            resp = main.logs_download_get(names="a.csv", zipname='../../evil"; rm -rf /')
            cd = resp.headers["content-disposition"]
            assert '"' not in cd.split("filename=")[1][1:-1], cd
            assert ".." not in cd, cd
        finally:
            main.storage = old_storage
    finally:
        shutil.rmtree(d, ignore_errors=True)


for name, fn in [
    ("'..' no longer survives the name check", t_safe_name_refuses_dot_dot),
    ("a safe name is the basename and nothing else", t_safe_name_keeps_the_basename),
    ("the names query parameter splits and drops blanks", t_split_names),
    ("resolving stays inside the directory", t_resolve_in_stays_in_the_directory),
    ("a symlink out of the directory is refused", t_resolve_in_refuses_a_symlink_pointing_out),
    ("the bundler dedupes and skips what is gone", t_zip_entries_bundles_and_dedupes),
    ("a firmware bundle carries each description", t_flat_entries_pulls_in_the_description),
    ("unsafe and missing names are skipped, not fatal", t_flat_entries_skips_what_it_cannot_have),
    ("one image comes back whole, several as a zip", t_firmware_download_one_and_many),
    ("a log bundle mirrors the board's day folders", t_logs_download_keeps_the_day_folder),
    ("a hostile zip name cannot break out of the header", t_a_hostile_zipname_cannot_escape_the_header),
]:
    test(name, fn)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
