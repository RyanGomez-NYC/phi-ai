# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/terminology/loader.py (SPEC §7.4): licence classes
enforced, missing credentials fail loud or disable the dependent
expansion WITH a named reason, and the example value-set file actually
loads into segmentation's shape.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.governance.segmentation import SensitiveCategory, classify  # noqa: E402
from core.terminology.loader import (  # noqa: E402
    TerminologyError,
    load,
    load_value_sets,
)

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config" / "sensitive_value_sets.example.yaml"


def test_example_value_sets_load_and_drive_the_engine():
    value_sets = load_value_sets(EXAMPLE)
    assert (
        "http://hl7.org/fhir/sid/icd-10-cm",
        "B20",
    ) in value_sets.codes[SensitiveCategory.HIV]
    assert value_sets.departments["dept-part2-program"] is SensitiveCategory.PART2_SUD

    decision = classify(
        {
            "resourceType": "Condition",
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "F32.9"}]},
        },
        value_sets,
    )
    assert not decision.include
    assert SensitiveCategory.MENTAL_HEALTH in decision.categories


def test_missing_value_set_file_fails_loud():
    with pytest.raises(TerminologyError, match="not found"):
        load_value_sets(REPO / "config" / "does_not_exist.yaml")


def test_unknown_category_is_refused_not_dropped(tmp_path):
    bad = tmp_path / "vs.yaml"
    bad.write_text("categories:\n  super_secret:\n    - {system: s, code: c}\n")
    with pytest.raises(TerminologyError, match="Unknown sensitive category"):
        load_value_sets(bad)


def test_malformed_entry_is_refused(tmp_path):
    bad = tmp_path / "vs.yaml"
    bad.write_text("categories:\n  hiv:\n    - {system: s}\n")
    with pytest.raises(TerminologyError, match="both 'system' and 'code'"):
        load_value_sets(bad)


def test_unlicensed_deployment_disables_expansions_with_named_reasons():
    loaded = load(value_sets_path=EXAMPLE)
    assert "loinc" in loaded.available  # committed class always present
    assert "snomed" not in loaded.available
    assert "cpt" not in loaded.available  # disabled by default

    assert loaded.expansions_enabled["snomed_subsumption"] is False
    assert "licence" in loaded.disabled_reasons["snomed_subsumption"]
    assert loaded.expansions_enabled["loinc_value_sets"] is True

    report = loaded.expansion_report()
    assert "[DISABLED] rxnorm_ingredient_brand" in report
    assert "[ENABLED ] loinc_value_sets" in report


def test_credentials_enable_the_dependent_expansions():
    loaded = load(value_sets_path=EXAMPLE, umls_api_key="uts-key")
    assert loaded.expansions_enabled["snomed_subsumption"] is True
    assert loaded.expansions_enabled["rxnorm_ingredient_brand"] is True

    with_cpt = load(value_sets_path=EXAMPLE, cpt_license_id="AMA-12345")
    assert "cpt" in with_cpt.available
    hollow_cpt = load(value_sets_path=EXAMPLE, cpt_license_id="   ")
    assert "cpt" not in hollow_cpt.available
# Made by Ryan Gomez & Co. Inc.
