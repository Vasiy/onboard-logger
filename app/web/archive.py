"""Name validation and zip bundling for the file lists in the UI.

Lives outside main.py so the path guards can be tested without importing the
whole app: importing ``app.main`` builds the FastAPI application and reads
``/etc``, which a unit test has no business doing.

The bundler here is deliberately dumb — it is handed pairs that the caller has
already resolved. The two file trees it serves do not share a shape: firmware
images sit flat in one directory, while ride logs live in a folder per day and
have their own two-segment rule (``_safe_rel`` in main.py). Teaching one
resolver both would mean weakening the stricter of them.
"""

import io
import zipfile
from pathlib import Path
from typing import Iterable


def safe_name(name: str) -> str:
    """Basename restricted to a safe charset (defends the fw/log dirs).

    ``".."`` is named explicitly: ``Path("..").name`` is ``".."``, and a dot is
    in the allowed charset, so it used to survive both checks and come back out
    as a usable name. Every call site happened to guard with ``is_file()``, but
    a rule that only works because of what its callers do next is not a rule.
    """
    base = Path(name).name
    if not base or base in (".", "..") or not all(c.isalnum() or c in "._-" for c in base):
        raise ValueError("bad file name")
    return base


def resolve_in(base_dir: Path, name: str) -> Path | None:
    """Path of `name` directly inside base_dir, or None if it escapes or is missing.

    For flat directories only — the firmware dir is one. ``p.parent == base_dir``
    is the second lock on the same door as ``safe_name``: a symlink planted in
    the directory resolves elsewhere and is refused here.
    """
    try:
        base = safe_name(name)
    except ValueError:
        return None
    base_dir = Path(base_dir).resolve()
    p = (base_dir / base).resolve()
    return p if p.parent == base_dir and p.is_file() else None


def zip_entries(entries: Iterable[tuple[str, Path]]) -> bytes:
    """Bundle (arcname, path) pairs into a zip, first name wins on a repeat."""
    buf = io.BytesIO()
    added: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, path in entries:
            if arcname in added or path is None or not path.is_file():
                continue
            z.write(path, arcname=arcname)
            added.add(arcname)
    return buf.getvalue()


def flat_entries(base_dir: Path, names: Iterable[str],
                 sidecars: tuple[str, ...] = ()) -> list[tuple[str, Path]]:
    """Resolve names in a flat directory, pulling in companions by suffix.

    A firmware image carries its description as ``<name>.txt`` and is worth
    little without it, so a download round-trips back through the uploader
    unchanged. Unknown or unsafe names are skipped rather than refused: the
    caller asked for a bundle, not for a verdict on each name.
    """
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw in names:
        p = resolve_in(base_dir, str(raw))
        if p is None or p.name in seen:
            continue
        out.append((p.name, p))
        seen.add(p.name)
        for suf in sidecars:
            sc = resolve_in(base_dir, p.name + suf)
            if sc is not None and sc.name not in seen:
                out.append((sc.name, sc))
                seen.add(sc.name)
    return out


def split_names(raw: str) -> list[str]:
    """The ``names=a,b,c`` query parameter, emptied entries dropped."""
    return [p for p in str(raw or "").split(",") if p]
