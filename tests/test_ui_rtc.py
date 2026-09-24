"""Offline test for the clock-source block in Config -> System.

The behaviour is browser-side, so the assertions live in ``tests/ui_rtc.js``: it
evaluates ``app/static/app.js`` in a Node vm on the shared DOM stub and drives
``loadRtc()`` against a board that answers with this NEO3's two RTCs.

What it guards: the block is hidden unless a battery-backed module was found,
what it names is the module and not the PMIC clock sharing the bus with it, and
the standing hint stops saying "the board has no battery-backed clock" once the
board has one.

This wrapper exists so the feature is covered by the usual ``for f in tests/*.py``
run; without node on the host it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_rtc.js"


def test_rtc_block():
    if shutil.which("node") is None:
        print("skip test_rtc_block (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_rtc.js failed:\n" + out
    assert "ok a board with a real module shows the switch" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
