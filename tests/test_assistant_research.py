# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The assistant's cross-record research tools and the psychotherapy gate.

Four properties are worth their own file, because each is a boundary
rather than a behavior: the config gates refuse to enable psychotherapy
access casually; the tools appear only for the roles that hold them;
every search and read is audited BEFORE it runs; and the runtime drops
access objects that a mis-wired caller offers against configuration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.assistant import knowledge, tools  # noqa: E402
from core.assistant.config import AssistantConfigError, settings_from_env  # noqa: E402
from core.web.auth import PERMISSIONS, Role  # noqa: E402


@pytest.fixture(scope="module")
def kb():
    return knowledge.load()


def _base_env(monkeypatch):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PROVIDER", "bedrock")
    monkeypatch.setenv("PHI_AI_ASSISTANT_AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Config gates
# ---------------------------------------------------------------------------

def test_psychotherapy_access_needs_the_lookup_tier(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", "in_context")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED", "true")
    with pytest.raises(AssistantConfigError) as exc:
        settings_from_env()
    assert "lookup" in str(exc.value)


def test_psychotherapy_access_needs_its_own_acknowledgement(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", "lookup")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS", "true")
    monkeypatch.delenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED", raising=False)
    with pytest.raises(AssistantConfigError) as exc:
        settings_from_env()
    assert "164.508" in str(exc.value)


def test_the_general_phi_acknowledgement_does_not_cover_psychotherapy(monkeypatch):
    """Acknowledging PHI access must not quietly acknowledge the record
    class with its own authorization regime - separate flags, checked
    separately."""
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", "lookup")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", "true")
    monkeypatch.delenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS", raising=False)
    settings = settings_from_env()
    assert settings.psychotherapy_access is False


# ---------------------------------------------------------------------------
# Tool presence follows role and configuration
# ---------------------------------------------------------------------------

def _research_access(**overrides):
    audited = []
    defaults = dict(
        search_connection=lambda: (_ for _ in ()).throw(AssertionError("no query expected")),
        psychotherapy_connection=None,
        read_psychotherapy=None,
        record=lambda action, detail: audited.append((action, detail)),
        purpose="research",
    )
    defaults.update(overrides)
    return tools.ResearchAccess(**defaults), audited


def test_researcher_gets_the_search_tool_and_viewer_does_not(kb):
    access, _ = _research_access()
    researcher = tools.build(kb, capabilities=PERMISSIONS[Role.RESEARCHER], research=access)
    viewer = tools.build(kb, capabilities=PERMISSIONS[Role.VIEWER], research=access)
    assert "search_clinical_records" in researcher.names
    assert "search_clinical_records" not in viewer.names


def test_psychotherapy_tools_follow_their_own_role(kb):
    access, _ = _research_access(
        psychotherapy_connection=lambda: None,
        read_psychotherapy=lambda key: {},
    )
    psych = tools.build(kb, capabilities=PERMISSIONS[Role.PSYCHOTHERAPY], research=access)
    researcher = tools.build(kb, capabilities=PERMISSIONS[Role.RESEARCHER], research=access)

    assert "search_psychotherapy_notes" in psych.names
    assert "read_psychotherapy_note" in psych.names
    # The researcher role, for all its breadth, never sees them.
    assert "search_psychotherapy_notes" not in researcher.names
    assert "read_psychotherapy_note" not in researcher.names
    # And the psychotherapy role's narrowness holds in the other
    # direction: it gets no general search.
    assert "search_clinical_records" not in psych.names


# ---------------------------------------------------------------------------
# Audit-before-anything ordering
# ---------------------------------------------------------------------------

class _OneRowConn:
    """A connection whose cursor returns one canned search row."""

    class _Cur:
        description = [("storage_key",), ("resource_index",), ("patient_reference",),
                       ("resource_type",), ("resource_id",), ("clinical_date",),
                       ("snippet",), ("rank",)]

        def execute(self, sql, params):
            pass

        def fetchall(self):
            return [("fhir/Condition/c1.json", 0, "Patient/e1", "Condition",
                     "c1", None, "… insulin pump failure …", 0.9)]

        def close(self):
            pass

    def cursor(self):
        return self._Cur()

    def close(self):
        pass


def test_a_search_is_audited_verbatim_before_it_runs(kb):
    order = []
    access = tools.ResearchAccess(
        search_connection=lambda: (order.append("connect"), _OneRowConn())[1],
        record=lambda action, detail: order.append((action, detail)),
        purpose="research",
    )
    box = tools.build(kb, capabilities=PERMISSIONS[Role.RESEARCHER], research=access)
    text, is_error = box.run("search_clinical_records", {"query": 'insulin "pump failure"'})
    assert not is_error
    assert order[0] == ("research.search", 'insulin "pump failure"'), (
        "the audit entry must be written before any connection is opened"
    )
    assert order[1] == "connect"
    assert "insulin pump failure" in text


def test_a_psychotherapy_read_is_audited_before_decryption(kb):
    order = []
    access = tools.ResearchAccess(
        read_psychotherapy=lambda key: (order.append("decrypt"), {"resourceType": "DocumentReference"})[1],
        record=lambda action, detail: order.append((action, detail)),
        purpose="treatment",
    )
    box = tools.build(kb, capabilities=PERMISSIONS[Role.PSYCHOTHERAPY], research=access)
    _, is_error = box.run("read_psychotherapy_note", {"storage_key": "notes/DocumentReference/n1.json"})
    assert not is_error
    assert order == [("psychotherapy.read", "notes/DocumentReference/n1.json"), "decrypt"]


# ---------------------------------------------------------------------------
# The runtime's belt-and-braces
# ---------------------------------------------------------------------------

def _runtime(assistant_settings):
    from core.assistant.runtime import AssistantRuntime

    return AssistantRuntime(
        settings=assistant_settings, client=object(),
        knowledge_base=knowledge.load(),
    )


def test_runtime_drops_research_access_below_the_lookup_tier(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", "none")
    rt = _runtime(settings_from_env())
    access, _ = _research_access()
    session = rt.session_for(actor="t", capabilities=PERMISSIONS[Role.RESEARCHER],
                             require_audit=False, research=access)
    assert "search_clinical_records" not in session._toolbox.names


def test_runtime_strips_psychotherapy_pieces_the_config_never_enabled(monkeypatch):
    """A caller wiring psych access against a deployment that never set
    the gate builds a toolbox WITHOUT those tools - and keeps the
    general search, which was legitimately configured."""
    _base_env(monkeypatch)
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", "lookup")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", "true")
    monkeypatch.delenv("PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS", raising=False)
    rt = _runtime(settings_from_env())
    access, _ = _research_access(
        psychotherapy_connection=lambda: None,
        read_psychotherapy=lambda key: {},
    )
    caps = PERMISSIONS[Role.RESEARCHER] | PERMISSIONS[Role.PSYCHOTHERAPY]
    session = rt.session_for(actor="t", capabilities=caps,
                             require_audit=False, research=access)
    assert "search_clinical_records" in session._toolbox.names
    assert "search_psychotherapy_notes" not in session._toolbox.names
    assert "read_psychotherapy_note" not in session._toolbox.names
# Made by Ryan Gomez & Co. Inc.
