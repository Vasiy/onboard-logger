"""Offline test for the keep-awake switch.

The behaviour is browser-side, so the assertions live in
``tests/ui_wakelock.js``: it evaluates ``app/static/app.js`` in a Node vm on the
shared DOM stub, whose ``navigator`` is empty and ``isSecureContext`` false —
exactly the board's plain-HTTP case, where ``navigator.wakeLock`` does not exist
and the muted looping clip is the only thing left. This wrapper exists so the
feature is covered by the usual ``for f in tests/*.py`` run; without node on the
host it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_wakelock.js"


def test_wakelock():
    if shutil.which("node") is None:
        print("skip test_wakelock (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_wakelock.js failed:\n" + out
    assert "ok the preference is device-local" in out, "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
