"""Offline test for the software-update panel in Config -> System.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_update.js``: it evaluates ``app/static/app.js`` in a Node vm on the
shared DOM stub and drives the panel through its own handlers — including the
confirm dialog, which is the one thing between a tapped file and a board that
restarts itself. This wrapper only exists so the feature is covered by the usual
``for f in tests/*.py`` run; without node on the host it skips instead of failing.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_update.js"


def test_update_panel():
    if shutil.which("node") is None:
        print("skip test_update_panel (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_update.js failed:\n" + out
    assert "ok nothing is uploaded until" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
