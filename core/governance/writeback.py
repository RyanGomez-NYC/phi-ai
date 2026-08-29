# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
EMR write-back: staged-draft-plus-signature protocol and the verified
Epic R4 write surface (docs/SPEC.md §6.4, Invariant 13).

Invariant 13: nothing the platform generates enters the legal medical
record without a human signature event. Write-back is a two-step
protocol — stage a draft resource, a licensed human signs, the
signature commits. No auto-commit path exists in this module, including
for content classified low-risk: there is simply no method that reaches
the writer without a recorded signature.

WHY THE SIGNATURE QUEUE IS PLATFORM-SIDE (§6.4, open dependency #1):
whether a note created via DocumentReference lands *unsigned in a
clinician's Epic signing queue* or lands *committed* could not be
confirmed from public Epic documentation. Until that is resolved by an
authenticated read of Epic API spec 1046/845 or written Epic
confirmation, document writes are staged in THIS queue and committed to
Epic only after the human signature event is recorded on our side —
strictly more conservative than relying on unverified Epic-side
behavior, and it degrades gracefully to the native workflow once
confirmed.

The Epic capability matrix below is data, not documentation, so a
mis-targeted write fails at staging time with the reason — before any
network call, and before a clinician has signed something that can
never land.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class WritebackError(Exception):
    pass


#: Epic R4 write surface, [VERIFIED] against the live CapabilityStatement
#: (Epic software version "August 2026") — SPEC §6.4. Everything absent
#: from this mapping is read/search only on Epic's FHIR REST surface.
EPIC_R4_WRITE_SURFACE: dict[str, frozenset[str]] = {
    "AllergyIntolerance": frozenset({"create"}),
    "BodyStructure": frozenset({"create", "update"}),
    "Communication": frozenset({"create"}),
    "Condition": frozenset({"create"}),  # no update — verified
    "DiagnosticReport": frozenset({"update"}),  # update only — verified
    "DocumentReference": frozenset({"create", "update"}),
    "Observation": frozenset({"create", "update"}),
    "ConceptMap": frozenset({"create"}),
}

#: [VERIFIED negative]: these expose read and search only. Order writes
#: exist solely as CDS Hooks "unsigned order" suggestions — a different
#: transport, not a POST. Named here so the refusal can say where the
#: supported path actually is.
EPIC_R4_READ_ONLY = frozenset(
    {"MedicationRequest", "ServiceRequest", "Encounter", "Immunization", "Goal"}
)


def assert_epic_writable(resource_type: str, interaction: str) -> None:
    """Refuses, with the reason, any write Epic's verified R4 surface
    does not support. Any design assuming a medication write via FHIR
    REST against Epic is wrong (SPEC §6.4), and this is where that
    design finds out."""
    allowed = EPIC_R4_WRITE_SURFACE.get(resource_type)
    if allowed and interaction in allowed:
        return
    if resource_type in EPIC_R4_READ_ONLY:
        raise WritebackError(
            f"Epic exposes {resource_type} as read/search only over FHIR "
            "REST; order-shaped writes exist only as CDS Hooks 'unsigned "
            "order' suggestions, a different transport (SPEC §6.4)"
        )
    if allowed:
        raise WritebackError(
            f"Epic's verified R4 surface supports {resource_type} "
            f"{sorted(allowed)} but not {interaction!r} (SPEC §6.4)"
        )
    raise WritebackError(
        f"Epic's verified R4 write surface does not include {resource_type}; "
        "no write is attempted (SPEC §6.4)"
    )


class DraftStatus(Enum):
    STAGED = "staged"
    SIGNED = "signed"
    COMMITTED = "committed"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class SignatureEvent:
    draft_id: str
    signer: str
    signed_at: str
    #: hash of exactly the content the human saw and signed; commit
    #: re-hashes and refuses on mismatch, so nothing can be edited
    #: between signature and commit without a new signature.
    content_hash: str


@dataclass
class StagedDraft:
    draft_id: str
    resource_type: str
    interaction: str
    content: str
    status: DraftStatus
    signature: Optional[SignatureEvent] = None


class SignatureQueue:
    """
    The platform-side signature queue. `writer` is the deployment's EMR
    write callable (StagedDraft -> None); it is invoked only from
    commit(), and commit() runs only after sign() has recorded a human
    signature over the exact content being committed.

    AI-assisted provenance: the committed draft carries its signature
    event, and the audit trail records stage/sign/commit as separate
    events with the signer's identity, which is the provenance record
    Invariant 13 requires on the written resource.
    """

    def __init__(self, writer, audit=None):
        self._writer = writer
        self._audit = audit
        self._drafts: dict[str, StagedDraft] = {}

    def _record(self, actor: str, action: str, draft_id: str) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=actor,
                action=action,
                resource_key=f"writeback/{draft_id}",
                purpose_of_use="treatment",
            )

    def stage(
        self, resource_type: str, interaction: str, content: str, *, actor: str
    ) -> StagedDraft:
        assert_epic_writable(resource_type, interaction)
        draft = StagedDraft(
            draft_id=uuid.uuid4().hex,
            resource_type=resource_type,
            interaction=interaction,
            content=content,
            status=DraftStatus.STAGED,
        )
        self._drafts[draft.draft_id] = draft
        self._record(actor, "writeback.draft.staged", draft.draft_id)
        return draft

    def sign(self, draft_id: str, *, signer: str) -> SignatureEvent:
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise WritebackError(f"No staged draft {draft_id!r}")
        if not signer.strip():
            raise WritebackError(
                "sign() requires a named licensed human; the signature event "
                "IS the thing that commits (Invariant 13)"
            )
        if draft.status is not DraftStatus.STAGED:
            raise WritebackError(
                f"Draft {draft_id!r} is {draft.status.value}, not staged; "
                "each signature covers exactly one staged draft"
            )
        event = SignatureEvent(
            draft_id=draft_id,
            signer=signer,
            signed_at=datetime.now(timezone.utc).isoformat(),
            content_hash=_content_hash(draft.content),
        )
        draft.signature = event
        draft.status = DraftStatus.SIGNED
        self._record(signer, "writeback.draft.signed", draft_id)
        return event

    def commit(self, draft_id: str, *, actor: str) -> StagedDraft:
        """The only path to the EMR writer. Refuses unsigned drafts and
        drafts whose content no longer matches what was signed."""
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise WritebackError(f"No staged draft {draft_id!r}")
        if draft.status is not DraftStatus.SIGNED or draft.signature is None:
            self._record(actor, "writeback.commit.refused_unsigned", draft_id)
            raise WritebackError(
                f"Draft {draft_id!r} has no recorded human signature and "
                "does not commit (Invariant 13). There is no auto-commit "
                "path, including for content classified low-risk."
            )
        if _content_hash(draft.content) != draft.signature.content_hash:
            self._record(actor, "writeback.commit.refused_content_changed", draft_id)
            raise WritebackError(
                f"Draft {draft_id!r} content changed after signature; the "
                "signature covers exactly what the human saw. Re-stage and "
                "re-sign."
            )
        self._writer(draft)
        draft.status = DraftStatus.COMMITTED
        self._record(actor, "writeback.draft.committed", draft_id)
        return draft

    def get(self, draft_id: str) -> Optional[StagedDraft]:
        return self._drafts.get(draft_id)
# Made by Ryan Gomez & Co. Inc.
