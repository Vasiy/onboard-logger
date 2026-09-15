"""Offline test for the derived channel that follows its inputs.

Gear has no rli of its own -- the board works it out of channels the rider is
already polling -- so the UI couples its tick to them: completing the set turns
it on for free, losing rpm or speed turns it off instead of logging a column of
"--". The behaviour is browser-side, so the assertions live in
``tests/ui_gear_link.js``; this wrapper only exists so the feature is covered by
the usual ``for f in tests/*.py`` run, and it skips instead of failing where
node is not installed.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_gear_link.js"


def test_gear_link():
    if shutil.which("node") is None:
        print("skip test_gear_link (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_gear_link.js failed:\n" + out
    assert "ok completing the set gear is derived from ticks gear by itself" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
