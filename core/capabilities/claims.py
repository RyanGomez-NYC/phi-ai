# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Denial risk on a pending claim, scored from this store's own history.

EVERY FACTOR IS PRINTED AND EVERY FACTOR CITES ITS COUNTS. The model is a
transparent additive one - not because a gradient-boosted model would
score worse, but because the output of this screen is an argument a
biller makes to a payer, and an argument whose premises cannot be
inspected is not one. A risk score nobody can take apart is a rumour with
a decimal point.

IT IS FITTED ON THIS DEPLOYMENT'S OWN ADJUDICATED HISTORY, and where it
has no history it says so instead of guessing. A payer the store has
never seen adjudicate anything gets NO score - an "average" denial rate
borrowed from other payers is not a fact about this one, and a biller who
acts on it is acting on a number the platform invented.

IT IS ADVISORY AND IT SAYS SO. Nothing here withholds a claim, reorders a
queue, or decides anything. A denial-risk score that silently
deprioritised low-value claims would be an operational prediction driving
a restrictive action, which core/governance/action_space.py exists to
refuse - and this module produces no action at all, only a number and its
reasons.

PAYMENT IS THE PURPOSE. Reading claim history is a disclosure under
Payment and its callers audit it as one, exactly as a chart view is
audited under Treatment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

#: A factor contributes at most this much, so no single signal can carry
#: a score on its own. A model where one factor reaches 100 is a lookup
#: table with extra steps.
MAX_FACTOR_POINTS = 40

#: Below this many adjudicated claims, a rate is not a rate. The screen
#: reports "insufficient history" rather than a score computed from four
#: observations and quoted to a decimal place.
MIN_HISTORY = 10


class ClaimsError(Exception):
    pass


@dataclass(frozen=True)
class Factor:
    """One additive contribution, with the counts that produced it."""

    name: str
    points: int
    numerator: int
    denominator: int
    explanation: str

    @property
    def rate(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0

    def render(self) -> str:
        return (
            f"{self.name}: +{self.points} "
            f"({self.numerator}/{self.denominator} = {self.rate:.1%}) - {self.explanation}"
        )


@dataclass
class DenialRisk:
    claim_id: str
    factors: list[Factor] = field(default_factory=list)
    #: Set when the store holds too little adjudicated history to score.
    insufficient_history: Optional[str] = None

    @property
    def score(self) -> int:
        """0-100, the sum of the factors, clamped.

        Clamped rather than normalised: a claim that trips every factor is
        not more than certain, and rescaling would change what each
        printed factor contributed - which is the one thing this design
        promises stays legible.
        """
        return max(0, min(100, sum(f.points for f in self.factors)))

    @property
    def band(self) -> str:
        if self.insufficient_history:
            return "unscored"
        return "high" if self.score >= 60 else ("medium" if self.score >= 35 else "low")

    def render(self) -> str:
        if self.insufficient_history:
            return f"{self.claim_id}: unscored - {self.insufficient_history}"
        lines = [f"{self.claim_id}: {self.score} ({self.band})"]
        lines.extend("  " + f.render() for f in self.factors)
        return "\n".join(lines)


@dataclass(frozen=True)
class History:
    """Adjudicated outcomes this deployment has actually seen.

    Each mapping is key -> (denied, adjudicated). Nothing is defaulted:
    an absent key means no history, which is a different answer from a
    zero denial rate and is reported differently.
    """

    by_payer: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    by_service_line: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: Claims previously denied for missing documentation, by service line.
    documentation_denials: Mapping[str, tuple[int, int]] = field(default_factory=dict)


def _factor(name: str, counts: Optional[tuple[int, int]], explanation: str) -> Optional[Factor]:
    if not counts:
        return None
    denied, total = counts
    if total < MIN_HISTORY:
        return None
    rate = denied / total if total else 0.0
    return Factor(
        name=name,
        points=int(round(rate * MAX_FACTOR_POINTS)),
        numerator=denied,
        denominator=total,
        explanation=explanation,
    )


def score_claim(
    claim_id: str,
    *,
    payer: str,
    service_line: str,
    history: History,
    documented: bool = True,
) -> DenialRisk:
    """Score one pending claim, or decline to.

    `documented` is whether clinical documentation supporting the line is
    on file - the same question core/capabilities/coding_integrity.py asks
    in its second direction, and the one factor here that is about this
    claim rather than about the population it resembles.
    """
    risk = DenialRisk(claim_id=claim_id)

    payer_counts = history.by_payer.get(payer)
    line_counts = history.by_service_line.get(service_line)
    if not payer_counts and not line_counts:
        risk.insufficient_history = (
            f"this store holds no adjudicated history for payer {payer!r} or "
            f"service line {service_line!r}; a denial rate borrowed from other "
            "payers is not a fact about this one"
        )
        return risk

    for factor in (
        _factor(f"payer {payer}", payer_counts,
                "this payer's denial rate across the adjudicated history"),
        _factor(f"service line {service_line}", line_counts,
                "this service line's denial rate across the adjudicated history"),
        _factor(f"documentation denials on {service_line}",
                history.documentation_denials.get(service_line),
                "share of this line's denials that cited missing documentation"),
    ):
        if factor is not None:
            risk.factors.append(factor)

    if not risk.factors:
        risk.insufficient_history = (
            f"fewer than {MIN_HISTORY} adjudicated claims behind every factor; "
            "a rate computed from a handful of observations is not a rate"
        )
        return risk

    if not documented:
        # The one claim-specific factor. Full weight, because a line with
        # no documentation on file is not a statistical concern about
        # claims like this one - it is a defect in this one.
        risk.factors.append(Factor(
            name="no documentation on file",
            points=MAX_FACTOR_POINTS,
            numerator=1, denominator=1,
            explanation="no clinical documentation in the chart supports this line",
        ))
    return risk
# Made by Ryan Gomez & Co. Inc.
