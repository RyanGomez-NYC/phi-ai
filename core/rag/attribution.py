# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The attribution hard gate (SPEC §5.1h, §10, and the pre-existing
invariant: entity attribution is a hard gate).

Every chunk cited in an answer must belong to the queried patient, and
— when the session is encounter-scoped — to the queried encounter. The
gate is DETERMINISTIC string comparison on the references the
serializer preserved, which is why §10 can say: any wrong-patient chunk
that passes is a bug, not a tuning parameter. There is no similarity
threshold here and never will be; the cross-patient near-duplicate
fixture (same name, same DOB, different subject — §7.2 Layer 4) passes
through embedding space looking identical, and this gate is the thing
that catches it anyway, because it never looks at content at all.

Refuses rather than degrades: a violation raises and the whole answer
is withheld. Silently dropping the offending chunk would let a
generation built partly on wrong-patient content ship with the evidence
of that removed.
"""

from __future__ import annotations

from typing import Iterable, Optional

from core.rag.serialization import Chunk


class AttributionError(Exception):
    """A chunk failed the attribution gate. The message names the
    offending storage keys — keys only, never content, matching the
    audit rule (§6.8)."""


def assert_attribution(
    chunks: Iterable[Chunk],
    patient_reference: str,
    *,
    encounter_reference: Optional[str] = None,
    audit=None,
    actor: str = "system",
) -> tuple[Chunk, ...]:
    """
    Passes every chunk or raises. A chunk with NO subject reference
    fails the patient arm — unattributed content can't be proven to
    belong to this patient, and fail-closed is the posture everywhere.
    The encounter arm treats a chunk without an encounter reference as
    passing: patient-level resources (allergies, problem list) are
    legitimately encounter-less and remain in scope for an
    encounter-scoped question.
    """
    checked: list[Chunk] = []
    wrong_patient: list[str] = []
    wrong_encounter: list[str] = []

    for chunk in chunks:
        if chunk.subject_reference != patient_reference:
            wrong_patient.append(chunk.storage_key)
        elif (
            encounter_reference is not None
            and chunk.encounter_reference is not None
            and chunk.encounter_reference != encounter_reference
        ):
            wrong_encounter.append(chunk.storage_key)
        else:
            checked.append(chunk)

    if wrong_patient or wrong_encounter:
        if audit is not None:
            for key in wrong_patient + wrong_encounter:
                audit.record(
                    actor=actor,
                    action="attribution_gate.refused",
                    resource_key=key,
                    purpose_of_use="operations",
                )
        parts = []
        if wrong_patient:
            parts.append(
                f"subject mismatch (expected {patient_reference!r}): "
                + ", ".join(wrong_patient)
            )
        if wrong_encounter:
            parts.append(
                f"encounter mismatch (expected {encounter_reference!r}): "
                + ", ".join(wrong_encounter)
            )
        raise AttributionError(
            "Attribution gate refused the answer — " + "; ".join(parts)
        )

    return tuple(checked)
# Made by Ryan Gomez & Co. Inc.
