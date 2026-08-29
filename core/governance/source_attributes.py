# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
HTI-1 predictive source attributes and IRM summary (docs/SPEC.md §6.6).

Who is obligated (verified, §6.6): the 45 CFR 170.315(b)(11)
obligations run only to certified health IT developers for predictive
DSIs they supply in a certified Module — a bring-your-own-infrastructure
platform that is not certified health IT has NO direct obligation. But
(b)(11)(v)(B) requires the certified Module to let users record,
change, and access source attributes for DSIs developed by OTHER
parties, so a health system surfacing our output through a certified EHR has
fields to populate and will demand that content contractually. Shipping
this artifact is the price of admission to certified-EHR deployments,
not a legal requirement — and it is populated for the grounded
assistant itself (§5.1), not only for hosted models, because
§170.102's Predictive DSI definition ("...prediction, classification,
recommendation, evaluation, or ANALYSIS") is broad enough to capture a
retrieval-grounded assistant and the alternative is arguing the point
under a health system's procurement review.

DELIBERATELY NOT CODED TO "31 ATTRIBUTES": the spec's per-category item
counts are [UNVERIFIED] and open dependency #6 says they must be
confirmed against eCFR text before being coded to. This module
therefore validates that every one of the NINE verified categories is
present and non-empty, and treats the attribute list within each
category as free-form. When #6 resolves, per-category required-item
lists slot into REQUIRED_ITEMS below without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


class SourceAttributeError(Exception):
    pass


#: The nine source-attribute categories (§6.6, verified as categories).
ATTRIBUTE_CATEGORIES = (
    "details_and_output",
    "purpose",
    "cautioned_out_of_scope_use",
    "development_data_and_input_features",
    "fairness_in_development",
    "external_validation",
    "quantitative_performance_measures",
    "ongoing_maintenance_of_validity_and_fairness",
    "update_and_continued_validation_schedule",
)

#: Per-category required item names — EMPTY until open dependency #6
#: (eCFR confirmation of per-category counts) resolves. Kept as the
#: named landing spot so resolving #6 is a data change, not a redesign.
REQUIRED_ITEMS: dict[str, tuple[str, ...]] = {}

#: The eight IRM risk-analysis characteristics named by the rule
#: (§6.6, verified).
IRM_CHARACTERISTICS = (
    "validity",
    "reliability",
    "robustness",
    "fairness",
    "intelligibility",
    "safety",
    "security",
    "privacy",
)


@dataclass(frozen=True)
class IRMSummary:
    """Intervention risk management summary: risk analysis across the
    eight named characteristics, plus risk mitigation and data
    governance. Published at a hyperlink accessible without
    preconditions, mirroring §170.523(f)(1)(xxi)."""

    risk_analysis: Mapping[str, str]  # characteristic -> analysis text
    risk_mitigation: str
    data_governance: str

    def validate(self) -> None:
        missing = [c for c in IRM_CHARACTERISTICS if not (self.risk_analysis.get(c) or "").strip()]
        if missing:
            raise SourceAttributeError(
                "IRM summary must analyze every named characteristic; "
                f"missing or empty: {', '.join(missing)}"
            )
        if not self.risk_mitigation.strip():
            raise SourceAttributeError("IRM summary requires a risk-mitigation section")
        if not self.data_governance.strip():
            raise SourceAttributeError(
                "IRM summary requires a data-governance section (how data "
                "are acquired, managed, and used)"
            )


@dataclass(frozen=True)
class SourceAttributeSet:
    """One model target's publishable source-attribute artifact."""

    model_id: str
    #: category -> {attribute name -> value}. Every ATTRIBUTE_CATEGORIES
    #: entry must be present and non-empty.
    attributes: Mapping[str, Mapping[str, str]]
    irm: IRMSummary

    def validate(self) -> None:
        missing = [c for c in ATTRIBUTE_CATEGORIES if c not in self.attributes]
        if missing:
            raise SourceAttributeError(
                f"Source attributes for {self.model_id!r} missing categories: "
                f"{', '.join(missing)}"
            )
        empty = [c for c in ATTRIBUTE_CATEGORIES if not self.attributes[c]]
        if empty:
            raise SourceAttributeError(
                f"Source attributes for {self.model_id!r} have empty "
                f"categories: {', '.join(empty)}"
            )
        for category, items in self.attributes.items():
            required = REQUIRED_ITEMS.get(category, ())
            item_missing = [i for i in required if not (items.get(i) or "").strip()]
            if item_missing:
                raise SourceAttributeError(
                    f"Source attributes for {self.model_id!r}, category "
                    f"{category!r}, missing required items: "
                    f"{', '.join(item_missing)}"
                )
        self.irm.validate()

    def publishable(self) -> dict:
        """The dict rendered to the publicly hyperlinked page. validate()
        first — an incomplete artifact is refused, not published thin."""
        self.validate()
        return {
            "model_id": self.model_id,
            "source_attributes": {
                c: dict(self.attributes[c]) for c in ATTRIBUTE_CATEGORIES
            },
            "irm_summary": {
                "risk_analysis": {
                    c: self.irm.risk_analysis[c] for c in IRM_CHARACTERISTICS
                },
                "risk_mitigation": self.irm.risk_mitigation,
                "data_governance": self.irm.data_governance,
            },
        }
# Made by Ryan Gomez & Co. Inc.
