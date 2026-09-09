"""Offline test for the fixed sparkline window on the temperature channels.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_sparkline.js``: it evaluates ``app/static/app.js`` in a Node vm on the
shared DOM stub, feeds snapshots through ``updateSpanBase()`` and draws through
``drawSpark()`` onto a canvas that records where the line was actually put. This
wrapper only exists so the feature is covered by the usual ``for f in tests/*.py``
run; without node on the host it skips instead of failing.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_sparkline.js"


def test_sparkline_window():
    if shutil.which("node") is None:
        print("skip test_sparkline_window (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_sparkline.js failed:\n" + out
    assert "ok inside the window, the same drift is nearly a flat line" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
