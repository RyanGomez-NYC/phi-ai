# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The answer contract (SPEC §5.1h) and the Invariant 19 refusal path
(§5.1i).

Contract, enforced here rather than promised in a prompt:

- Every claim carries ≥1 citation, and every citation resolves to a
  storage key that was actually retrieved (or sits in the structured
  spine) for THIS question. A model output whose claims don't meet
  that is refused — the model is asked again or the user gets an
  error, never an uncited assertion.
- The attribution gate runs before release, over exactly the chunks
  the claims cite.
- Empty retrieval produces ABSTENTION, never a parametric-knowledge
  answer. There is deliberately no parameter on assemble_answer() that
  disables abstention — the spec says the abstention default is not
  operator-disableable, and the reliable way to make a knob
  un-turnable is to not build it.
- Purpose-of-use is declared per session and travels with the answer;
  the caller's retrieval scope was already bounded by it upstream
  (grant-bounded pre-filters, 5.1d), and the audit event carries it.

Invariant 19: differential-diagnosis-shaped requests are refused, and
the refusal NAMES the supported alternative — hypothesis-directed
evidence retrieval — because a refusal that teaches the working path
gets followed, and one that doesn't gets worked around.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from core.rag.attribution import assert_attribution
from core.rag.serialization import Chunk


class AnswerContractError(Exception):
    pass


class PurposeOfUse(Enum):
    TREATMENT = "treatment"
    PAYMENT = "payment"
    OPERATIONS = "operations"
    RESEARCH = "research"


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[str, ...]  # storage keys


@dataclass(frozen=True)
class Answer:
    claims: tuple[Claim, ...]
    purpose: PurposeOfUse
    patient_reference: str
    cited_keys: frozenset[str]


@dataclass(frozen=True)
class Abstention:
    """Not an error: the honest answer when the record holds nothing
    responsive. Renders as itself, never as prose that could be
    mistaken for a finding."""

    reason: str
    purpose: PurposeOfUse
    patient_reference: str


#: Invariant 19's refusal, verbatim enough to be consistent everywhere
#: it appears. The second sentence is the load-bearing one.
DIFFERENTIAL_REFUSAL = (
    "This platform does not generate, rank, or order differential "
    "diagnoses, and does not produce a specific diagnostic or treatment "
    "directive (Invariant 19). The supported alternative is "
    "hypothesis-directed evidence retrieval: state the hypothesis — for "
    'example, "is there anything in this chart relevant to amyloidosis?" '
    "— and the platform will return cited evidence consistent and "
    "inconsistent with it."
)


def refuse_differential() -> str:
    return DIFFERENTIAL_REFUSAL


def assemble_answer(
    claims: Iterable[Claim],
    retrieved: Iterable[Chunk],
    *,
    patient_reference: str,
    purpose: PurposeOfUse,
    spine_keys: frozenset[str] = frozenset(),
    encounter_reference: Optional[str] = None,
    audit=None,
    actor: str = "assistant",
) -> Answer | Abstention:
    """
    The release gate for one generated answer. Raises
    AnswerContractError on a contract violation (uncited claim, or a
    citation of something never retrieved); returns Abstention when
    there was nothing to answer from. AttributionError propagates from
    the gate — a wrong-patient citation withholds the whole answer.
    """
    retrieved = tuple(retrieved)
    claims = tuple(claims)

    if not retrieved and not spine_keys:
        if audit is not None:
            audit.record(
                actor=actor,
                action="answer.abstained_empty_retrieval",
                resource_key=patient_reference,
                purpose_of_use=purpose.value,
            )
        return Abstention(
            reason="No responsive content was retrieved from this patient's "
            "record; the assistant does not answer from model memory.",
            purpose=purpose,
            patient_reference=patient_reference,
        )

    retrieved_by_key = {c.storage_key: c for c in retrieved}
    allowed_keys = frozenset(retrieved_by_key) | spine_keys

    uncited = [c.text for c in claims if not c.citations]
    if uncited:
        raise AnswerContractError(
            f"{len(uncited)} claim(s) carry no citation; every claim cites "
            "at least one storage key (SPEC §5.1h)"
        )

    unknown = sorted(
        {key for c in claims for key in c.citations} - allowed_keys
    )
    if unknown:
        raise AnswerContractError(
            "Citations reference content that was never retrieved for this "
            f"question: {', '.join(unknown)}. A citation is evidence, not "
            "decoration."
        )

    # Attribution gate, over exactly the retrieved chunks the claims
    # cite. Spine keys were built from attribution-checked chunks
    # upstream; retrieved citations are re-checked here at release.
    cited_chunks = [
        retrieved_by_key[key]
        for c in claims
        for key in c.citations
        if key in retrieved_by_key
    ]
    assert_attribution(
        cited_chunks,
        patient_reference,
        encounter_reference=encounter_reference,
        audit=audit,
        actor=actor,
    )

    cited_keys = frozenset(key for c in claims for key in c.citations)
    if audit is not None:
        audit.record(
            actor=actor,
            action="answer.released",
            resource_key=patient_reference,
            purpose_of_use=purpose.value,
        )
    return Answer(
        claims=claims,
        purpose=purpose,
        patient_reference=patient_reference,
        cited_keys=cited_keys,
    )
# Made by Ryan Gomez & Co. Inc.
