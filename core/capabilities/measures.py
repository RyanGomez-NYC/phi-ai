# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Population structure and prevalence, with the interval always attached.

A PREVALENCE WITHOUT AN INTERVAL IS A RUMOUR. "14% of this population has
diabetes" reads identically whether it came from 40,000 patients or from
14. This module never returns a proportion on its own: every one arrives
as a Prevalence carrying its numerator, its denominator and a confidence
interval, and the renderers below always print all of it.

WILSON, NOT WALD. The textbook interval - p +/- 1.96*sqrt(p(1-p)/n) - is
the one everybody writes and it is wrong exactly where a clinical
population is most interesting: small subgroups and rare conditions. At
p=0 it produces the interval [0, 0], which asserts with total confidence
that a condition nobody in the sample has cannot occur. Wilson's score
interval does not degenerate there, needs no continuity fudge, and is a
few lines. There is no reason to ship the broken one.

NO NORMAL-DISTRIBUTION IMPORT. The 1.96 is the 95% z-score and it is
written as a named constant with its own comment, so nobody has to
wonder whether a stats dependency is doing something subtler.

AGE STANDARDISATION IS OFFERED, NEVER ASSUMED. Comparing two populations'
crude rates when their age structures differ is the most common way to
publish a false disparity. `standardise` does the direct method against a
supplied standard population; the caller supplies the standard, because
which one is right (US 2000, WHO World, the organization's own) is a
question about the comparison being made, not about arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

#: The 95% two-sided normal quantile. Named because a bare 1.96 in the
#: middle of an interval calculation is the kind of number people change
#: without realising what it is.
Z_95 = 1.959963984540054


class MeasureError(Exception):
    pass


@dataclass(frozen=True)
class Prevalence:
    """A proportion that cannot be quoted without its uncertainty."""

    label: str
    numerator: int
    denominator: int
    low: float
    high: float

    @property
    def point(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0

    @property
    def width(self) -> float:
        return self.high - self.low

    def as_text(self, value: float) -> str:
        """A percentage at enough precision to still be true.

        FOUND WHILE EXERCISING THIS: 14 cases in 100,000 rendered as
        "0.0%" at one decimal place - a real rate displayed as no rate at
        all, which is worse than the missing interval this class exists to
        prevent. The precision therefore follows the magnitude: a rate
        small enough to vanish at one decimal gets the decimals it needs,
        up to four, and only a genuine zero prints as 0%.
        """
        if value <= 0:
            return "0%"
        for places in (1, 2, 3, 4):
            rendered = f"{value:.{places}%}"
            if float(rendered.rstrip("%")) > 0:
                return rendered
        # Smaller than four decimal places can express. The bound is
        # written literally rather than formatted, because formatting the
        # proportion 0.0001 at .4% yields "0.0100%" - a hundred times the
        # intended bound, and the first version of this line said exactly
        # that.
        return "<0.0001%"

    def render(self) -> str:
        if not self.denominator:
            return f"{self.label}: no denominator - nothing to report"
        return (
            f"{self.label}: {self.as_text(self.point)} "
            f"(95% CI {self.as_text(self.low)}-{self.as_text(self.high)}; "
            f"{self.numerator}/{self.denominator})"
        )


def wilson_interval(numerator: int, denominator: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval. Defined at 0 and at n, unlike Wald.

    A zero numerator returns an interval with a real upper bound - the
    honest statement that a condition unobserved in n patients is not
    thereby impossible, only bounded. That single property is why this
    function exists rather than the two-line textbook one.
    """
    if denominator <= 0:
        raise MeasureError("a prevalence needs a denominator; 0 patients measures nothing")
    if numerator < 0 or numerator > denominator:
        raise MeasureError(
            f"numerator {numerator} outside 0..{denominator}; a proportion "
            "cannot exceed its denominator"
        )
    n = float(denominator)
    p = numerator / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / (1 + z2 / n)
    low, high = max(0.0, centre - half), min(1.0, centre + half)

    # SNAPPED AT THE ENDS. At numerator 0 the true lower bound is exactly
    # zero and at numerator n the true upper bound is exactly one, but
    # centre - half leaves float noise around 4e-19 - which the renderer
    # then honestly reports as "<0.0001%" rather than "0%". Snapping is
    # not cosmetic: a lower bound of 4e-19 says the rate is bounded away
    # from zero, and it is not.
    if numerator == 0:
        low = 0.0
    if numerator == denominator:
        high = 1.0
    return (low, high)


def prevalence(label: str, numerator: int, denominator: int) -> Prevalence:
    low, high = wilson_interval(numerator, denominator)
    return Prevalence(label=label, numerator=numerator, denominator=denominator,
                      low=low, high=high)


@dataclass(frozen=True)
class StandardisedRate:
    """A directly age-standardised rate and the crude rate beside it.

    BOTH ARE SHOWN, ALWAYS. A standardised rate quoted alone invites the
    reader to treat it as the real one; the pair is what tells them how
    much of the difference was age.
    """

    label: str
    crude: float
    standardised: float
    standard_population: str

    def render(self) -> str:
        return (
            f"{self.label}: crude {self.crude:.1%}, age-standardised "
            f"{self.standardised:.1%} (against {self.standard_population})"
        )


def standardise(
    label: str,
    strata: Mapping[str, tuple[int, int]],
    standard_weights: Mapping[str, float],
    *,
    standard_population: str = "supplied standard",
) -> StandardisedRate:
    """Direct standardisation. `strata` maps a band to (cases, population).

    REFUSES ON A STRATUM THE STANDARD DOES NOT COVER, rather than dropping
    it. Silently ignoring an age band the weights omit produces a rate
    that looks standardised, is not, and differs from the crude rate by an
    amount nobody can explain.
    """
    missing = [band for band in strata if band not in standard_weights]
    if missing:
        raise MeasureError(
            "the standard population has no weight for: " + ", ".join(sorted(missing))
            + "; standardising over a partial set of bands is not standardisation"
        )
    total_cases = sum(c for c, _ in strata.values())
    total_pop = sum(p for _, p in strata.values())
    if total_pop <= 0:
        raise MeasureError("no population across the supplied strata")

    weight_total = sum(standard_weights[b] for b in strata)
    if weight_total <= 0:
        raise MeasureError("the standard population weights sum to zero")

    expected = 0.0
    for band, (cases, pop) in strata.items():
        if pop <= 0:
            continue
        expected += (cases / pop) * (standard_weights[band] / weight_total)

    return StandardisedRate(
        label=label,
        crude=total_cases / total_pop,
        standardised=expected,
        standard_population=standard_population,
    )


@dataclass
class PopulationProfile:
    """What a population is, before any model says anything about it."""

    total: int
    prevalences: list[Prevalence]
    by_band: Mapping[str, int]

    def render(self) -> str:
        lines = [f"Population: {self.total:,} patients"]
        lines.extend("  " + p.render() for p in self.prevalences)
        return "\n".join(lines)


def profile(
    total_patients: int,
    condition_counts: Mapping[str, int],
    *,
    by_band: Optional[Mapping[str, int]] = None,
) -> PopulationProfile:
    """Prevalence for each named condition, each with its interval."""
    if total_patients < 0:
        raise MeasureError("a population cannot have a negative size")
    return PopulationProfile(
        total=total_patients,
        prevalences=[prevalence(name, count, total_patients)
                     for name, count in sorted(condition_counts.items())]
        if total_patients else [],
        by_band=dict(by_band or {}),
    )
# Made by Ryan Gomez & Co. Inc.
