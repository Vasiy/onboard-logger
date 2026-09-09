"""Every message the UI shows has to be an i18n key that exists in every locale.

`t()` falls back to the string it was given, so prose from the backend renders
untranslated and still *looks* fine — which is how a whole set of messages sat
in one language for months without anyone noticing a bug. Nothing catches that
by eye, so it is caught here: the backend's strings are read out of the source,
and every one of them has to be a key i18n.js actually carries, in all eight
locales.

Run directly:  python tests/test_i18n_messages.py
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Fields whose value reaches the browser and is handed to t().
KEYFIELDS = {"error", "message", "client_error", "status_msg", "k"}
KEY = re.compile(r"^[a-z][a-z0-9_]*\.[a-zA-Z][a-zA-Z0-9_.]*$")

# Raisers whose message becomes an `error` field one frame up.
RAISERS = {"ValueError", "RuntimeError", "ConfigError"}

# Prose that never reaches a browser: an internal invariant, and the two dev-host
# notes that are only ever printed next to a command that does not exist.
ALLOWED_PROSE = {
    "data length must be 1..63 for single-byte format",
    "blocked",
}


def _locales():
    out = subprocess.run(
        ["node", "-e",
         "const fs=require('fs');global.window={};"
         "eval(fs.readFileSync('app/static/i18n.js','utf8'));"
         "console.log(JSON.stringify(window.I18N))"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _sent():
    """Every literal the backend puts where the UI will translate it."""
    found = []
    for f in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value in KEYFIELDS
                            and isinstance(v, ast.Constant) and isinstance(v.value, str)
                            and v.value):
                        found.append((f.name, node.lineno, v.value))
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if name in RAISERS | {"_act"} and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value:
                        found.append((f.name, node.lineno, a.value))
                    if isinstance(a, ast.IfExp):        # _act("a" if x else "b")
                        for side in (a.body, a.orelse):
                            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                                found.append((f.name, node.lineno, side.value))
    return found


def test_no_prose_reaches_the_ui():
    bad = [(f, n, v) for f, n, v in _sent()
           if not KEY.match(v) and v not in ALLOWED_PROSE]
    assert not bad, "these are shown to the rider as-is instead of being translated:\n" + \
        "\n".join("   %s:%d  %r" % b for b in bad)


def test_every_key_the_backend_sends_exists_in_every_locale():
    if shutil.which("node") is None:
        print("skip test_every_key_the_backend_sends_exists_in_every_locale (no node)")
        return
    loc = _locales()
    sent = {v for _, _, v in _sent() if KEY.match(v)}
    assert sent, "the reader found nothing — it has stopped seeing the messages"
    missing = {}
    for name, table in loc.items():
        gone = sorted(k for k in sent if k not in table)
        if gone:
            missing[name] = gone
    assert not missing, "keys the backend sends but a locale has no text for:\n" + \
        "\n".join("   %s: %s" % (k, ", ".join(v)) for k, v in missing.items())


def test_the_locales_agree_on_their_key_set():
    if shutil.which("node") is None:
        print("skip test_the_locales_agree_on_their_key_set (no node)")
        return
    loc = _locales()
    en = set(loc["en"])
    for name, table in loc.items():
        assert set(table) == en, "%s differs from en by %s" % (
            name, sorted(en.symmetric_difference(table))[:8])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok " + name)
