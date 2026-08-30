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
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .kline.ecu_id import load_fields, parse_desc
from .kline.logger import KLineWorker
from .web import system
from .web.config_mgr import ConfigManager, REPO_DIR, _deep_merge
from .web.wifi_scan import scan_networks
from .web.firmware import FirmwareBlocked, FirmwareManager
from .web import fw_catalog, fw_guard
from .kline import fw_ident
from .web.led import Led
from .web.state import State

ETC = Path("/etc/onboard-logger")
STATIC_DIR = Path(__file__).resolve().parent / "static"
UTIL_PATH = Path("/opt/onboard-logger/bin/5am_util")
# IAW 5AM firmware image size (0x50000). Writing anything else bricks the ECU.
DEFAULT_FW_SIZE = 327680


def _required_fw_size() -> int:
    return int(cm.load().get("firmware_size", DEFAULT_FW_SIZE))


def _safe_name(name: str) -> str:
    """Basename restricted to a safe charset (defends the fw/log dirs)."""
    base = Path(name).name
    if not base or not all(c.isalnum() or c in "._-" for c in base):
        raise ValueError("недопустимое имя файла")
    return base


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


state = State()
led = Led()
cm = ConfigManager()
worker: KLineWorker | None = None
fwm: FirmwareManager | None = None


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
        state.set_status("error", "Wi-Fi: " + rep.get("client_error", "клиент не подключился"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, fwm
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

    asyncio.create_task(_startup_network())
    ecu_id_path = ETC / "ecu_id.json"
    if not ecu_id_path.exists():
        ecu_id_path = REPO_DIR / "config" / "ecu_id.json"
    ecu_fields = load_fields(ecu_id_path)
    worker = KLineWorker(
        port=cfg["kline"]["port"],
        params_path=_params_path(),
        log_dir=cfg["log_dir"],
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
    )
    worker.start()
    # restore the parameter selection saved from a previous run (falls back to the
    # named-default set the worker already applied)
    _saved = _load_selected()
    if _saved:
        _valid = {c["key"] for c in state.snapshot().get("catalog", [])}
        _sel = [k for k in _saved if k in _valid]
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
    )
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            worker.join(timeout=3.0)


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
    keys = [str(k) for k in payload.get("keys", [])]
    state.set_selected(keys)
    if worker is not None:  # also drives decoded-log columns (rolls the file)
        worker.set_selected(keys)
    _save_selected(keys)    # persist across restarts
    return {"selected": state.snapshot()["selected"]}


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
        return JSONResponse(status_code=503, content={"error": "не готово"})
    name = payload.get("name") or f"dump-{datetime.now():%Y%m%d-%H%M%S}.bin"
    try:
        name = _safe_name(name if name.endswith(".bin") else name + ".bin")
        fwm.start_read(name, verbose=bool(payload.get("verbose")))
    except (ValueError, RuntimeError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True, "name": name}


@app.post("/api/firmware/write")
async def firmware_write(payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(payload.get("name", ""))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = fwm.fw_dir / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
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
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not (fwm.fw_dir / name).is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    return _guard_verdict(name)


@app.post("/api/firmware/rename")
async def firmware_rename(payload: dict):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "не готово"})
    if fwm.status().get("op", "idle") != "idle":
        return JSONResponse(status_code=400, content={"error": "fw_busy"})
    try:
        src = _safe_name(payload.get("from", ""))
        dst = _safe_name(payload.get("to", ""))
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "fw_bad_name"})
    if not dst.endswith(".bin"):
        dst += ".bin"
    sp = fwm.fw_dir / src
    if not sp.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
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
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(file.filename or "")
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
                bn = _safe_name(Path(m).name)
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
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not (fwm.fw_dir / name).is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
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
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        a, b = _safe_name(a), _safe_name(b)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    pa, pb = fwm.fw_dir / a, fwm.fw_dir / b
    if not pa.is_file() or not pb.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    return {"a": a, "b": b, **_diff_files(pa, pb)}


@app.get("/api/firmware/desc/{name}")
async def firmware_desc(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    p = fwm.fw_dir / (name + ".txt")
    return {"name": name, "text": p.read_text() if p.is_file() else ""}


@app.get("/api/firmware/files/{name}")
async def firmware_download(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "не готово"})
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = fwm.fw_dir / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@app.delete("/api/firmware/files/{name}")
async def firmware_file_delete(name: str):
    if fwm is None:
        return JSONResponse(status_code=503, content={"error": "не готово"})
    if fwm.status()["op"] != "idle":
        return JSONResponse(status_code=409, content={"error": "операция выполняется"})
    try:
        name = _safe_name(name)
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


# -- power -----------------------------------------------------------------
@app.post("/api/system/reboot")
async def do_reboot():
    return system.reboot()


@app.post("/api/system/shutdown")
async def do_shutdown():
    return system.shutdown()


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
def _log_kind(name: str) -> str:
    return "raw" if (".raw.log" in name or name.endswith(".ndjson") or "-raw-" in name) else "decoded"


@app.get("/api/logs")
async def list_logs():
    cfg = cm.load()
    log_dir = Path(cfg["log_dir"])
    files = []
    if log_dir.is_dir():
        for p in log_dir.iterdir():
            n = p.name
            if not (n.endswith(".csv") or n.endswith(".raw.log") or n.endswith(".ndjson") or n.endswith(".zip")):
                continue
            st = p.stat()
            files.append({
                "name": n, "size": st.st_size, "mtime": st.st_mtime,
                "kind": _log_kind(n), "zip": n.endswith(".zip"),
            })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return {"dir": str(log_dir), "files": files, **_disk_free(log_dir)}


@app.post("/api/logs/download")
def logs_download(payload: dict):
    """Bundle the selected logs into one zip and stream it back as an attachment.

    Body: {"names": [...], "zipname": "k-line-....log.zip"}. Names are validated and
    confined to the log dir; a selected .zip is stored as-is. Sync def -> threadpool.
    """
    log_dir = Path(cm.load()["log_dir"]).resolve()
    zipname = _safe_name(payload.get("zipname", "k-line.log.zip"))
    if not zipname.endswith(".zip"):
        zipname += ".zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for raw in payload.get("names", []):
            try:
                name = _safe_name(str(raw))
            except ValueError:
                continue
            p = (log_dir / name).resolve()
            if p.parent == log_dir and p.is_file():
                z.write(p, arcname=name)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zipname}"'},
    )


@app.get("/api/logs/{name}")
async def get_log(name: str):
    cfg = cm.load()
    log_dir = Path(cfg["log_dir"])
    path = (log_dir / name).resolve()
    if path.parent != log_dir.resolve() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    if name.endswith(".zip"):
        media = "application/zip"
    elif name.endswith(".csv"):
        media = "text/csv"
    else:
        media = "application/x-ndjson"
    return FileResponse(path, filename=name, media_type=media)


@app.delete("/api/logs/{name}")
async def delete_log(name: str):
    cfg = cm.load()
    log_dir = Path(cfg["log_dir"])
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = (log_dir / name).resolve()
    if path.parent != log_dir.resolve() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    path.unlink()
    return {"ok": True}


@app.get("/api/logs/{name}/data")
async def log_data(name: str):
    """Decoded CSV text for charting (unzips a .csv.zip). Rejects raw logs."""
    cfg = cm.load()
    log_dir = Path(cfg["log_dir"])
    try:
        name = _safe_name(name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    path = log_dir / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "не найдено"})
    if _log_kind(name) != "decoded":
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
