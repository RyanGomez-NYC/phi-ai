# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/config/retention_rules.py.

Uses real temporary YAML files on disk (via tempfile), not mocked file
I/O - load_ruleset() reads from a real path, and the thing worth
verifying is that it correctly parses real YAML and produces the exact,
specific error messages a Health Information Manager (not a developer)
would need to fix a malformed file, which is easiest to get right by
actually exercising the real PyYAML parsing path rather than a stand-in
for it.
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config.retention_rules import (  # noqa: E402
    RetentionRulesetError,
    load_ruleset,
)


def _write_yaml(content: str) -> str:
    """Writes content to a new temporary file and returns its path. Not
    cleaned up automatically - these are short-lived test-process temp
    files, and explicit cleanup would add noise without meaningfully
    changing what's being tested."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


VALID_WITH_OVERRIDE = """\
jurisdiction: "FL"

default_rule:
  retention_years: 5
  citation: "Fla. Admin. Code r. 64B8-10.002"
  source_note: "Florida Board of Medicine physician record retention rule"
  reviewed_by: "Jane Smith, RHIA"
  reviewed_on: "2026-06-01"
  regime: "physician"

resource_type_rules:
  - resource_type: "Immunization"
    retention_years: 30
    citation: "Fla. Stat. 381.003"
    source_note: "Immunization records commonly retained longer"
    reviewed_by: "Jane Smith, RHIA"
    reviewed_on: "2026-06-01"
"""

VALID_MINIMAL = """\
jurisdiction: "TX"

default_rule:
  retention_years: 7
  citation: "22 Tex. Admin. Code 165.1(b)"
  reviewed_by: "Maria Garcia, RHIT"
  reviewed_on: "2024-01-15"
"""


# ---------------------------------------------------------------------------
# Valid parsing
# ---------------------------------------------------------------------------

def test_valid_ruleset_with_override_parses_correctly():
    path = _write_yaml(VALID_WITH_OVERRIDE)
    rs = load_ruleset(path)

    assert rs.jurisdiction == "FL"
    assert rs.default_rule.retention_years == 5
    assert rs.default_rule.citation == "Fla. Admin. Code r. 64B8-10.002"
    assert rs.default_rule.regime == "physician"
    assert "Immunization" in rs.resource_type_rules
    assert rs.resource_type_rules["Immunization"].retention_years == 30


def test_to_retention_config_produces_the_expected_shape():
    path = _write_yaml(VALID_WITH_OVERRIDE)
    rs = load_ruleset(path)

    years, overrides = rs.to_retention_config()
    assert years == 5
    assert overrides == {"Immunization": 30}


def test_minimal_ruleset_with_no_overrides_or_optional_fields():
    path = _write_yaml(VALID_MINIMAL)
    rs = load_ruleset(path)

    assert rs.jurisdiction == "TX"
    assert rs.resource_type_rules == {}
    assert rs.default_rule.regime is None
    assert rs.default_rule.source_note == ""

    years, overrides = rs.to_retention_config()
    assert overrides == {}


# ---------------------------------------------------------------------------
# Validation errors - each must name the specific problem, since the
# person fixing the file is expected to be a Health Information Manager,
# not a developer.
# ---------------------------------------------------------------------------

def test_missing_citation_raises_specific_error():
    path = _write_yaml("""\
jurisdiction: "CA"
default_rule:
  retention_years: 7
  reviewed_by: "John Doe"
  reviewed_on: "2026-01-01"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "citation" in str(exc).lower()
    assert raised


def test_duplicate_resource_type_raises_specific_error():
    path = _write_yaml("""\
jurisdiction: "NY"
default_rule:
  retention_years: 6
  citation: "10 NYCRR 405.10"
  reviewed_by: "Someone"
  reviewed_on: "2026-01-01"
resource_type_rules:
  - resource_type: "Immunization"
    retention_years: 20
    citation: "cite 1"
    reviewed_by: "Someone"
    reviewed_on: "2026-01-01"
  - resource_type: "Immunization"
    retention_years: 25
    citation: "cite 2"
    reviewed_by: "Someone Else"
    reviewed_on: "2026-01-01"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "Immunization" in str(exc) and "more than one" in str(exc)
    assert raised


def test_bad_date_format_raises_specific_error():
    path = _write_yaml("""\
jurisdiction: "OH"
default_rule:
  retention_years: 6
  citation: "some citation"
  reviewed_by: "Someone"
  reviewed_on: "not-a-date"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "YYYY-MM-DD" in str(exc)
    assert raised


def test_negative_retention_years_raises_specific_error():
    path = _write_yaml("""\
jurisdiction: "OH"
default_rule:
  retention_years: -3
  citation: "some citation"
  reviewed_by: "Someone"
  reviewed_on: "2026-01-01"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "at least 1" in str(exc)
    assert raised


def test_malformed_yaml_raises_clear_error_not_a_raw_traceback():
    path = _write_yaml("""\
jurisdiction: "OH"
default_rule:
  retention_years: 6
    citation: "bad indentation here"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "not valid YAML" in str(exc)
    assert raised


def test_non_mapping_top_level_raises_specific_error():
    path = _write_yaml("- just\n- a\n- list\n")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError as exc:
        raised = True
        assert "mapping" in str(exc).lower()
    assert raised


def test_missing_file_raises_specific_error():
    raised = False
    try:
        load_ruleset("/tmp/definitely-does-not-exist-phi-ai-test.yaml")
    except RetentionRulesetError as exc:
        raised = True
        assert "not found" in str(exc).lower()
    assert raised


def test_unedited_template_is_rejected():
    """The actual config/retention_ruleset.example.yaml ships with every
    value an unmistakable placeholder (retention_years: 0, etc.) - this
    must fail validation, not be silently accepted, or someone copying
    the template without editing it would get a working-looking but
    meaningless 0-year retention configuration."""
    path = _write_yaml("""\
jurisdiction: "XX"
default_rule:
  retention_years: 0
  citation: "REPLACE with the specific statute/regulation citation"
  reviewed_by: "REPLACE with the reviewer's name and credential"
  reviewed_on: "2000-01-01"
""")
    raised = False
    try:
        load_ruleset(path)
    except RetentionRulesetError:
        raised = True
    assert raised, "An unedited template (retention_years: 0) must be rejected, not silently accepted"


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

def test_recently_reviewed_rule_is_not_stale():
    path = _write_yaml(VALID_WITH_OVERRIDE)  # reviewed_on: 2026-06-01
    rs = load_ruleset(path)

    stale = rs.stale_rules(as_of=date(2026, 8, 16), warn_after_years=2)
    assert stale == []


def test_same_rule_becomes_stale_after_the_threshold():
    path = _write_yaml(VALID_WITH_OVERRIDE)
    rs = load_ruleset(path)

    stale = rs.stale_rules(as_of=date(2029, 8, 16), warn_after_years=2)
    labels = {label for label, _ in stale}
    assert labels == {"default", "Immunization"}


def test_stale_threshold_is_configurable():
    path = _write_yaml("""\
jurisdiction: "WA"
default_rule:
  retention_years: 10
  citation: "old citation"
  reviewed_by: "Someone Long Ago"
  reviewed_on: "2020-01-01"
""")
    rs = load_ruleset(path)

    assert len(rs.stale_rules(as_of=date(2026, 8, 16), warn_after_years=2)) == 1
    assert rs.stale_rules(as_of=date(2026, 8, 16), warn_after_years=10) == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
