"""AddonManager: install, serve, store, remove.

The point of most of these is what is refused. This code runs as root next to
/opt, and an add-on archive is something the rider downloaded from the internet
on a phone.
"""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.web.addons import (AddonManager, MAX_DATA_FILE, MAX_DATA_TOTAL,
                            safe_addon, safe_data)

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


def tmp_root():
    return Path(tempfile.mkdtemp(prefix="addons-"))


def make_tar(path, files, mode="w:gz"):
    with tarfile.open(path, mode) as t:
        for name, blob in files.items():
            if isinstance(blob, str):
                blob = blob.encode()
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, blob in files.items():
            z.writestr(name, blob)


VIEWER = {
    "addon.json": json.dumps({"name": "maps", "version": "0.3.0",
                              "title": "ECU map viewer"}),
    "index.html": "<title>ECU map viewer</title>",
    "js/app.js": "// app",
    "css/style.css": "body{}",
}


def installed_manager():
    root = tmp_root()
    am = AddonManager(root)
    arc = root.parent / (root.name + ".tar.gz")
    make_tar(arc, VIEWER)
    name, err = am.install(arc)
    assert (name, err) == ("maps", ""), (name, err)
    return am, root, arc


# ---------- names ----------
def t_names():
    for good in ("maps", "a", "map-viewer", "x_9"):
        assert safe_addon(good) == good, good
    for bad in ("", "Maps", "../etc", "a/b", "-lead", "x" * 40, None, 7):
        assert safe_addon(bad) is None, bad
    for good in ("shared.xdf", "A-1_b.bin", "notes.txt"):
        assert safe_data(good) == good, good
    for bad in ("", ".hidden", "..", "a/b", "a\\b", "x" * 200, None):
        assert safe_data(bad) is None, bad


# ---------- install ----------
def t_install_tar():
    am, root, _ = installed_manager()
    assert (root / "maps" / "addon.json").is_file()
    assert (root / "maps" / "web" / "index.html").is_file()
    assert (root / "maps" / "web" / "js" / "app.js").is_file()
    assert (root / "maps" / "data").is_dir()
    listed = am.installed()
    assert len(listed) == 1 and listed[0]["name"] == "maps"
    assert listed[0]["version"] == "0.3.0"
    assert listed[0]["url"] == "/addons/maps/"
    shutil.rmtree(root, ignore_errors=True)


def t_install_zip_with_wrapper():
    # GitHub's "Download ZIP" wraps everything in one folder; it has to work
    root = tmp_root()
    am = AddonManager(root)
    arc = root / "a.zip"
    make_zip(arc, {"ecu-map-viewer-main/" + k: v for k, v in VIEWER.items()})
    name, err = am.install(arc)
    assert (name, err) == ("maps", ""), (name, err)
    assert (root / "maps" / "web" / "index.html").is_file()
    shutil.rmtree(root, ignore_errors=True)


def t_manifest_required():
    root = tmp_root()
    am = AddonManager(root)
    arc = root / "a.tar.gz"
    make_tar(arc, {"index.html": "hi"})
    assert am.install(arc) == ("", "err.addon_no_manifest")
    # ... and it has to name a usable add-on
    make_tar(arc, {"addon.json": json.dumps({"name": "../evil"}), "index.html": "hi"})
    assert am.install(arc) == ("", "err.addon_bad_name")
    make_tar(arc, {"addon.json": json.dumps({"name": "maps"})})
    assert am.install(arc) == ("", "err.addon_incomplete")
    assert am.installed() == []
    shutil.rmtree(root, ignore_errors=True)


def t_a_tar_of_a_directory_installs():
    """`tar -C dir -czf out .` names the root itself "./".

    That entry resolves to no path at all and was refused as a traversal, which
    killed the first archive release-addon.sh ever produced.
    """
    root = tmp_root()
    am = AddonManager(root)
    stage = root / "stage"
    for rel, blob in VIEWER.items():
        p = stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(blob)
    arc = root / "a.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(str(stage), arcname=".")
    assert am.install(arc) == ("maps", "")
    assert (root / "maps" / "web" / "js" / "app.js").is_file()
    shutil.rmtree(root, ignore_errors=True)


def t_unsafe_members_refused():
    root = tmp_root()
    am = AddonManager(root)
    arc = root / "a.tar.gz"
    escaped = root.parent / "escaped.html"
    make_tar(arc, dict(VIEWER, **{"../escaped.html": "pwned"}))
    assert am.install(arc) == ("", "err.addon_unsafe_member")
    assert not escaped.exists()

    # a symlink is not a page
    arc2 = root / "b.tar"
    with tarfile.open(arc2, "w") as t:
        for n, blob in VIEWER.items():
            info = tarfile.TarInfo(n)
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob.encode()))
        link = tarfile.TarInfo("web/passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        t.addfile(link)
    assert am.install(arc2) == ("", "err.addon_unsafe_member")
    assert am.installed() == []
    shutil.rmtree(root, ignore_errors=True)


def t_upgrade_keeps_the_store():
    am, root, arc = installed_manager()
    assert am.write_data("maps", "shared.xdf", b"<XDFFORMAT/>") == ""
    newer = dict(VIEWER)
    newer["addon.json"] = json.dumps({"name": "maps", "version": "0.4.0"})
    make_tar(arc, newer)
    assert am.install(arc) == ("maps", "")
    assert am.installed()[0]["version"] == "0.4.0"
    assert am.read_data("maps", "shared.xdf") == b"<XDFFORMAT/>", "the rider's files survived"
    # and nothing is left lying around beside it
    assert sorted(os.listdir(root)) == sorted([p for p in os.listdir(root)
                                               if not p.endswith((".new", ".old"))])
    shutil.rmtree(root, ignore_errors=True)


# ---------- serving ----------
def t_web_file():
    am, root, _ = installed_manager()
    assert am.web_file("maps", "").name == "index.html"
    assert am.web_file("maps", "js/app.js").read_text() == "// app"
    for bad in ("../addon.json", "../../etc/passwd", "/etc/passwd", "../data/x"):
        assert am.web_file("maps", bad) is None, bad
    assert am.web_file("nope", "index.html") is None
    assert am.web_file("maps", "missing.js") is None
    shutil.rmtree(root, ignore_errors=True)


def t_web_file_never_follows_a_symlink_out():
    am, root, _ = installed_manager()
    # named plainly: the pre-push scan looks for the word, and a test fixture
    # tripping it teaches everyone to wave the scanner through
    outside = root.parent / "outside.txt"
    outside.write_text("not for the web")
    os.symlink(outside, root / "maps" / "web" / "leak.txt")
    assert am.web_file("maps", "leak.txt") is None
    outside.unlink()
    shutil.rmtree(root, ignore_errors=True)


def t_content_type_never_guesses_into_something_executable():
    am, root, _ = installed_manager()
    assert am.content_type(Path("a.html")).startswith("text/html")
    assert am.content_type(Path("a.js")).startswith("application/javascript")
    # anything unlisted is bytes, not a type a browser will act on
    assert am.content_type(Path("a.xdf")) == "application/octet-stream"
    assert am.content_type(Path("a.svgz")) == "application/octet-stream"
    shutil.rmtree(root, ignore_errors=True)


# ---------- the store ----------
def t_store_round_trip():
    am, root, _ = installed_manager()
    assert am.write_data("maps", "shared.xdf", b"abc") == ""
    assert am.list_data("maps")["files"][0]["name"] == "shared.xdf"
    assert am.list_data("maps")["files"][0]["size"] == 3
    assert am.read_data("maps", "shared.xdf") == b"abc"
    assert am.delete_data("maps", "shared.xdf") == ""
    assert am.list_data("maps")["files"] == []
    shutil.rmtree(root, ignore_errors=True)


def t_store_refuses_to_leave_itself():
    am, root, _ = installed_manager()
    outside = root / "maps" / "web" / "index.html"
    before = outside.read_bytes()
    for bad in ("../web/index.html", "../../maps/addon.json", "/etc/passwd", ".."):
        assert am.write_data("maps", bad, b"pwned") == "err.addon_bad_name", bad
        assert am.read_data("maps", bad) is None, bad
    assert outside.read_bytes() == before
    shutil.rmtree(root, ignore_errors=True)


def t_store_caps():
    am, root, _ = installed_manager()
    assert am.write_data("maps", "big.xdf", b"x" * (MAX_DATA_FILE + 1)) == "err.addon_too_big"
    assert am.list_data("maps")["files"] == []
    # and the whole store has a ceiling, not just each file
    chunk = b"y" * MAX_DATA_FILE
    n = MAX_DATA_TOTAL // MAX_DATA_FILE
    for i in range(n):
        assert am.write_data("maps", "f%d.xdf" % i, chunk) == "", i
    assert am.write_data("maps", "one-too-many.xdf", chunk) == "err.addon_store_full"
    # replacing an existing file is not counted twice
    assert am.write_data("maps", "f0.xdf", chunk) == ""
    shutil.rmtree(root, ignore_errors=True)


def t_store_of_an_unknown_addon():
    root = tmp_root()
    am = AddonManager(root)
    assert am.write_data("nope", "a.xdf", b"x") == "err.addon_bad_name"
    assert am.read_data("nope", "a.xdf") is None
    assert am.list_data("nope") == {"files": []}
    shutil.rmtree(root, ignore_errors=True)


# ---------- remove ----------
def t_remove_takes_the_data_with_it():
    am, root, _ = installed_manager()
    am.write_data("maps", "shared.xdf", b"abc")
    assert am.remove("maps") == ""
    assert am.installed() == []
    assert not (root / "maps").exists(), "code and data go together"
    assert os.listdir(root) == [], "nothing left behind"
    assert am.remove("maps") == "err.addon_unknown"
    assert am.remove("../etc") == "err.addon_unknown"
    shutil.rmtree(root, ignore_errors=True)


def t_missing_root_is_not_an_error():
    am = AddonManager(Path(tempfile.gettempdir()) / "definitely-not-here-12345")
    assert am.installed() == []
    assert am.dir_of("maps") is None
    assert am.web_file("maps", "index.html") is None


# ---------- the endpoints ----------
# Same shape as tests/test_logs_api.py: the handlers are called directly, with
# the module's globals pointed at a temp tree.
import asyncio                                             # noqa: E402
from app import main                                       # noqa: E402


class FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


class FakeRequest:
    def __init__(self, data):
        self._data = data

    async def body(self):
        return self._data


def with_api(fn):
    am, root, arc = installed_manager()
    old_adm, old_dir = main.adm, main.ADDONS_DIR
    main.adm, main.ADDONS_DIR = am, root
    try:
        fn(am, root, arc)
    finally:
        main.adm, main.ADDONS_DIR = old_adm, old_dir
        shutil.rmtree(root, ignore_errors=True)


def body_of(resp):
    return resp.body if hasattr(resp, "body") else resp


def t_api_lists_and_serves():
    def go(am, root, arc):
        listed = asyncio.run(main.addons_list())
        assert [a["name"] for a in listed["addons"]] == ["maps"]

        resp = asyncio.run(main.addon_file("maps", "js/app.js"))
        assert getattr(resp, "status_code", 200) == 200
        assert resp.headers["x-content-type-options"] == "nosniff"

        # the page itself, with and without the trailing slash
        assert asyncio.run(main.addon_file("maps", "")).path.name == "index.html"
        assert asyncio.run(main.addon_root("maps")).status_code == 307
        assert asyncio.run(main.addon_root("nope")).status_code == 404
    with_api(go)


def t_api_refuses_to_serve_outside_the_addon():
    def go(am, root, arc):
        for bad in ("../addon.json", "../../../etc/passwd", "../data/x"):
            assert asyncio.run(main.addon_file("maps", bad)).status_code == 404, bad
    with_api(go)


def t_api_store_round_trip():
    def go(am, root, arc):
        r = asyncio.run(main.addon_data_write("maps", "shared.xdf",
                                              FakeRequest(b"<XDFFORMAT/>")))
        assert r == {"name": "shared.xdf", "size": 12}, r
        listed = asyncio.run(main.addon_data_list("maps"))
        assert listed["files"][0]["name"] == "shared.xdf"

        got = asyncio.run(main.addon_data_read("maps", "shared.xdf"))
        assert got.body == b"<XDFFORMAT/>"
        # never a type a browser will act on
        assert got.media_type == "application/octet-stream"

        assert asyncio.run(main.addon_data_delete("maps", "shared.xdf")) == {"name": "shared.xdf"}
        assert asyncio.run(main.addon_data_read("maps", "shared.xdf")).status_code == 404
    with_api(go)


def t_api_store_of_an_unknown_addon_is_404():
    def go(am, root, arc):
        assert asyncio.run(main.addon_data_list("nope")).status_code == 404
        assert asyncio.run(main.addon_data_write("nope", "a.xdf",
                                                 FakeRequest(b"x"))).status_code == 404
        assert asyncio.run(main.addon_data_write("maps", "../x",
                                                 FakeRequest(b"x"))).status_code == 400
        assert asyncio.run(main.addon_data_write("maps", "a.xdf",
                                                 FakeRequest(b""))).status_code == 400
    with_api(go)


def t_api_upload_and_remove():
    def go(am, root, arc):
        assert asyncio.run(main.addon_upload(
            FakeUpload("notes.txt", b"x"))).status_code == 400

        payload = arc.read_bytes()
        r = asyncio.run(main.addon_upload(FakeUpload("maps.tar.gz", payload)))
        assert r == {"ok": True, "name": "maps"}, r
        # the upload does not survive as a stray file in the add-on directory
        assert [p for p in os.listdir(root) if p.startswith(".upload-")] == []

        assert asyncio.run(main.addon_remove("maps")) == {"ok": True, "name": "maps"}
        assert asyncio.run(main.addon_remove("maps")).status_code == 404
    with_api(go)


for name, fn in [
    ("an add-on name is a slug and a stored name cannot escape", t_names),
    ("a tarball installs into web/ with a data dir beside it", t_install_tar),
    ("a zip with a wrapper folder installs too", t_install_zip_with_wrapper),
    ("a tar of a whole directory installs, root entry and all", t_a_tar_of_a_directory_installs),
    ("without a usable manifest and an index nothing is installed", t_manifest_required),
    ("traversal and symlinks in the archive are refused", t_unsafe_members_refused),
    ("an upgrade keeps the files the add-on was holding", t_upgrade_keeps_the_store),
    ("only files under web/ are served", t_web_file),
    ("a symlink inside web/ does not lead out of it", t_web_file_never_follows_a_symlink_out),
    ("an unlisted extension is served as bytes", t_content_type_never_guesses_into_something_executable),
    ("the store round-trips", t_store_round_trip),
    ("the store cannot be talked out of its directory", t_store_refuses_to_leave_itself),
    ("the store has a per-file and a total ceiling", t_store_caps),
    ("an unknown add-on has no store", t_store_of_an_unknown_addon),
    ("removing an add-on removes its data too", t_remove_takes_the_data_with_it),
    ("a missing addons directory is simply empty", t_missing_root_is_not_an_error),
    ("the endpoints list and serve an add-on", t_api_lists_and_serves),
    ("the endpoints serve nothing outside the add-on", t_api_refuses_to_serve_outside_the_addon),
    ("the store endpoints round-trip", t_api_store_round_trip),
    ("the store endpoints refuse an unknown add-on and a bad name", t_api_store_of_an_unknown_addon_is_404),
    ("upload installs and delete removes", t_api_upload_and_remove),
]:
    test(name, fn)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
