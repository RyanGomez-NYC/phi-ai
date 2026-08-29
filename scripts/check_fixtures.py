# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Synthetic-fixture gates (docs/SPEC.md §7.1 R2 and R4), runnable two
ways per R5 — as a test target (tests/test_fixtures.py calls the
functions) and from the pre-push script (scripts/pre_push_gates.sh
runs this module):

    python scripts/check_fixtures.py

R2: every fixture resource carries the explicit synthetic marker —
`meta.tag` HTEST from http://terminology.hl7.org/CodeSystem/v3-ActReason.
The marker is what lets any later audit distinguish "this repository
contains fabricated records" from "this repository contains records" in
one grep, so a missing marker fails the gate even on an obviously fake
resource.

R4: every fixture set carries a provenance MANIFEST.json — generator,
version, seed, command line, calibration sources — and every fixture
file is listed in it with the invariant or acceptance criterion it
exercises. A fixture nobody can say the purpose of is a fixture nobody
dares delete or trust.

No GitHub Actions runs this (R5 — project constraint); the pre-push
hook and the test target are the enforcement points.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

HTEST_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActReason"
HTEST_CODE = "HTEST"

MANIFEST_REQUIRED_FIELDS = (
    "fixture_set",
    "generator",
    "generator_version",
    "seed",
    "command_line",
    "calibration_sources",
    "fixtures",
)


def check_marker(resource: dict) -> bool:
    """R2: the HTEST tag, exactly — system and code both."""
    for tag in (resource.get("meta") or {}).get("tag", []):
        if tag.get("system") == HTEST_SYSTEM and tag.get("code") == HTEST_CODE:
            return True
    return False


def check_fixture_dir(directory: Path) -> list[str]:
    """Returns every violation in one fixture-set directory, empty when
    clean. Collects everything rather than stopping at the first — the
    author fixing fixtures wants the whole list once."""
    problems: list[str] = []

    manifest_path = directory / "MANIFEST.json"
    if not manifest_path.exists():
        return [f"{directory}: no MANIFEST.json (R4: provenance manifest required)"]

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{manifest_path}: unparseable JSON ({exc})"]

    for field in MANIFEST_REQUIRED_FIELDS:
        if field not in manifest:
            problems.append(f"{manifest_path}: missing required field {field!r} (R4)")

    listed = set((manifest.get("fixtures") or {}).keys())
    present = {p.name for p in directory.glob("*.json")} - {"MANIFEST.json"}

    for name in sorted(present - listed):
        problems.append(
            f"{directory / name}: fixture file not listed in MANIFEST.json (R4)"
        )
    for name in sorted(listed - present):
        problems.append(
            f"{manifest_path}: lists {name!r} but the file does not exist"
        )

    for name, entry in (manifest.get("fixtures") or {}).items():
        for required in ("exercises", "spec_ref"):
            if not (entry or {}).get(required, "").strip():
                problems.append(
                    f"{manifest_path}: fixture {name!r} missing {required!r} "
                    "(R4: every fixture names what it exercises)"
                )

    for name in sorted(present):
        path = directory / name
        try:
            resource = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: unparseable JSON ({exc})")
            continue
        if not check_marker(resource):
            problems.append(
                f"{path}: missing meta.tag HTEST synthetic marker (R2 — "
                "no fixture ships without one, fake-looking or not)"
            )

    return problems


def check_all(root: Path = FIXTURES_ROOT) -> list[str]:
    if not root.exists():
        return [f"{root}: fixtures directory does not exist"]
    directories = [d for d in sorted(root.iterdir()) if d.is_dir()]
    if not directories:
        return [f"{root}: contains no fixture-set directories"]
    problems: list[str] = []
    for directory in directories:
        problems.extend(check_fixture_dir(directory))
    return problems


def main() -> int:
    problems = check_all()
    if problems:
        for problem in problems:
            print(f"FIXTURE GATE: {problem}", file=sys.stderr)
        return 1
    print("fixture gates passed (R2 markers, R4 manifests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
