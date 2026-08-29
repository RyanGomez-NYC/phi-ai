# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/rag/serialization.py — SPEC §5.1(a)(b)(f) and the §7.3
absence rules, driven by the Layer-4 fixtures in tests/fixtures/layer4
so the fixtures are load-bearing, not decorative.

This is the "dedicated test suite required" that 5.1(f) demands: a
refuted penicillin allergy serializing as "penicillin allergy" is a
data-integrity failure that looks exactly like a good retrieval, so
every status-bearing fixture is asserted against the rendered TEXT,
not just the structured fields — the text is what the embedding sees.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.governance.segmentation import CategoryValueSets, SensitiveCategory  # noqa: E402
from core.rag.serialization import TEMPLATE_VERSION, serialize_resource  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"

VALUE_SETS = CategoryValueSets(
    codes={
        SensitiveCategory.HIV: frozenset({(ICD10, "B20")}),
        SensitiveCategory.PART2_SUD: frozenset({(ICD10, "F11.20")}),
        SensitiveCategory.REPRODUCTIVE_HEALTH: frozenset({(ICD10, "Z33.1")}),
    },
    departments={"dept-part2-program": SensitiveCategory.PART2_SUD},
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _chunk(name: str, **kwargs):
    result = serialize_resource(_load(name), f"fixtures/{name}", VALUE_SETS, **kwargs)
    assert result.chunk is not None, f"{name} unexpectedly excluded: {result.excluded}"
    return result.chunk


# ------------------------------------------------- status inversion (5.1f)


def test_refuted_allergy_text_leads_with_the_negation():
    chunk = _chunk("allergy_penicillin_refuted.json")
    assert chunk.text.startswith("[REFUTED")
    assert "NOT true" in chunk.text
    assert chunk.verification_status == "refuted"
    # The allergen still appears - the record says "refuted penicillin
    # allergy", not nothing - but never before the negation.
    assert chunk.text.index("REFUTED") < chunk.text.index("Penicillin")


def test_entered_in_error_condition_is_marked_in_text():
    chunk = _chunk("condition_mi_entered_in_error.json")
    assert chunk.text.startswith("[ENTERED-IN-ERROR")
    assert "myocardial infarction" in chunk.text.lower()


def test_resolved_condition_text_carries_its_status():
    chunk = _chunk("condition_asthma_resolved_2019.json")
    assert "status: resolved" in chunk.text
    active = _chunk("condition_asthma_active.json")
    assert "status: active" in active.text


# -------------------------------------------- serialization-time exclusion


def test_sensitive_fixtures_never_become_chunks_labeled_or_stripped():
    for name in ("condition_hiv_labeled.json", "condition_hiv_stripped.json",
                 "condition_pregnancy_ab352.json", "condition_sud_part2.json"):
        result = serialize_resource(_load(name), f"fixtures/{name}", VALUE_SETS)
        assert result.chunk is None, name
        assert result.excluded is not None and not result.excluded.include


def test_department_signal_excludes_at_serialization():
    resource = _load("condition_asthma_active.json")  # clinically unremarkable
    result = serialize_resource(
        resource, "k", VALUE_SETS, source_department="dept-part2-program"
    )
    assert result.chunk is None


# ------------------------------------------------------- §7.3 absence rules


def test_missing_must_support_renders_not_recorded_never_a_negative():
    chunk = _chunk("condition_missing_must_support.json")
    assert "status: not recorded" in chunk.text
    assert "verification: not recorded" in chunk.text
    assert "effective: not recorded" in chunk.text
    for forbidden in ("inactive", "none", "no ", "denied"):
        assert forbidden not in chunk.text.lower().replace("not recorded", "")


def test_no_known_allergy_and_not_asked_do_not_collapse():
    nka = _chunk("allergy_no_known.json")
    not_asked = _chunk("allergy_not_asked.json")
    assert "716186003" in nka.text and "No known allergy" in nka.text
    assert "1631000175102" in not_asked.text and "not asked" in not_asked.text.lower()
    assert nka.text != not_asked.text


def test_both_medication_x_forms_produce_subject_bearing_chunks():
    cc = _chunk("medication_codeableconcept.json")
    ref = _chunk("medication_reference.json")
    assert "Lisinopril" in cc.text
    assert cc.subject_reference == ref.subject_reference == "Patient/syn-anna-1"
    # The Reference form has no inline coding; the chunk still exists
    # and still carries status - handling both forms is the consumer
    # obligation (§7.2).
    assert "status: active" in ref.text


# ----------------------------------------------------------- determinism


def test_serialization_is_idempotent_and_versioned():
    a = _chunk("condition_asthma_active.json")
    b = _chunk("condition_asthma_active.json")
    assert a == b
    assert a.template_version == TEMPLATE_VERSION


def test_references_and_effective_survive_onto_the_chunk():
    chunk = _chunk("observation_wrong_encounter.json")
    assert chunk.subject_reference == "Patient/syn-anna-1"
    assert chunk.encounter_reference == "Encounter/syn-enc-77"
    assert chunk.effective == "2026-02-01"


def test_psychotherapy_notes_never_serialize_labeled_or_stripped():
    # Driven by the operator value-set file the terminology loader
    # ships as its example - the same wiring a deployment gets.
    from core.terminology.loader import load_value_sets

    value_sets = load_value_sets(
        Path(__file__).resolve().parents[1]
        / "config"
        / "sensitive_value_sets.example.yaml"
    )
    for name in (
        "document_psychotherapy_note_labeled.json",
        "document_psychotherapy_note_stripped.json",
    ):
        result = serialize_resource(_load(name), f"fixtures/{name}", value_sets)
        assert result.chunk is None, name
        assert not result.excluded.include


def test_multiyear_condition_survives_serialization_with_its_date():
    chunk = _chunk("condition_multiyear_1998.json")
    assert chunk.effective == "1998-04-01"
    assert "status: active" in chunk.text
# Made by Ryan Gomez & Co. Inc.
