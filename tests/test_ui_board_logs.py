"""Offline test for the two log browsers (Logs tab and Config -> System).

The behaviour under test is browser-side, so the assertions live in
``tests/ui_board_logs.js``: it evaluates ``app/static/app.js`` in a Node vm on a
DOM stub and drives the browsers through their own refresh/filter handlers. This
wrapper only exists so the feature is covered by the usual ``for f in tests/*.py``
run; without node on the host it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_board_logs.js"


def test_board_log_browser():
    if shutil.which("node") is None:
        print("skip test_board_log_browser (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_board_logs.js failed:\n" + out
    assert "ok the System list groups by day" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
