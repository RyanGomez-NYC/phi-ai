# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Integration tests for the grounded_patient_evidence assistant tool —
core/rag wired into core/assistant/tools.py.

What must hold at the seam: the tool exists only when the deployment
configured sensitive value sets (fail-closed, absent-not-degraded);
every stored object is audited BEFORE decryption; segmentation excludes
sensitive content from the evidence even though the reader returned it;
the differential refusal survives the tool boundary; and evidence lines
carry resolvable [cite: ...] keys.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assistant.tools import ClinicalAccess, build  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layer4"
EXAMPLE_VS = (
    Path(__file__).resolve().parents[1] / "config" / "sensitive_value_sets.example.yaml"
)

ANNA = "Patient/syn-anna-1"
KYLE = "Patient/syn-kyle-1"


class FakeReader:
    """Index + store double: every layer4 fixture is one stored object
    whose subject decides which patient it belongs to."""

    def __init__(self):
        self._objects = {}
        for path in sorted(FIXTURES.glob("*.json")):
            if path.name == "MANIFEST.json":
                continue
            resource = json.loads(path.read_text())
            subject = (resource.get("subject") or resource.get("patient") or {}).get(
                "reference"
            )
            self._objects[f"obj/{path.stem}"] = (subject, [resource])

    def resources_for_patient(self, patient_reference):
        return [
            {"storage_key": key, "resource_type": resources[0]["resourceType"]}
            for key, (subject, resources) in sorted(self._objects.items())
            if subject == patient_reference
        ]

    def read_resources(self, storage_key):
        return self._objects[storage_key][1]


class FakeKnowledgeBase:
    def search(self, *a, **k):
        return []

    def read(self, *a, **k):
        return ""

    def list(self, *a, **k):
        return []


def _toolbox(monkeypatch, tier="lookup", patient=None, audit_events=None):
    monkeypatch.setenv("PHI_AI_SENSITIVE_VALUE_SETS", str(EXAMPLE_VS))
    events = audit_events if audit_events is not None else []
    access = ClinicalAccess(
        reader=FakeReader(),
        record_read=lambda action, key: events.append((action, key)),
        purpose="treatment",
        tier=tier,
        patient_reference=patient,
    )
    return build(FakeKnowledgeBase(), clinical=access)


def test_tool_absent_without_configured_value_sets(monkeypatch):
    monkeypatch.delenv("PHI_AI_SENSITIVE_VALUE_SETS", raising=False)
    access = ClinicalAccess(
        reader=FakeReader(),
        record_read=lambda *a: None,
        purpose="treatment",
        tier="lookup",
    )
    toolbox = build(FakeKnowledgeBase(), clinical=access)
    assert "grounded_patient_evidence" not in toolbox.names
    # The ungated read tools are still there - only grounding is absent.
    assert "read_record" in toolbox.names


def test_malformed_value_sets_disable_the_tool_not_the_exclusions(monkeypatch, tmp_path):
    bad = tmp_path / "vs.yaml"
    bad.write_text("categories:\n  not_a_category:\n    - {system: s, code: c}\n")
    monkeypatch.setenv("PHI_AI_SENSITIVE_VALUE_SETS", str(bad))
    access = ClinicalAccess(
        reader=FakeReader(),
        record_read=lambda *a: None,
        purpose="treatment",
        tier="lookup",
    )
    toolbox = build(FakeKnowledgeBase(), clinical=access)
    assert "grounded_patient_evidence" not in toolbox.names


def test_grounded_evidence_cites_audits_and_excludes(monkeypatch):
    events = []
    toolbox = _toolbox(monkeypatch, audit_events=events)
    assert "grounded_patient_evidence" in toolbox.names

    text, is_error = toolbox.run(
        "grounded_patient_evidence",
        {"question": "is the asthma active?", "patient_reference": ANNA},
    )
    assert not is_error
    assert "[cite: obj/condition_asthma_active#0]" in text

    # Every one of Anna's stored objects was audited before reading.
    audited = {key for action, key in events if action == "record.read"}
    anna_objects = {
        row["storage_key"] for row in FakeReader().resources_for_patient(ANNA)
    }
    assert audited == anna_objects

    # Sensitive content the reader returned never reaches the evidence:
    # Anna's corpus is clean, but Kyle's psychotherapy note and SUD
    # condition are excluded when his record is grounded.
    kyle_text, _ = toolbox.run(
        "grounded_patient_evidence",
        {"question": "summarize the record", "patient_reference": KYLE},
    )
    assert "Psychotherapy" not in kyle_text
    assert "Opioid dependence" not in kyle_text
    # Kyle's ENTIRE record is policy-excluded: the response must say the
    # exclusion happened, not present the chart as empty.
    assert "sensitive-category policy" in kyle_text
    assert "absence here is not absence from the record" in kyle_text


def test_differential_refusal_survives_the_tool_boundary(monkeypatch):
    toolbox = _toolbox(monkeypatch)
    text, is_error = toolbox.run(
        "grounded_patient_evidence",
        {"question": "what is the differential diagnosis?", "patient_reference": ANNA},
    )
    assert not is_error
    assert "hypothesis-directed evidence retrieval" in text.lower()


def test_in_context_tier_binds_the_open_patient(monkeypatch):
    toolbox = _toolbox(monkeypatch, tier="in_context", patient=ANNA)
    assert "grounded_patient_evidence" in toolbox.names
    # The in-context tool takes no patient argument at all - the
    # binding is server-side, so there is nothing for a model to widen.
    schema = next(
        t for t in toolbox.definitions() if t["name"] == "grounded_patient_evidence"
    )["input_schema"]
    assert "patient_reference" not in schema["properties"]

    text, is_error = toolbox.run(
        "grounded_patient_evidence", {"question": "medication list?"}
    )
    assert not is_error
    assert "Lisinopril" in text


def test_summary_question_includes_the_structured_record(monkeypatch):
    toolbox = _toolbox(monkeypatch)
    text, _ = toolbox.run(
        "grounded_patient_evidence",
        {"question": "summarize this patient's history", "patient_reference": ANNA},
    )
    assert "Structured record (complete, deterministic)" in text
    assert "Recorded as NOT true" in text  # negated content shown AS negated
    assert "Metformin" in text  # the Reference-form medication, present


def test_unresponsive_question_reports_nothing_responsive(monkeypatch):
    toolbox = _toolbox(monkeypatch)
    text, is_error = toolbox.run(
        "grounded_patient_evidence",
        {"question": "spacecraft telemetry anomalies", "patient_reference": ANNA},
    )
    assert not is_error
    assert "nothing responsive" in text.lower() or "No responsive content" in text
# Made by Ryan Gomez & Co. Inc.
