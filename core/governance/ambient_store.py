# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Ambient audio store contract (Invariant 15: ambient audio is a
first-class PHI store — own CMK, own grant, own retention schedule
distinct from the note it produces).

The decision core over an injected blob writer/deleter, in the same
pattern the psychotherapy store set (core/storage/factory.py's separate
client that refuses to fall back to the general store): this class
never touches bytes itself, and the deployment supplies `write` /
`delete` callables bound to the DEDICATED ambient bucket and key — the
constructor refuses a store that self-reports as the general one,
because "we reused the general bucket for audio" is exactly the
configuration Invariant 15 exists to prevent.

Every capture is admitted only through a CaptureDecision from
core/governance/consent_gate.evaluate_capture — the decision object is
the ticket, not a boolean the caller could fabricate more easily than
re-evaluate — and revocation deletes the audio AND the derived
transcript under the retention schedule, audited (§6.5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.governance.consent_gate import CaptureDecision


class AmbientStoreError(Exception):
    pass


@dataclass(frozen=True)
class AmbientObject:
    audio_key: str
    encounter_key: str
    captured_at: str
    #: days, from the AMBIENT schedule — deliberately its own number,
    #: never inherited from the note's schedule (Invariant 15).
    retention_days: int
    transcript_key: Optional[str] = None


class AmbientAudioStore:
    def __init__(
        self,
        write,
        delete,
        *,
        store_label: str,
        retention_days: int,
        audit=None,
    ):
        if "general" in store_label.lower():
            raise AmbientStoreError(
                "Ambient audio requires its own store with its own CMK and "
                "grant; refusing a store labeled as the general PHI store "
                "(Invariant 15)"
            )
        if retention_days <= 0:
            raise AmbientStoreError(
                "Ambient retention must be a positive, deliberate number of "
                "days — it is a policy decision, not a default"
            )
        self._write = write
        self._delete = delete
        self._retention_days = retention_days
        self._audit = audit
        self._objects: dict[str, AmbientObject] = {}

    def _record(self, actor: str, action: str, key: str) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=actor,
                action=action,
                resource_key=key,
                purpose_of_use="treatment",
            )

    def store_capture(
        self,
        audio: bytes,
        *,
        encounter_key: str,
        decision: CaptureDecision,
        actor: str,
    ) -> AmbientObject:
        """The only write path, and it takes the consent gate's own
        decision object. A refused decision refuses storage — audio
        that should never have been captured is certainly never
        persisted."""
        if not decision.allowed:
            raise AmbientStoreError(
                f"Capture was refused by the consent gate ({decision.reason}); "
                "refusing to store audio for it"
            )
        obj = AmbientObject(
            audio_key=f"ambient/{encounter_key}/{uuid.uuid4().hex}.audio",
            encounter_key=encounter_key,
            captured_at=datetime.now(timezone.utc).isoformat(),
            retention_days=self._retention_days,
        )
        self._write(obj.audio_key, audio)
        self._objects[obj.audio_key] = obj
        self._record(actor, "ambient.audio_stored", obj.audio_key)
        return obj

    def attach_transcript(self, audio_key: str, transcript_key: str) -> AmbientObject:
        """Links the STT output so revocation can find it. The
        transcript itself lives wherever the note pipeline put it; the
        link is what makes §6.5's 'deletes the audio and the derived
        transcript' executable rather than aspirational."""
        obj = self._objects.get(audio_key)
        if obj is None:
            raise AmbientStoreError(f"no ambient object {audio_key!r}")
        updated = AmbientObject(
            audio_key=obj.audio_key,
            encounter_key=obj.encounter_key,
            captured_at=obj.captured_at,
            retention_days=obj.retention_days,
            transcript_key=transcript_key,
        )
        self._objects[audio_key] = updated
        return updated

    def delete_for_revocation(self, audio_key: str, *, actor: str) -> tuple[str, ...]:
        """§6.5 revocation: the audio and the derived transcript go,
        and both deletions are audited. Returns the keys deleted."""
        obj = self._objects.get(audio_key)
        if obj is None:
            raise AmbientStoreError(f"no ambient object {audio_key!r}")
        deleted = [obj.audio_key]
        self._delete(obj.audio_key)
        self._record(actor, "ambient.audio_deleted_revocation", obj.audio_key)
        if obj.transcript_key:
            self._delete(obj.transcript_key)
            self._record(actor, "ambient.transcript_deleted_revocation", obj.transcript_key)
            deleted.append(obj.transcript_key)
        del self._objects[audio_key]
        return tuple(deleted)
# Made by Ryan Gomez & Co. Inc.
