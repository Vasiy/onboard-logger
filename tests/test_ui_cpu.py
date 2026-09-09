"""Offline test for the Processor block.

The behaviour is browser-side, so the assertions live in ``tests/ui_cpu.js``: it
evaluates ``app/static/app.js`` in a Node vm on the shared DOM stub and drives
the two selects through their own change handlers, with a fetch that records the
request as well as answering it.

That last part is the point. ``api()`` hands its second argument straight to
fetch as RequestInit, so passing the payload alone sent a GET, which reached the
status endpoint, came back without an ``ok`` and made an accepted change look
like a refusal. The harness asserts the method and the body, not just the reply.

This wrapper exists so the feature is covered by the usual ``for f in tests/*.py``
run; without node on the host it skips instead of failing the suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_cpu.js"


def test_cpu_panel():
    if shutil.which("node") is None:
        print("skip test_cpu_panel (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_cpu.js failed:\n" + out
    assert "ok changing the governor sends a POST" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
