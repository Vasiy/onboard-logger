"""Offline test for the tile order on the Logger tab.

Read mode lays the picked channels out as tiles, and the rider arranges them by
pressing and holding one until the grid wiggles. The behaviour under test is
browser-side, so the assertions live in ``tests/ui_tile_order.js``: it evaluates
``app/static/app.js`` in a Node vm on the shared DOM stub and drives the pure
half — which order these keys resolve to, which tile a finger at (x, y) is over,
what gets written back — plus the arrange bar's two buttons, through their own
handlers. The stub has no pointer events, so the gesture layer itself is
deliberately thin and calls into exactly those functions.

This wrapper only exists so the feature is covered by the usual
``for f in tests/*.py`` run; without node on the host it skips instead of
failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_tile_order.js"


def test_tile_order():
    if shutil.which("node") is None:
        print("skip test_tile_order (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_tile_order.js failed:\n" + out
    assert "ok merging keeps an unticked channel anchored where it was" in out, \
        "harness ran but asserted nothing:\n" + out
    assert "ok a tap on a tile no longer unticks the channel" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
