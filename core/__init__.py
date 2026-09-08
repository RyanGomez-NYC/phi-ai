# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""PHI AI Platform core package."""

from pathlib import Path as _Path

# ONE release source. The RELEASE file at the repository root names the
# release; the CHANGELOG's top heading, the git tag, the build stamp and
# the colophon at the foot of every platform page are all tested against
# it (tests/test_components.py), so the release string cannot be right in
# one place and stale in another. The literal on the next line is the
# fallback for an install that carries no RELEASE file, and what the
# demo's generator reads; the same test holds it equal to the file.
__version__ = "1.1.0"


def _release(fallback: str) -> str:
    try:
        text = (_Path(__file__).resolve().parent.parent / "RELEASE").read_text(encoding="utf-8")
    except OSError:
        return fallback
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first or fallback


__version__ = _release(__version__)
# Made by Ryan Gomez & Co. Inc.
