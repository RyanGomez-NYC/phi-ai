# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The build stamp.

RELEASE (one line at the repository root) is the one source of the release
string: core/__init__.py reads it, the CHANGELOG's top heading and the git
tag are tested against it, the colophon prints it.

BUILD.json is written by the workstation build (scripts/components.py
build) and shipped inside the image:

    {release, commit, branch, built_at, tree_sha, image_digest?}

At runtime with no BUILD.json, the stamp is derived from git when the
checkout has a .git; otherwise every field is "unknown", and the screen
says so rather than guessing what is running.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STAMP_FILE = "BUILD.json"
RELEASE_FILE = "RELEASE"
UNKNOWN = "unknown"


def read_release(root: Path) -> Optional[str]:
    """The RELEASE file's first non-empty line, or None when there is none."""
    try:
        text = (Path(root) / RELEASE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0] if lines else None


def git(root: Path, *args: str) -> Optional[str]:
    """`git -C root ARGS`, stripped; None when git is missing, the
    directory is not a checkout, or the command fails. Never raises: a
    reader that cannot answer says unknown."""
    try:
        run = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                             text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    return run.stdout.strip()


def has_checkout(root: Path) -> bool:
    return (Path(root) / ".git").exists()


def git_facts(root: Path) -> dict:
    """commit, branch and tree_sha from git, each "unknown" when the
    checkout cannot say. `source` names where the values came from."""
    root = Path(root)
    if not has_checkout(root):
        return {"commit": UNKNOWN, "branch": UNKNOWN, "tree_sha": UNKNOWN,
                "source": f"no .git under {root.name or root}"}
    commit = git(root, "rev-parse", "HEAD") or UNKNOWN
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD") or UNKNOWN
    tree = git(root, "rev-parse", "HEAD^{tree}") or UNKNOWN
    return {"commit": commit, "branch": branch, "tree_sha": tree,
            "source": "git rev-parse HEAD / --abbrev-ref HEAD / HEAD^{tree}"}


def make_stamp(root: Path, *, image_digest: Optional[str] = None,
               built_at: Optional[datetime] = None) -> dict:
    facts = git_facts(root)
    stamp = {
        "release": read_release(root) or UNKNOWN,
        "commit": facts["commit"],
        "branch": facts["branch"],
        "built_at": (built_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "tree_sha": facts["tree_sha"],
    }
    if image_digest:
        stamp["image_digest"] = image_digest
    return stamp


def write_stamp(root: Path, out: Optional[Path] = None, *, image_digest: Optional[str] = None,
                built_at: Optional[datetime] = None) -> dict:
    """Write BUILD.json (to `out`, default <root>/BUILD.json) and return it."""
    stamp = make_stamp(root, image_digest=image_digest, built_at=built_at)
    path = Path(out) if out else Path(root) / STAMP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return stamp


def read_stamp(root: Path) -> Optional[dict]:
    """BUILD.json under root, parsed; None when absent or unreadable."""
    path = Path(root) / STAMP_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def runtime_stamp(root: Path) -> tuple[dict, str]:
    """What this checkout or image can say about itself: BUILD.json when
    it ships one, git facts when it is a checkout, unknowns otherwise.
    Returns (stamp, source)."""
    stamp = read_stamp(root)
    if stamp is not None:
        return stamp, STAMP_FILE
    facts = git_facts(root)
    derived = {"release": read_release(root) or UNKNOWN, "commit": facts["commit"],
               "branch": facts["branch"], "built_at": UNKNOWN, "tree_sha": facts["tree_sha"]}
    if has_checkout(root):
        return derived, "git (no BUILD.json)"
    return derived, "none (no BUILD.json, no .git)"


def short(value: Optional[str], n: int = 12) -> str:
    """A hash cut for display, never by git's own abbreviation."""
    if not value or value == UNKNOWN:
        return UNKNOWN
    return value[:n]
# Made by Ryan Gomez & Co. Inc.
