"""Offline tests for the log endpoints once logs live in per-day folders.

The interesting part is the path check: ``/api/logs/{name:path}`` now accepts a
slash, so the rule that keeps a request inside the log directory had to be
rewritten. These tests are that rule's guard rail.

Run directly:  python tests/test_logs_api.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402
from app.web.storage import StorageManager, day_name  # noqa: E402

DAY = day_name()


def _root(tmp) -> Path:
    """Point the endpoints at a temp tree with one day folder and a legacy file."""
    root = Path(tmp)
    (root / DAY).mkdir(parents=True)
    (root / DAY / "kline-dec-20260831-193200.csv").write_text("time,rpm\n1,2\n")
    (root / DAY / "diag-20260831-193200.log").write_text("hello\n")
    (root / "kline-dec-20260819-101500.csv").write_text("time,rpm\n")   # legacy
    main.storage = StorageManager(str(root), {})
    return root


def _clear():
    main.storage = None


# -- name validation --------------------------------------------------------
def test_plain_and_dated_names_pass():
    assert main._safe_rel("kline-dec-1.csv") == "kline-dec-1.csv"
    assert main._safe_rel(f"{DAY}/kline-dec-1.csv") == f"{DAY}/kline-dec-1.csv"


def test_traversal_is_rejected():
    for bad in ("../etc/hosts", f"{DAY}/../../etc/hosts", "..", ".",
                "/etc/hosts", f"{DAY}/..", "a/b/c.csv", "", "/"):
        try:
            main._safe_rel(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_only_a_real_day_folder_is_a_folder():
    for bad in ("2026-08-31/x.csv", "etc/x.csv", "31-13-2026/x.csv"):
        try:
            main._safe_rel(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_backslashes_do_not_smuggle_a_segment():
    try:
        main._safe_rel("..\\..\\etc\\hosts")
    except ValueError:
        return
    raise AssertionError("backslash path accepted")


# -- confinement ------------------------------------------------------------
def test_log_file_stays_inside_the_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        assert main._log_file(f"{DAY}/diag-20260831-193200.log") is not None
        assert main._log_file("kline-dec-20260819-101500.csv") is not None
        assert main._log_file("nope.csv") is None
        outside = Path(tmp).parent / "outside.csv"
        try:
            outside.write_text("x")
            assert main._log_file(f"../{outside.name}") is None, "escaped the root"
        finally:
            outside.unlink(missing_ok=True)
            _clear()


# -- listing ----------------------------------------------------------------
def test_listing_covers_day_folders_and_legacy_files():
    with tempfile.TemporaryDirectory() as tmp:
        _root(tmp)
        try:
            res = asyncio.run(main.list_logs())
            names = {f["name"] for f in res["files"]}
            assert f"{DAY}/kline-dec-20260831-193200.csv" in names
            assert "kline-dec-20260819-101500.csv" in names, "legacy files stay visible"
            days = {f["name"]: f["day"] for f in res["files"]}
            assert days["kline-dec-20260819-101500.csv"] == ""
            assert res["dest"] == "internal" and res["fallback"] == ""
        finally:
            _clear()


def test_listing_can_be_filtered_to_the_system_log():
    with tempfile.TemporaryDirectory() as tmp:
        _root(tmp)
        try:
            res = asyncio.run(main.list_logs(kind="diag"))
            assert [f["file"] for f in res["files"]] == ["diag-20260831-193200.log"]
        finally:
            _clear()


def test_delete_removes_the_file_and_the_emptied_day_folder():
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        try:
            asyncio.run(main.delete_log(f"{DAY}/kline-dec-20260831-193200.csv"))
            asyncio.run(main.delete_log(f"{DAY}/diag-20260831-193200.log"))
            assert not (root / DAY).exists(), "an empty day folder is swept up"
            assert (root / "kline-dec-20260819-101500.csv").exists()
        finally:
            _clear()


def test_delete_refuses_to_walk_out():
    with tempfile.TemporaryDirectory() as tmp:
        _root(tmp)
        try:
            res = asyncio.run(main.delete_log("../outside.csv"))
            assert getattr(res, "status_code", 0) in (400, 404), res
        finally:
            _clear()


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
