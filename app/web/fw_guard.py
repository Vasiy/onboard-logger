"""Decide whether a firmware image may be flashed into the connected ECU.

Pure function, no I/O and no State, so the whole decision table is testable
without an ECU or a 320 KB file.

The tempting check — "image code == live Drawing" — is wrong and would block
every legitimate Ducati and Guzzi write: a 1098 answers 96520610B live while its
image carries a calibration mnemonic, so the two strings never match. They line
up on a Morini, which is exactly the sample-of-one that makes the naive guard
look correct. So we compare identities *resolved through the catalog*, and only
hard-block when the catalog positively says these are two different bikes.
Everything else warns loudly and offers to name the firmware, which turns the
next attempt into a real answer.
"""

from __future__ import annotations

from . import fw_catalog

BLOCK = "block"
WARN = "warn"
OK = "ok"

MIN_COMMON = 4          # shorter than this, a shared prefix says nothing


def _side(code: str, catalog: dict, space: str) -> dict:
    entry = fw_catalog.match(code, catalog, space)
    fam, rev = fw_catalog.split_code(code, catalog, space)
    # "guard": false marks a reference entry — imported tune lists name one bike
    # under several strings ("1098", "Superbike 1098 (USA)"), so letting them
    # resolve a side would turn a legitimate write into a model_mismatch block.
    # They still name the bike everywhere else in the UI.
    counts = bool(entry) and entry.get("guard", True) is not False
    out = {"code": code, "family": fam, "rev": rev, "resolved": counts}
    out.update(fw_catalog.describe_entry(entry, rev))
    return out


def _common(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def decide(image: dict, live: dict, catalog: dict,
           override: bool = False, size_ok: bool = True) -> dict:
    """-> {"level": ok|warn|block, "reason": str, "image": {...}, "ecu": {...}}"""
    img_code = str((image or {}).get("code", "")).strip().upper()
    live_code = str((live or {}).get("Drawing", "")).strip().upper()
    img = _side(img_code, catalog, "image")
    ecu = _side(live_code, catalog, "live")

    def verdict(level, reason, overridable=False):
        overridden = False
        if level == BLOCK and overridable and override:
            level, overridden = WARN, True
        return {"level": level, "reason": reason, "image": img, "ecu": ecu,
                "overridden": overridden}

    # the size gate protects against a bricked ECU, not against the wrong bike:
    # no override ever opens it
    if not size_ok:
        return verdict(BLOCK, "size_mismatch")
    if not img_code:
        return verdict(WARN, "image_unknown")
    if not live_code:
        return verdict(WARN, "no_ecu")

    if img["resolved"] and ecu["resolved"]:
        same_bike = (img.get("brand", ""), img.get("model", "")) == \
                    (ecu.get("brand", ""), ecu.get("model", ""))
        if not same_bike:
            return verdict(BLOCK, "model_mismatch", overridable=True)
        if img["rev"] != ecu["rev"]:
            return verdict(WARN, "revision")
        return verdict(OK, "")

    if img["resolved"] != ecu["resolved"]:
        # one side known, the other not — cannot claim a mismatch, cannot clear it
        return verdict(WARN, "unresolved")

    # neither side is catalogued: a long shared prefix means the same family with
    # a different revision, anything else is genuinely unknown
    if _common(img_code, live_code) >= MIN_COMMON:
        return verdict(WARN, "revision")
    return verdict(WARN, "unresolved")
