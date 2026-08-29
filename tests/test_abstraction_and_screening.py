# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for §5.10 (abstraction worklist), §5.12 (trial pre-screening),
and the Invariant 15 ambient audio store.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.capabilities.abstraction import (  # noqa: E402
    AbstractionError,
    AbstractionWorklist,
    MeasureElement,
)
from core.capabilities.trial_screening import TrialCriterion, screen  # noqa: E402
from core.governance.ambient_store import AmbientAudioStore, AmbientStoreError  # noqa: E402
from core.governance.consent_gate import (  # noqa: E402
    ConsentRecord,
    ConsentStatus,
    Modality,
    evaluate_capture,
)
from core.governance.segmentation import CategoryValueSets  # noqa: E402
from core.rag.pipeline import serialize_corpus  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"
ANNA = "Patient/syn-anna-1"


def _chunks():
    resources = {}
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name != "MANIFEST.json":
            resources[f"fixtures/{path.name}"] = json.loads(path.read_text())
    return serialize_corpus(resources, CategoryValueSets())


# ------------------------------------------------------- 5.10 abstraction


def _worklist(audit=None):
    worklist = AbstractionWorklist(
        "eCQM-synth-1",
        ANNA,
        [
            MeasureElement("E1", "Diabetes diagnosis documented", "diabetes mellitus"),
            MeasureElement("E2", "HbA1c result recorded", "hemoglobin A1c"),
        ],
        audit=audit,
    )
    worklist.propose(_chunks())
    return worklist


def test_export_refuses_until_every_element_is_humanly_confirmed():
    worklist = _worklist()
    with pytest.raises(AbstractionError, match="unconfirmed elements: E1, E2"):
        worklist.export()

    e1 = worklist._elements["E1"]
    assert e1.proposed_citations  # evidence proposed with citations
    worklist.confirm(
        "E1",
        confirmed_by="abstractor.rhia",
        value="yes",
        citations=[e1.proposed_citations[0]],
    )
    with pytest.raises(AbstractionError, match="E2"):
        worklist.export()

    worklist.mark_not_found("E2", confirmed_by="abstractor.rhia")
    exported = worklist.export()
    assert exported["elements"]["E1"]["value"] == "yes"
    assert exported["elements"]["E2"]["not_found_in_chart"] is True
    assert exported["elements"]["E1"]["citations"]


def test_confirmation_requires_a_named_human_and_real_citations():
    worklist = _worklist()
    with pytest.raises(AbstractionError, match="named human"):
        worklist.confirm("E1", confirmed_by=" ", value="yes", citations=["x"])
    with pytest.raises(AbstractionError, match="not among the proposed"):
        worklist.confirm(
            "E1", confirmed_by="a.b", value="yes", citations=["fabricated/key"]
        )
    with pytest.raises(AbstractionError, match="at least one citation"):
        worklist.confirm("E1", confirmed_by="a.b", value="yes", citations=[])


def test_confirmations_are_audited():
    events = []
    worklist = _worklist(audit=AuditLog(sink=events.append))
    e1 = worklist._elements["E1"]
    worklist.confirm(
        "E1", confirmed_by="abstractor.rhia", value="yes",
        citations=[e1.proposed_citations[0]],
    )
    assert events[-1]["action"] == "abstraction.element_confirmed"
    assert AuditLog.verify_chain(events)


# --------------------------------------------------- 5.12 trial screening


def test_screening_builds_a_cited_worklist_and_flags_exclusions_visibly():
    chunks = _chunks()
    by_patient = {}
    for chunk in chunks:
        if chunk.subject_reference:
            by_patient.setdefault(chunk.subject_reference, []).append(chunk)

    criteria = [
        TrialCriterion("I1", "Type 2 diabetes diagnosis", "type 2 diabetes mellitus", "inclusion"),
        TrialCriterion("X1", "Active asthma", "asthma", "exclusion"),
    ]
    events = []
    worklist = screen(
        criteria, by_patient, audit=AuditLog(sink=events.append)
    )
    anna = [c for c in worklist if c.patient_reference == ANNA]
    assert len(anna) == 1
    candidate = anna[0]
    assert candidate.inclusion_matches[0].citations
    # Anna HAS active asthma: flagged for review, still on the worklist.
    assert candidate.excluded_pending_review
    assert any(
        "asthma" in m.excerpts[0].lower() for m in candidate.exclusion_matches
    )
    # Purpose of use is research on every audit event.
    assert all(e["purpose_of_use"] == "research" for e in events)


def test_screening_never_matches_negated_content():
    chunks = _chunks()
    by_patient = {ANNA: [c for c in chunks if c.subject_reference == ANNA]}
    criteria = [
        TrialCriterion("I1", "Penicillin allergy", "penicillin allergy", "inclusion"),
    ]
    # Anna's only penicillin record is REFUTED: she is not a candidate
    # on its strength.
    worklist = screen(criteria, by_patient)
    for candidate in worklist:
        for match in candidate.inclusion_matches:
            assert "fixtures/allergy_penicillin_refuted.json" not in match.citations


# ----------------------------------------------- Invariant 15 audio store


def _granted_decision():
    return evaluate_capture(
        "CT",
        Modality.IN_PERSON,
        ConsentRecord(
            status=ConsentStatus.GRANTED,
            timestamp="2026-08-21T14:00:00+00:00",
            obtained_by="ma.rivera",
        ),
    )


def test_store_refuses_general_bucket_and_unconsented_audio():
    with pytest.raises(AmbientStoreError, match="general"):
        AmbientAudioStore(
            write=lambda k, b: None, delete=lambda k: None,
            store_label="general-phi-store", retention_days=90,
        )

    store = AmbientAudioStore(
        write=lambda k, b: None, delete=lambda k: None,
        store_label="ambient-audio-cmk", retention_days=90,
    )
    refused = evaluate_capture("MI", Modality.IN_PERSON, None)
    with pytest.raises(AmbientStoreError, match="refused by the consent gate"):
        store.store_capture(b"...", encounter_key="enc/1", decision=refused, actor="scribe")


def test_revocation_deletes_audio_and_transcript_audited():
    written, deleted, events = {}, [], []
    store = AmbientAudioStore(
        write=lambda k, b: written.update({k: b}),
        delete=deleted.append,
        store_label="ambient-audio-cmk",
        retention_days=90,
        audit=AuditLog(sink=events.append),
    )
    obj = store.store_capture(
        b"audio-bytes", encounter_key="enc/1", decision=_granted_decision(), actor="scribe"
    )
    assert obj.audio_key in written
    assert obj.retention_days == 90  # its own schedule, not the note's

    store.attach_transcript(obj.audio_key, "notes/enc-1-transcript")
    gone = store.delete_for_revocation(obj.audio_key, actor="scribe")
    assert set(deleted) == set(gone) == {obj.audio_key, "notes/enc-1-transcript"}
    assert [e["action"] for e in events] == [
        "ambient.audio_stored",
        "ambient.audio_deleted_revocation",
        "ambient.transcript_deleted_revocation",
    ]
    assert AuditLog.verify_chain(events)
# Made by Ryan Gomez & Co. Inc.
