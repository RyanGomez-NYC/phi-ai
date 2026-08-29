# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Inbox and message triage (SPEC §5.6): routing plus draft, never
auto-send.

The two constraints the market form omits, implemented rather than
promised:

1. **Conservatively biased toward escalation.** A missed urgent
   message is a patient-safety event, not a precision statistic. The
   operating point is CHOSEN ON RECALL OF THE URGENT CLASS:
   choose_operating_point() takes the minimum acceptable urgent-class
   recall and returns the threshold that achieves it on the validation
   set — precision is whatever it then is. The resulting miss rate is
   a PUBLISHED field of the operating point, monitored and reported
   (§10), never an implicit consequence of a tuned number.
2. **Triage is registered decision support** (Invariant 14 / §6.3):
   route() takes the ModelRegistry and refuses to route with an
   unregistered scorer, because triage priority that varies in effect
   by protected class or a proxy is squarely within 45 CFR 92.210.

Drafting is not here at all: a reply draft goes through
core/governance/release_gate.PatientReleaseGate like every other piece
of patient-directed output (Invariant 17). This module decides queue
order, and only queue order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.governance.registry import ModelRegistry


class TriageError(Exception):
    pass


@dataclass(frozen=True)
class OperatingPoint:
    """threshold plus its PUBLISHED validation-set performance — the
    miss rate on the urgent class is a first-class number, not a
    footnote."""

    threshold: float
    urgent_recall: float
    urgent_miss_rate: float
    routine_escalation_rate: float  # the precision cost, stated plainly
    validation_set_size: int


def choose_operating_point(
    scores: Sequence[float],
    is_urgent: Sequence[bool],
    *,
    min_urgent_recall: float = 0.98,
) -> OperatingPoint:
    """
    Picks the HIGHEST threshold whose urgent-class recall on the
    validation set still meets `min_urgent_recall`; when no threshold
    does, the threshold is 0.0 — every message escalates, which is the
    correct failure mode for a scorer that cannot find the urgent
    class. Deterministic; ties resolve toward escalation.
    """
    if len(scores) != len(is_urgent) or not scores:
        raise TriageError("scores and labels must be same-length and non-empty")
    urgent_scores = sorted(
        (s for s, u in zip(scores, is_urgent) if u), reverse=True
    )
    if not urgent_scores:
        raise TriageError(
            "validation set contains no urgent examples; an operating "
            "point chosen without them would be fiction"
        )

    total_urgent = len(urgent_scores)
    need = min_urgent_recall * total_urgent
    # Walk candidate thresholds from high to low until enough urgent
    # examples clear the bar.
    threshold = 0.0
    for candidate in sorted(set(scores), reverse=True):
        caught = sum(1 for s in urgent_scores if s >= candidate)
        if caught >= need:
            threshold = candidate
            break

    caught = sum(1 for s in urgent_scores if s >= threshold)
    recall = caught / total_urgent
    routine = [(s, u) for s, u in zip(scores, is_urgent) if not u]
    escalated_routine = sum(1 for s, _ in routine if s >= threshold)
    return OperatingPoint(
        threshold=threshold,
        urgent_recall=recall,
        urgent_miss_rate=1.0 - recall,
        routine_escalation_rate=(escalated_routine / len(routine)) if routine else 0.0,
        validation_set_size=len(scores),
    )


@dataclass(frozen=True)
class RoutingDecision:
    message_key: str
    escalate: bool
    score: float


def route(
    message_key: str,
    score: float,
    operating_point: OperatingPoint,
    *,
    registry: ModelRegistry,
    model_id: str,
    actor: str = "triage",
    audit=None,
) -> RoutingDecision:
    """Refuses to route with an unregistered scorer — raises
    UnregisteredModelError from the registry's own gate. Escalation is
    >= threshold: the boundary case escalates, per the bias."""
    registry.ensure_executable(model_id, actor=actor)
    decision = RoutingDecision(
        message_key=message_key, escalate=score >= operating_point.threshold, score=score
    )
    if audit is not None:
        audit.record(
            actor=actor,
            action="triage.routed."
            + ("urgent" if decision.escalate else "routine"),
            resource_key=message_key,
            purpose_of_use="treatment",
        )
    return decision
# Made by Ryan Gomez & Co. Inc.
