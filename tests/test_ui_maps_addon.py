"""Offline test for the add-on wiring in the browser.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_maps_addon.js``: it evaluates ``app/static/app.js`` in a Node vm on
the shared DOM stub and drives the Firmware tab's "Show map" button and the
Config -> System panel through their own handlers. This wrapper only exists so
the feature is covered by the usual ``for f in tests/*.py`` run; without node on
the host it skips instead of failing.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_maps_addon.js"


def test_maps_addon_ui():
    if shutil.which("node") is None:
        print("skip test_maps_addon_ui (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_maps_addon.js failed:\n" + out
    assert "ok pressing again lands in the tab" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
