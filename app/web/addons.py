"""Pluggable add-ons: a page the board serves, and a place for it to keep files.

An add-on is static content and nothing else. It carries no Python and none of
it is imported: executing code out of an uploaded archive, as root, next to
/opt is exactly the risk ``update.py`` spends its content gate defending
against, and an add-on has no claim to that trust.

What the board offers in return is a **nameless** file store. It never looks
inside, never learns what the bytes mean, and imposes nothing but a size. The
map viewer keeps its XDF definitions there; the logger does not know the word.
Everything an add-on owns lives under ``addons/<name>/``::

    addons/maps/addon.json   {"name": "maps", "version": "...", "title": "..."}
    addons/maps/web/         the page, served read-only at /addons/maps/
    addons/maps/data/        opaque files, reachable only through the API

The split between ``web/`` and ``data/`` is not tidiness. If stored files sat
inside the served tree they would also be reachable as static content, typed by
their extension -- and an uploaded .html would then run in the logger's own
origin. Served files and stored files are different doors on purpose.

Removing an add-on is ``rm -rf`` of that one directory: code and data go
together, and nothing of it is left anywhere else. ``addons/`` itself lives
inside the install tree but outside git, like ``.venv`` and ``bin/5am_util`` --
so ``deploy.sh`` excludes it and ``update.py`` carries it across a swap.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

from .update import _rel, _strip_top

# An add-on is a page: the viewer is ~4 MB of vendored Plotly and change.
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_UNPACKED = 64 * 1024 * 1024
MAX_MEMBERS = 2000

# A stored file is a definition or something like it. An XDF is ~110 KB; the
# caps are here so a wrong PUT cannot fill the SD card the ride logs live on.
MAX_DATA_FILE = 4 * 1024 * 1024
MAX_DATA_TOTAL = 64 * 1024 * 1024

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# deliberately not an extension whitelist: the store is nameless, and what an
# add-on keeps in it is its own business
DATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

# Enough to serve a dependency-free page. Anything else is handed over as bytes
# rather than guessed at -- a served file must never be typed into something a
# browser will execute on its own.
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def _is_root_member(name: str, top: str) -> bool:
    """True for the archive's own root entry -- "." , "./" or the wrapper folder.

    It carries no path once the wrapper is stripped, so it is neither a file to
    write nor a traversal to refuse; it is simply where everything else lives.
    """
    n = name.replace("\\", "/").strip()
    if top and n.startswith(top):
        n = n[len(top):]
    return not [p for p in n.split("/") if p and p != "."]


def safe_addon(name: str) -> str | None:
    """An add-on name is one lowercase slug -- it becomes a directory and a URL."""
    return name if isinstance(name, str) and NAME_RE.match(name) else None


def safe_data(name: str) -> str | None:
    """A stored file is one plain name; nothing that could leave the store."""
    if not isinstance(name, str) or not DATA_RE.match(name):
        return None
    return None if name in (".", "..") or "/" in name or "\\" in name else name


class AddonManager:
    """Installs, serves and stores for the add-ons under one directory."""

    def __init__(self, root):
        self.root = Path(root)

    # ---- inventory -------------------------------------------------------
    def dir_of(self, name: str) -> Path | None:
        name = safe_addon(name)
        if not name:
            return None
        p = self.root / name
        return p if (p / "addon.json").is_file() else None

    def manifest(self, name: str) -> dict:
        d = self.dir_of(name)
        if not d:
            return {}
        try:
            m = json.loads((d / "addon.json").read_text())
        except (OSError, ValueError):
            return {}
        return m if isinstance(m, dict) else {}

    def installed(self) -> list[dict]:
        out = []
        if not self.root.is_dir():
            return out
        for entry in sorted(os.listdir(self.root)):
            if not self.dir_of(entry):
                continue
            m = self.manifest(entry)
            out.append({
                "name": entry,
                "version": str(m.get("version", "")),
                "title": str(m.get("title", entry)),
                "url": "/addons/%s/" % entry,
                "files": len(self._data_names(entry)),
            })
        return out

    # ---- serving ---------------------------------------------------------
    def web_file(self, name: str, rel: str) -> Path | None:
        """The file this URL asks for, or None -- never anything outside web/."""
        d = self.dir_of(name)
        if not d:
            return None
        rel = _rel(rel or "index.html", "")
        if rel is None:
            return None
        web = (d / "web").resolve()
        p = (web / rel).resolve()
        # resolve() first, then check the parentage: a symlink planted in the
        # tree is the one way a relative path can still end up elsewhere
        if web not in p.parents and p != web:
            return None
        if p.is_dir():
            p = p / "index.html"
        return p if p.is_file() else None

    @staticmethod
    def content_type(path: Path) -> str:
        return TYPES.get(path.suffix.lower(), "application/octet-stream")

    # ---- the nameless store ---------------------------------------------
    def data_dir(self, name: str) -> Path | None:
        d = self.dir_of(name)
        return (d / "data") if d else None

    def _data_names(self, name: str) -> list[str]:
        d = self.data_dir(name)
        if not d or not d.is_dir():
            return []
        return sorted(f for f in os.listdir(d)
                      if safe_data(f) and (d / f).is_file())

    def list_data(self, name: str) -> dict:
        d = self.data_dir(name)
        files = []
        for f in self._data_names(name):
            st = (d / f).stat()
            files.append({"name": f, "size": st.st_size, "mtime": st.st_mtime})
        return {"files": files}

    def data_path(self, name: str, file: str) -> Path | None:
        d = self.data_dir(name)
        f = safe_data(file)
        return (d / f) if (d and f) else None

    def read_data(self, name: str, file: str) -> bytes | None:
        p = self.data_path(name, file)
        if not p or not p.is_file():
            return None
        return p.read_bytes()

    def write_data(self, name: str, file: str, data: bytes) -> str:
        p = self.data_path(name, file)
        if not p:
            return "err.addon_bad_name"
        if len(data) > MAX_DATA_FILE:
            return "err.addon_too_big"
        d = p.parent
        d.mkdir(parents=True, exist_ok=True)
        used = sum((d / f).stat().st_size for f in self._data_names(name)
                   if f != p.name)
        if used + len(data) > MAX_DATA_TOTAL:
            return "err.addon_store_full"
        # written aside and renamed, so a half-received file never appears in
        # the listing under its real name
        tmp = d / ("." + p.name + ".part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)
            return "err.addon_failed"
        return ""

    def delete_data(self, name: str, file: str) -> str:
        p = self.data_path(name, file)
        if not p:
            return "err.addon_bad_name"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            return "err.addon_failed"
        return ""

    # ---- install / remove ------------------------------------------------
    def install(self, archive: Path) -> tuple[str, str]:
        """Unpack an add-on archive beside the running one, then swap it in.

        Returns (name, ""), or ("", key). Nothing is disturbed until the new
        tree is complete, and an upgrade keeps the old store: the definitions an
        add-on holds are the rider's, not the release's.
        """
        archive = Path(archive)
        try:
            members = self._read_members(archive)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
            return "", "err.addon_bad_archive"
        if isinstance(members, str):
            return "", members

        manifest = members.get("addon.json")
        if manifest is None:
            return "", "err.addon_no_manifest"
        try:
            meta = json.loads(manifest.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "", "err.addon_no_manifest"
        name = safe_addon((meta or {}).get("name", "")) if isinstance(meta, dict) else None
        if not name:
            return "", "err.addon_bad_name"
        if "index.html" not in members:
            return "", "err.addon_incomplete"

        stage = self.root / (name + ".new")
        old = self.root / (name + ".old")
        live = self.root / name
        try:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(old, ignore_errors=True)
            (stage / "web").mkdir(parents=True, exist_ok=True)
            (stage / "data").mkdir(parents=True, exist_ok=True)
            (stage / "addon.json").write_bytes(manifest)
            for rel, blob in members.items():
                if rel == "addon.json":
                    continue
                target = stage / "web" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)

            if live.exists():
                keep = live / "data"
                if keep.is_dir():
                    shutil.rmtree(stage / "data", ignore_errors=True)
                    os.replace(keep, stage / "data")
                os.replace(live, old)
            os.replace(stage, live)
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            if old.exists() and not live.exists():
                os.replace(old, live)
            return "", "err.addon_failed"
        shutil.rmtree(old, ignore_errors=True)
        return name, ""

    def _read_members(self, archive: Path):
        """{relative path: bytes} for a validated archive, or an error key."""
        if archive.stat().st_size > MAX_ARCHIVE:
            return "err.addon_too_big"
        out: dict[str, bytes] = {}
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                infos = [i for i in z.infolist() if not i.is_dir()]
                if len(infos) > MAX_MEMBERS:
                    return "err.addon_bad_archive"
                if sum(i.file_size for i in infos) > MAX_UNPACKED:
                    return "err.addon_too_big"
                top = _strip_top([i.filename for i in z.infolist()])
                for i in infos:
                    rel = _rel(i.filename, top)
                    if rel is None:
                        return "err.addon_unsafe_member"
                    out[rel] = z.read(i)
            return out
        with tarfile.open(archive, "r:*") as t:
            members = t.getmembers()
            if len(members) > MAX_MEMBERS:
                return "err.addon_bad_archive"
            files = [m for m in members if m.isreg()]
            if sum(m.size for m in files) > MAX_UNPACKED:
                return "err.addon_too_big"
            top = _strip_top([m.name for m in members])
            for m in members:
                # symlinks, devices and fifos have no business in a page and
                # every business in an exploit
                if not (m.isreg() or m.isdir()):
                    return "err.addon_unsafe_member"
                # `tar -C dir -czf out .` names the root itself "./", which
                # resolves to no path at all; that is the archive, not a member
                if m.isdir() and _is_root_member(m.name, top):
                    continue
                if _rel(m.name, top) is None:
                    return "err.addon_unsafe_member"
            for m in files:
                fh = t.extractfile(m)
                if fh is not None:
                    out[_rel(m.name, top)] = fh.read()
        return out

    def remove(self, name: str) -> str:
        d = self.dir_of(name)
        if not d:
            return "err.addon_unknown"
        try:
            shutil.rmtree(d)
        except OSError:
            return "err.addon_failed"
        return ""
