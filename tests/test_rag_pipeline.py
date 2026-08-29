# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/rag/retriever.py, pipeline.py, and eval.py — the
grant-bounded scan (5.1d), the end-to-end ask() flow, and the §10
metric machinery, including proof that the metrics DETECT the failures
they exist to count (a metric that can't fail is a dashboard, not a
measurement).
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.governance.segmentation import CategoryValueSets  # noqa: E402
from core.rag.answer_contract import (  # noqa: E402
    Abstention,
    Answer,
    AnswerContractError,
    Claim,
    PurposeOfUse,
)
from core.rag.eval import MetricsReport, measure_status_inversion, run_all  # noqa: E402
from core.rag.pipeline import (  # noqa: E402
    Refusal,
    ask,
    is_differential_request,
    serialize_corpus,
    wants_spine,
)
from core.rag.retriever import GrantScope, RetrievalScopeError, retrieve  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"


def _corpus():
    resources = {}
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name != "MANIFEST.json":
            resources[f"fixtures/{path.name}"] = json.loads(path.read_text())
    chunks = serialize_corpus(resources, CategoryValueSets())
    return resources, chunks


ANNA = "Patient/syn-anna-1"


# ---------------------------------------------------------------- retriever


def test_retrieval_requires_a_patient_scope():
    _, chunks = _corpus()
    with pytest.raises(RetrievalScopeError):
        retrieve("asthma", chunks, GrantScope(patient_reference="  "))


def test_prefilters_run_inside_the_scan_not_after_top_k():
    _, chunks = _corpus()
    # Anna's scope never returns the near-duplicate patient's chunk,
    # no matter how well it scores lexically.
    hits = retrieve("diabetes mellitus", chunks, GrantScope(patient_reference=ANNA), k=50)
    assert all(c.subject_reference == ANNA for c, _ in hits)

    # Resource-type and date filters are predicates, not post-hoc trims.
    typed = retrieve(
        "asthma",
        chunks,
        GrantScope(patient_reference=ANNA, resource_types=frozenset({"Condition"})),
    )
    assert typed and all(c.resource_type == "Condition" for c, _ in typed)

    dated = retrieve(
        "asthma",
        chunks,
        GrantScope(patient_reference=ANNA, date_from="2024-01-01"),
    )
    assert all(
        c.effective is None or c.effective >= "2024-01-01" for c, _ in dated
    )


def test_undated_chunks_survive_date_filters():
    _, chunks = _corpus()
    hits = retrieve(
        "hypertension",
        chunks,
        GrantScope(patient_reference=ANNA, date_from="2020-01-01"),
    )
    # The sparse hypertension fixture has no effective date and must
    # not vanish under a date-scoped question (§7.3 / §10 omission).
    assert any("hypertension" in c.text.lower() for c, _ in hits)


def test_exact_tokens_hit_lexically():
    _, chunks = _corpus()
    hits = retrieve("A1c", chunks, GrantScope(patient_reference=ANNA))
    assert hits and "Hemoglobin A1c" in hits[0][0].text


# ----------------------------------------------------------------- pipeline


def _compose_citing_top(question, ranked, spine):
    top = ranked[0].chunk
    return [Claim(text=f"Answer about {top.resource_type}.", citations=(top.storage_key,))]


def test_ask_end_to_end_returns_cited_answer():
    resources, chunks = _corpus()
    result = ask(
        "is the asthma active?",
        chunks,
        scope=GrantScope(patient_reference=ANNA),
        purpose=PurposeOfUse.TREATMENT,
        compose=_compose_citing_top,
        resources_by_key=resources,
        anchor=date(2026, 8, 21),
    )
    assert isinstance(result, Answer)
    assert result.cited_keys


def test_ask_abstains_when_nothing_matches():
    resources, chunks = _corpus()
    result = ask(
        "phlogiston catalytic converter recall",
        chunks,
        scope=GrantScope(patient_reference=ANNA),
        purpose=PurposeOfUse.TREATMENT,
        compose=_compose_citing_top,
        anchor=date(2026, 8, 21),
    )
    assert isinstance(result, Abstention)


def test_differential_requests_are_refused_before_retrieval():
    assert is_differential_request("What's the differential for this patient?")
    assert is_differential_request("give me a ddx")
    assert is_differential_request("what could the patient have?")
    # Hypothesis-directed retrieval is NOT a differential request (5.1i).
    assert not is_differential_request(
        "is there anything in this chart relevant to amyloidosis?"
    )

    _, chunks = _corpus()
    result = ask(
        "What is this patient's differential diagnosis?",
        chunks,
        scope=GrantScope(patient_reference=ANNA),
        purpose=PurposeOfUse.TREATMENT,
        compose=_compose_citing_top,
    )
    assert isinstance(result, Refusal)
    assert "hypothesis-directed" in result.reason


def test_summary_questions_carry_the_spine_and_its_citations():
    resources, chunks = _corpus()
    assert wants_spine("summarize this patient's history")

    def compose(question, ranked, spine):
        assert spine is not None
        # Cite a spine entry - allowed alongside retrieved chunks (5.1g).
        med = spine.active_medications[0]
        return [Claim(text="On lisinopril.", citations=(med.storage_key,))]

    result = ask(
        "summarize this patient's medication list",
        chunks,
        scope=GrantScope(patient_reference=ANNA),
        purpose=PurposeOfUse.TREATMENT,
        compose=compose,
        resources_by_key=resources,
        anchor=date(2026, 8, 21),
    )
    assert isinstance(result, Answer)


def test_model_cannot_mint_citations_through_the_pipeline():
    resources, chunks = _corpus()

    def hallucinating_compose(question, ranked, spine):
        return [Claim(text="Fact.", citations=("keys/that/were/never/retrieved",))]

    with pytest.raises(AnswerContractError, match="never retrieved"):
        ask(
            "is the asthma active?",
            chunks,
            scope=GrantScope(patient_reference=ANNA),
            purpose=PurposeOfUse.TREATMENT,
            compose=hallucinating_compose,
            anchor=date(2026, 8, 21),
        )


# --------------------------------------------------------------------- eval


def test_layer4_metrics_are_clean():
    resources, chunks = _corpus()
    report = run_all(resources, chunks, anchor=date(2026, 8, 21))
    assert report.status_inversions == 0
    assert report.attribution_false_negatives == 0
    assert report.omitted_spine_entries == 0
    assert report.recall_at_k == 1.0
    assert report.abstention_failures == 0


def test_metrics_detect_an_injected_status_inversion():
    _, chunks = _corpus()
    refuted = next(c for c in chunks if c.verification_status == "refuted")
    # Simulate the 5.1f bug: a refuted allergy whose text lost its
    # banner - the metric must count it.
    broken = refuted.__class__(
        **{**refuted.__dict__, "text": "AllergyIntolerance | Penicillin"}
    )
    report = MetricsReport()
    measure_status_inversion([broken], date(2026, 8, 21), report)
    assert report.status_inversions == 1
# Made by Ryan Gomez & Co. Inc.
