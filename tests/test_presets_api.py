"""Offline tests for the three parameter presets the Logger tab switches between.

``_norm_presets()`` is the only validation point on the way in: whatever the UI
posts, and whatever an old presets.json holds, has to come out as exactly three
slots naming channels that still exist. A params.json edit can retire a channel a
preset still names, and the decoded CSV writes its columns in selection order, so
the order of what survives matters too.

Run directly:  python tests/test_presets_api.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402

CAT = [{"key": k} for k in ("rpm", "vbat", "tmot", "r36")]


def _catalog():
    main.state.set_catalog([dict(c) for c in CAT], ["rpm"])


def test_three_slots_whatever_comes_in():
    _catalog()
    for raw in (None, [], "nonsense", [{"name": "a", "keys": ["rpm"]}],
                [{}] * 5, [1, 2, 3]):
        out = main._norm_presets(raw)
        assert len(out) == main.PRESET_SLOTS, f"{raw!r} -> {out!r}"
        assert all(set(p) == {"name", "keys", "note"} for p in out), out
    # the one real slot in that third case survived
    assert main._norm_presets([{"name": "a", "keys": ["rpm"]}])[0] == \
        {"name": "a", "keys": ["rpm"], "note": ""}


def test_name_is_trimmed_then_clipped():
    _catalog()
    out = main._norm_presets([{"name": "  cold starting  ", "keys": []}])
    assert out[0]["name"] == "cold sta", out[0]["name"]
    assert main._norm_presets([{"name": None, "keys": []}])[0]["name"] == ""


def test_unknown_keys_are_dropped_and_order_is_kept():
    _catalog()
    out = main._norm_presets([{"name": "x", "keys": ["tmot", "gone", "rpm", "tmot"]}])
    assert out[0]["keys"] == ["tmot", "rpm"], out[0]["keys"]


def test_round_trip_through_the_file():
    _catalog()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "presets.json"
        main._presets_path = lambda: path
        want = main._norm_presets([{"name": "Dyno", "keys": ["rpm", "tmot"]}])
        main._save_presets(want)
        assert json.loads(path.read_text())["slots"] == want
        assert main._norm_presets(main._split_presets_file(main._load_presets())[0]) == want


def test_endpoint_normalises_stores_and_answers():
    _catalog()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "presets.json"
        main._presets_path = lambda: path
        r = asyncio.run(main.set_presets({"presets": [
            {"name": "  Dyno  ", "keys": ["rpm", "nope", "tmot"]},
            {"name": "much too long", "keys": []},
        ]}))
        assert r["presets"][0] == {"name": "Dyno", "keys": ["rpm", "tmot"], "note": ""}, \
        r["presets"][0]
        assert r["presets"][1]["name"] == "much too", r["presets"][1]
        assert len(r["presets"]) == main.PRESET_SLOTS
        # what came back is what the snapshot serves and what landed on disk
        assert main.state.snapshot()["presets"] == r["presets"]
        # the file carries the free note beside the slots now; the old bare-list
        # shape is still read back (test_the_old_bare_list_still_loads)
        on_disk = json.loads(path.read_text())
        assert on_disk["slots"] == r["presets"], on_disk
        assert on_disk["free_note"] == r["free_note"]


def test_first_preset_is_the_boot_default_but_never_an_override():
    valid = {"rpm", "vbat", "tmot"}
    presets = [{"name": "Dyno", "keys": ["rpm", "tmot"]}, {"name": "", "keys": ["vbat"]},
               {"name": "", "keys": []}]
    # a remembered selection wins: a hand-picked set survives the reboot
    assert main._boot_selection(["vbat"], presets, valid) == ["vbat"]
    # nothing remembered -> the first preset
    assert main._boot_selection(None, presets, valid) == ["rpm", "tmot"]
    assert main._boot_selection([], presets, valid) == ["rpm", "tmot"]
    # a remembered selection of retired channels is nothing remembered
    assert main._boot_selection(["gone"], presets, valid) == ["rpm", "tmot"]
    # first preset empty -> nothing, and the worker's named-default set stands
    empty = [{"name": "", "keys": []}] * 3
    assert main._boot_selection(None, empty, valid) == []
    # a preset naming a channel that params.json no longer has is filtered here too
    stale = [{"name": "", "keys": ["rpm", "gone"]}, {"name": "", "keys": []}, {"name": "", "keys": []}]
    assert main._boot_selection(None, stale, valid) == ["rpm"]


def test_poll_cost_is_per_request_and_smoothed():
    main.state.poll_req_ms = 0.0
    main.state.set_poll_cost(0.180, 9)          # 180 ms for nine requests
    assert main.state.poll_req_ms == 20.0, main.state.poll_req_ms
    main.state.set_poll_cost(0.360, 9)          # 40 ms each -> smoothed towards it
    assert 20.0 < main.state.poll_req_ms < 40.0, main.state.poll_req_ms
    # a cycle that polled nothing says nothing about the cost of a request
    before = main.state.poll_req_ms
    main.state.set_poll_cost(0.5, 0)
    main.state.set_poll_cost(0.0, 4)
    assert main.state.poll_req_ms == before
    assert main.state.snapshot()["poll_req_ms"] == before


def test_snapshot_hands_out_copies():
    _catalog()
    main.state.set_presets(main._norm_presets([{"name": "a", "keys": ["rpm"]}]))
    snap = main.state.snapshot()
    snap["presets"][0]["keys"].append("vbat")
    assert main.state.snapshot()["presets"][0]["keys"] == ["rpm"], "the UI edits its own copy"


# ---------- notes ----------
# A preset says which channels; the note says how to capture them — cold engine,
# before the fan, that sort of thing. It is the part that used to live in the
# rider's head and go missing between rides.

def test_a_note_survives_the_round_trip():
    _catalog()
    out = main._norm_presets([{"name": "a", "keys": ["rpm"], "note": "## Cold\n- fan off"}])
    assert out[0]["note"] == "## Cold\n- fan off"
    # state.set_presets() rebuilds the dicts, so the note has to be carried there too
    main.state.set_presets(out)
    assert main.state.snapshot()["presets"][0]["note"] == "## Cold\n- fan off"


def test_a_note_is_cut_by_the_same_rule_as_a_name():
    _catalog()
    long = "x" * (main.NOTE_MAX + 500)
    out = main._norm_presets([{"name": "a", "keys": ["rpm"], "note": long}])
    assert len(out[0]["note"]) == main.NOTE_MAX
    assert main._norm_note(long) == out[0]["note"], "one cut, not two"
    for junk in (None, 7, {"a": 1}):
        assert isinstance(main._norm_presets([{"note": junk}])[0]["note"], str)


def test_the_free_note_belongs_to_a_set_with_no_slot():
    """Which preset is "active" is derived from the live selection, never stored,
    so a hand-picked set has nowhere of its own — hence one free note."""
    _catalog()
    main.state.set_presets(main._norm_presets([]), "free text")
    assert main.state.snapshot()["free_note"] == "free text"
    # set_presets without the argument must not wipe it
    main.state.set_presets(main._norm_presets([]))
    assert main.state.snapshot()["free_note"] == "free text"


def test_the_old_bare_list_still_loads():
    """Every board in the field has a bare list in /etc/onboard-logger. Reading
    only the new shape would drop three working presets on the first start after
    an update."""
    old = [{"name": "Cold", "keys": ["rpm"]}, {"name": "Adv", "keys": ["vbat"]}]
    slots, free = main._split_presets_file(old)
    assert slots == old and free == ""
    new = {"slots": old, "free_note": "hello"}
    slots, free = main._split_presets_file(new)
    assert slots == old and free == "hello"
    # and anything else does not raise
    for junk in (None, "nonsense", 7):
        slots, free = main._split_presets_file(junk)
        assert free == ""


def test_the_file_round_trips_through_disk():
    _catalog()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "presets.json"
        old_path = main._presets_path
        main._presets_path = lambda: path
        try:
            presets = main._norm_presets([{"name": "a", "keys": ["rpm"], "note": "keep me"}])
            main._save_presets(presets, "free too")
            slots, free = main._split_presets_file(main._load_presets())
            assert main._norm_presets(slots)[0]["note"] == "keep me"
            assert free == "free too"
            # written as text, not as escaped code points: a Russian note has to
            # stay readable to anyone opening the file on the board
            main._save_presets(main._norm_presets(
                [{"name": "a", "keys": ["rpm"], "note": "cold engine"}]), "")
            assert "cold engine" in path.read_text()
        finally:
            main._presets_path = old_path


def test_the_endpoint_carries_the_free_note():
    _catalog()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "presets.json"
        old_path = main._presets_path
        main._presets_path = lambda: path
        try:
            r = asyncio.run(main.set_presets({
                "presets": [{"name": "a", "keys": ["rpm"], "note": "n1"}],
                "free_note": "f1",
            }))
            assert r["presets"][0]["note"] == "n1"
            assert r["free_note"] == "f1"
            # a post that says nothing about the free note leaves it alone
            r = asyncio.run(main.set_presets({"presets": [{"name": "b", "keys": ["rpm"]}]}))
            assert r["free_note"] == "f1", r
        finally:
            main._presets_path = old_path


def test_a_dead_rli_cannot_enter_a_preset_or_a_selection():
    """34 of the 79 rli are answered from a shared placeholder slot in the
    firmware, so they can only ever log a column of zeros while costing a request
    per poll cycle. ``_selectable_keys()`` is the single gate: a preset, a stored
    selection or a POST naming one must come back without it."""
    main.state.set_catalog(
        [{"key": "rpm"}, {"key": "vbat"}, {"key": "r72", "dead": True}], ["rpm"])
    assert main._selectable_keys() == {"rpm", "vbat"}, main._selectable_keys()

    out = main._norm_presets([{"name": "x", "keys": ["rpm", "r72", "vbat"]}])
    assert out[0]["keys"] == ["rpm", "vbat"], out[0]

    # a selected.json written before the channel was known to be dead
    sel = main._boot_selection(["rpm", "r72"], out, main._selectable_keys())
    assert sel == ["rpm"], sel

    # and the API drops it rather than handing the worker a request to waste
    got = asyncio.run(main.set_selected({"keys": ["r72", "vbat"]}))
    assert got["selected"] == ["vbat"], got

    # a board that predates the flag sends no `dead` at all -- nothing is dropped
    main.state.set_catalog([{"key": "rpm"}, {"key": "r72"}], [])
    assert main._selectable_keys() == {"rpm", "r72"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
