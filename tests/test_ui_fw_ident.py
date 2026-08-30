"""Offline test for the Firmware tab's identification UI.

The behaviour is browser-side, so the assertions live in ``tests/ui_fw_ident.js``:
it evaluates ``app/static/app.js`` in a Node vm on a DOM stub and drives
``loadFirmware()`` with a stubbed ``api()``. This wrapper only exists so the
feature is covered by the usual ``for f in tests/*.py`` run; without node (the
board has none) it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_fw_ident.js"


def test_fw_ident_ui():
    if shutil.which("node") is None:
        print("skip test_fw_ident_ui (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_fw_ident.js failed:\n" + out
    assert "ok the file list shows" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
