"""Layered calibration catalog: seed + /etc overlay, prefix matching, hand edits."""

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import fw_catalog  # noqa: E402

REPO = Path(__file__).resolve().parent.parent / "config" / "fw_catalog.json"


def _seed(tmp, entries):
    p = Path(tmp) / "seed.json"
    p.write_text(json.dumps({"entries": entries}))
    return p


def test_longest_prefix_wins():
    cat = {"entries": [
        {"code": "23EC", "brand": "Wrong", "model": "Wrong"},
        {"code": "23ECCLGPSM", "brand": "Moto Morini", "model": "Granpasso 1200"},
    ]}
    assert fw_catalog.match("23ECCLGPSMD", cat)["model"] == "Granpasso 1200"


def test_exact_beats_a_longer_prefix():
    cat = {"entries": [
        {"code": "23ECCLGPSM", "brand": "Moto Morini", "model": "Granpasso 1200"},
        {"code": "23ECCLGPSMD", "match": "exact", "brand": "Moto Morini", "model": "GP one-off"},
    ]}
    assert fw_catalog.match("23ECCLGPSMD", cat)["model"] == "GP one-off"
    assert fw_catalog.match("23ECCLGPSMC", cat)["model"] == "Granpasso 1200"


def test_space_keeps_image_and_live_codes_apart():
    cat = {"entries": [
        {"code": "23ECCLGPSM", "space": "image", "brand": "Moto Morini", "model": "Granpasso"},
        {"code": "96520610", "space": "live", "brand": "Ducati", "model": "1098"},
    ]}
    assert fw_catalog.match("96520610B", cat, "image") == {}
    assert fw_catalog.match("96520610B", cat, "live")["model"] == "1098"
    assert fw_catalog.match("23ECCLGPSMD", cat, "image")["model"] == "Granpasso"


def test_split_code_handles_both_code_shapes():
    cat = fw_catalog.load_catalog(REPO)
    assert fw_catalog.split_code("23ECCLGPSMD", cat, "image") == ("23ECCLGPSM", "D")
    assert fw_catalog.split_code("96520610B", cat, "live") == ("96520610", "B")
    # unknown code: trailing letter is a display-only guess
    assert fw_catalog.split_code("ZZQQ12345X", cat) == ("ZZQQ12345", "X")
    assert fw_catalog.split_code("1234", cat) == ("1234", "")


def test_unknown_code_is_empty_not_an_error():
    assert fw_catalog.match("NOPE", fw_catalog.load_catalog(REPO)) == {}
    assert fw_catalog.describe_entry({}) == {}


def test_bad_json_in_either_layer_degrades():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.json"
        bad.write_text("{{{")
        seed = _seed(tmp, [{"code": "AAA", "brand": "Seed"}])
        assert fw_catalog.load_catalog(seed, bad)["entries"][0]["brand"] == "Seed"
        assert fw_catalog.load_catalog(bad, seed)["entries"][0]["brand"] == "Seed"
        assert fw_catalog.load_catalog(bad, bad)["entries"] == []


def test_overlay_shadows_a_seed_but_never_hides_the_rest():
    """The reason this is not loaded through _cfg_file(): that would replace the seed."""
    with tempfile.TemporaryDirectory() as tmp:
        seed = _seed(tmp, [
            {"code": "23ECCLGPSM", "space": "image", "brand": "Moto Morini"},
            {"code": "23ACMCOR", "space": "image", "brand": "Moto Morini"},
        ])
        etc = Path(tmp) / "etc.json"
        fw_catalog.upsert({"code": "23ECCLGPSM", "space": "image",
                           "brand": "Renamed", "model": "GP"}, etc)
        fw_catalog.upsert({"code": "ZZNEW", "space": "image", "brand": "New"}, etc)
        cat = fw_catalog.load_catalog(seed, etc)
        got = {(e["code"], e["space"]): e for e in cat["entries"]}
        assert got[("23ECCLGPSM", "image")]["brand"] == "Renamed"   # overlay wins
        assert got[("23ACMCOR", "image")]["brand"] == "Moto Morini"  # seed survives
        assert got[("ZZNEW", "image")]["brand"] == "New"             # addition appears


def test_upsert_round_trips_and_marks_the_entry_as_user():
    with tempfile.TemporaryDirectory() as tmp:
        etc = Path(tmp) / "sub" / "etc.json"          # parent must be created
        res = fw_catalog.upsert({"code": "abcd", "brand": "B", "model": "M"}, etc)
        assert res["ok"] and res["entry"]["code"] == "ABCD"
        e = fw_catalog.match("ABCD1", fw_catalog.load_catalog(etc), "image")
        assert e["brand"] == "B" and e["user"] is True
        assert e["verified"] is False       # hand-named is never promoted to verified


def test_upsert_rejects_an_empty_code():
    with tempfile.TemporaryDirectory() as tmp:
        assert fw_catalog.upsert({"code": "  "}, Path(tmp) / "e.json")["error"] == "fw_bad_code"


def test_remove_only_touches_user_entries():
    with tempfile.TemporaryDirectory() as tmp:
        seed = _seed(tmp, [{"code": "SEEDED", "space": "image", "brand": "S"}])
        etc = Path(tmp) / "etc.json"
        fw_catalog.upsert({"code": "MINE", "space": "image", "brand": "M"}, etc)
        assert fw_catalog.remove("SEEDED", "image", etc)["error"] == "fw_catalog_seed"
        assert fw_catalog.match("SEEDED", fw_catalog.load_catalog(seed, etc))["brand"] == "S"
        assert fw_catalog.remove("MINE", "image", etc)["ok"] is True
        assert fw_catalog.match("MINE", fw_catalog.load_catalog(seed, etc)) == {}


def test_shipped_seed_resolves_the_bike_this_logger_targets():
    cat = fw_catalog.load_catalog(REPO)
    for code in ("23ECCLGPSMB", "23ECCLGPSMC", "23ECCLGPSMD"):
        e = fw_catalog.match(code, cat, "image")
        assert e["brand"] == "Moto Morini" and e["model"] == "Granpasso 1200"
        assert e["verified"] is True
    assert fw_catalog.match("23ACMCORA", cat, "image")["verified"] is False


def test_revision_notes_are_display_only():
    """A revision must never reach `model`: the guard compares brand+model, so a
    per-revision model string would read as a different motorcycle."""
    cat = fw_catalog.load_catalog(REPO)
    for code, want in (("23ECCLGPSMD", "stock D — baseline"), ("23ECCLGPSMC", "working base")):
        e = fw_catalog.match(code, cat, "image")
        _, rev = fw_catalog.split_code(code, cat, "image")
        d = fw_catalog.describe_entry(e, rev)
        assert d["model"] == "Granpasso 1200", d["model"]
        assert d["rev_note"] == want, (code, d["rev_note"])
    # an entry without a revisions map, or an unknown revision, stays quiet
    assert fw_catalog.describe_entry({"brand": "X"}, "Z")["rev_note"] == ""
    assert fw_catalog.describe_entry({"revisions": "nonsense"}, "A")["rev_note"] == ""


def test_seeded_ducati_families_group_by_prefix():
    """22ADBADLM02/03/10/12 are one bike, so they must share an entry — otherwise
    flashing one over another would look like a different motorcycle."""
    cat = fw_catalog.load_catalog(REPO)
    seen = set()
    for rev in ("02", "03", "10", "12"):
        e = fw_catalog.match("22ADBADLM" + rev, cat, "image")
        assert e["brand"] == "Ducati" and e["model"] == "848"
        assert fw_catalog.split_code("22ADBADLM" + rev, cat, "image")[1] == rev
        seen.add(id(e))
    assert len(seen) == 1, "the four stock 848 codes must resolve to one entry"
    assert fw_catalog.match("22ADADPSMA1", cat, "image")["model"] == "848"
    assert fw_catalog.match("96519508B", cat, "live")["model"] == "848"


def test_seeded_multistrada_and_unnamed_entries():
    cat = fw_catalog.load_catalog(REPO)
    for code in ("28640761C", "96511803B"):
        assert fw_catalog.match(code, cat, "live")["model"] == "Multistrada 1000DS"
    # sources that never named the bike keep an empty model rather than a guess
    for code, space in (("28642091A", "live"), ("2232B32LEPA", "image")):
        e = fw_catalog.match(code, cat, space)
        assert e["brand"] == "Ducati" and e["model"] == ""
        assert e["verified"] is False
    e = fw_catalog.match("0130DC27", cat, "both")
    assert e["ecu"] == "IAW 59M" and e["match"] == "exact"


def test_every_seed_entry_is_well_formed():
    cat = fw_catalog.load_catalog(REPO)
    for e in cat["entries"]:
        assert e.get("code"), e
        assert e.get("space") in ("image", "live", "both"), e
        assert e.get("match", "prefix") in ("prefix", "exact"), e
        assert isinstance(e.get("verified"), bool), e
        # a revision key must actually follow its family code, or it can never match
        for rev in (e.get("revisions") or {}):
            assert fw_catalog.split_code(e["code"] + rev, cat,
                                         e.get("space", "both"))[1] == rev, (e["code"], rev)


def test_imported_reference_entries_are_well_formed():
    cat = fw_catalog.load_catalog(REPO)
    ref = [e for e in cat["entries"] if e.get("guard") is False]
    assert len(ref) > 100
    for e in ref:
        assert e["match"] == "exact", e          # a full code, not a family
        assert e["space"] == "image", e
        assert e["verified"] is False, e         # nothing here was read off a bike
        assert e["brand"] and e["model"], e
        assert re.fullmatch(r"[A-Z0-9]+", e["code"]), e
        assert len(e["code"]) <= 14, e


def test_curated_entries_still_win_over_the_import():
    """The Granpasso codes must keep resolving through the family entry: an exact
    reference row would beat it and silence the revision warning."""
    cat = fw_catalog.load_catalog(REPO)
    for code in ("23ECCLGPSMB", "23ECCLGPSMC", "23ECCLGPSMD"):
        e = fw_catalog.match(code, cat, "image")
        assert e["code"] == "23ECCLGPSM", e
        assert e.get("guard", True) is not False
        assert e["verified"] is True


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
