"""Offline test for the end-of-operation notice on the Firmware read/write.

The behaviour is browser-side, so the assertions live in ``tests/ui_fw_done.js`` (app.js
in a Node vm on the shared DOM stub). This wrapper only exists so the feature is covered
by the usual ``for f in tests/*.py`` run; without node it skips instead of failing.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_fw_done.js"


def test_fw_done_ui():
    if shutil.which("node") is None:
        print("skip test_fw_done_ui (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_fw_done.js failed:\n" + out
    assert "ok a finished read announces itself" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
