# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Human release gate on patient-directed output (docs/SPEC.md
Invariant 17; capabilities §5.3, §5.6).

Anything the platform drafts that would reach a patient — an inbox
reply, patient-friendly instructions, an appointment message — is
staged here and leaves only through release() with a named human
releaser. Drafts are never auto-sent, and NO CONFIGURATION DISABLES
THE GATE: that sentence from the invariant is implemented as an
absence, not a check — this class takes no flag, reads no environment
variable, and has no second method that sends. If a code path wants to
send patient-directed output without a human, it has nowhere to call.

This gate is about *release*, not content. Content checks (e.g.
§5.3's no-new-assertions diff) run before staging; by the time a draft
is here, the remaining question is only "which licensed human decided
this leaves," and that answer is recorded and audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class ReleaseGateError(Exception):
    pass


@dataclass(frozen=True)
class PatientDraft:
    draft_id: str
    patient_key: str
    channel: str  # e.g. "inbox_reply", "discharge_instructions"
    content: str
    ai_generated: bool = True


@dataclass(frozen=True)
class ReleaseEvent:
    draft_id: str
    released_by: str
    released_at: str


class PatientReleaseGate:
    """`send` is the deployment's outbound callable (portal message
    writer, etc.) and is invoked exactly once per draft, from
    release(), after the human decision is recorded — never before,
    and never from anywhere else."""

    def __init__(self, send, audit=None):
        self._send = send
        self._audit = audit
        self._staged: dict[str, PatientDraft] = {}
        self._released: dict[str, ReleaseEvent] = {}

    def stage(self, patient_key: str, channel: str, content: str) -> PatientDraft:
        draft = PatientDraft(
            draft_id=uuid.uuid4().hex,
            patient_key=patient_key,
            channel=channel,
            content=content,
        )
        self._staged[draft.draft_id] = draft
        return draft

    def release(self, draft_id: str, *, released_by: str) -> ReleaseEvent:
        draft = self._staged.get(draft_id)
        if draft is None:
            raise ReleaseGateError(
                f"No staged draft {draft_id!r}; drafts leave only through "
                "this gate and only once (Invariant 17)"
            )
        if not released_by.strip():
            raise ReleaseGateError(
                "release() requires a named human releaser; patient-directed "
                "output is never auto-sent (Invariant 17)"
            )
        event = ReleaseEvent(
            draft_id=draft_id,
            released_by=released_by,
            released_at=datetime.now(timezone.utc).isoformat(),
        )
        if self._audit is not None:
            self._audit.record(
                actor=released_by,
                action="patient_output.released",
                resource_key=f"draft/{draft_id}",
                purpose_of_use="treatment",
            )
        self._send(draft)
        del self._staged[draft_id]
        self._released[draft_id] = event
        return event

    def discard(self, draft_id: str, *, discarded_by: str) -> None:
        """The clinician's other button. Discarding is audited too —
        a triage draft a human rejected is signal for §5.6's monitored
        miss rate."""
        if draft_id not in self._staged:
            raise ReleaseGateError(f"No staged draft {draft_id!r}")
        if self._audit is not None:
            self._audit.record(
                actor=discarded_by,
                action="patient_output.discarded",
                resource_key=f"draft/{draft_id}",
                purpose_of_use="treatment",
            )
        del self._staged[draft_id]

    def released(self, draft_id: str) -> Optional[ReleaseEvent]:
        return self._released.get(draft_id)
# Made by Ryan Gomez & Co. Inc.
