"""The write guard's decision table — one case per row, no ECU and no image needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import fw_catalog, fw_guard  # noqa: E402

CAT = {"entries": [
    {"code": "23ECCLGPSM", "space": "image", "brand": "Moto Morini", "model": "Granpasso 1200"},
    {"code": "23ECCLGPSM", "space": "live", "brand": "Moto Morini", "model": "Granpasso 1200"},
    {"code": "23ACMCOR", "space": "image", "brand": "Moto Morini", "model": "Corsaro 1200"},
    {"code": "96520610", "space": "live", "brand": "Ducati", "model": "1098"},
]}


def _d(img_code, live_code, **kw):
    return fw_guard.decide({"code": img_code}, {"Drawing": live_code}, CAT, **kw)


def test_same_bike_same_revision_is_ok():
    r = _d("23ECCLGPSMD", "23ECCLGPSMD")
    assert r["level"] == "ok" and r["reason"] == ""


def test_same_bike_other_revision_warns():
    r = _d("23ECCLGPSMC", "23ECCLGPSMD")
    assert r["level"] == "warn" and r["reason"] == "revision"
    assert r["image"]["rev"] == "C" and r["ecu"]["rev"] == "D"


def test_different_bike_is_blocked():
    r = _d("23ACMCORA", "23ECCLGPSMD")       # Corsaro image into a Granpasso
    assert r["level"] == "block" and r["reason"] == "model_mismatch"
    assert r["overridden"] is False


def test_override_downgrades_the_model_block_and_says_so():
    r = _d("23ACMCORA", "23ECCLGPSMD", override=True)
    assert r["level"] == "warn" and r["reason"] == "model_mismatch"
    assert r["overridden"] is True


def test_size_gate_is_never_overridable():
    for ov in (False, True):
        r = _d("23ECCLGPSMD", "23ECCLGPSMD", override=ov, size_ok=False)
        assert r["level"] == "block" and r["reason"] == "size_mismatch"
        assert r["overridden"] is False


def test_unidentified_image_warns_rather_than_blocking():
    r = _d("", "23ECCLGPSMD")
    assert r["level"] == "warn" and r["reason"] == "image_unknown"


def test_no_live_ecu_warns():
    r = _d("23ECCLGPSMD", "")
    assert r["level"] == "warn" and r["reason"] == "no_ecu"


def test_naive_string_compare_would_have_blocked_a_legitimate_ducati():
    """Live Drawing is an OEM part number there; the image holds a mnemonic."""
    r = fw_guard.decide({"code": "3215SWA5CAL"}, {"Drawing": "96520610B"}, CAT)
    assert r["level"] != "block"                    # the whole point of the design
    assert r["reason"] == "unresolved"
    assert r["ecu"]["brand"] == "Ducati" and r["image"]["resolved"] is False


def test_two_unknown_codes_sharing_a_family_read_as_a_revision():
    r = _d("ZZQQ1234A", "ZZQQ1234B")
    assert r["level"] == "warn" and r["reason"] == "revision"


def test_two_unrelated_unknown_codes_stay_unresolved():
    r = _d("ZZQQ1234A", "WWEE9999B")
    assert r["level"] == "warn" and r["reason"] == "unresolved"


def test_verdict_carries_both_sides_for_the_dialog():
    r = _d("23ECCLGPSMC", "23ECCLGPSMD")
    assert r["image"]["brand"] == "Moto Morini" and r["ecu"]["model"] == "Granpasso 1200"
    assert r["image"]["code"] == "23ECCLGPSMC"


def test_lowercase_and_padding_do_not_change_the_verdict():
    assert _d(" 23ecclgpsmd ", "23ECCLGPSMD")["level"] == "ok"


def test_real_seed_file_resolves_this_bike():
    repo = Path(__file__).resolve().parent.parent / "config" / "fw_catalog.json"
    cat = fw_catalog.load_catalog(repo)
    r = fw_guard.decide({"code": "23ECCLGPSMC"}, {"Drawing": "23ECCLGPSMD"}, cat)
    assert r["level"] == "warn" and r["reason"] == "revision"


def test_reference_entries_never_cause_a_block():
    """Imported tune lists name one bike several ways, so they must not resolve a
    side — otherwise flashing a 1098 map into a 1098 would read as another bike."""
    cat = {"entries": [
        {"code": "AAA111", "match": "exact", "space": "image", "brand": "Ducati",
         "model": "1098", "guard": False},
        {"code": "BBB222", "match": "exact", "space": "image", "brand": "Ducati",
         "model": "Superbike 1098 (USA)", "guard": False},
        {"code": "BBB222", "match": "exact", "space": "live", "brand": "Ducati",
         "model": "Superbike 1098 (USA)", "guard": False},
    ]}
    r = fw_guard.decide({"code": "AAA111"}, {"Drawing": "BBB222"}, cat)
    assert r["level"] == "warn", r
    assert r["reason"] == "unresolved"
    # the bike is still named for the human reading the dialog
    assert r["image"]["brand"] == "Ducati" and r["image"]["model"] == "1098"
    assert r["image"]["resolved"] is False


def test_the_shipped_catalog_cannot_block_on_a_reference_entry():
    repo = Path(__file__).resolve().parent.parent / "config" / "fw_catalog.json"
    cat = fw_catalog.load_catalog(repo)
    ref = [e for e in cat["entries"] if e.get("guard") is False]
    assert len(ref) > 100, "the imported tune lists should be in the seed"
    # two different Ducati reference codes must never come out as a hard block
    a, b = ref[0]["code"], ref[-1]["code"]
    r = fw_guard.decide({"code": a}, {"Drawing": b}, cat)
    assert r["level"] != "block", (a, b, r)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
