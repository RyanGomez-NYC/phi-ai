# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for the grounded-assistant gates: attribution (hard gate),
temporal weighting (5.1e), the structured spine's silent-omission
guarantee (5.1g, §10's primary acceptance metric), and the answer
contract (5.1h/i).

The attribution tests use the cross-patient near-duplicate fixtures —
same name, same DOB, same diagnosis, different subject — which look
identical to any embedding and are exactly what the deterministic gate
exists to catch.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.governance.segmentation import CategoryValueSets  # noqa: E402
from core.rag.attribution import AttributionError, assert_attribution  # noqa: E402
from core.rag.answer_contract import (  # noqa: E402
    Abstention,
    AnswerContractError,
    Claim,
    PurposeOfUse,
    assemble_answer,
    refuse_differential,
)
from core.rag.serialization import serialize_resource  # noqa: E402
from core.rag.spine import build_spine  # noqa: E402
from core.rag.temporal import rank  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"
EMPTY_SETS = CategoryValueSets()


def _chunk(name: str):
    resource = json.loads((FIXTURES / name).read_text())
    result = serialize_resource(resource, f"fixtures/{name}", EMPTY_SETS)
    assert result.chunk is not None
    return result.chunk


# ------------------------------------------------------------- attribution


def test_near_duplicate_from_the_wrong_patient_refuses_the_answer():
    ours = _chunk("near_duplicate_patient_a.json")
    theirs = _chunk("near_duplicate_patient_b.json")
    with pytest.raises(AttributionError) as exc:
        assert_attribution([ours, theirs], "Patient/syn-anna-cadence-1")
    # The error names the offending key, never content.
    assert "near_duplicate_patient_b" in str(exc.value)
    assert "diabetes" not in str(exc.value).lower()


def test_encounter_arm_refuses_mismatched_encounter():
    obs = _chunk("observation_wrong_encounter.json")  # Encounter/syn-enc-77
    with pytest.raises(AttributionError):
        assert_attribution(
            [obs], "Patient/syn-anna-1", encounter_reference="Encounter/syn-enc-12"
        )
    # Same chunk passes when the session is scoped to its own encounter,
    # and encounter-less chunks pass the encounter arm (patient-level
    # resources stay in scope).
    allergy = _chunk("allergy_no_known.json")
    passed = assert_attribution(
        [obs, allergy], "Patient/syn-anna-1", encounter_reference="Encounter/syn-enc-77"
    )
    assert len(passed) == 2


def test_unattributed_chunk_fails_closed():
    chunk = _chunk("near_duplicate_patient_a.json")
    stripped = chunk.__class__(**{**chunk.__dict__, "subject_reference": None})
    with pytest.raises(AttributionError):
        assert_attribution([stripped], "Patient/syn-anna-cadence-1")


# ---------------------------------------------------- temporal weighting


def test_resolved_2019_problem_never_outranks_the_active_list():
    resolved = _chunk("condition_asthma_resolved_2019.json")
    active = _chunk("condition_asthma_active.json")
    # Give the RESOLVED chunk the better retriever score - the fusion
    # must still put the active one first.
    ranked = rank([(resolved, 0.9), (active, 0.6)], anchor=date(2026, 8, 21))
    assert ranked[0].chunk is active
    assert ranked[0].score > ranked[1].score


def test_negated_content_scores_zero_by_default_and_ranks_when_asked():
    refuted = _chunk("allergy_penicillin_refuted.json")
    current = rank([(refuted, 1.0)], anchor=date(2026, 8, 21))
    assert current[0].score == 0.0
    history = rank(
        [(refuted, 1.0)], anchor=date(2026, 8, 21), include_negated=True
    )
    assert history[0].score > 0.0


def test_undated_chunk_gets_neutral_recency_not_a_penalty():
    sparse = _chunk("condition_missing_must_support.json")
    ranked = rank([(sparse, 0.5)], anchor=date(2026, 8, 21))
    assert ranked[0].score == 0.5


# ------------------------------------------------------------------ spine


def _all_layer4_chunks():
    chunks = []
    resources = {}
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        resource = json.loads(path.read_text())
        result = serialize_resource(resource, f"fixtures/{path.name}", EMPTY_SETS)
        if result.chunk:
            chunks.append(result.chunk)
            resources[result.chunk.storage_key] = resource
    return chunks, resources


def test_spine_never_silently_omits_active_medications_or_problems():
    chunks, resources = _all_layer4_chunks()
    spine = build_spine(chunks, resources)

    med_keys = {e.storage_key for e in spine.active_medications}
    assert "fixtures/medication_codeableconcept.json" in med_keys
    assert "fixtures/medication_reference.json" in med_keys  # both forms present

    problem_labels = " ".join(e.label for e in spine.active_problems)
    assert "Asthma" in problem_labels
    assert "hypertension" in problem_labels.lower()  # undated but PRESENT

    # Refuted / entered-in-error live in negated_assertions, not among
    # allergies or problems.
    assert not any("Penicillin" in e.label for e in spine.allergies)
    assert any("Penicillin" in e.label for e in spine.negated_assertions)
    assert not any("myocardial" in e.label.lower() for e in spine.active_problems)


def test_spine_entries_all_carry_citations_and_labs_get_series():
    chunks, resources = _all_layer4_chunks()
    spine = build_spine(chunks, resources)
    assert spine.citation_keys()  # nonempty, all storage keys
    a1c = [s for s in spine.labs if s.code == "4548-4"]
    assert len(a1c) == 1 and a1c[0].points == (("2026-02-01", 7.2),)
    assert a1c[0].trend == "single value"


# -------------------------------------------------------- answer contract


def test_empty_retrieval_abstains_and_there_is_no_flag_to_stop_it():
    result = assemble_answer(
        [], [], patient_reference="Patient/p1", purpose=PurposeOfUse.TREATMENT
    )
    assert isinstance(result, Abstention)
    import inspect

    # The non-disableable default, verified structurally: no parameter
    # of assemble_answer mentions abstention.
    params = inspect.signature(assemble_answer).parameters
    assert not any("abstain" in p or "abstention" in p for p in params)


def test_uncited_claims_and_fabricated_citations_are_refused():
    chunk = _chunk("condition_asthma_active.json")
    with pytest.raises(AnswerContractError, match="no citation"):
        assemble_answer(
            [Claim(text="Asthma is active.", citations=())],
            [chunk],
            patient_reference="Patient/syn-anna-1",
            purpose=PurposeOfUse.TREATMENT,
        )
    with pytest.raises(AnswerContractError, match="never retrieved"):
        assemble_answer(
            [Claim(text="X.", citations=("fixtures/never_fetched.json",))],
            [chunk],
            patient_reference="Patient/syn-anna-1",
            purpose=PurposeOfUse.TREATMENT,
        )


def test_wrong_patient_citation_withholds_the_whole_answer():
    theirs = _chunk("near_duplicate_patient_b.json")
    with pytest.raises(AttributionError):
        assemble_answer(
            [Claim(text="T2DM.", citations=(theirs.storage_key,))],
            [theirs],
            patient_reference="Patient/syn-anna-cadence-1",
            purpose=PurposeOfUse.TREATMENT,
        )


def test_valid_answer_releases_with_cited_keys():
    chunk = _chunk("condition_asthma_active.json")
    answer = assemble_answer(
        [Claim(text="Asthma, currently active.", citations=(chunk.storage_key,))],
        [chunk],
        patient_reference="Patient/syn-anna-1",
        purpose=PurposeOfUse.TREATMENT,
    )
    assert answer.cited_keys == {chunk.storage_key}


def test_differential_refusal_names_the_supported_alternative():
    refusal = refuse_differential()
    assert "does not generate" in refusal
    assert "hypothesis-directed evidence retrieval" in refusal.lower()
    assert "amyloidosis" in refusal  # the worked example, verbatim from 5.1i
# Made by Ryan Gomez & Co. Inc.
