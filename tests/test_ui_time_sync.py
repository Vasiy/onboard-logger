"""Offline test for offering the browser's clock to a board that has none.

The behaviour is browser-side, so the assertions live in
``tests/ui_time_sync.js``: it evaluates ``app/static/app.js`` in a Node vm on the
shared DOM stub and fires the websocket's ``onopen`` by hand — the reconnect a
phone does when the board comes back without the page ever being loaded again.
That case used to leave the board's clock untouched for a whole ride, and every
file of it in the previous day's folder. This wrapper exists so the feature is
covered by the usual ``for f in tests/*.py`` run; without node on the host it
skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_time_sync.js"


def test_time_sync():
    if shutil.which("node") is None:
        print("skip test_time_sync (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_time_sync.js failed:\n" + out
    assert "the clock is offered on every socket" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
