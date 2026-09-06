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
        assert all(set(p) == {"name", "keys"} for p in out), out
    # the one real slot in that third case survived
    assert main._norm_presets([{"name": "a", "keys": ["rpm"]}])[0] == {"name": "a", "keys": ["rpm"]}


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
        assert json.loads(path.read_text()) == want
        assert main._norm_presets(main._load_presets()) == want


def test_endpoint_normalises_stores_and_answers():
    _catalog()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "presets.json"
        main._presets_path = lambda: path
        r = asyncio.run(main.set_presets({"presets": [
            {"name": "  Dyno  ", "keys": ["rpm", "nope", "tmot"]},
            {"name": "much too long", "keys": []},
        ]}))
        assert r["presets"][0] == {"name": "Dyno", "keys": ["rpm", "tmot"]}, r["presets"][0]
        assert r["presets"][1]["name"] == "much too", r["presets"][1]
        assert len(r["presets"]) == main.PRESET_SLOTS
        # what came back is what the snapshot serves and what landed on disk
        assert main.state.snapshot()["presets"] == r["presets"]
        assert json.loads(path.read_text()) == r["presets"]


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
