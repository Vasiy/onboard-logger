"""Onboard Logger web server (FastAPI).

Serves the SPA, streams live K-Line values over WebSocket, and exposes REST
endpoints for logging control, parameter selection, network configuration and
power actions. Runs as root (needs port 80, sysfs LED, hostapd/dnsmasq/ip).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from .kline.ecu_id import load_fields, parse_desc
from .kline.logger import KLineWorker
from .web import system
from .web import addons as addons_mod
from .web.archive import (flat_entries, resolve_in, safe_name, split_names,
                          zip_entries)
from .web.addons import AddonManager
from .web.config_mgr import ConfigManager, REPO_DIR, _deep_merge
from .web.diag import DiagLog
from .web.storage import StorageManager, parse_day
from .web import update as update_mod
from .web.update import UpdateBusy, UpdateManager, confirm_boot
from .web.wifi_scan import scan_networks
from .web.firmware import FirmwareBlocked, FirmwareManager
from .web import fw_catalog, fw_guard
from .kline import fw_ident
from .web.led import Led
from .web.state import State

ETC = Path("/etc/onboard-logger")
STATIC_DIR = Path(__file__).resolve().parent / "static"
UTIL_PATH = Path("/opt/onboard-logger/bin/5am_util")
# The tree the service runs from, and the scratch dir the updater unpacks into.
# REPO_DIR is the checkout on a dev host, /opt/onboard-logger on the board.
INSTALL_DIR = REPO_DIR
UPDATE_DIR = Path("/opt/updates")
# Add-ons live inside the install tree but outside the repository, like .venv and
# bin/5am_util: deploy.sh excludes them and update.py carries them across a swap.
ADDONS_DIR = INSTALL_DIR / "addons"
# IAW 5AM firmware image size (0x50000). Writing anything else bricks the ECU.
DEFAULT_FW_SIZE = 327680


def _required_fw_size() -> int:
    return int(cm.load().get("firmware_size", DEFAULT_FW_SIZE))


def _safe_rel(name: str) -> str:
    """A log's path relative to the log root: ``file`` or ``DD-MM-YYYY/file``.

    Logs live in one folder per day now, so the old basename-only rule would
    reject every current file. Exactly two segments are allowed and the first
    has to be a real day folder, which is what keeps ``..`` out; the resolve()
    check at the call site is the second lock on the same door.
    """
    parts = [q for q in str(name).replace("\\", "/").split("/") if q]
    if not parts or len(parts) > 2 or any(q in (".", "..") for q in parts):
        raise ValueError("bad file name")
    if len(parts) == 2 and parse_day(parts[0]) is None:
        raise ValueError("bad file name")
    return "/".join(parts[:-1] + [safe_name(parts[-1])])


def _logs_root() -> Path:
    """The directory the log endpoints read.

    Goes through StorageManager so a reader and the worker can never disagree
    about where the logs are — before this they read the config independently.
    """
    if storage is not None:
        return storage.active_root()
    return Path(cm.load()["log_dir"])


def _log_file(rel: str) -> Path | None:
    """Resolve a validated relative name inside the log root, or None."""
    root = _logs_root().resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    return p


def _unique_name(directory: Path, name: str) -> str:
    """Append -N before the suffix so an upload never overwrites an existing file."""
    if not (directory / name).exists():
        return name
    stem, suf = Path(name).stem, Path(name).suffix
    i = 1
    while (directory / f"{stem}-{i}{suf}").exists():
        i += 1
    return f"{stem}-{i}{suf}"


def _disk_free(path: Path) -> dict:
    try:
        u = shutil.disk_usage(path if path.exists() else path.parent)
        return {"free": u.free, "total": u.total}
    except OSError:
        return {"free": 0, "total": 0}


def _diff_files(pa: Path, pb: Path) -> dict:
    """Byte comparison: equal? and how many bytes differ (mismatches + size gap)."""
    a, b = pa.read_bytes(), pb.read_bytes()
    n = min(len(a), len(b))
    mismatch = sum(1 for i in range(n) if a[i] != b[i])
    diff = mismatch + abs(len(a) - len(b))
    return {
        "equal": diff == 0,
        "diff_bytes": diff,
        "mismatch_overlap": mismatch,
        "size_a": len(a),
        "size_b": len(b),
    }


def _params_path() -> str:
    etc = ETC / "params.json"
    return str(etc if etc.exists() else REPO_DIR / "config" / "params.json")


def _cfg_file(name: str) -> Path:
    """Config asset: prefer /etc/onboard-logger, fall back to the repo config dir."""
    etc = ETC / name
    return etc if etc.exists() else REPO_DIR / "config" / name


@functools.lru_cache(maxsize=1)
def _actuators() -> list:
    try:
        return json.loads(_cfg_file("actuators.json").read_text()).get("actuators", [])
    except (OSError, ValueError):
        return []


@functools.lru_cache(maxsize=1)
def _status_maps() -> dict:
    try:
        return json.loads(_cfg_file("status_maps.json").read_text()).get("maps", {})
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def _profiles() -> dict:
    try:
        return json.loads(_cfg_file("profiles.json").read_text())
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def _fw_layouts() -> list:
    return fw_ident.load_layouts(_cfg_file("fw_layout.json"))


def _catalog_paths() -> tuple:
    """Seed in the repo, user overlay in /etc — deliberately NOT _cfg_file().

    _cfg_file() replaces one file with the other, so the first bike named from the
    UI would erase every seeded entry and hide the ones later updates ship.
    """
    repo = REPO_DIR / "config" / "fw_catalog.json"
    etc = ETC / "fw_catalog.json" if ETC.is_dir() else REPO_DIR / "config" / "fw_catalog.user.json"
    return repo, etc


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


@functools.lru_cache(maxsize=4)
def _catalog_cached(repo: str, etc: str, repo_m: int, etc_m: int) -> dict:
    return fw_catalog.load_catalog(repo, etc)


def _fw_catalog() -> dict:
    repo, etc = _catalog_paths()
    return _catalog_cached(str(repo), str(etc), _mtime(repo), _mtime(etc))


@functools.lru_cache(maxsize=128)
def _ident_cached(path: str, mtime_ns: int, size: int) -> dict:
    """Identify one image. mtime+size are part of the key, so a rewritten file
    re-identifies itself and no explicit invalidation is needed."""
    return fw_ident.identify_file(path, _fw_layouts())


@functools.lru_cache(maxsize=128)
def _desc_code_cached(path: str, mtime_ns: int) -> str:
    """The Drawing: line of a .bin.txt sidecar ("" when absent or unreadable)."""
    try:
        return parse_desc(Path(path).read_text(errors="replace")).get("Drawing", "")
    except OSError:
        return ""


def _ident_of(p: Path, st=None) -> dict:
    """Image identity plus whatever the catalog knows about it."""
    try:
        st = st or p.stat()
    except OSError:
        return dict(fw_ident.identify_file(p), brand="", model="", verified=False)
    ident = dict(_ident_cached(str(p), st.st_mtime_ns, st.st_size))
    cat = _fw_catalog()
    code = ident.get("code", "")
    entry = fw_catalog.match(code, cat, "image")
    _, rev = fw_catalog.split_code(code, cat, "image")
    ident["rev"] = rev
    ident.update(fw_catalog.describe_entry(entry, rev) or
                 {"brand": "", "model": "", "verified": False})
    return ident


def _live_drawing() -> str:
    return str(state.snapshot().get("ecu_fields", {}).get("Drawing", ""))


def _guard_verdict(name: str) -> dict:
    """Verdict for flashing <name> into whatever is on the bus right now."""
    p = fwm.fw_dir / name
    try:
        size_ok = p.stat().st_size == _required_fw_size()
    except OSError:
        size_ok = False
    override = bool(cm.load().get("system", {}).get("fw_guard_override", False))
    return fw_guard.decide(_ident_of(p), {"Drawing": _live_drawing()},
                           _fw_catalog(), override=override, size_ok=size_ok)


def _image_desc_lines(name: str) -> list:
    """Extra sidecar lines: which bike this is, and what the bytes themselves say."""
    ident = _ident_of(fwm.fw_dir / name)
    if not ident.get("code"):
        return []
    hw = ident.get("hardware", "")
    model = ident.get("model", "")
    if model and ident.get("rev_note"):
        model += f" — {ident['rev_note']}"
    return [
        ("Brand", ident.get("brand", "")),
        ("Model", model),
        ("Image", ident["code"] + (f" ({hw})" if hw else "")),
        ("Catalog", "verified" if ident.get("verified")
         else ("unverified" if ident.get("brand") else "unknown")),
    ]


@functools.lru_cache(maxsize=8)
def _dtc_table(lang: str) -> dict:
    d = _cfg_file("dtc")
    p = d / f"{lang}.txt"
    if not p.exists():
        p = d / "en.txt"
    tbl: dict[str, str] = {}
    try:
        for ln in p.read_text().splitlines():
            code, _, desc = ln.partition("\t")
            if desc:
                tbl[code] = desc
    except OSError:
        pass
    return tbl


def _dtc_desc(code: str) -> str:
    loc = cm.load().get("locale", "en")
    if loc not in ("en", "es", "fr", "it"):   # only these language tables exist
        loc = "en"
    return _dtc_table(loc).get(code, "")


def _selected_path() -> Path:
    return ETC / "selected.json" if ETC.exists() else REPO_DIR / "config" / "selected.json"


def _save_selected(keys: list) -> None:
    try:
        _selected_path().write_text(json.dumps(list(keys)))
    except OSError:
        pass


def _load_selected() -> list | None:
    try:
        return json.loads(_selected_path().read_text())
    except (OSError, ValueError):
        return None


PRESET_SLOTS = 3
PRESET_NAME_MAX = 8
# A note explains how to capture a preset — cold engine, before the fan, that
# sort of thing. Long enough for a real procedure, capped so a paste cannot fill
# the card the ride logs share.
NOTE_MAX = 4096


def _presets_path() -> Path:
    return ETC / "presets.json" if ETC.exists() else REPO_DIR / "config" / "presets.json"


def _norm_note(raw) -> str:
    """One note, trimmed to NOTE_MAX. The same cut for a slot and for the free
    selection, so there is one answer to "how long may this be"."""
    return str(raw or "")[:NOTE_MAX]


def _selectable_keys() -> set[str]:
    """Keys a selection may name: everything the catalog carries minus the rli the
    firmware answers from a shared placeholder slot. One answer, so a dead channel
    cannot slip in through a preset, a stored selection or the API."""
    return {c["key"] for c in state.snapshot().get("catalog", []) if not c.get("dead")}


def _norm_presets(raw) -> list[dict]:
    """Coerce anything into exactly PRESET_SLOTS slots. The only validation point:
    names are trimmed to PRESET_NAME_MAX, notes to NOTE_MAX, keys are filtered
    against the live catalog (a params.json edit can retire a channel a preset
    still names) and de-duped with their order kept — the decoded CSV writes its
    columns in selection order."""
    valid = _selectable_keys()
    out: list[dict] = []
    items = raw if isinstance(raw, list) else []
    for i in range(PRESET_SLOTS):
        item = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        name = str(item.get("name", "") or "").strip()[:PRESET_NAME_MAX]
        keys, seen = [], set()
        for k in item.get("keys", []) or []:
            k = str(k)
            if k in valid and k not in seen:
                seen.add(k)
                keys.append(k)
        out.append({"name": name, "keys": keys, "note": _norm_note(item.get("note"))})
    return out


def _boot_selection(saved, presets: list[dict], valid: set[str]) -> list[str]:
    """What the board polls after a restart: what the rider last had, then the
    first preset as the factory default, then nothing (the worker's own
    named-default set already stands). The first preset is a *default*, not an
    override — it must not wipe a hand-picked selection on every reboot."""
    sel = [k for k in (saved or []) if k in valid]
    if not sel and presets:
        sel = [k for k in presets[0]["keys"] if k in valid]
    return sel


def _save_presets(presets: list[dict], free_note: str = "") -> None:
    try:
        _presets_path().write_text(json.dumps(
            {"slots": presets, "free_note": free_note}, ensure_ascii=False))
    except OSError:
        pass


def _split_presets_file(raw):
    """(slots, free_note) out of either shape of presets.json.

    The file was a bare list of slots before notes existed, and that is what is
    sitting in /etc on every board today. Reading only the new shape would drop
    three working presets on the first start after an update.
    """
    if isinstance(raw, dict):
        return raw.get("slots"), _norm_note(raw.get("free_note"))
    return raw, ""


def _load_presets() -> list | None:
    try:
        return json.loads(_presets_path().read_text())
    except (OSError, ValueError):
        return None


state = State()
led = Led()
cm = ConfigManager()
worker: KLineWorker | None = None
fwm: FirmwareManager | None = None
diag: DiagLog | None = None
storage: StorageManager | None = None
upd: UpdateManager | None = None


def _after_network(cfg: dict, rep: dict) -> None:
    """Publish the resulting link and persist a client->AP fallback.

    If the join failed, ConfigManager has already put the AP back on the air;
    writing the mode back to "ap" here means a reboot also comes up reachable
    instead of retrying a network that did not work.
    """
    state.set_ap_channel(rep.get("channel", 0))
    state.set_wifi(cm.mode(cfg) if not rep.get("fallback_to_ap") else "ap",
                   rep.get("client") or cm.client_status())
    if rep.get("fallback_to_ap"):
        cfg["wifi"]["mode"] = "ap"
        cm.save(cfg)
        state.set_status("error", "Wi-Fi: " + rep.get("client_error", "client did not connect"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, fwm, diag, storage, upd
    # uvicorn configures only its own loggers; without this the diagnostics
    # events never reach journald (and from there /var/log/syslog)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    cfg = cm.load()

    # Bring the AP up (idempotent) *after* the port is listening: hostapd waits up
    # to 30 s for wlan0 in its pre-start, so doing this inline kept the web UI
    # unreachable for that whole window and every browser request in it failed.
    async def _startup_network():
        try:
            rep = await asyncio.to_thread(cm.apply_network, cfg, None)
            _after_network(cfg, rep)
        except Exception as exc:   # never take the server down over the AP
            state.set_status("error", f"network bring-up: {exc}")
        # The CPU settings live in config and are re-applied here rather than by a
        # unit of their own: sysfs forgets them on every boot, and this task is
        # already the place that puts the board into the state config asks for.
        try:
            await asyncio.to_thread(_apply_cpu_cfg, cfg)
        except Exception as exc:
            logging.getLogger("cpu").warning("cpu settings: %s", exc)

    asyncio.create_task(_startup_network())

    # Board health on its own clock. The diagnostics log samples the same sysfs,
    # but only while a ride log is open, and the header has to show a temperature
    # whether or not anything is being recorded -- or the one moment it matters,
    # a hot board on the driveway, is the moment it says nothing.
    async def _cpu_health():
        while True:
            try:
                state.set_cpu(await asyncio.to_thread(system.cpu_status))
            except Exception:
                pass
            await asyncio.sleep(5)

    asyncio.create_task(_cpu_health())
    ecu_id_path = ETC / "ecu_id.json"
    if not ecu_id_path.exists():
        ecu_id_path = REPO_DIR / "config" / "ecu_id.json"
    ecu_fields = load_fields(ecu_id_path)
    storage = StorageManager(
        cfg["log_dir"], cfg.get("storage"),
        on_change=lambda st: state.set_storage(st["dest"], st["root"], st["fallback"]))
    storage.start()
    state.set_storage(storage.dest, str(storage.active_root()), storage.fallback())
    # both writers ask the manager where to write at every file open, so a stick
    # plugged, pulled or picked takes effect without a restart
    diag = DiagLog(storage.active_root, cfg.get("diag"),
                   probe=lambda: {**(worker.stats() if worker else {}),
                                  "link": state.snapshot().get("status", "")})
    worker = KLineWorker(
        port=cfg["kline"]["port"],
        params_path=_params_path(),
        log_dir=storage.active_root,
        state=state,
        led=led,
        log_decoded_default=cfg.get("logging", {}).get("decoded_default", True),
        log_raw_default=cfg.get("logging", {}).get("raw_default", False),
        zip_after=cfg.get("logging", {}).get("zip_after", False),
        echo=cfg["kline"].get("echo", True),
        baud=cfg["kline"].get("baud", "auto"),
        init=cfg["kline"].get("init", "fast"),
        ecu_fields=ecu_fields,
        session_init=cfg.get("testing", {}).get("session_init", True),
        diag=diag,
        gear_cfg=cfg.get("gear", {}),
    )
    worker.start()
    # the catalog is up by now, so the filter in _norm_presets has something to
    # filter against
    _slots, _free = _split_presets_file(_load_presets())
    _presets = _norm_presets(_slots)
    state.set_presets(_presets, _free)
    # Restore the selection: what the rider last had wins, then the first preset as
    # the factory default, then the named-default set the worker already applied.
    # The first preset is the default only when there is nothing to remember — it
    # must not wipe a hand-picked set on every reboot.
    _valid = _selectable_keys()
    _sel = _boot_selection(_load_selected(), _presets, _valid)
    if _sel:
        state.set_selected(_sel)
        worker.set_selected(_sel)
    fwm = FirmwareManager(
        worker_getter=lambda: worker,
        util_path=UTIL_PATH,
        fw_dir=cfg.get("firmware_dir", "/root/firmware"),
        port=cfg["kline"]["port"],
        state=state,
        ecu_fields=ecu_fields,
        guard=_guard_verdict,
        describe_image=_image_desc_lines,
        log_dir=cfg["log_dir"],
        diag=diag,
        fw_size=int(cfg.get("firmware_size", DEFAULT_FW_SIZE)),
    )
    upd = UpdateManager(
        dest=INSTALL_DIR,
        state=state,
        log_dir=storage.active_root,
        fwm_getter=lambda: fwm,
        diag=diag,
        etc_dir=ETC,
        work_dir=UPDATE_DIR,
    )
    # "the new tree came up" — the whole app is constructed by now, which is what
    # the rollback watchdog is waiting to hear. Left unsaid, it puts the previous
    # tree back in two minutes.
    if confirm_boot(upd.run_dir, INSTALL_DIR, ETC):
        logging.getLogger("update").info("update confirmed: %s", upd.version())
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            worker.join(timeout=3.0)
        if diag is not None:
            diag.stop()
        if storage is not None:
            storage.stop()


app = FastAPI(title="Onboard Logger", lifespan=lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    """Force revalidation of the SPA + assets so a redeploy is picked up
    immediately (StaticFiles otherwise only sends ETag/Last-Modified, which
    browsers cache heuristically — leaving stale app.js running)."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# -- pages / static --------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# -- add-ons ---------------------------------------------------------------
# An add-on is static content and a nameless file store; nothing in it is ever
# imported. Served by hand rather than by app.mount() so a freshly installed one
# works without restarting the service — mounting is a start-up act.
adm = AddonManager(ADDONS_DIR)


@app.get("/api/addons")
async def addons_list():
    return {"addons": adm.installed()}


@app.get("/addons/{name}")
async def addon_root(name: str):
    if not adm.dir_of(name):
        return JSONResponse(status_code=404, content={"error": "err.addon_unknown"})
    return RedirectResponse("/addons/%s/" % name)


@app.get("/addons/{name}/{path:path}")
async def addon_file(name: str, path: str = ""):
    p = adm.web_file(name, path)
    if p is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(p, media_type=adm.content_type(p), headers={
        "Cache-Control": "no-cache",
        # the page is the add-on's, the origin is ours: never let a browser
        # decide for itself what an unlisted file is
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/addons/{name}/data")
async def addon_data_list(name: str):
    if not adm.dir_of(name):
        return JSONResponse(status_code=404, content={"error": "err.addon_unknown"})
    return adm.list_data(name)


@app.get("/api/addons/{name}/data/{file}")
async def addon_data_read(name: str, file: str):
    blob = adm.read_data(name, file)
    if blob is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    # stored bytes are opaque to the board; handing them back as anything a
    # browser might run is how a file store becomes an XSS hole
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"X-Content-Type-Options": "nosniff"})


@app.put("/api/addons/{name}/data/{file}")
async def addon_data_write(name: str, file: str, request: Request):
    if not adm.dir_of(name):
        return JSONResponse(status_code=404, content={"error": "err.addon_unknown"})
    body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={"error": "err.addon_empty"})
    err = adm.write_data(name, file, body)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return {"name": file, "size": len(body)}


@app.delete("/api/addons/{name}/data/{file}")
async def addon_data_delete(name: str, file: str):
    if not adm.dir_of(name):
        return JSONResponse(status_code=404, content={"error": "err.addon_unknown"})
    err = adm.delete_data(name, file)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return {"name": file}


@app.post("/api/addons/upload")
async def addon_upload(file: UploadFile = File(...)):
    name = file.filename or ""
    if not any(name.lower().endswith(x) for x in addons_mod.ARCHIVE_SUFFIXES):
        return JSONResponse(status_code=400, content={"error": "err.addon_bad_archive"})
    data = await file.read()
    if len(data) > addons_mod.MAX_ARCHIVE:
        return JSONResponse(status_code=400, content={"error": "err.addon_too_big"})
    try:
        ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ADDONS_DIR / (".upload-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        tmp.write_bytes(data)
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": "err.addon_failed",
                                                      "detail": str(exc)})
    try:
        installed, err = adm.install(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return {"ok": True, "name": installed}


@app.delete("/api/addons/{name}")
async def addon_remove(name: str):
    err = adm.remove(name)
    if err:
        return JSONResponse(status_code=404, content={"error": err})
    return {"ok": True, "name": name}


# -- live state ------------------------------------------------------------
@app.get("/api/state")
async def get_state():
    return state.snapshot()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(state.snapshot())
            await asyncio.sleep(0.2)
    except (WebSocketDisconnect, RuntimeError):
        pass


# -- logger control --------------------------------------------------------
@app.post("/api/logging")
async def set_logging(payload: dict):
    """Toggle either log stream independently: {"decoded": bool} and/or {"raw": bool}."""
    if worker is not None:
        if "decoded" in payload:
            worker.set_logging_decoded(bool(payload["decoded"]))
        if "raw" in payload:
            worker.set_logging_raw(bool(payload["raw"]))
    return {"ok": True}


@app.post("/api/selected")
async def set_selected(payload: dict):
    valid = _selectable_keys()
    keys = [str(k) for k in payload.get("keys", []) if str(k) in valid]
    state.set_selected(keys)
    if worker is not None:  # also drives decoded-log columns (rolls the file)
        worker.set_selected(keys)
    _save_selected(keys)    # persist across restarts
    return {"selected": state.snapshot()["selected"]}


@app.post("/api/presets")
async def set_presets(payload: dict):
    """Store the three channel sets the Logger tab switches between, and the notes
    that go with them. The worker is deliberately not touched: a preset does not
    change what is polled — applying one goes through /api/selected like any other
    change of the selection.

    `free_note` belongs to a hand-picked selection, which has no slot to live in:
    which preset is "active" is derived from the live selection, never stored, so
    a set that matches none of them still needs somewhere to keep its note.
    """
    presets = _norm_presets(payload.get("presets", []))
    free = _norm_note(payload.get("free_note", state.snapshot().get("free_note", "")))
    state.set_presets(presets, free)
    _save_presets(presets, free)
    snap = state.snapshot()
    return {"presets": snap["presets"], "free_note": snap["free_note"]}


def _parse_rli(v, default: int) -> int:
    """Accept 0..255 as int or hex string ('0x3c' / '3c')."""
    try:
        if isinstance(v, str):
            return int(v, 16) if v.lower().startswith("0x") or any(
                c in "abcdefABCDEF" for c in v) else int(v)
        return int(v)
    except (TypeError, ValueError):
        return default


@app.post("/api/kline/scan")
async def kline_scan(payload: dict):
    """Start/stop the rli-scan (bus sweep) capture. Body:
    {"on": true, "start": 0, "end": 255, "fmt": "both"|2|0} or {"on": false}."""
    if worker is None:
        return {"ok": False, "scan_on": False}
    if payload.get("on"):
        start = _parse_rli(payload.get("start", 0), 0)
        end = _parse_rli(payload.get("end", 255), 255)
        fmt = payload.get("fmt", "both")
        fmt = "both" if fmt == "both" else int(fmt)
        try:
            dur = max(0, min(3600, int(payload.get("duration", 0))))
        except (TypeError, ValueError):
            dur = 0
        worker.start_scan(start, end, fmt, dur)
    else:
        worker.stop_scan()
    return {"ok": True, "scan": worker.scan_state()}


@app.post("/api/kline/scan/mark")
async def kline_scan_mark(payload: dict):
    """Write a labelled marker into the running scan log (status-channel hunt)."""
    if worker is None:
        return {"ok": False, "error": "err.no_adapter"}
    label = str(payload.get("label", "")).strip() or "mark"
    return worker.mark_event(label)


# -- testing (diagnostics) -------------------------------------------------
# Sync handler: run_command blocks (waits on the worker thread) so Starlette
# runs it in a threadpool instead of stalling the event loop.
MIN_PULSE_MS, MAX_PULSE_MS, DEFAULT_PULSE_MS = 500, 30000, 5000


def _clamp_pulse(value) -> int:
    """Clamp an actuator pulse length to the safe 0.5..30 s window."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = DEFAULT_PULSE_MS
    return max(MIN_PULSE_MS, min(MAX_PULSE_MS, v))


def _pulse_ms(cfg: dict | None = None) -> int:
    c = cfg if cfg is not None else cm.load()
    return _clamp_pulse(c.get("testing", {}).get("pulse_ms", DEFAULT_PULSE_MS))


def _testing_guard() -> dict | None:
    """Shared preconditions for every Testing command."""
    if worker is None:
        return {"ok": False, "error": "err.no_adapter"}
    snap = state.snapshot()
    if snap.get("scan_on"):
        return {"ok": False, "error": "err.busy_scan"}
    if snap.get("status") != "connected":
        return {"ok": False, "error": "err.not_connected"}
    return None


@app.post("/api/testing/settings")
def testing_settings(payload: dict):
    """Persist the actuator pulse length. Deliberately not routed through
    /api/config: that path also runs apply_network, and nothing here may touch
    the AP the UI is served over."""
    cfg = cm.load()
    ms = _clamp_pulse(payload.get("pulse_ms", DEFAULT_PULSE_MS))
    cfg.setdefault("testing", {})["pulse_ms"] = ms
    cm.save(cfg)
    return {"ok": True, "pulse_ms": ms}


@app.post("/api/testing/{cmd}")
def testing_cmd(cmd: str):
    """Run a one-shot diagnostic command (DTC read/clear, adaptation resets).
    Requires a live ECU link and no scan in progress."""
    if cmd not in ("read_dtc", "clear_dtc", "reset_tps", "reset_adaptation"):
        return {"ok": False, "error": "unknown"}
    err = _testing_guard()
    if err:
        return err
    if worker.actuator_active():   # one thing on the bus at a time
        return {"ok": False, "error": "err.busy_test"}
    res = worker.run_command(cmd)
    if cmd == "read_dtc" and res.get("ok"):   # enrich each DTC with a localized description
        for d in res.get("dtcs", []):
            d["desc"] = _dtc_desc(d.get("code", ""))
    return res


@app.get("/api/profiles")
async def get_profiles():
    """Bike -> ECU -> protocol presets + per-ECU command matrix (reference only)."""
    return _profiles()


@app.get("/api/actuators")
async def get_actuators():
    """Actuator-test catalog (LocalID -> name); the UI renders the buttons."""
    return {"actuators": _actuators(), "status_maps": _status_maps(),
            "pulse_ms": _pulse_ms(), "min_ms": MIN_PULSE_MS, "max_ms": MAX_PULSE_MS}


@app.post("/api/testing/actuator/stop")
def testing_actuator_stop():
    """Release the running actuator early (30 <lid> 00)."""
    err = _testing_guard()
    if err:
        return err
    return worker.run_command("actuator_stop")


@app.post("/api/testing/actuator/{localid}")
def testing_actuator(localid: int):
    """Momentary actuator test: 30 <localid> 07 now, 30 <localid> 00 at the
    deadline (the worker owns the timing, so polling keeps running and the output
    is released even if the browser goes away). Guarded like other tests."""
    act = next((a for a in _actuators() if a.get("localid") == localid), None)
    if act is None:
        return {"ok": False, "error": "unknown"}
    err = _testing_guard()
    if err:
        return err
    if worker.actuator_active():
        return {"ok": False, "error": "err.busy_test"}
    secs = _pulse_ms() / 1000.0
    return worker.run_command("actuator", arg=(localid, secs, act.get("key", "")))


# -- configuration ---------------------------------------------------------
@app.get("/api/config")
async def get_config():
    return cm.load()


@app.get("/api/wifi/scan")
async def wifi_scan():
    """Last startup band survey for the channel-occupancy chart."""
    return {
        "chosen": cm.last_scan_channel,
        "ts": cm.last_scan_ts,
        "survey": cm.last_survey or [],
    }


def _scan_networks_blocking() -> dict:
    """Scan for joinable networks. In AP mode the radio must be freed first, so
    hostapd goes down for the duration and is brought back afterwards — the
    client's Wi-Fi link drops for a couple of seconds."""
    cfg = cm.load()
    if not cm.iface_present():
        return {"ok": False, "error": "err.no_iface", "networks": []}
    msgs: list[str] = []
    ap_up = cm.mode(cfg) == "ap"
    if ap_up:
        cm._stop_ap(msgs)
    try:
        nets, err = scan_networks(cm.iface)
    finally:
        if ap_up:                      # always put the AP back, even on a failure
            cm._run(["systemctl", "restart", "hostapd"], msgs)
            if cfg["dhcp"]["enabled"]:
                cm._run(["systemctl", "restart", "dnsmasq"], msgs)
    return {"ok": not err, "error": err, "networks": nets, "messages": msgs,
            "ap_interrupted": ap_up}


@app.post("/api/wifi/networks")
async def wifi_networks():
    """Live scan of nearby networks for the client-mode SSID picker."""
    return await asyncio.to_thread(_scan_networks_blocking)


@app.get("/api/wifi/status")
async def wifi_status():
    """Which mode is live and, in client mode, where the board ended up."""
    cfg = cm.load()
    return {"mode": cm.mode(cfg), "iface": cm.iface, **cm.client_status()}


# -- firmware --------------------------------------------------------------
def _fw_mismatch(p: Path, ident: dict) -> list:
    """Where the image's own code contradicts a claim someone made about it.

    Only claims count: an auto-named dump-20260828-113000.bin asserts nothing, and
    badging it every time would train the warning away within a day.
    """
    code = ident.get("code", "")
    if not code:
        return []
    out = []
    txt = p.with_name(p.name + ".txt")
    said = _desc_code_cached(str(txt), _mtime(txt)) if txt.is_file() else ""
    if said and said.upper() != code.upper():
        out.append("desc")
    cat = _fw_catalog()
    for token in re.split(r"[^A-Za-z0-9]+", p.stem):
        claim = fw_catalog.match(token, cat, "image")
        if claim and claim is not fw_catalog.match(code, cat, "image"):
            out.append("name")
            break
    return out


def _fw_files() -> list[dict]:
    d = fwm.fw_dir
    if not d.is_dir():
        return []
    busy = fwm.status().get("op", "idle") != "idle"
    rows = []
    for p in sorted(d.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        # the file being written right now is mid-flight; its bytes mean nothing yet
        ident = ({"code": "", "reason": "busy"} if busy and p.name == fwm.current
                 else _ident_of(p, st))
        rows.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                     "desc": (d / (p.name + ".txt")).exists(),
                     "ident": ident, "mismatch": _fw_mismatch(p, ident)})
    return rows


def _fw_suggest() -> dict | None:
    """After a read, offer a name built from the image — the user still confirms it."""
    name = getattr(fwm, "last_read", "")
    if not name:
        return None
    p = fwm.fw_dir / name
    if not p.is_file():
        return None
    code = _ident_of(p).get("code", "")
    if not code or name.upper().startswith(code.upper()):
        return None
    stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
    return {"for": name, "name": _unique_name(fwm.fw_dir, f"{code}-{stamp}.bin")}


@app.get("/api/firmware")
async def firmware_status():
    if fwm is None:
        return {"available": False, "op": "idle", "files": []}
    return {**fwm.status(), "files": _fw_files(), "dir": str(fwm.fw_dir),
            "required_size": _required_fw_size(), "suggest": _fw_suggest(),
            "guard_override": bool(cm.load().get("system", {}).get("fw_guard_override", False)),
            **_disk_free(fwm.fw_dir)}


@app.post("/api/firmware/read")
async def firmware_read(payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    name = payload.get("name") or f"dump-{datetime.now():%Y%m%d-%H%M%S}.bin"
    try:
        name = safe_name(name if name.endswith(".bin") else name + ".bin")
        fwm.start_read(name, verbose=bool(payload.get("verbose")))
    except (ValueError, RuntimeError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True, "name": name}


@app.post("/api/firmware/write")
async def firmware_write(payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(payload.get("name", ""))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = fwm.fw_dir / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    # hard safety gate: refuse to flash anything but an exact-size image
    req = _required_fw_size()
    if path.stat().st_size != req:
        return JSONResponse(status_code=400, content={"error": "size_mismatch"})
    verdict = _guard_verdict(name)
    if verdict["level"] == "block":
        return JSONResponse(status_code=400,
                            content={"error": "fw_" + verdict["reason"], "guard": verdict})
    try:
        fwm.start_write(name, verbose=bool(payload.get("verbose")))
    except FirmwareBlocked as e:
        return JSONResponse(status_code=400,
                            content={"error": "fw_" + e.verdict["reason"], "guard": e.verdict})
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True, "name": name, "guard": verdict}


@app.get("/api/firmware/check")
async def firmware_check(name: str):
    """Guard verdict without starting anything — lets the UI warn before the POST."""
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not (fwm.fw_dir / name).is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return _guard_verdict(name)


@app.post("/api/firmware/rename")
async def firmware_rename(payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    if fwm.status().get("op", "idle") != "idle":
        return JSONResponse(status_code=400, content={"error": "fw_busy"})
    try:
        src = safe_name(payload.get("from", ""))
        dst = safe_name(payload.get("to", ""))
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "fw_bad_name"})
    if not dst.endswith(".bin"):
        dst += ".bin"
    sp = fwm.fw_dir / src
    if not sp.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    dst = _unique_name(fwm.fw_dir, dst)     # never clobber another firmware
    try:
        sp.rename(fwm.fw_dir / dst)
        stxt = sp.with_name(src + ".txt")
        if stxt.is_file():                  # the passport travels with the image
            stxt.rename(fwm.fw_dir / (dst + ".txt"))
    except OSError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if getattr(fwm, "last_read", "") == src:
        fwm.last_read = dst
    return {"ok": True, "name": dst}


@app.get("/api/firmware/catalog")
async def firmware_catalog():
    _, etc = _catalog_paths()
    return {"entries": _fw_catalog()["entries"], "path": str(etc)}


@app.post("/api/firmware/catalog")
async def firmware_catalog_save(payload: dict):
    _, etc = _catalog_paths()
    res = fw_catalog.upsert(payload, etc)
    _catalog_cached.cache_clear()
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"error": res.get("error", "")})
    return res


@app.delete("/api/firmware/catalog/{code}")
async def firmware_catalog_delete(code: str, space: str = "image"):
    _, etc = _catalog_paths()
    res = fw_catalog.remove(code, space, etc)
    _catalog_cached.cache_clear()
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"error": res.get("error", "")})
    return res


@app.post("/api/firmware/cancel")
async def firmware_cancel():
    return {"cancelled": bool(fwm and fwm.cancel())}


@app.post("/api/firmware/upload")
async def firmware_upload(file: UploadFile = File(...)):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(file.filename or "")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    fwm.fw_dir.mkdir(parents=True, exist_ok=True)
    data = await file.read()

    if name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return JSONResponse(status_code=400, content={"error": "bad_zip"})
        # ignore dot-files (macOS AppleDouble "._x.bin", __MACOSX/, .DS_Store)
        members = [m for m in zf.namelist()
                   if not m.endswith("/") and not Path(m).name.startswith(".")]
        bins = [m for m in members
                if Path(m).name.endswith(".bin") and not Path(m).name.endswith(".bin.txt")]
        if not bins:  # nothing to import -> archive discarded (never persisted)
            return JSONResponse(status_code=400, content={"error": "no_firmware_in_zip"})
        txt_by_base = {Path(m).name: m for m in members if Path(m).name.endswith(".bin.txt")}
        extracted = []
        for m in bins:  # extract ONLY .bin (+ matching .bin.txt) for safety
            try:
                bn = safe_name(Path(m).name)
            except ValueError:
                continue
            final = _unique_name(fwm.fw_dir, bn)
            (fwm.fw_dir / final).write_bytes(zf.read(m))
            tm = txt_by_base.get(bn + ".txt")
            if tm:
                (fwm.fw_dir / (final + ".txt")).write_bytes(zf.read(tm))
            extracted.append(final)
        return {"ok": True, "extracted": extracted}

    name = _unique_name(fwm.fw_dir, name)  # never clobber an existing firmware
    (fwm.fw_dir / name).write_bytes(data)
    return {"ok": True, "name": name, "size": len(data)}


@app.post("/api/firmware/desc/{name}")
async def firmware_desc_save(name: str, payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not (fwm.fw_dir / name).is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    text = str(payload.get("text", ""))
    p = fwm.fw_dir / (name + ".txt")
    if text.strip() == "":            # empty -> drop the description file
        if p.is_file():
            p.unlink()
    else:
        p.write_bytes(text.encode("utf-8", "replace"))
    return {"ok": True}


@app.get("/api/firmware/diff")
async def firmware_diff(a: str, b: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        a, b = safe_name(a), safe_name(b)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    pa, pb = fwm.fw_dir / a, fwm.fw_dir / b
    if not pa.is_file() or not pb.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"a": a, "b": b, **_diff_files(pa, pb)}


@app.get("/api/firmware/desc/{name}")
async def firmware_desc(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    p = fwm.fw_dir / (name + ".txt")
    return {"name": name, "text": p.read_text() if p.is_file() else ""}


@app.get("/api/firmware/download")
def firmware_download_many(names: str = "", zipname: str = "firmware.zip"):
    """One pick comes back as the image itself, several as a zip.

    Reached by navigating to it, for the same reason the log bundler is: the
    old UI looped over several synthesized <a download> anchors and iOS
    delivered at most the first of them.

    A bundle carries each image's `<name>.txt` description too -- a dump is
    worth little without the notes, and this way a download round-trips back
    through the uploader unchanged.

    Declared before /api/firmware/files/{name}, which would otherwise match it.
    """
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    wanted = split_names(names)
    if not wanted:
        return JSONResponse(status_code=400, content={"error": "nothing to download"})
    if len(wanted) == 1:
        p = resolve_in(fwm.fw_dir, wanted[0])
        if p is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")
    entries = flat_entries(fwm.fw_dir, wanted, sidecars=(".txt",))
    if not entries:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return _zip_response(zip_entries(entries), _zipname(zipname, "firmware.zip"))


@app.get("/api/firmware/files/{name}")
async def firmware_download(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    try:
        name = safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = fwm.fw_dir / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@app.delete("/api/firmware/files/{name}")
async def firmware_file_delete(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "not ready"})
    if fwm.status()["op"] != "idle":
        return JSONResponse(status_code=409, content={"error": "operation in progress"})
    try:
        name = safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = fwm.fw_dir / name
    if path.is_file():
        path.unlink()
    txt = fwm.fw_dir / (name + ".txt")  # drop the companion description too
    if txt.is_file():
        txt.unlink()
    return {"ok": True}


@app.post("/api/config")
async def post_config(payload: dict):
    prev = cm.load()
    cfg = _deep_merge(prev, payload)
    try:
        cm.validate(cfg)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    cm.save(cfg)
    if diag is not None:
        diag.apply(cfg.get("diag"))
    if storage is not None:
        storage.apply(cfg.get("storage"))
    if worker is not None:  # apply live, no reboot
        worker.set_zip_after(cfg.get("logging", {}).get("zip_after", False))
        kp, kn = prev.get("kline", {}), cfg.get("kline", {})
        if (kp.get("baud") != kn.get("baud") or kp.get("echo") != kn.get("echo")
                or kp.get("init") != kn.get("init")):
            worker.apply_kline(kn.get("baud", "auto"), kn.get("echo", True),
                               kn.get("init", "fast"))
    report = cm.plan(prev, cfg)

    async def _apply():
        # let the HTTP response reach the client before the AP may drop
        await asyncio.sleep(1.0)
        rep = await asyncio.to_thread(cm.apply_network, cfg, prev)
        _after_network(cfg, rep)

    asyncio.create_task(_apply())
    return report


# -- storage ---------------------------------------------------------------
# Their own endpoints rather than /api/config keys: picking a destination has to
# mount something, and a config POST would drag apply_network along with it.
def _storage_state() -> dict:
    st = storage.status()
    state.set_storage(st["dest"], st["root"], st["fallback"])
    return {**st, **_disk_free(Path(st["root"]))}


@app.get("/api/storage")
async def get_storage():
    if storage is None:
        return {"dest": "internal", "devices": [], "error": "err.storage_off"}
    return _storage_state()


@app.post("/api/storage/select")
async def post_storage_select(payload: dict):
    """Choose the log destination, and remember the stick that was chosen."""
    if storage is None:
        return JSONResponse(status_code=503, content={"error": "err.storage_off"})
    res = await asyncio.to_thread(
        storage.select, str(payload.get("dest", "internal")),
        str(payload.get("uuid", "")), str(payload.get("dev", "")))
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    cfg = cm.load()
    st = cfg.setdefault("storage", {})
    st["dest"], st["usb_uuid"] = storage.dest, storage.usb_uuid
    cm.save(cfg)
    return _storage_state()


@app.post("/api/storage/mount")
async def post_storage_mount(payload: dict):
    if storage is None:
        return JSONResponse(status_code=503, content={"error": "err.storage_off"})
    res = await asyncio.to_thread(storage.mount, str(payload.get("dev", "")),
                                  str(payload.get("uuid", "")))
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return _storage_state()


@app.post("/api/storage/unmount")
async def post_storage_unmount():
    """Safe eject. The destination stays "usb" so re-plugging just works."""
    if storage is None:
        return JSONResponse(status_code=503, content={"error": "err.storage_off"})
    res = await asyncio.to_thread(storage.unmount)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return _storage_state()


@app.post("/api/storage/format")
async def post_storage_format(payload: dict):
    """Erase a stick and lay down one exFAT/FAT32 partition. Irreversible.

    Refuses while a log is open: the worker may be writing to the very device
    the request wants to wipe.
    """
    if storage is None:
        return JSONResponse(status_code=503, content={"error": "err.storage_off"})
    if worker is not None and (state.snapshot().get("log_decoded_file")
                               or state.snapshot().get("log_raw_file")):
        return JSONResponse(status_code=409, content={"error": "err.storage_recording"})
    res = await asyncio.to_thread(
        storage.format, str(payload.get("dev", "")),
        str(payload.get("fs", "exfat")), str(payload.get("label", "LOGS")))
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return {**res, **_storage_state()}


# -- power -----------------------------------------------------------------
@app.post("/api/system/reboot")
async def do_reboot():
    return system.reboot()


@app.post("/api/system/shutdown")
async def do_shutdown():
    return system.shutdown()


def _apply_cpu_cfg(cfg: dict) -> dict:
    """Put the board where config says. Empty governor / zero ceiling means the
    kernel default is left alone, which is what a fresh config asks for."""
    c = (cfg.get("system", {}) or {}).get("cpu", {}) or {}
    gov = str(c.get("governor", "") or "")
    mx = int(c.get("max_khz", 0) or 0)
    if not gov and not mx:
        return system.cpu_status()
    return system.cpu_apply(gov, mx)


@app.get("/api/system/cpu")
async def get_cpu():
    return system.cpu_status()


@app.post("/api/system/cpu")
async def set_cpu(payload: dict):
    """Governor and frequency ceiling. Deliberately not routed through
    /api/config: that path also runs apply_network, and nothing here may touch
    the AP the UI is served over."""
    gov = str(payload.get("governor", "") or "")
    try:
        mx = int(payload.get("max_khz", 0) or 0)
    except (TypeError, ValueError):
        mx = 0
    res = await asyncio.to_thread(system.cpu_apply, gov, mx)
    if res.get("ok"):
        cfg = cm.load()
        cfg.setdefault("system", {}).setdefault("cpu", {})
        cfg["system"]["cpu"]["governor"] = gov
        cfg["system"]["cpu"]["max_khz"] = mx
        cm.save(cfg)
    return res


def _auto_time_enabled(cfg: dict | None = None) -> bool:
    c = cfg if cfg is not None else cm.load()
    return bool(c.get("system", {}).get("auto_time_sync", True))


@app.get("/api/system/time")
async def get_time():
    return {**system.time_status(),
            "auto_enabled": _auto_time_enabled(),
            "auto_done": system.auto_sync_done()}


@app.post("/api/system/time")
def post_time(payload: dict):
    """Set the board clock from the connected browser (no internet on the bike)."""
    return system.set_time(payload.get("epoch", 0), str(payload.get("tz", "")))


@app.post("/api/system/time/auto")
def post_time_auto(payload: dict):
    """One automatic clock sync per power-up, from whichever browser gets here first.

    Every page load calls this; the once-per-boot marker lives on the server, so
    several tabs racing each other cannot set the clock twice.
    """
    if not _auto_time_enabled():
        return {"ok": False, "error": "err.auto_off"}
    return system.auto_sync(payload.get("epoch", 0), str(payload.get("tz", "")))


# -- logs ------------------------------------------------------------------
# what a file is, and which of the two lists it belongs to: the Logs tab shows
# rides, Config -> System shows the board's own artefacts
KIND_GROUPS = {"ride": ("decoded", "raw"), "board": ("diag", "fw", "update")}


def _log_kind(name: str) -> str:
    if name.startswith("diag-"):
        return "diag"
    if name.startswith("fw-"):
        return "fw"
    if name.startswith("update-"):
        return "update"
    return "raw" if (".raw.log" in name or name.endswith(".ndjson") or "-raw-" in name) else "decoded"


def _is_log(name: str) -> bool:
    return (name.endswith(".csv") or name.endswith(".raw.log")
            or name.endswith(".ndjson") or name.endswith(".zip")
            or name.startswith("diag-") or name.startswith("fw-")
            or name.startswith("update-"))


@app.get("/api/logs")
async def list_logs(kind: str = ""):
    """Logs from the day folders plus whatever is still loose in the root.

    The flat files predate the per-day layout; they are listed rather than moved
    so a board that has been recording for months does not have its history
    rearranged under it. ``kind`` filters to one class (the System tab asks for
    "board") so a second endpoint with its own path checks is not needed;
    ``kind`` takes one class ("decoded", "raw", "diag", "fw") or a group name
    from KIND_GROUPS.
    """
    root = _logs_root()
    files = []

    def _collect(d: Path, day: str) -> None:
        for p in d.iterdir():
            n = p.name
            if not p.is_file() or not _is_log(n):
                continue
            k = _log_kind(n)
            if kind and k != kind and k not in KIND_GROUPS.get(kind, ()):
                continue
            st = p.stat()
            files.append({
                "name": f"{day}/{n}" if day else n, "file": n, "day": day,
                "size": st.st_size, "mtime": st.st_mtime,
                "kind": k, "zip": n.endswith(".zip"),
            })

    if root.is_dir():
        try:
            _collect(root, "")
            for d in root.iterdir():
                if d.is_dir() and parse_day(d.name):
                    _collect(d, d.name)
        except OSError:
            pass
    files.sort(key=lambda f: f["mtime"], reverse=True)
    st = storage.status(with_devices=False) if storage is not None else {}
    return {"dir": str(root), "root": str(root), "files": files,
            "dest": st.get("dest", "internal"), "is_usb": st.get("is_usb", False),
            "fallback": st.get("fallback", ""), **_disk_free(root)}


def _board_log_text(file: str, prefix: str, lines: int) -> PlainTextResponse:
    """A board artefact (diag-*, fw-*) as text, from the root or a day folder.

    An archived .zip is bytes, not a page — it is refused here and downloaded
    through /api/logs/{name} instead.
    """
    root = _logs_root().resolve()
    try:
        rel = _safe_rel(file)
    except ValueError:
        return PlainTextResponse("bad name\n", status_code=400)
    path = (root / rel).resolve()
    base = Path(rel).name
    if (not base.startswith(prefix) or base.endswith(".zip")
            or root not in path.parents or not path.is_file()):
        return PlainTextResponse("not found\n", status_code=404)
    return _tail_text(path, lines)


def _tail_text(path: Path, lines: int) -> PlainTextResponse:
    try:
        tail = path.read_text(errors="replace").splitlines()[-max(1, min(int(lines), 20000)):]
    except OSError as exc:
        return PlainTextResponse(f"{path.name}: {exc}\n")
    return PlainTextResponse(f"# {path}\n" + "\n".join(tail) + "\n")


@app.get("/api/diag.txt")
def diag_text(lines: int = 200, file: str = ""):
    """The board diagnostics log as plain text, for a phone with no shell.

    Without ``file``: the current health line plus the tail of the open file, so
    the kernel's USB/Wi-Fi messages and our link events are read in one place.
    With ``file``: that closed file's tail — an archived .zip is a download, not
    text, so it is rejected here.
    """
    if file:
        return _board_log_text(file, "diag-", lines)
    if diag is None or not diag.enabled:
        return PlainTextResponse("diagnostics log is off\n")
    if not diag.current_file():
        # the log is tied to a ride: nothing is written while nothing is recorded
        return PlainTextResponse("diagnostics log is idle (no recording)\n")
    head = (f"# {diag.current_file()}  limit={diag.max_bytes // 1048576}MB "
            f"keep={diag.keep} interval={diag.interval_s}s\n"
            "# NOW " + diag.render(diag.health()) + "\n")
    return PlainTextResponse(head + diag.tail(max(1, min(int(lines), 5000))) + "\n")


@app.get("/api/firmware/log.txt")
def firmware_log_text(lines: int = 400, file: str = ""):
    """One firmware operation log as plain text (default: the latest one)."""
    if file:
        return _board_log_text(file, "fw-", lines)
    if fwm is None or not fwm._vlog_path:
        return PlainTextResponse("no firmware operation has run yet\n")
    path = Path(fwm._vlog_path)
    if path.suffix == ".zip":
        return PlainTextResponse(f"# {path.name} is archived — download it from Config → System\n")
    return _tail_text(path, lines)


# -- software update -------------------------------------------------------
# The board holds no repository and has no internet: an update arrives as an
# archive the phone already has. UpdateManager does the work; these three
# endpoints are the only way in.
@app.get("/api/update")
async def update_status():
    if upd is None:
        return {"op": "idle", "version": "", "available": False}
    return {**upd.status(), **upd.version(), "available": True,
            "busy": upd.busy_reason(), **_disk_free(INSTALL_DIR)}


@app.post("/api/update/upload")
async def update_upload(file: UploadFile = File(...)):
    if upd is None:
        return JSONResponse(status_code=503, content={"error": "\u043d\u0435 \u0433\u043e\u0442\u043e\u0432\u043e"})
    try:
        name = safe_name(file.filename or "")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not any(name.lower().endswith(x) for x in (".tar.gz", ".tgz", ".tar", ".zip")):
        return JSONResponse(status_code=400, content={"error": "err.update_bad_archive"})
    reason = upd.busy_reason()          # answer before spending the upload
    if reason:
        return JSONResponse(status_code=409, content={"error": reason})
    data = await file.read()
    if len(data) > update_mod.MAX_ARCHIVE:
        return JSONResponse(status_code=400, content={"error": "err.update_too_big"})
    try:
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        path = UPDATE_DIR / f"upload-{datetime.now():%Y%m%d-%H%M%S}-{name}"
        path.write_bytes(data)
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": "err.update_failed",
                                                      "detail": str(exc)})
    try:
        upd.start(path)
    except UpdateBusy as e:
        path.unlink(missing_ok=True)
        return JSONResponse(status_code=409, content={"error": str(e)})
    return {"ok": True, "name": name, "size": len(data)}


@app.get("/api/update/log.txt")
def update_log_text(lines: int = 400, file: str = ""):
    """One update log as plain text (default: the latest one)."""
    if file:
        return _board_log_text(file, "update-", lines)
    if upd is None or not upd._vlog_path:
        return PlainTextResponse("no update has run yet\n")
    return _tail_text(Path(upd._vlog_path), lines)


def _log_entries(names) -> list[tuple[str, Path]]:
    """Validated (arcname, path) pairs for the log tree.

    The arcname keeps the day folder, so the zip mirrors the board. Logs do not
    go through archive.resolve_in: they live two segments deep and _safe_rel is
    the stricter rule for that shape.
    """
    out: list[tuple[str, Path]] = []
    for raw in names or []:
        try:
            rel = _safe_rel(str(raw))
        except ValueError:
            continue
        p = _log_file(rel)
        if p is not None:
            out.append((rel, p))
    return out


def _zip_response(blob: bytes, zipname: str) -> Response:
    return Response(content=blob, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{zipname}"',
        "Content-Length": str(len(blob)),
    })


def _zipname(raw, default: str) -> str:
    try:
        name = safe_name(raw or default)
    except ValueError:
        name = default
    return name if name.endswith(".zip") else name + ".zip"


@app.get("/api/logs/download")
def logs_download_get(names: str = "", zipname: str = "k-line.log.zip"):
    """Same bundle as the POST, reached by navigating to it.

    Safari on iOS ignores a synthesized <a download> and refuses to save a
    blob: URL, so a fetch-then-save-blob download hands an iPhone nothing at
    all. A plain GET carrying Content-Disposition is the one shape every
    browser saves -- which means the bundling has to happen here.

    Declared before /api/logs/{name:path}, which would otherwise swallow it.
    """
    entries = _log_entries(split_names(names))
    if not entries:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return _zip_response(zip_entries(entries), _zipname(zipname, "k-line.log.zip"))


@app.post("/api/logs/download")
def logs_download(payload: dict):
    """Bundle the selected logs into one zip and stream it back as an attachment.

    Body: {"names": [...], "zipname": "k-line-....log.zip"}. Kept for anything
    already calling it; it shares the bundler with the GET above.
    """
    entries = _log_entries(payload.get("names", []))
    return _zip_response(zip_entries(entries),
                         _zipname(payload.get("zipname"), "k-line.log.zip"))


@app.get("/api/logs/{name:path}")
async def get_log(name: str):
    try:
        name = _safe_rel(name)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    path = _log_file(name)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    if name.endswith(".zip"):
        media = "application/zip"
    elif name.endswith(".csv"):
        media = "text/csv"
    else:
        media = "application/x-ndjson"
    return FileResponse(path, filename=path.name, media_type=media)


@app.delete("/api/logs/{name:path}")
async def delete_log(name: str):
    try:
        name = _safe_rel(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = _log_file(name)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    path.unlink()
    # an emptied day folder is noise in the list; the root itself stays
    parent = path.parent
    if parent != _logs_root().resolve() and parse_day(parent.name):
        try:
            parent.rmdir()
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/logs/{name:path}/data")
async def log_data(name: str):
    """Decoded CSV text for charting (unzips a .csv.zip). Rejects raw logs."""
    try:
        name = _safe_rel(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = _log_file(name)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    if _log_kind(Path(name).name) != "decoded":
        return JSONResponse(status_code=400, content={"error": "not_decoded"})
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                inner = [n for n in z.namelist() if n.endswith(".csv")]
                if not inner:
                    return JSONResponse(status_code=400, content={"error": "not_decoded"})
                data = z.read(inner[0])
        else:
            data = path.read_bytes()
    except (OSError, zipfile.BadZipFile):
        return JSONResponse(status_code=400, content={"error": "read_error"})
    if len(data) > 40 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "too_large"})
    return {"name": name, "text": data.decode("utf-8", "replace")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=80)
