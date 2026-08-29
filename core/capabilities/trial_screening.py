# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Trial pre-screening (SPEC §5.12): match a patient corpus against a
trial's inclusion/exclusion criteria and output a COORDINATOR WORKLIST
— candidates with cited evidence for a human coordinator to evaluate,
never an enrollment decision (Invariant 19 bounds this exactly as it
bounds diagnosis: evidence retrieval, not clinical determination).

Access posture, per the spec: restricted role, distinct purpose tag
("research"), 45 CFR 164.512(i) basis — reviews preparatory to
research. The audit events this module emits carry that purpose, and
the caller is responsible for holding the restricted role; the §11
open item on drafting the 164.512(i) representations is the
implementing organization's (docs/COMPLIANCE.md, Responsibility
boundary).

Matching semantics, conservative in the coordinator's favor:
- An INCLUSION criterion with retrieved evidence counts toward
  candidacy, citations attached.
- An EXCLUSION criterion with retrieved evidence flags the patient as
  `excluded_pending_review` rather than silently dropping them — a
  lexical match on an exclusion is a reason for a human to look, not
  proof, and a screening tool that silently discards candidates is
  unauditable.
- Negated content (refuted / entered-in-error) never matches either
  direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.rag.retriever import GrantScope, retrieve
from core.rag.serialization import Chunk


@dataclass(frozen=True)
class TrialCriterion:
    criterion_id: str
    text: str
    query: str
    kind: str  # "inclusion" | "exclusion"

    def __post_init__(self):
        if self.kind not in ("inclusion", "exclusion"):
            raise ValueError(f"criterion kind must be inclusion/exclusion, got {self.kind!r}")


@dataclass(frozen=True)
class CriterionMatch:
    criterion: TrialCriterion
    citations: tuple[str, ...]
    excerpts: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    patient_reference: str
    inclusion_matches: tuple[CriterionMatch, ...]
    exclusion_matches: tuple[CriterionMatch, ...]

    @property
    def excluded_pending_review(self) -> bool:
        return bool(self.exclusion_matches)

    @property
    def inclusion_count(self) -> int:
        return len(self.inclusion_matches)


def screen(
    criteria: Sequence[TrialCriterion],
    chunks_by_patient: Mapping[str, Sequence[Chunk]],
    *,
    min_inclusion_matches: int = 1,
    k: int = 5,
    audit=None,
    actor: str = "trial-screening",
) -> tuple[Candidate, ...]:
    """Returns the coordinator worklist: candidates ordered by inclusion
    evidence (most first), each carrying every citation a coordinator
    needs to verify the match — and every exclusion flag found, shown,
    never silently applied."""
    inclusions = [c for c in criteria if c.kind == "inclusion"]
    exclusions = [c for c in criteria if c.kind == "exclusion"]
    if not inclusions:
        raise ValueError("screening requires at least one inclusion criterion")

    worklist: list[Candidate] = []
    for patient, chunks in sorted(chunks_by_patient.items()):
        scope = GrantScope(patient_reference=patient)

        def _matches(criterion: TrialCriterion) -> CriterionMatch | None:
            hits = [
                (c, s)
                for c, s in retrieve(criterion.query, chunks, scope, k=k)
                if c.verification_status not in ("refuted", "entered-in-error")
            ]
            if not hits:
                return None
            return CriterionMatch(
                criterion=criterion,
                citations=tuple(c.storage_key for c, _ in hits),
                excerpts=tuple(c.text for c, _ in hits),
            )

        inc = tuple(m for m in (_matches(c) for c in inclusions) if m)
        if len(inc) < min_inclusion_matches:
            continue
        exc = tuple(m for m in (_matches(c) for c in exclusions) if m)
        worklist.append(
            Candidate(
                patient_reference=patient,
                inclusion_matches=inc,
                exclusion_matches=exc,
            )
        )
        if audit is not None:
            audit.record(
                actor=actor,
                action="trial_screening.candidate_listed",
                resource_key=patient,
                purpose_of_use="research",
            )

    worklist.sort(key=lambda c: (-c.inclusion_count, c.patient_reference))
    return tuple(worklist)
# Made by Ryan Gomez & Co. Inc.
