"""Calibration code -> bike catalog, layered like the config: seed + user overlay.

Deliberately NOT loaded through main._cfg_file(): that helper *replaces* the repo
file with the /etc one, so the first bike named from the UI would silently erase
every seeded entry, and seeds shipped by later updates would never appear again.
Here the repo file is the base and /etc/onboard-logger/fw_catalog.json is merged
on top, keyed by (code, space) — the same reasoning as ConfigManager deep-merging
config.default.json under /etc/config.json.

`space` keeps the two identity namespaces apart: "image" codes come out of a
firmware file, "live" codes are the Drawing field of the 0x1A 0x80 response.
On a Morini they happen to be the same string; on a Ducati they never are.
"""

from __future__ import annotations

import json
from pathlib import Path

SPACE_ANY = "both"


def _entries(path: str | Path) -> list:
    """Read one layer. A missing or malformed file contributes nothing, never raises."""
    try:
        raw = json.loads(Path(path).read_text())
        out = []
        for e in raw.get("entries", []):
            if isinstance(e, dict) and str(e.get("code", "")).strip():
                out.append(dict(e))
        return out
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def _key(entry: dict) -> tuple:
    return (str(entry.get("code", "")).upper(), str(entry.get("space", SPACE_ANY)))


def load_catalog(repo_path: str | Path, etc_path: str | Path | None = None) -> dict:
    """Seed entries with the /etc overlay merged on top (overlay wins per key)."""
    merged: dict[tuple, dict] = {}
    for e in _entries(repo_path):
        merged[_key(e)] = e
    if etc_path:
        for e in _entries(etc_path):
            e.setdefault("user", True)
            merged[_key(e)] = e
    return {"entries": list(merged.values())}


def _matches_space(entry: dict, space: str) -> bool:
    es = str(entry.get("space", SPACE_ANY))
    return es == SPACE_ANY or space == SPACE_ANY or es == space


def match(code: str, catalog: dict, space: str = SPACE_ANY) -> dict:
    """Longest prefix wins; an exact entry beats any prefix. {} when unknown."""
    code = str(code or "").strip().upper()
    if not code:
        return {}
    best: dict = {}
    best_len = -1
    for e in catalog.get("entries", []):
        if not _matches_space(e, space):
            continue
        ec = str(e.get("code", "")).strip().upper()
        if not ec:
            continue
        if str(e.get("match", "prefix")) == "exact":
            if ec == code:
                return e
            continue
        if code.startswith(ec) and len(ec) > best_len:
            best, best_len = e, len(ec)
    return best


def split_code(code: str, catalog: dict, space: str = SPACE_ANY) -> tuple:
    """(family, revision) — 23ECCLGPSMD -> (23ECCLGPSM, D), 96520610B -> (96520610, B).

    Falls back to "trailing letter is the revision" only for display; the guard
    must not act on a revision nobody confirmed.
    """
    code = str(code or "").strip().upper()
    e = match(code, catalog, space)
    if e:
        fam = str(e.get("code", "")).strip().upper()
        return fam, code[len(fam):]
    if len(code) > 4 and code[-1].isalpha():
        return code[:-1], code[-1]
    return code, ""


def describe_entry(entry: dict, rev: str = "") -> dict:
    """Flatten one entry for the UI/sidecar; {} stays {} so callers can test truthiness.

    `rev_note` names the specific calibration when we know it ("stock D — baseline").
    It is display only: the guard compares brand+model, so two revisions of one bike
    must never be told apart by their model string, or flashing one over the other
    would read as a different motorcycle and get blocked.
    """
    if not entry:
        return {}
    revs = entry.get("revisions") or {}
    return {
        "brand": str(entry.get("brand", "")),
        "model": str(entry.get("model", "")),
        "ecu": str(entry.get("ecu", "")),
        "hw": str(entry.get("hw", "")),
        "note": str(entry.get("note", "")),
        "rev_note": str(revs.get(rev, "")) if isinstance(revs, dict) else "",
        "verified": bool(entry.get("verified", False)),
        "source": str(entry.get("source", "")),
        "user": bool(entry.get("user", False)),
    }


def upsert(entry: dict, etc_path: str | Path) -> dict:
    """Write one user entry into the overlay. Returns {"ok": bool, "error": str}."""
    code = str(entry.get("code", "")).strip().upper()
    if not code:
        return {"ok": False, "error": "fw_bad_code"}
    space = str(entry.get("space", "image"))
    row = {
        "code": code,
        "match": "prefix" if str(entry.get("match", "prefix")) != "exact" else "exact",
        "space": space,
        "brand": str(entry.get("brand", "")).strip(),
        "model": str(entry.get("model", "")).strip(),
        "ecu": str(entry.get("ecu", "")).strip(),
        "verified": False,          # named by hand, never promoted to verified
        "user": True,
        "source": str(entry.get("source", "")).strip() or "named on the device",
    }
    p = Path(etc_path)
    rows = [e for e in _entries(p) if _key(e) != _key(row)]
    rows.append(row)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 1, "entries": rows},
                                ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return {"ok": False, "error": "fw_catalog_readonly"}
    return {"ok": True, "entry": row}


def remove(code: str, space: str, etc_path: str | Path) -> dict:
    """Drop a user entry. A seeded entry can only be shadowed, never deleted."""
    want = _key({"code": code, "space": space})
    p = Path(etc_path)
    rows = _entries(p)
    keep = [e for e in rows if _key(e) != want]
    if len(keep) == len(rows):
        return {"ok": False, "error": "fw_catalog_seed"}
    try:
        p.write_text(json.dumps({"version": 1, "entries": keep},
                                ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return {"ok": False, "error": "fw_catalog_readonly"}
    return {"ok": True}
