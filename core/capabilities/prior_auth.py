# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Prior authorization and appeals (SPEC §5.4): given a payer criteria
set, retrieve and cite chart evidence supporting each criterion,
assemble the packet. Purpose tag Payment. Appeals are the same
pipeline with a different output template — so this module has ONE
pipeline and a `packet_kind` label, not two pipelines.

The honest rule that shapes the output: an UNMET criterion is listed
as unmet, with zero citations, prominently. A prior-auth packet that
pads thin evidence is precisely the artifact a payer's auditor — and
§5.5's false-claims analysis — reads against the submitter. The packet
renders what the chart supports and says plainly what it does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from core.rag.retriever import GrantScope, retrieve
from core.rag.serialization import Chunk


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    text: str  # the payer's wording, verbatim - the packet quotes it
    query: str  # retrieval query for chart evidence


@dataclass(frozen=True)
class CriterionEvidence:
    criterion: Criterion
    met: bool  # "evidence found", not a clinical judgment
    citations: tuple[str, ...]
    excerpts: tuple[str, ...]  # chunk texts - status banners included


@dataclass(frozen=True)
class Packet:
    packet_kind: str  # "prior_authorization" | "appeal"
    patient_reference: str
    evidence: tuple[CriterionEvidence, ...]

    @property
    def unmet(self) -> tuple[CriterionEvidence, ...]:
        return tuple(e for e in self.evidence if not e.met)

    def render(self) -> str:
        title = (
            "PRIOR AUTHORIZATION EVIDENCE PACKET"
            if self.packet_kind == "prior_authorization"
            else "APPEAL EVIDENCE PACKET"
        )
        lines = [title, f"Patient: {self.patient_reference}", ""]
        if self.unmet:
            lines.append(
                f"UNMET CRITERIA ({len(self.unmet)}) — no supporting chart "
                "evidence retrieved; review before submitting:"
            )
            for item in self.unmet:
                lines.append(f"  [{item.criterion.criterion_id}] {item.criterion.text}")
            lines.append("")
        for item in self.evidence:
            if not item.met:
                continue
            lines.append(f"[{item.criterion.criterion_id}] {item.criterion.text}")
            for excerpt, key in zip(item.excerpts, item.citations):
                lines.append(f"  - {excerpt} [cite: {key}]")
            lines.append("")
        return "\n".join(lines).rstrip()


def assemble_packet(
    criteria: Iterable[Criterion],
    chunks: Sequence[Chunk],
    *,
    patient_reference: str,
    packet_kind: str = "prior_authorization",
    k: int = 5,
    audit=None,
    actor: str = "prior-auth",
) -> Packet:
    """Purpose is Payment by definition here; the audit event says so.
    Negated (refuted / entered-in-error) chunks are never offered as
    supporting evidence — their banner text makes them useless to a
    payer anyway, and offering them would be exactly the
    status-inversion failure §10 counts."""
    if packet_kind not in ("prior_authorization", "appeal"):
        raise ValueError(f"unknown packet kind {packet_kind!r}")
    scope = GrantScope(patient_reference=patient_reference)
    evidence: list[CriterionEvidence] = []
    for criterion in criteria:
        hits = [
            (chunk, score)
            for chunk, score in retrieve(criterion.query, chunks, scope, k=k)
            if chunk.verification_status not in ("refuted", "entered-in-error")
        ]
        evidence.append(
            CriterionEvidence(
                criterion=criterion,
                met=bool(hits),
                citations=tuple(c.storage_key for c, _ in hits),
                excerpts=tuple(c.text for c, _ in hits),
            )
        )
    if audit is not None:
        audit.record(
            actor=actor,
            action=f"{packet_kind}.packet_assembled",
            resource_key=patient_reference,
            purpose_of_use="payment",
        )
    return Packet(
        packet_kind=packet_kind,
        patient_reference=patient_reference,
        evidence=tuple(evidence),
    )
# Made by Ryan Gomez & Co. Inc.
