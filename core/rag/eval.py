# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The §10 validation metrics, as executable checks.

Every metric here runs against the synthetic corpus (docs/TESTDATA.md)
and is therefore a LOWER BOUND ON ERROR — read SPEC §7.7 before quoting
any number this module produces. Gate behavior is where synthetic
measurement carries real weight, and that is what these metrics watch:

- **Silent omission rate** — §10's primary acceptance metric: an
  active medication, non-negated allergy, or active problem present in
  the raw resources but absent from the spine. Invisible to
  hallucination metrics; measured here by construction, because the
  ground truth is derived from the same resources the spine consumed.
- **Status inversion rate** — resolved / refuted / entered-in-error
  content whose chunk text fails to carry its status, or negated
  content that ranks above zero in current-question mode. Target zero.
- **Attribution false negatives** — wrong-patient chunks injected
  deliberately; any that pass the gate is a bug, not a tuning
  parameter (the gate is deterministic). Target zero, asserted zero.
- **Retrieval recall@k** — known-answer probes built from each chunk's
  own code displays.
- **Abstention correctness** — questions with no responsive content
  must abstain; questions with responsive content must not.

The ground truth is derived FROM the input resources, not hand-listed
beside them, so adding a fixture (or pointing the harness at a
25-patient Synthea corpus) extends the measurement with no bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from core.rag.attribution import AttributionError, assert_attribution
from core.rag.retriever import GrantScope, retrieve
from core.rag.serialization import (
    NEGATING_VERIFICATIONS,
    Chunk,
)
from core.rag.spine import build_spine
from core.rag.temporal import rank


@dataclass
class MetricsReport:
    # silent omission
    expected_spine_entries: int = 0
    omitted_spine_entries: int = 0
    omitted_keys: list[str] = field(default_factory=list)
    # status inversion
    status_bearing_chunks: int = 0
    status_inversions: int = 0
    inversion_keys: list[str] = field(default_factory=list)
    # attribution
    attribution_probes: int = 0
    attribution_false_negatives: int = 0
    # recall
    recall_probes: int = 0
    recall_hits: int = 0
    # abstention
    abstention_probes: int = 0
    abstention_failures: int = 0

    @property
    def silent_omission_rate(self) -> float:
        return (
            self.omitted_spine_entries / self.expected_spine_entries
            if self.expected_spine_entries
            else 0.0
        )

    @property
    def status_inversion_rate(self) -> float:
        return (
            self.status_inversions / self.status_bearing_chunks
            if self.status_bearing_chunks
            else 0.0
        )

    @property
    def recall_at_k(self) -> float:
        return self.recall_hits / self.recall_probes if self.recall_probes else 1.0

    def render(self) -> str:
        return "\n".join(
            [
                "§10 metrics (synthetic corpus — lower bound on error, SPEC §7.7):",
                f"  silent omission rate:    {self.silent_omission_rate:.4f} "
                f"({self.omitted_spine_entries}/{self.expected_spine_entries})"
                + (f"  OMITTED: {self.omitted_keys}" if self.omitted_keys else ""),
                f"  status inversion rate:   {self.status_inversion_rate:.4f} "
                f"({self.status_inversions}/{self.status_bearing_chunks}) — target zero"
                + (f"  INVERTED: {self.inversion_keys}" if self.inversion_keys else ""),
                f"  attribution false negs:  {self.attribution_false_negatives}"
                f"/{self.attribution_probes} — any nonzero is a bug",
                f"  retrieval recall@k:      {self.recall_at_k:.4f} "
                f"({self.recall_hits}/{self.recall_probes})",
                f"  abstention failures:     {self.abstention_failures}"
                f"/{self.abstention_probes}",
            ]
        )


def _ground_truth_spine_keys(resources_by_key: Mapping[str, Mapping]) -> set[str]:
    """What the spine MUST contain, derived from the raw resources:
    active MedicationRequest/Statement, non-negated AllergyIntolerance,
    and Conditions not marked refuted/entered-in-error."""
    expected: set[str] = set()
    for key, resource in resources_by_key.items():
        rtype = resource.get("resourceType")
        if rtype in ("MedicationRequest", "MedicationStatement"):
            if resource.get("status") == "active":
                expected.add(key)
        elif rtype in ("Condition", "AllergyIntolerance"):
            verification = ""
            node = resource.get("verificationStatus")
            if isinstance(node, dict):
                codings = node.get("coding", [])
                verification = codings[0].get("code", "") if codings else ""
            if verification not in NEGATING_VERIFICATIONS:
                expected.add(key)
    return expected


def measure_silent_omission(
    chunks: Sequence[Chunk],
    resources_by_key: Mapping[str, Mapping],
    report: MetricsReport,
) -> None:
    spine = build_spine(list(chunks), dict(resources_by_key))
    spine_keys = spine.citation_keys() | {
        k for s in spine.labs for k in s.storage_keys
    }
    chunk_keys = {c.storage_key for c in chunks}
    # Only resources that survived serialization can appear; a
    # segmentation exclusion is not an omission, it is the point.
    expected = _ground_truth_spine_keys(resources_by_key) & chunk_keys
    report.expected_spine_entries += len(expected)
    missing = sorted(expected - spine_keys)
    report.omitted_spine_entries += len(missing)
    report.omitted_keys.extend(missing)


def measure_status_inversion(
    chunks: Sequence[Chunk], anchor: date, report: MetricsReport
) -> None:
    for chunk in chunks:
        if chunk.verification_status in NEGATING_VERIFICATIONS:
            report.status_bearing_chunks += 1
            text_ok = chunk.text.startswith(
                f"[{chunk.verification_status.upper()}"
            )
            ranked = rank([(chunk, 1.0)], anchor=anchor)
            rank_ok = ranked[0].score == 0.0
            if not (text_ok and rank_ok):
                report.status_inversions += 1
                report.inversion_keys.append(chunk.storage_key)
        elif chunk.clinical_status in ("resolved", "inactive", "remission"):
            report.status_bearing_chunks += 1
            if f"status: {chunk.clinical_status}" not in chunk.text:
                report.status_inversions += 1
                report.inversion_keys.append(chunk.storage_key)


def measure_attribution(
    chunks_by_patient: Mapping[str, Sequence[Chunk]], report: MetricsReport
) -> None:
    """Every patient's chunk set is probed against every OTHER
    patient's identity: each probe must raise. A probe that passes is
    counted, and the caller's test asserts the count is zero."""
    patients = sorted(chunks_by_patient)
    for victim in patients:
        for other in patients:
            if other == victim or not chunks_by_patient[other]:
                continue
            report.attribution_probes += 1
            try:
                assert_attribution(
                    list(chunks_by_patient[other])[:1], victim
                )
                report.attribution_false_negatives += 1
            except AttributionError:
                pass


def measure_recall(
    chunks: Sequence[Chunk],
    patient_reference: str,
    report: MetricsReport,
    *,
    k: int = 10,
    max_probes: int | None = None,
) -> None:
    """Known-answer probes: query each chunk by its own first code
    display; the chunk must appear in its patient's top-k. `max_probes`
    caps the per-patient probe count — the probe set is quadratic in
    chunk count, and probe 1,500 of a large Synthea patient proves
    nothing probe 50 didn't."""
    scope = GrantScope(patient_reference=patient_reference)
    probes = 0
    for chunk in chunks:
        if max_probes is not None and probes >= max_probes:
            break
        if chunk.subject_reference != patient_reference or not chunk.codes:
            continue
        display = chunk.codes[0][2]
        if not display or display == "no codes recorded":
            continue
        probes += 1
        report.recall_probes += 1
        hits = retrieve(display, chunks, scope, k=k)
        if any(c.storage_key == chunk.storage_key for c, _ in hits):
            report.recall_hits += 1


def measure_abstention(
    chunks: Sequence[Chunk], patient_reference: str, report: MetricsReport
) -> None:
    """A query with no responsive tokens must return nothing (which the
    pipeline turns into abstention); a query with a known answer must
    not come back empty."""
    scope = GrantScope(patient_reference=patient_reference)
    report.abstention_probes += 1
    if retrieve("zzqx unmatched veterinary phlogiston", chunks, scope):
        report.abstention_failures += 1
    answerable = [c for c in chunks if c.subject_reference == patient_reference and c.codes]
    if answerable:
        report.abstention_probes += 1
        probe = answerable[0].codes[0][2]
        if probe and not retrieve(probe, chunks, scope):
            report.abstention_failures += 1


def run_all(
    resources_by_key: Mapping[str, Mapping],
    chunks: Sequence[Chunk],
    *,
    anchor: date,
    max_recall_probes_per_patient: int | None = 50,
) -> MetricsReport:
    report = MetricsReport()
    by_patient: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        if chunk.subject_reference:
            by_patient.setdefault(chunk.subject_reference, []).append(chunk)

    for patient, patient_chunks in sorted(by_patient.items()):
        patient_keys = {c.storage_key for c in patient_chunks}
        patient_resources = {
            k: r for k, r in resources_by_key.items() if k in patient_keys
        }
        measure_silent_omission(patient_chunks, patient_resources, report)
        measure_recall(
            patient_chunks, patient, report,
            max_probes=max_recall_probes_per_patient,
        )
        measure_abstention(patient_chunks, patient, report)

    measure_status_inversion(list(chunks), anchor, report)
    measure_attribution(by_patient, report)
    return report
# Made by Ryan Gomez & Co. Inc.
