# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/governance/writeback.py (SPEC §6.4, Invariant 13).

The two things worth proving here: (1) there is no path from staged
content to the EMR writer that skips a recorded human signature — not
by status manipulation, and not by editing content after signing; and
(2) the verified Epic R4 write surface is enforced as data, so a
medication write via FHIR REST fails at staging with the CDS Hooks
pointer rather than at an Epic endpoint.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.governance.writeback import (  # noqa: E402
    DraftStatus,
    SignatureQueue,
    WritebackError,
    assert_epic_writable,
)


def test_epic_write_surface_is_enforced_as_verified():
    # Verified writable interactions pass.
    assert_epic_writable("DocumentReference", "create")
    assert_epic_writable("Observation", "update")
    assert_epic_writable("Condition", "create")

    # Condition has create only - no update (verified).
    with pytest.raises(WritebackError, match="not 'update'"):
        assert_epic_writable("Condition", "update")

    # DiagnosticReport is update only (verified).
    with pytest.raises(WritebackError):
        assert_epic_writable("DiagnosticReport", "create")

    # MedicationRequest has no REST create; the refusal names the real
    # transport (CDS Hooks unsigned-order suggestions).
    with pytest.raises(WritebackError, match="CDS Hooks"):
        assert_epic_writable("MedicationRequest", "create")

    # Off-surface types are refused before any network call.
    with pytest.raises(WritebackError, match="does not include"):
        assert_epic_writable("Appointment", "create")


def test_unsigned_draft_never_commits():
    written = []
    events = []
    queue = SignatureQueue(writer=written.append, audit=AuditLog(sink=events.append))
    draft = queue.stage(
        "DocumentReference", "create", "Progress note text", actor="assistant"
    )
    with pytest.raises(WritebackError, match="no recorded human signature"):
        queue.commit(draft.draft_id, actor="assistant")
    assert written == []
    assert events[-1]["action"] == "writeback.commit.refused_unsigned"


def test_signature_then_commit_writes_once_with_provenance_chain():
    written = []
    events = []
    queue = SignatureQueue(writer=written.append, audit=AuditLog(sink=events.append))
    draft = queue.stage(
        "DocumentReference", "create", "Progress note text", actor="assistant"
    )
    event = queue.sign(draft.draft_id, signer="dr.okafor")
    assert event.signer == "dr.okafor"

    committed = queue.commit(draft.draft_id, actor="assistant")
    assert committed.status is DraftStatus.COMMITTED
    assert written == [draft]
    assert [e["action"] for e in events] == [
        "writeback.draft.staged",
        "writeback.draft.signed",
        "writeback.draft.committed",
    ]
    assert AuditLog.verify_chain(events)

    # Committed drafts don't sign or commit again.
    with pytest.raises(WritebackError):
        queue.sign(draft.draft_id, signer="dr.okafor")


def test_content_edited_after_signature_refuses_commit():
    written = []
    queue = SignatureQueue(writer=written.append)
    draft = queue.stage("Observation", "create", "BP 120/80", actor="assistant")
    queue.sign(draft.draft_id, signer="dr.okafor")
    # Mutate after signature: the signature covers what the human saw.
    queue.get(draft.draft_id).content = "BP 200/120"
    with pytest.raises(WritebackError, match="content changed after signature"):
        queue.commit(draft.draft_id, actor="assistant")
    assert written == []


def test_anonymous_signature_is_refused():
    queue = SignatureQueue(writer=lambda d: None)
    draft = queue.stage("Observation", "create", "BP 120/80", actor="assistant")
    with pytest.raises(WritebackError, match="named licensed human"):
        queue.sign(draft.draft_id, signer="")


def test_staging_an_unwritable_resource_fails_before_any_signature():
    queue = SignatureQueue(writer=lambda d: None)
    with pytest.raises(WritebackError):
        queue.stage("MedicationRequest", "create", "order text", actor="assistant")
# Made by Ryan Gomez & Co. Inc.
