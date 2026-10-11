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


def test_the_named_channels_carry_the_names_the_firmware_gives_them():
    """0x75 and 0x62 were named by the 2026-09-10 ride; 0x5C, 0x6A and 0x58 were
    renamed, and 0x6E and 0x70 named, by the 2026-10-08 read of the code behind
    each slot. The 2026-10-11 cross-check settled 0x6E, 0x70 and 0x77 from that
    code; 0x3E keeps 'check' because the code names the input, not the sensor."""
    for key, rli, group, mp in (("engine_run", 0x75, "known", "onoff"),
                                ("idle_mode", 0x62, "known", "idle_mode"),
                                ("throttle_zone", 0x5C, "known", "throttle_zone"),
                                ("tps_bypass", 0x6A, "known", ""),
                                ("fuel_mode", 0x58, "known", "fuel_mode"),
                                ("overheat", 0x6E, "known", "onoff"),
                                ("fans", 0x70, "known", "fans"),
                                ("p36_permit", 0x77, "known", "kill_run"),
                                ("an1", 0x3E, "check", "")):
        p = BY_KEY.get(key)
        assert p, f"{key} is missing from params.json"
        assert p["rli"] == rli, (key, hex(p["rli"]))
        assert p["group"] == group, (key, p["group"])
        assert p.get("map", "") == mp, (key, p.get("map"))
        assert not p.get("dead"), f"{key} has code behind it"
    for gone in ("r75", "r62", "r5c", "r6a", "warmup_on", "warmup_enrich",
                 "engine_state", "r6e", "r70"):
        assert gone not in BY_KEY, f"{gone} was renamed; the old key must not linger"
    assert "motorstate" not in MAPS and "warmup_on" not in MAPS
    assert MAPS["fuel_mode"]["type"] == "enum", "0x58 is a mode number, not a bitfield"


def test_each_fan_request_sits_beside_the_output_it_drives():
    """Handler 0x22DA2 packs D3A6 (fan 2, P3.3) into bit 0, D3A7 (fan 1, P7.1)
    into bit 1, D3A4 into bit 3 and D3A5 into bit 4; the fan logic at 0x22BB6
    drives D3A6 from D3A4 and D3A7 from D3A5. The first labels paired the
    requests with the wrong fans."""
    v = MAPS["fans"]["values"]
    assert v["01"] == "Fan 2 on" and v["08"] == "Fan 2 requested"
    assert v["02"] == "Fan 1 on" and v["10"] == "Fan 1 requested"


def test_advance_is_signed():
    """A retarded spark is a negative angle; unsigned it would read ~6550 deg."""
    for key in ("advance", "advance2"):
        assert BY_KEY[key]["signed"] is True, key


def test_stubs_and_calibration_constants_are_dead_and_the_rest_are_not():
    """Dead is 'answers, but never says anything on this software'.

    Two reasons qualify: the rli is served from a shared zero slot (no code
    behind it -- the rear bank among them, stubbed in every Moto Morini image),
    or every Granpasso calibration holds its source still (0x74, 0x63) -- each
    such name says 'constant by calibration'. The learned trims 0x5D/0x5E/0x5F
    and the CO trim 0x3D depend on the build and stay selectable, as do 0x77
    (const 1) and 0x3E (const 52), live inputs not yet seen in every state.
    """
    sel = _selectable()
    for key in ("lambda_r", "lambda_int_r", "lambda_reg_r", "lambda_ctl_r"):
        assert BY_KEY[key].get("dead") is True, f"{key} is a stub"
        assert key not in sel
    for key in ("r74", "r63"):
        p = BY_KEY[key]
        assert p.get("dead") is True, f"{key} is constant by calibration"
        assert "constant by calibration" in p["name"], f"{key} should say why"
        assert key not in sel
    for key in ("p36_permit", "an1", "co_trim", "adapt_idle", "adapt_part", "adapt_mult"):
        assert not BY_KEY[key].get("dead"), f"{key} is live on some build"
        assert key in sel, f"{key} still has a state left to record"
    for gone in ("r5d", "r5e", "r5f", "r77", "r3e"):
        assert gone not in BY_KEY, f"{gone} was renamed; the old key must not linger"
    assert sum(1 for p in PARAMS["params"] if p.get("dead")) == 38 + 2


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


def test_an_rli_is_asked_for_the_same_way_by_every_channel_that_shares_it():
    """The UI prices a selection at one request per distinct rli; the worker
    dedupes on ``(rli, with_addr)``, and ``with_addr`` is ``fmt != 0``
    (``app/kline/params.py``). Two channels on one rli with different ``fmt``
    would therefore cost two requests and be priced as one. The catalog does not
    carry ``with_addr``, so rather than plumb it through for a case that does not
    exist, the case is kept from existing."""
    fmts = {}
    for p in PARAMS["params"]:
        if p.get("dead") or p.get("derived"):
            continue
        fmts.setdefault(p["rli"], set()).add(int(p.get("fmt", 0)))
    mixed = {hex(rli): sorted(f) for rli, f in fmts.items() if len(f) > 1}
    assert not mixed, f"these rli are requested two ways, so the cost estimate lies: {mixed}"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
