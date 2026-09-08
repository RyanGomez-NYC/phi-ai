# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
VENDORED.json: every vendored front-end file with the version its own
banner records and its sha256, generated from the tree, never typed.

    scripts/components.py vendored        # rewrite VENDORED.json from the tree

Scanned: every directory named assets or static within three levels of
the root that holds front-end files - found, never listed, so a tree with
more stacks scans more and a tree with fewer scans fewer. Recorded: files whose
first line carries a version banner (d3: "v7.9.0"), files carrying a
version string (ECharts: version:"5.6.0"), the vendored typefaces
(*.woff2) with their README, which records the upstream but no version -
so the version field says "unrecorded", never a number from memory.

The Vendored front-end row verifies the tree against this record: each
file present hashes to what was recorded, or the row reads drifted.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

FILE = "VENDORED.json"
ASSET_DIR_NAMES = ("assets", "static")
ASSET_SUFFIXES = (".js", ".css", ".woff2", ".woff", ".ttf", ".otf")
_SKIP_DIRS = {"node_modules", "__pycache__", "restore-output", "releases"}


def asset_dirs(root: Path, depth: int = 3) -> list[str]:
    """The asset directories of this tree: named assets or static, at most
    `depth` levels down, holding at least one front-end file. Hidden and
    dependency directories are not descended into."""
    import os
    root = Path(root)
    found: list[str] = []
    for cur, dirs, files in os.walk(root):
        rel = Path(cur).relative_to(root)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        if len(rel.parts) >= depth:
            dirs[:] = []
        if rel.parts and rel.name in ASSET_DIR_NAMES:
            has_assets = any(Path(c).joinpath(f).suffix in ASSET_SUFFIXES for c, _, fs in os.walk(cur) for f in fs)
            if has_assets:
                found.append(rel.as_posix())
            dirs[:] = []   # an asset directory is a leaf of this search
    return sorted(found)
UNRECORDED = "unrecorded"

_BANNER = re.compile(r"\bv(\d+\.\d+\.\d+)\b")
_VERSION_STRING = re.compile(r'version\s*[:=]\s*"(\d+\.\d+\.\d+)"')
_FIRST_PARTY = "Ryan Gomez & Co."

FONT_TOKENS = {"latin", "ext", "italic", "var", "300", "400", "500", "600", "700"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def font_family(filename: str) -> str:
    """'inter-500-latin-ext.woff2' -> 'Inter'."""
    stem = filename.rsplit(".", 1)[0]
    words = [w for w in stem.split("-") if w not in FONT_TOKENS]
    return " ".join(w.capitalize() for w in words)


def _head(path: Path, n: int = 4096) -> str:
    try:
        with path.open("rb") as f:
            return f.read(n).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _js_version(path: Path) -> Optional[tuple[str, str]]:
    """(version, how) for a vendored script, from its own text."""
    head = _head(path)
    first = head.splitlines()[0] if head else ""
    m = _BANNER.search(first)
    if m:
        return m.group(1), "first-line banner"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _VERSION_STRING.search(text)
    if m:
        return m.group(1), 'first version:"x.y.z" in the file'
    return None


def scan(root: Path) -> list[dict]:
    """Every vendored file the rules above recognise, sorted by path."""
    root = Path(root)
    rows: list[dict] = []
    for rel_dir in asset_dirs(root):
        d = root / rel_dir
        if not d.is_dir():
            continue
        for path in sorted(p for p in d.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            if path.suffix == ".js":
                if _FIRST_PARTY in _head(path):
                    continue  # our own script, not vendored
                found = _js_version(path)
                if not found:
                    continue  # a third-party file with no version of its own is not recorded as one
                version, how = found
                rows.append({"path": rel, "kind": "script", "version": version,
                             "version_source": f"{rel} ({how})",
                             "sha256": sha256_file(path), "bytes": path.stat().st_size})
            elif path.suffix == ".woff2":
                readme = path.parent / "README.md"
                rows.append({"path": rel, "kind": "font", "family": font_family(path.name),
                             "version": UNRECORDED,
                             "version_source": (readme.relative_to(root).as_posix() + " records the upstream, no version")
                             if readme.is_file() else "no README beside the file",
                             "sha256": sha256_file(path), "bytes": path.stat().st_size})
            elif path.name == "README.md" and path.parent.name == "fonts":
                text = path.read_text(encoding="utf-8", errors="replace")
                urls = sorted(set(re.findall(r"https?://\S+", text)))
                lic = re.search(r"\*\*([^*]+)\*\*", text)
                rows.append({"path": rel, "kind": "fonts README", "version": UNRECORDED,
                             "version_source": f"{rel} records the upstream, no version",
                             "upstream": urls, "license": lic.group(1) if lic else UNRECORDED,
                             "sha256": sha256_file(path), "bytes": path.stat().st_size})
    rows.sort(key=lambda r: r["path"])
    return rows


def write(root: Path, out: Optional[Path] = None) -> Path:
    data = {"generated_by": "scripts/components.py vendored", "files": scan(root)}
    path = Path(out) if out else Path(root) / FILE
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(root: Path) -> Optional[dict]:
    try:
        data = json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None


def verify(root: Path, recorded: dict) -> list[dict]:
    """Each recorded file against the tree: 'as recorded', 'differs', or
    'not in this tree' (the image ships core/ only; a demo asset absent
    from it is not drift)."""
    root = Path(root)
    out = []
    for row in recorded.get("files", []):
        path = root / row["path"]
        if not path.is_file():
            state = "not in this tree"
        elif sha256_file(path) == row.get("sha256"):
            state = "as recorded"
        else:
            state = "differs"
        out.append({**row, "state": state})
    return out
# Made by Ryan Gomez & Co. Inc.
