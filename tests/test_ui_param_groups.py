"""Offline test for the three-way split of the parameter list.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_param_groups.js``: it evaluates ``app/static/app.js`` in a Node vm on
the shared DOM stub and drives the list through ``applySnapshot()``, exactly as
the websocket does. This wrapper only exists so the feature is covered by the
usual ``for f in tests/*.py`` run; without node on the host it skips instead of
failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_param_groups.js"


def test_param_groups():
    if shutil.which("node") is None:
        print("skip test_param_groups (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_param_groups.js failed:\n" + out
    assert "ok known channels come first" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
