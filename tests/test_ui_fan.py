"""Offline test for the fan telemetry readout in Config -> System.

The behaviour is browser-side, so the assertions live in ``tests/ui_fan.js``:
it evaluates ``app/static/app.js`` in a Node vm on the shared DOM stub and
drives ``renderFan()`` directly. See that file for what it guards.

This wrapper exists so the feature is covered by the usual ``for f in
tests/*.py`` run; without node on the host it skips instead of failing the
suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_fan.js"


def test_fan_readout():
    if shutil.which("node") is None:
        print("skip test_fan_readout (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_fan.js failed:\n" + out
    assert "ok with the feature off, the corner shows nothing tied to nothing" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
