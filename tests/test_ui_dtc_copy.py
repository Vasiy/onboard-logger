"""Offline test for the Fault-codes "Copy" button.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_dtc_copy.js``: it evaluates ``app/static/app.js`` in a Node vm on a
DOM stub and drives the button's own click handler. This wrapper only exists so
the feature is covered by the usual ``for f in tests/*.py`` run. Node is already
required by the JS syntax check in CLAUDE.md; if it is missing (the board itself
has no node), the test skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_dtc_copy.js"


def test_dtc_copy_button():
    if shutil.which("node") is None:
        print("skip test_dtc_copy_button (no node on this host)")
        return
    r = subprocess.run(
        ["node", str(HARNESS)], cwd=ROOT,
        capture_output=True, text=True,
    )
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_dtc_copy.js failed:\n" + out
    assert "ok copied text carries" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
