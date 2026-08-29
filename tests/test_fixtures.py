# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The §7.1 fixture gates as a test target (R5: they run both here and in
scripts/pre_push_gates.sh via scripts/check_fixtures.py — same
functions, two enforcement points, no GitHub Actions).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_fixtures import (  # noqa: E402
    FIXTURES_ROOT,
    check_all,
    check_fixture_dir,
    check_marker,
)


def test_every_committed_fixture_passes_the_r2_and_r4_gates():
    problems = check_all()
    assert problems == [], "\n".join(problems)


def test_marker_check_rejects_unmarked_and_mismarked_resources():
    assert not check_marker({"resourceType": "Condition"})
    assert not check_marker(
        {"meta": {"tag": [{"system": "http://example.org", "code": "HTEST"}]}}
    )
    assert not check_marker(
        {
            "meta": {
                "tag": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
                        "code": "TREAT",
                    }
                ]
            }
        }
    )
    assert check_marker(
        {
            "meta": {
                "tag": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
                        "code": "HTEST",
                    }
                ]
            }
        }
    )


def test_unlisted_fixture_file_fails_the_manifest_gate(tmp_path):
    # A minimal fixture-set directory with one marked fixture that the
    # manifest doesn't mention: R4 catches it.
    (tmp_path / "MANIFEST.json").write_text(
        '{"fixture_set": "t", "generator": "hand-authored", '
        '"generator_version": "n/a", "seed": "n/a", "command_line": "n/a", '
        '"calibration_sources": "none", "fixtures": {}}'
    )
    (tmp_path / "stray.json").write_text(
        '{"resourceType": "Condition", "meta": {"tag": [{"system": '
        '"http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": "HTEST"}]}}'
    )
    problems = check_fixture_dir(tmp_path)
    assert any("not listed in MANIFEST.json" in p for p in problems)


def test_missing_marker_is_reported_by_file(tmp_path):
    (tmp_path / "MANIFEST.json").write_text(
        '{"fixture_set": "t", "generator": "hand-authored", '
        '"generator_version": "n/a", "seed": "n/a", "command_line": "n/a", '
        '"calibration_sources": "none", "fixtures": {"bare.json": '
        '{"exercises": "x", "spec_ref": "y"}}}'
    )
    (tmp_path / "bare.json").write_text('{"resourceType": "Condition"}')
    problems = check_fixture_dir(tmp_path)
    assert any("HTEST" in p and "bare.json" in p for p in problems)


def test_fixtures_root_is_where_the_layer4_set_lives():
    assert (FIXTURES_ROOT / "layer4" / "MANIFEST.json").exists()
# Made by Ryan Gomez & Co. Inc.
