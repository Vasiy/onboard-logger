"""The shipped catalog has to agree with itself.

``config/params.json`` is the map, but three other files quote it: the presets
name channels by key — in their channel list and again in the order their tiles
sit in — ``status_maps.json`` owns the labels a status channel renders through,
and ``i18n.js`` carries the display name of every channel that has a real one. A rename that lands in one and not the others fails silently —
``_norm_presets()`` drops an unknown key without a word, and ``t()`` falls back
to the English name in params.json, which still *looks* right. So the agreement
is checked here rather than noticed on the bike.

Run directly:  python tests/test_catalog.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.kline.params import ParamMap  # noqa: E402

PARAMS = json.loads((ROOT / "config" / "params.json").read_text())
MAPS = json.loads((ROOT / "config" / "status_maps.json").read_text())["maps"]
PRESETS = json.loads((ROOT / "config" / "presets.json").read_text())
BY_KEY = {p["key"]: p for p in PARAMS["params"]}
RAW = re.compile(r"^r[0-9a-f]{2}$")      # a key still named after its rli


def _selectable():
    return ParamMap.load(ROOT / "config" / "params.json").selectable()


def test_the_2026_09_10_ride_named_four_channels():
    """0x75, 0x62, 0x5C and 0x6A stopped being raw rli that day."""
    for key, group, mp in (("engine_run", "known", "onoff"),
                           ("idle_mode", "known", "idle_mode"),
                           ("warmup_on", "check", "warmup_on"),
                           ("warmup_enrich", "check", "")):
        p = BY_KEY.get(key)
        assert p, f"{key} is missing from params.json"
        assert p["group"] == group, (key, p["group"])
        assert p.get("map", "") == mp, (key, p.get("map"))
    for gone in ("r75", "r62", "r5c", "r6a"):
        assert gone not in BY_KEY, f"{gone} was renamed; the old key must not linger"


def test_the_six_flat_zero_rli_are_dead_and_the_constants_are_not():
    """Dead is 'answers, but never says anything' — measured, not assumed.

    0x5D/0x5E/0x5F/0x63/0x6E/0x70 held a flat 0 through a whole ride (limiter,
    25 -> 96 C, 5375 rpm). 0x74/0x77 (const 1) and 0x3E (const 52) stay
    selectable on purpose: a constant is not a zero, and none of the three has
    ever been recorded with the ignition switched off, which is what the Sleep
    preset is for.
    """
    sel = _selectable()
    for key in ("r5d", "r5e", "r5f", "r63", "r6e", "r70"):
        assert BY_KEY[key].get("dead") is True, f"{key} should be dead"
        assert key not in sel, f"{key} is dead and must not be selectable"
    for key in ("r74", "r77", "r3e"):
        assert not BY_KEY[key].get("dead"), f"{key} is a constant, not a zero"
        assert key in sel, f"{key} still has a state left to record"


def test_every_preset_key_is_a_channel_that_can_be_polled():
    """A rename that forgets presets.json costs the preset, quietly."""
    sel = _selectable()
    for slot in PRESETS["slots"]:
        missing = [k for k in slot["keys"] if k not in sel]
        assert not missing, f"preset {slot['name']!r} names {missing}"
        assert len(slot["name"]) <= 8, slot["name"]     # PRESET_NAME_MAX


def test_every_tile_order_names_channels_too():
    """The order the tiles sit in is a fifth list of keys quoting params.json —
    the slots' own and the free one. A key that no longer exists is dropped by
    ``_norm_order()`` in silence, and the tile it placed slides back to wherever
    the catalog happens to put it."""
    sel = _selectable()
    for slot in PRESETS["slots"]:
        missing = [k for k in slot.get("order", []) if k not in sel]
        assert not missing, f"preset {slot['name']!r} orders {missing}"
    missing = [k for k in PRESETS.get("free_order", []) if k not in sel]
    assert not missing, f"the free order names {missing}"


def test_every_status_channel_names_a_map_that_exists():
    for p in PARAMS["params"]:
        if p.get("map"):
            assert p["map"] in MAPS, f"{p['key']} -> missing map {p['map']}"
            vals = MAPS[p["map"]].get("values", {})
            assert vals, f"map {p['map']} has no labels"


def test_a_named_channel_has_a_name_in_every_locale():
    """A key that is not r<hex> claims to be a channel with a real name, and a
    real name is an i18n key — otherwise it renders in English everywhere."""
    if shutil.which("node") is None:
        print("skip test_a_named_channel_has_a_name_in_every_locale (no node)")
        return
    out = subprocess.run(
        ["node", "-e", "const fs=require('fs');global.window={};"
         "eval(fs.readFileSync('app/static/i18n.js','utf8'));"
         "console.log(JSON.stringify(window.I18N))"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    loc = json.loads(out.stdout)
    named = [p["key"] for p in PARAMS["params"]
             if not p.get("dead") and not RAW.match(p["key"])]
    for key in named:
        for lang, table in loc.items():
            assert f"param.{key}" in table, f"param.{key} missing from {lang}"


def test_the_ui_couples_gear_to_what_the_board_derives_it_from():
    """app.js names gear's inputs itself, and has to keep naming the right ones.

    The list is duplicated in the browser rather than read from params.json:
    /etc/onboard-logger/params.json wins over the repo copy and outlives a
    deploy, so a new key there would never reach a board that already has the
    file. The duplicate is only safe while it agrees with ``_derive()``.
    """
    js = (ROOT / "app" / "static" / "app.js").read_text()
    m = re.search(r"const DERIVED_DEPS = \{(.+?)\};", js, re.S)
    assert m, "app.js no longer declares DERIVED_DEPS"
    table = m.group(1)
    logger = (ROOT / "app" / "kline" / "logger.py").read_text()
    body = logger.split("def _derive(")[1].split("\n    def ")[0]
    reads = set(re.findall(r'values\.get\("(\w+)"\)', body))
    assert reads, "_derive() no longer reads its inputs from the polled values"
    for kind in re.findall(r"(\w+): \{", table):
        assert f'kind != "{kind}"' in body or f'"{kind}"' in body, \
            f"app.js couples {kind}, which _derive() does not know"
    for key in re.findall(r'"(\w+)"', table):
        assert key in BY_KEY, f"app.js couples an unknown channel: {key}"
        assert key in reads, f"_derive() does not read {key}; the UI would tick it for nothing"
    need = re.search(r"need: \[([^\]]*)\]", table)
    assert need and set(re.findall(r'"(\w+)"', need.group(1))) == {"rpm", "speed"}, \
        "gear falls out of rpm over speed; nothing else is required"
    gear = BY_KEY["gear"]
    assert gear.get("derived") == "gear" and gear.get("rli") == 0, \
        "the derived channel must not carry a real rli -- it is never requested"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
