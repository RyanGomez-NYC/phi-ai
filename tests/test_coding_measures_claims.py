# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The three engines written to close the last of the parity gap.

coding_integrity, measures and claims are new: the demonstration had these
screens and the platform had neither screen nor logic behind them. Each
one encodes a decision that is easy to get wrong in a way nobody notices,
and those are what these tests pin:

- CODING READS BOTH WAYS. A tool that only finds diagnoses to add is
  upcoding with better branding, and the failure is invisible because
  every finding it produces looks like a good one.
- A PREVALENCE WITHOUT AN INTERVAL IS A RUMOUR, and the textbook interval
  is broken exactly where a clinical population is most interesting.
- A DENIAL RATE BORROWED FROM ANOTHER PAYER IS NOT A FACT. Declining to
  score is the correct answer far more often than an industry average is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.capabilities.claims import (  # noqa: E402
    MAX_FACTOR_POINTS,
    MIN_HISTORY,
    History,
    score_claim,
)
from core.capabilities.coding_integrity import analyze  # noqa: E402
from core.capabilities.measures import (  # noqa: E402
    MeasureError,
    prevalence,
    profile,
    standardise,
    wilson_interval,
)


# ---------------------------------------------------------------------------
# Coding integrity
# ---------------------------------------------------------------------------

def _obs(code, display="A1c"):
    return {"resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": code,
                                 "display": display}]}}


def _condition(code):
    return {"resourceType": "Condition",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]}}


def test_evidence_without_a_matching_problem_is_a_gap():
    report = analyze({"fhir/Observation/o1.json": _obs("4548-4")})
    assert [u.code for u in report.uncoded_support] == ["4548-4"]
    assert "problem list does not include it" in report.uncoded_support[0].query


def test_evidence_matched_by_the_problem_list_is_not_a_gap():
    report = analyze({
        "fhir/Observation/o1.json": _obs("4548-4"),
        "fhir/Condition/c1.json": _condition("4548-4"),
    })
    assert report.uncoded_support == []


def test_a_billed_line_with_no_documentation_at_all_is_a_compliance_gap():
    report = analyze({
        "fhir/ExplanationOfBenefit/e1.json": {
            "resourceType": "ExplanationOfBenefit",
            "item": [{"productOrService": {"coding": [
                {"system": "http://www.ama-assn.org/go/cpt", "code": "99215"}]}}],
        },
    })
    assert [b.code for b in report.unsupported_bills] == ["99215"]
    assert "What supports this line" in report.unsupported_bills[0].query


def test_a_billed_line_with_documentation_on_file_is_not_flagged():
    report = analyze({
        "fhir/ExplanationOfBenefit/e1.json": {
            "resourceType": "ExplanationOfBenefit",
            "item": [{"productOrService": {"coding": [{"system": "cpt", "code": "99215"}]}}],
        },
        "fhir/DocumentReference/d1.json": {"resourceType": "DocumentReference"},
    })
    assert report.unsupported_bills == []


def test_the_report_can_say_whether_it_found_anything_in_each_direction():
    """A deployment that only ever sees the revenue-positive list should be
    able to notice that about itself."""
    one_way = analyze({"fhir/Observation/o1.json": _obs("4548-4")})
    assert one_way.uncoded_support and not one_way.unsupported_bills
    assert one_way.both_directions is False


def test_a_missing_support_map_is_reported_rather_than_implied_clean():
    report = analyze({"fhir/Observation/o1.json": _obs("4548-4")})
    assert report.support_map_used is False


def test_a_support_map_lets_evidence_answer_for_a_different_code():
    report = analyze(
        {"fhir/Observation/o1.json": _obs("4548-4"),
         "fhir/Condition/c1.json": _condition("E11.9")},
        support_map={"4548-4": ["E11.9"]},
    )
    assert report.uncoded_support == []
    assert report.support_map_used is True


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------

def test_zero_cases_does_not_mean_zero_risk():
    """Wald returns [0, 0] here, asserting with total confidence that a
    condition nobody in the sample has cannot occur. That single failure
    is why this module exists."""
    low, high = wilson_interval(0, 500)
    assert low == 0.0
    assert high > 0.0, "a condition unobserved in 500 patients is bounded, not impossible"


def test_every_case_does_not_mean_certainty():
    low, high = wilson_interval(500, 500)
    assert high == 1.0 and low < 1.0


def test_the_interval_narrows_as_the_denominator_grows():
    assert prevalence("x", 14, 100).width > prevalence("x", 140, 1000).width


def test_a_rate_too_small_for_one_decimal_is_not_printed_as_zero():
    """14 in 100,000 rendered as "0.0%" is a real rate displayed as no rate
    at all - worse than the missing interval this class exists to prevent."""
    text = prevalence("x", 14, 100_000).render()
    assert "0.0%" not in text.split("(")[0]
    assert "0.01%" in text


def test_a_genuine_zero_still_prints_as_zero():
    assert prevalence("x", 0, 500).render().startswith("x: 0% ")


def test_a_prevalence_always_carries_its_counts():
    assert "14/100" in prevalence("x", 14, 100).render()


@pytest.mark.parametrize("n,d", [(1, 0), (-1, 10), (11, 10)])
def test_an_impossible_proportion_is_refused(n, d):
    with pytest.raises(MeasureError):
        wilson_interval(n, d)


def test_standardising_over_a_partial_set_of_bands_is_refused():
    """Silently dropping an age band the weights omit produces a rate that
    looks standardised, is not, and differs from the crude rate by an
    amount nobody can explain."""
    with pytest.raises(MeasureError, match="partial set of bands"):
        standardise("m", {"0-44": (1, 10), "65+": (5, 10)}, {"0-44": 1.0})


def test_standardisation_shows_the_crude_rate_beside_it():
    r = standardise("m", {"0-44": (2, 1000), "65+": (30, 200)},
                    {"0-44": 0.8, "65+": 0.2}, standard_population="US 2000")
    assert "crude" in r.render() and "age-standardised" in r.render()
    assert r.crude != r.standardised


def test_a_profile_of_nobody_reports_nothing_rather_than_dividing_by_zero():
    assert profile(0, {"diabetes": 0}).prevalences == []


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def test_an_unseen_payer_is_not_scored():
    """An average borrowed from other payers is not a fact about this one."""
    risk = score_claim("c1", payer="NewPayer", service_line="99215", history=History())
    assert risk.insufficient_history
    assert risk.band == "unscored"
    assert "not a fact about this one" in risk.insufficient_history


def test_a_thin_history_is_not_scored_either():
    risk = score_claim("c1", payer="P", service_line="L",
                       history=History(by_payer={"P": (2, MIN_HISTORY - 1)}))
    assert risk.insufficient_history


def test_every_factor_carries_the_counts_that_produced_it():
    risk = score_claim("c1", payer="P", service_line="L",
                       history=History(by_payer={"P": (30, 100)},
                                       by_service_line={"L": (10, 100)}))
    assert risk.factors
    for f in risk.factors:
        assert f.denominator >= MIN_HISTORY
        assert f"{f.numerator}/{f.denominator}" in f.render()


def test_no_single_factor_can_carry_the_whole_score():
    risk = score_claim("c1", payer="P", service_line="L",
                       history=History(by_payer={"P": (100, 100)}))
    assert max(f.points for f in risk.factors) <= MAX_FACTOR_POINTS


def test_a_line_with_no_documentation_is_scored_on_this_claim_not_the_population():
    with_doc = score_claim("c1", payer="P", service_line="L",
                           history=History(by_payer={"P": (10, 100)}), documented=True)
    without = score_claim("c1", payer="P", service_line="L",
                          history=History(by_payer={"P": (10, 100)}), documented=False)
    assert without.score > with_doc.score
    assert any(f.name == "no documentation on file" for f in without.factors)


def test_the_score_is_clamped_rather_than_rescaled():
    """Rescaling would change what each printed factor contributed, which
    is the one thing this design promises stays legible."""
    risk = score_claim("c1", payer="P", service_line="L",
                       history=History(by_payer={"P": (100, 100)},
                                       by_service_line={"L": (100, 100)},
                                       documentation_denials={"L": (100, 100)}),
                       documented=False)
    assert sum(f.points for f in risk.factors) > 100
    assert risk.score == 100
# Made by Ryan Gomez & Co. Inc.
