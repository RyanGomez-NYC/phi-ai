# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Fairness screen — 45 CFR 92.210 made structural (docs/SPEC.md §6.3).

Section 1557's implementing rule (89 FR 37692, effective 5 July 2024)
bars covered entities from discriminating through patient care decision
support tools on the basis of race, color, national origin, sex, age,
or disability, and imposes an ongoing duty to identify tools using
variables measuring those categories and to mitigate the resulting
risk. This module turns that from an attestation into a registration
gate, the same way minimum-necessary is already structural elsewhere in
this codebase:

- a model whose declared input-variable schema names a protected-class
  variable is REJECTED at registration;
- a declared proxy candidate (ZIP in isolation, payer class, primary
  language, interpreter need) is permitted only with an explicit
  operator justification recorded with a basis — never silently;
- a fairness report disaggregating performance across the protected
  categories, plus a mitigation record, are REQUIRED registration
  artifacts.

HONEST LIMIT, stated here because this is where a reader will look for
the claim: excluding *declared* protected-class variables is
mechanical; identifying *undeclared* proxies is not, and this module
does not claim to have solved it. What it defensibly provides is
narrower and still substantial — it forces the question to be asked,
recorded, and justified at registration time, and produces the
disaggregated evidence a covered entity needs to discharge its own
ongoing identification-and-mitigation duty. The same limitation is
stated in runbooks/RUNBOOK_MODEL_GOVERNANCE.md, per the spec's
requirement that it live in the runbook rather than be discovered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional


class FairnessScreenError(Exception):
    """A registration-time fairness failure. Raised, never warned —
    registration failure stops the run and there is no degraded mode
    (SPEC §6.2)."""


#: The six protected categories named by 45 CFR 92.210. These exact
#: strings are the required keys of a fairness report's disaggregation.
PROTECTED_CATEGORIES = (
    "race",
    "color",
    "national_origin",
    "sex",
    "age",
    "disability",
)

#: Input-variable names that measure a protected category, in normalized
#: form (see _normalize). Aliases are included because a schema author
#: writes "patientRace" or "gender", not the regulation's vocabulary.
#: This list is a floor, not a ceiling: it catches declared protected
#: variables under common spellings. It cannot catch an undeclared
#: proxy - see the module docstring's honest limit.
PROTECTED_VARIABLES = frozenset(
    {
        "race",
        "patient_race",
        "color",
        "national_origin",
        "nationality",
        "country_of_origin",
        "ethnicity",  # measures race/national origin in every US clinical schema
        "patient_ethnicity",
        "sex",
        "patient_sex",
        "birth_sex",
        "sex_at_birth",
        "gender",  # in clinical schemas this measures sex for 92.210 purposes
        "patient_gender",
        "age",
        "patient_age",
        "date_of_birth",
        "birth_date",
        "dob",
        "disability",
        "disability_status",
    }
)

#: Proxy candidates the spec names explicitly (§5.7): variables that do
#: not measure a protected category directly but are known to correlate
#: with one. Each requires an explicit operator justification recorded
#: with a basis - flagged, never silently permitted.
PROXY_CANDIDATE_VARIABLES = frozenset(
    {
        "zip",
        "zip_code",
        "zipcode",
        "postal_code",
        "payer",
        "payer_class",
        "payer_type",
        "insurance",
        "insurance_type",
        "primary_language",
        "language",
        "preferred_language",
        "interpreter_need",
        "interpreter_required",
        "needs_interpreter",
    }
)


def _normalize(variable_name: str) -> str:
    """camelCase / kebab-case / spaced names all reduce to the same
    snake_case token, so "patientRace", "patient-race" and "Patient Race"
    each hit the PROTECTED_VARIABLES entry for "patient_race"."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", variable_name.strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


@dataclass(frozen=True)
class ProxyJustification:
    """An operator's recorded basis for including a proxy-candidate
    variable. Both fields are required content, not presence flags -
    an empty basis is treated as no justification at all."""

    operator: str
    basis: str


@dataclass(frozen=True)
class FairnessReport:
    """Performance disaggregated across the 92.210 protected categories.

    `disaggregated_performance` maps each of PROTECTED_CATEGORIES to a
    mapping of subgroup -> metric value. This module validates coverage
    (every category present, every category non-empty), not the numbers:
    whether the disaggregation reveals a disparity is the covered
    entity's mitigation question, and the mitigation_record is where
    their answer is recorded."""

    disaggregated_performance: Mapping[str, Mapping[str, float]]
    mitigation_record: str

    def validate(self) -> None:
        missing = [c for c in PROTECTED_CATEGORIES if c not in self.disaggregated_performance]
        if missing:
            raise FairnessScreenError(
                "Fairness report must disaggregate performance across every "
                f"protected category; missing: {', '.join(missing)}"
            )
        empty = [c for c in PROTECTED_CATEGORIES if not self.disaggregated_performance[c]]
        if empty:
            raise FairnessScreenError(
                "Fairness report contains no subgroup measurements for: "
                f"{', '.join(empty)}"
            )
        if not self.mitigation_record.strip():
            raise FairnessScreenError(
                "A mitigation record is a required registration artifact "
                "(45 CFR 92.210 mitigation duty); an empty one does not count"
            )


@dataclass(frozen=True)
class FairnessScreenResult:
    """What the screen found, whether it passed or not - kept whole so
    the registry can put the entire verdict into the audit event rather
    than a bare pass/fail."""

    ok: bool
    protected_variables_found: tuple[str, ...] = ()
    unjustified_proxies: tuple[str, ...] = ()
    justified_proxies: tuple[str, ...] = ()
    reason: Optional[str] = None
    #: normalized name -> justification, echoed back so the registry can
    #: persist the operator's recorded basis alongside the registration.
    proxy_record: Mapping[str, ProxyJustification] = field(default_factory=dict)


def screen_input_schema(
    input_variables: tuple[str, ...] | list[str],
    proxy_justifications: Optional[Mapping[str, ProxyJustification]] = None,
) -> FairnessScreenResult:
    """
    Screens a declared input-variable schema (SPEC §6.3, Invariant 14).

    Returns a failing result - never raises - so the caller (the model
    registry) can audit the full verdict and then refuse. Protected
    variables fail the screen outright; proxy candidates fail unless a
    non-empty justification is recorded for them (keys of
    `proxy_justifications` are matched after the same normalization as
    the variables themselves).
    """
    justifications = {
        _normalize(k): v for k, v in (proxy_justifications or {}).items()
    }

    protected: list[str] = []
    unjustified: list[str] = []
    justified: list[str] = []
    proxy_record: dict[str, ProxyJustification] = {}

    for raw in input_variables:
        name = _normalize(raw)
        if name in PROTECTED_VARIABLES:
            protected.append(raw)
        elif name in PROXY_CANDIDATE_VARIABLES:
            just = justifications.get(name)
            if just is None or not just.basis.strip() or not just.operator.strip():
                unjustified.append(raw)
            else:
                justified.append(raw)
                proxy_record[name] = just

    if protected:
        return FairnessScreenResult(
            ok=False,
            protected_variables_found=tuple(protected),
            unjustified_proxies=tuple(unjustified),
            justified_proxies=tuple(justified),
            proxy_record=proxy_record,
            reason=(
                "Declared input schema contains protected-class variables "
                f"({', '.join(protected)}); 45 CFR 92.210 screen rejects the "
                "registration"
            ),
        )
    if unjustified:
        return FairnessScreenResult(
            ok=False,
            unjustified_proxies=tuple(unjustified),
            justified_proxies=tuple(justified),
            proxy_record=proxy_record,
            reason=(
                "Declared proxy candidates lack an operator justification "
                f"with a basis ({', '.join(unjustified)}); proxies are never "
                "silently permitted"
            ),
        )
    return FairnessScreenResult(
        ok=True,
        justified_proxies=tuple(justified),
        proxy_record=proxy_record,
    )
# Made by Ryan Gomez & Co. Inc.
