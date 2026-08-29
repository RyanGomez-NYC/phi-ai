# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/capabilities/* (SPEC §5.2, §5.3, §5.4, §5.6, §5.13) and
the streaming checkpoint discipline (§6.2).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.capabilities.data_quality import analyze  # noqa: E402
from core.capabilities.patient_instructions import check_no_new_assertions  # noqa: E402
from core.capabilities.prior_auth import Criterion, assemble_packet  # noqa: E402
from core.capabilities.summarization import render_summary  # noqa: E402
from core.capabilities.triage import (  # noqa: E402
    TriageError,
    choose_operating_point,
    route,
)
from core.fhir.streaming_checkpoints import (  # noqa: E402
    StreamCheckpoints,
    StreamingCheckpointError,
)
from core.governance.registry import ModelRegistry, UnregisteredModelError  # noqa: E402
from core.governance.segmentation import CategoryValueSets  # noqa: E402
from core.rag.pipeline import serialize_corpus  # noqa: E402
from core.rag.spine import build_spine  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"


def _corpus():
    resources = {}
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name != "MANIFEST.json":
            resources[f"fixtures/{path.name}"] = json.loads(path.read_text())
    return resources, serialize_corpus(resources, CategoryValueSets())


# ------------------------------------------------------- 5.2 summarization


def test_summary_renders_the_complete_cited_skeleton():
    resources, chunks = _corpus()
    summary = render_summary(build_spine(chunks, resources))
    assert "Lisinopril" in summary and "Metformin" in summary
    assert "hypertension" in summary.lower()
    assert "[cite: fixtures/" in summary
    # Negated content appears only under its own heading.
    negated_section = summary.split("Recorded as NOT true")[1]
    assert "Penicillin" in negated_section
    before_negated = summary.split("Recorded as NOT true")[0]
    assert "Penicillin" not in before_negated


# ------------------------------------------ 5.3 no-new-assertions check


SOURCE = (
    "Take lisinopril 10 mg once daily. Return if your temperature is over "
    "101 F. Follow up with Dr. Okafor in 2 weeks."
)


def test_faithful_rewrite_passes():
    output = (
        "Take your blood pressure pill (lisinopril, 10 mg) one time each "
        "day. If your temperature goes above 101 F, call us. See Dr. "
        "Okafor in 2 weeks."
    )
    assert check_no_new_assertions(SOURCE, output).ok


def test_new_dose_new_drug_and_new_followup_all_fail():
    wrong_dose = check_no_new_assertions(SOURCE, "Take lisinopril 20 mg daily.")
    assert not wrong_dose.ok and "20 mg" in wrong_dose.new_numbers

    new_drug = check_no_new_assertions(
        SOURCE,
        "Also start metformin every morning.",
        known_medication_vocabulary=["lisinopril", "metformin"],
    )
    assert not new_drug.ok and "metformin" in new_drug.new_medications

    new_followup = check_no_new_assertions(SOURCE, "Follow up with Dr. Chen.")
    assert not new_followup.ok and "chen" in new_followup.new_followups


def test_structured_lists_extend_what_the_source_permits():
    result = check_no_new_assertions(
        "Rest and drink fluids.",
        "Keep taking metformin 500 mg as prescribed.",
        medication_names=["Metformin 500 MG Oral Tablet"],
        known_medication_vocabulary=["metformin"],
    )
    assert result.ok


# ----------------------------------------------------- 5.4 prior auth


def test_packet_cites_met_criteria_and_names_unmet_ones():
    _, chunks = _corpus()
    packet = assemble_packet(
        [
            Criterion("C1", "Documented asthma diagnosis", "asthma"),
            Criterion("C2", "Trial of biologic therapy", "omalizumab biologic"),
        ],
        chunks,
        patient_reference="Patient/syn-anna-1",
    )
    by_id = {e.criterion.criterion_id: e for e in packet.evidence}
    assert by_id["C1"].met and by_id["C1"].citations
    assert not by_id["C2"].met and not by_id["C2"].citations

    rendered = packet.render()
    assert "UNMET CRITERIA (1)" in rendered
    assert "Trial of biologic therapy" in rendered
    assert "[cite: " in rendered


def test_packet_never_offers_negated_content_as_evidence():
    _, chunks = _corpus()
    packet = assemble_packet(
        [Criterion("C1", "Penicillin allergy documented", "penicillin allergy")],
        chunks,
        patient_reference="Patient/syn-anna-1",
    )
    # The "No known allergy" record may legitimately match the query
    # lexically, but the REFUTED penicillin allergy must never appear
    # among the citations - offering it would be a status inversion in
    # a payer packet.
    assert "fixtures/allergy_penicillin_refuted.json" not in packet.evidence[0].citations
    assert all("REFUTED" not in x for x in packet.evidence[0].excerpts)


# ---------------------------------------------------------- 5.6 triage


def test_operating_point_is_chosen_on_urgent_recall():
    scores = [0.9, 0.8, 0.55, 0.4, 0.35, 0.3, 0.2, 0.1]
    urgent = [True, True, False, True, False, False, False, False]
    op = choose_operating_point(scores, urgent, min_urgent_recall=1.0)
    assert op.threshold <= 0.4  # must catch the 0.4 urgent message
    assert op.urgent_recall == 1.0
    assert op.urgent_miss_rate == 0.0
    assert op.routine_escalation_rate > 0  # the stated precision cost


def test_hopeless_scorer_escalates_everything():
    # Urgent messages score at the bottom: no threshold achieves the
    # recall floor except routing everything to a human.
    scores = [0.9, 0.8, 0.1, 0.05]
    urgent = [False, False, True, True]
    op = choose_operating_point(scores, urgent, min_urgent_recall=1.0)
    assert op.threshold <= 0.05
    assert op.urgent_recall == 1.0


def test_no_urgent_examples_is_refused():
    with pytest.raises(TriageError, match="no urgent examples"):
        choose_operating_point([0.5, 0.4], [False, False])


def test_routing_requires_a_registered_model():
    registry = ModelRegistry()
    op = choose_operating_point([0.9, 0.1], [True, False], min_urgent_recall=1.0)
    with pytest.raises(UnregisteredModelError):
        route("msg/1", 0.95, op, registry=registry, model_id="triage-v1")


# ------------------------------------------------------ 5.13 data quality


def test_data_quality_finds_unmapped_duplicates_and_drift():
    resources = {
        "k1": {
            "resourceType": "Condition",
            "code": {"coding": [{"system": "urn:local:epic:edg", "code": "EDG-771", "display": "local dx"}]},
        },
        "k2": {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c"}]},
        },
        "k3": {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "HbA1c (glycated)"}]},
        },
        "p1": {"resourceType": "Patient", "name": [{"given": ["Anna"], "family": "Cadence"}], "birthDate": "1985-02-14"},
        "p2": {"resourceType": "Patient", "name": [{"given": ["Anna"], "family": "Cadence"}], "birthDate": "1985-02-14"},
    }
    report = analyze(resources)
    assert any(u.system == "urn:local:epic:edg" for u in report.unmapped)
    assert len(report.duplicates) == 1
    assert report.duplicates[0].patient_keys == ("p1", "p2")
    assert any(d.code == "4548-4" and len(d.displays) == 2 for d in report.drift)
    assert "never auto-merged" in report.render()


def test_data_quality_findings_from_the_layer4_fixtures():
    resources, _ = _corpus()
    report = analyze(resources)
    assert any(
        u.storage_key == "fixtures/condition_unmapped_local_code.json"
        for u in report.unmapped
    )
    dup = [d for d in report.duplicates if d.name == "desiree powell"]
    assert len(dup) == 1 and len(dup[0].patient_keys) == 2


# ------------------------------------------- §6.2 streaming checkpoints


def test_gaps_are_recorded_loudly_and_can_be_backfilled():
    saved = {}
    checkpoints = StreamCheckpoints(persist=lambda p, s: saved.update({p: s}))
    assert checkpoints.observe("adt", 0) is None
    assert checkpoints.observe("adt", 1) is None
    gap = checkpoints.observe("adt", 5)  # skipped 2,3,4
    assert gap is not None and (gap.from_offset, gap.to_offset) == (2, 4)

    with pytest.raises(StreamingCheckpointError, match="unresolved gaps"):
        checkpoints.assert_gapless()

    # Late arrivals shrink the recorded gap...
    checkpoints.observe("adt", 3)
    remaining = checkpoints.open_gaps("adt")
    assert [(g.from_offset, g.to_offset) for g in remaining] == [(2, 2), (4, 4)]
    checkpoints.observe("adt", 2)
    checkpoints.observe("adt", 4)
    checkpoints.assert_gapless()

    # ...but a replay with no matching gap raises.
    with pytest.raises(StreamingCheckpointError, match="replayed or forked"):
        checkpoints.observe("adt", 1)

    # Durable state carried the gap list while it was open.
    assert saved["adt"]["next_offset"] == 6


def test_restore_resumes_gaps_across_restart():
    checkpoints = StreamCheckpoints()
    checkpoints.restore("adt", next_offset=10, gaps=[(4, 6)])
    with pytest.raises(StreamingCheckpointError):
        checkpoints.assert_gapless()
    for offset in (4, 5, 6):
        checkpoints.observe("adt", offset)
    checkpoints.assert_gapless()
# Made by Ryan Gomez & Co. Inc.
