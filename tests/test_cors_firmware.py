"""Offline tests for the CORS header on the read-only firmware endpoints.

ecu-map-viewer, run as a separate page rather than copied into app/static/,
needs to read /api/firmware and /api/firmware/files/{name} across origins.
The middleware in app.main (_cors_firmware_read) adds the header to those GETs
only -- every write verb (POST/DELETE) on /api/firmware/* must never see it,
and neither may an unrelated route. These tests call the middleware directly
with fake Request/call_next stand-ins, the same way the rest of this suite
avoids booting the whole app (KLineWorker, StorageManager, hostapd...) just to
check one function.

Run directly:  python tests/test_cors_firmware.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.responses import PlainTextResponse  # noqa: E402

from app import main  # noqa: E402


def _req(method: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def _run(method: str, path: str):
    async def call_next(_request):
        return PlainTextResponse("ok")
    return asyncio.run(main._cors_firmware_read(_req(method, path), call_next))


def test_firmware_listing_gets_the_header():
    resp = _run("GET", "/api/firmware")
    assert resp.headers["access-control-allow-origin"] == "*"


def test_firmware_file_download_gets_the_header():
    resp = _run("GET", "/api/firmware/files/stock.bin")
    assert resp.headers["access-control-allow-origin"] == "*"


def test_other_read_only_firmware_routes_get_it_too():
    for path in ("/api/firmware/check", "/api/firmware/catalog",
                 "/api/firmware/desc/stock.bin", "/api/firmware/diff"):
        resp = _run("GET", path)
        assert resp.headers["access-control-allow-origin"] == "*", path


def test_write_verbs_never_get_the_header():
    """A flash, a rename, an upload or a delete must stay same-origin only --
    only the method changes here, the path is identical to a route that does."""
    for method, path in (
        ("POST", "/api/firmware/read"),
        ("POST", "/api/firmware/write"),
        ("POST", "/api/firmware/rename"),
        ("POST", "/api/firmware/upload"),
        ("POST", "/api/firmware/cancel"),
        ("POST", "/api/firmware/catalog"),
        ("DELETE", "/api/firmware/files/stock.bin"),
        ("DELETE", "/api/firmware/catalog/XYZ"),
    ):
        resp = _run(method, path)
        assert "access-control-allow-origin" not in resp.headers, (method, path)


def test_unrelated_routes_are_untouched():
    for method, path in (("GET", "/api/state"), ("GET", "/"), ("GET", "/api/logs")):
        resp = _run(method, path)
        assert "access-control-allow-origin" not in resp.headers, (method, path)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
