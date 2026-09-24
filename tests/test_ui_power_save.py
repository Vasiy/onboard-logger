"""Offline test for the Power save hint in Config -> System.

The behaviour is browser-side, so the assertions live in
``tests/ui_power_save.js``: it evaluates ``app/static/app.js`` in a Node vm on
the shared DOM stub and drives ``renderPowerSave()`` directly.

What it is guarding: the countdown is the *board's* idle clock, pushed in the
snapshot, not a timer the phone starts for itself. A phone can join the AP long
after the ignition went off, so a browser-side timer would read zero on a board
minutes from powering itself down.

This wrapper exists so the feature is covered by the usual ``for f in tests/*.py``
run; without node on the host it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_power_save.js"


def test_power_save_hint():
    if shutil.which("node") is None:
        print("skip test_power_save_hint (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_power_save.js failed:\n" + out
    assert "ok the countdown is the board's idle clock" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
