# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/governance/segmentation.py (SPEC §6.1).

The fixtures here are SPEC §7.2 Layer 4 in miniature — hand-authored
adversarial resources Synthea cannot produce. The load-bearing pair is
the "two variants" fixture: every sensitive category is tested with
`meta.security` POPULATED and with it STRIPPED, because the stripped
variant is the production condition (ONC rates the sensitivity tag
Level 0/1; US Core doesn't profile meta.security at all). A design
that only passes the labeled variant would fail silently against fully
conformant servers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.governance.segmentation import (  # noqa: E402
    CategoryValueSets,
    SegmentationStats,
    SensitiveCategory,
    classify,
    evaluate_geo_gate,
)

SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
ACTCODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CONF = "http://terminology.hl7.org/CodeSystem/v3-Confidentiality"

#: Miniature curated value sets — structure-real, content-synthetic.
#: Production sets come from the §7.4 terminology loader.
VALUE_SETS = CategoryValueSets(
    codes={
        SensitiveCategory.HIV: frozenset({(ICD10, "B20"), (SNOMED, "86406008")}),
        SensitiveCategory.MENTAL_HEALTH: frozenset({(ICD10, "F32.9")}),
        SensitiveCategory.REPRODUCTIVE_HEALTH: frozenset({(ICD10, "Z33.1")}),
        SensitiveCategory.PART2_SUD: frozenset({(ICD10, "F11.20")}),
    },
    departments={"dept-part2-program": SensitiveCategory.PART2_SUD},
)


def _condition(code_system: str, code: str, security: list | None = None) -> dict:
    resource = {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": code_system, "code": code, "display": "x"}]},
    }
    if security is not None:
        resource["meta"] = {"security": security}
    return resource


def test_sensitive_code_excluded_with_meta_security_stripped():
    # The production condition: no label at all, value set does the work.
    for system, code, category in [
        (ICD10, "B20", SensitiveCategory.HIV),
        (ICD10, "F32.9", SensitiveCategory.MENTAL_HEALTH),
        (ICD10, "Z33.1", SensitiveCategory.REPRODUCTIVE_HEALTH),
        (ICD10, "F11.20", SensitiveCategory.PART2_SUD),
    ]:
        decision = classify(_condition(system, code), VALUE_SETS)
        assert not decision.include, code
        assert category in decision.categories


def test_sensitive_label_honored_when_present_even_without_code_match():
    labeled = _condition(
        SNOMED, "38341003",  # hypertension - not in any sensitive set
        security=[{"system": ACTCODE, "code": "HIV"}],
    )
    decision = classify(labeled, VALUE_SETS)
    assert not decision.include
    assert SensitiveCategory.HIV in decision.categories


def test_absence_of_label_is_never_read_as_absence_of_sensitivity():
    # Same resource, labeled and stripped: both excluded.
    labeled = _condition(ICD10, "B20", security=[{"system": ACTCODE, "code": "HIV"}])
    stripped = _condition(ICD10, "B20")
    assert not classify(labeled, VALUE_SETS).include
    assert not classify(stripped, VALUE_SETS).include


def test_clean_resource_is_included():
    decision = classify(_condition(SNOMED, "38341003"), VALUE_SETS)
    assert decision.include


def test_unknown_resource_type_excluded_fail_closed_and_counted():
    stats = SegmentationStats()
    decision = classify({"resourceType": "MysteryType"}, VALUE_SETS)
    assert not decision.include
    assert decision.unclassifiable
    stats.observe(decision)
    stats.observe(classify(_condition(SNOMED, "38341003"), VALUE_SETS))
    stats.observe(classify(_condition(ICD10, "B20"), VALUE_SETS))
    assert stats.excluded_unclassifiable == 1
    assert stats.included == 1
    assert stats.excluded_by_category == {"hiv": 1}


def test_unmapped_sensitivity_label_excludes_fail_closed():
    decision = classify(
        _condition(SNOMED, "38341003", security=[{"system": ACTCODE, "code": "SDV"}]),
        VALUE_SETS,
    )
    assert not decision.include
    assert decision.unclassifiable


def test_restricted_confidentiality_code_excludes():
    decision = classify(
        _condition(SNOMED, "38341003", security=[{"system": CONF, "code": "R"}]),
        VALUE_SETS,
    )
    assert not decision.include


def test_part2_department_signal_excludes_without_any_code_match():
    decision = classify(
        _condition(SNOMED, "38341003"),
        VALUE_SETS,
        source_department="dept-part2-program",
    )
    assert not decision.include
    assert SensitiveCategory.PART2_SUD in decision.categories


def test_codes_are_found_wherever_they_sit():
    # A sensitive code in an unusual field (e.g. a report's conclusion
    # coding) is still caught - the walk is recursive on purpose.
    report = {
        "resourceType": "DiagnosticReport",
        "conclusionCode": [{"coding": [{"system": ICD10, "code": "B20"}]}],
    }
    assert not classify(report, VALUE_SETS).include


# ------------------------------------------------------------ AB 352 geo-gate


def test_geo_gate_refuses_out_of_state_and_unresolved_requesters():
    categories = (SensitiveCategory.REPRODUCTIVE_HEALTH,)
    events = []
    audit = AuditLog(sink=events.append)

    out_of_state = evaluate_geo_gate(categories, "TX", audit=audit)
    assert not out_of_state.allowed

    unresolved = evaluate_geo_gate(categories, None, audit=audit)
    assert not unresolved.allowed
    assert "fails closed" in unresolved.reason

    assert [e["action"] for e in events] == [
        "segmentation.geo_gate.refused",
        "segmentation.geo_gate.refused",
    ]
    assert AuditLog.verify_chain(events)


def test_geo_gate_passes_in_state_and_non_ab352_content():
    categories = (SensitiveCategory.REPRODUCTIVE_HEALTH,)
    assert evaluate_geo_gate(categories, "CA").allowed
    assert evaluate_geo_gate((SensitiveCategory.HIV,), "TX").allowed
    assert evaluate_geo_gate(categories, "TX", record_state="NY").allowed
# Made by Ryan Gomez & Co. Inc.
