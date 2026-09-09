"""Offline test for the preset note on the Logger tab.

The behaviour under test is browser-side, so the assertions live in
``tests/ui_preset_note.js``: it evaluates ``app/static/app.js`` in a Node vm on
the shared DOM stub and drives the note through the same handlers the page
binds. The one that matters most is the escaping — a note is the only place in
this UI where text a person typed becomes markup. This wrapper exists so the
feature is covered by the usual ``for f in tests/*.py`` run; without node on the
host it skips instead of failing.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "ui_preset_note.js"


def test_preset_note_ui():
    if shutil.which("node") is None:
        print("skip test_preset_note_ui (no node on this host)")
        return
    r = subprocess.run(["node", str(HARNESS)], cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    assert r.returncode == 0, "ui_preset_note.js failed:\n" + out
    assert "ok markup in a note comes out as text" in out, \
        "harness ran but asserted nothing:\n" + out
    for line in out.splitlines():
        print(" ", line)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)
