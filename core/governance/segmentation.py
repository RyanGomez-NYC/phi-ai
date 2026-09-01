# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Sensitive-category segmentation engine (docs/SPEC.md §6.1).

Determines what never enters the embedding corpus, and what is withheld
from retrieval by requester geography. Runs at SERIALIZATION time, not
retrieval time (§5.1(b)): a chunk that was never embedded cannot be
leaked by a retrieval bug.

THE GOVERNING FINDING (§6.1, verified): FHIR security labels exist but
are not populated in practice — ONC rates the Security Label
Sensitivity Tag at Level 0/1, "in limited use in production
environments," and US Core does not profile `meta.security` at all
(§7.3). An Epic corpus will arrive with `meta.security` overwhelmingly
empty, and ABSENCE OF A LABEL MUST NEVER BE READ AS ABSENCE OF
SENSITIVITY. Classification is therefore driven primarily by:

  (i)   resource type,
  (ii)  code-system value-set membership — curated SNOMED / ICD-10 /
        LOINC / RxNorm sets per category, supplied per deployment via
        CategoryValueSets (the terminology loader, §7.4, feeds these),
  (iii) source department or compartment,

with `meta.security` honored WHEN PRESENT as an additional signal only.

FAIL-CLOSED: an unclassifiable resource — unknown resource type, or a
sensitivity label the engine has no mapping for — is EXCLUDED and
COUNTED, never included by default. The count is the operator's signal
that the value sets need curation, surfaced loudly rather than
interpolated away (the same posture as §6.2's streaming gap
accounting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Optional


class SensitiveCategory(Enum):
    PSYCHOTHERAPY_NOTES = "psychotherapy_notes"  # HIPAA; existing invariant
    PART2_SUD = "part2_sud"  # 42 CFR Part 2
    REPRODUCTIVE_HEALTH = "reproductive_health"  # Cal. AB 352 — also geo-gated
    HIV = "hiv"  # N.Y. PHL §2782; Tex. H&S §81.103
    GENETIC = "genetic"  # Alaska Stat. §18.13.010
    MENTAL_HEALTH = "mental_health"  # 740 ILCS 110; Tex. ch. 611; Cal. §56.104
    MINOR_CONSENTED = "minor_consented"  # state consent-age matrix (unresearched)
    # Added 2026-09-01. The reference demonstration served 735 rows of
    # "Victim of intimate partner abuse (finding)" to every role holding
    # patient:read, in charts, in AI answers, and printed into a
    # patient-facing instruction sheet, because no category for it existed
    # anywhere in the taxonomy. This enum had the same hole. Absence of a
    # category is not a judgement that the data is ordinary; it is a
    # judgement nobody made.
    DOMESTIC_VIOLENCE = "domestic_violence"  # IPV, stalking, sexual assault
    ABUSE_NEGLECT = "abuse_neglect"  # child / elder / vulnerable adult, trafficking


#: HL7 ActCode InformationSensitivityPolicy tokens -> category, for the
#: minority of resources that DO carry `meta.security`. A label token
#: absent from this mapping is itself sensitivity information the
#: engine cannot interpret — the resource is excluded as unclassifiable
#: rather than embedded with an unread warning attached.
SECURITY_LABEL_MAP: dict[str, SensitiveCategory] = {
    "PSY": SensitiveCategory.MENTAL_HEALTH,
    "PSYTHPN": SensitiveCategory.PSYCHOTHERAPY_NOTES,
    "MH": SensitiveCategory.MENTAL_HEALTH,
    "BH": SensitiveCategory.MENTAL_HEALTH,
    "SUD": SensitiveCategory.PART2_SUD,
    "ETH": SensitiveCategory.PART2_SUD,  # ActCode ETH = substance abuse info
    "OPIOIDUD": SensitiveCategory.PART2_SUD,
    "HIV": SensitiveCategory.HIV,
    "STD": SensitiveCategory.HIV,  # same statutory family in NY/TX handling
    "SEX": SensitiveCategory.REPRODUCTIVE_HEALTH,
    "PREGNANT": SensitiveCategory.REPRODUCTIVE_HEALTH,
    "GDIS": SensitiveCategory.GENETIC,
    "SICKLE": SensitiveCategory.GENETIC,
    "ADOL": SensitiveCategory.MINOR_CONSENTED,
    # ActCode InformationSensitivityPolicy carries these three; without the
    # mapping they were "unmapped token" exclusions, which is the right
    # OUTCOME reached for the wrong REASON - the engine could not say what
    # it had withheld, so an operator reading the counters saw an
    # unexplained exclusion rather than a domestic-violence one.
    "DVD": SensitiveCategory.DOMESTIC_VIOLENCE,  # all domestic violence info
    "DVSTAT": SensitiveCategory.DOMESTIC_VIOLENCE,  # DV status
    "SEV": SensitiveCategory.DOMESTIC_VIOLENCE,  # sexual assault / violence
    "ABUSE": SensitiveCategory.ABUSE_NEGLECT,
}

#: Confidentiality codes (the other half of the security-labels value
#: set). R and V mean someone upstream decided this needs restricted
#: handling; that is honored as exclusion even without a category.
RESTRICTED_CONFIDENTIALITY = frozenset({"R", "V"})


@dataclass(frozen=True)
class CategoryValueSets:
    """Curated per-category code sets, as (system, code) pairs, plus the
    department/compartment map — signals (ii) and (iii). Populated per
    deployment by the terminology loader (§7.4); this module defines the
    structure and the decision procedure, not the clinical content, the
    same division of labor as core/config/retention_rules.py."""

    codes: Mapping[SensitiveCategory, frozenset[tuple[str, str]]] = field(
        default_factory=dict
    )
    #: source department/compartment identifier -> category, e.g. a Part 2
    #: program's department code -> PART2_SUD. Part 2 provenance is a
    #: RETAINED attribute (§6.1): the 2024 rule's consent lineage follows
    #: the record, so the department signal is kept on the decision.
    departments: Mapping[str, SensitiveCategory] = field(default_factory=dict)


#: Resource types the serializer knows how to classify. Anything else is
#: unclassifiable — excluded and counted, never included by default.
KNOWN_RESOURCE_TYPES = frozenset(
    {
        "AllergyIntolerance",
        "CarePlan",
        "CareTeam",
        "Condition",
        "DiagnosticReport",
        "DocumentReference",
        "Encounter",
        "Goal",
        "Immunization",
        "MedicationRequest",
        "MedicationStatement",
        "Observation",
        "Procedure",
        "ServiceRequest",
    }
)


@dataclass(frozen=True)
class SegmentationDecision:
    include: bool
    categories: tuple[SensitiveCategory, ...] = ()
    unclassifiable: bool = False
    reason: str = ""


@dataclass
class SegmentationStats:
    """Serialization-run counters. `excluded_unclassifiable` is the
    number the operator watches: a rising count means resources the
    value sets cannot place, each one excluded fail-closed."""

    included: int = 0
    excluded_unclassifiable: int = 0
    excluded_by_category: dict[str, int] = field(default_factory=dict)

    def observe(self, decision: SegmentationDecision) -> None:
        if decision.include:
            self.included += 1
        elif decision.unclassifiable:
            self.excluded_unclassifiable += 1
        else:
            for cat in decision.categories:
                self.excluded_by_category[cat.value] = (
                    self.excluded_by_category.get(cat.value, 0) + 1
                )


def _codings(node) -> Iterable[tuple[str, str]]:
    """Walks a FHIR resource dict and yields every (system, code) pair
    found in Coding-shaped objects, wherever they sit — `code`, `type`,
    `category`, `medicationCodeableConcept`, component codes, and any
    profile extension alike. A recursive walk is deliberately chosen
    over named-field extraction: a curated field list silently misses
    the field a new resource type keeps its codes in, and a missed code
    here is a sensitive record embedded."""
    if isinstance(node, dict):
        system, code = node.get("system"), node.get("code")
        if isinstance(system, str) and isinstance(code, str):
            yield (system, code)
        for value in node.values():
            yield from _codings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _codings(item)


def classify(
    resource: Mapping,
    value_sets: CategoryValueSets,
    *,
    source_department: Optional[str] = None,
) -> SegmentationDecision:
    """
    The serialization-time decision for one resource. Returns
    include=True only when every signal comes back clean; any category
    hit, any restricted confidentiality code, any unmapped sensitivity
    label, and any unclassifiable shape all exclude.
    """
    resource_type = resource.get("resourceType")
    if not isinstance(resource_type, str) or resource_type not in KNOWN_RESOURCE_TYPES:
        return SegmentationDecision(
            include=False,
            unclassifiable=True,
            reason=f"unknown or missing resourceType {resource_type!r}; "
            "excluded fail-closed and counted",
        )

    categories: set[SensitiveCategory] = set()

    # Signal (i): resource type. Psychotherapy notes arrive as clinical
    # notes; the note-type code decides, but the type gives the fast
    # path for the note-shaped resources the code walk then confirms.
    # (No resource type is sensitive per se in R4 — the value sets and
    # department signals carry types' sensitivity.)

    # Signal (ii): value-set membership, over every coding in the
    # resource regardless of which field carries it.
    resource_codes = set(_codings(resource))
    for category, members in value_sets.codes.items():
        if resource_codes & members:
            categories.add(category)

    # Signal (iii): source department / compartment.
    if source_department is not None:
        dept_category = value_sets.departments.get(source_department)
        if dept_category is not None:
            categories.add(dept_category)

    # Additional signal only, never load-bearing: meta.security when
    # populated. An unmapped sensitivity token excludes fail-closed.
    meta = resource.get("meta")
    for system, code in _codings(meta if isinstance(meta, dict) else {}):
        if code in RESTRICTED_CONFIDENTIALITY and "Confidentiality" in system:
            # A bare R/V says "restricted" without saying why; exclude
            # without inventing a category.
            return SegmentationDecision(
                include=False,
                categories=tuple(sorted(categories, key=lambda c: c.value)),
                reason=f"meta.security confidentiality {code!r} marks the "
                "resource restricted; excluded",
            )
        if "ActCode" in system:
            mapped = SECURITY_LABEL_MAP.get(code)
            if mapped is not None:
                categories.add(mapped)
            elif code.isupper() and code not in {"HTEST"}:
                return SegmentationDecision(
                    include=False,
                    unclassifiable=True,
                    reason=f"meta.security carries unmapped ActCode {code!r}; "
                    "a label the engine cannot interpret excludes fail-closed",
                )

    if categories:
        return SegmentationDecision(
            include=False,
            categories=tuple(sorted(categories, key=lambda c: c.value)),
            reason="sensitive-category exclusion at serialization time",
        )
    return SegmentationDecision(include=True, reason="no sensitive signal")


@dataclass(frozen=True)
class GeoGateDecision:
    allowed: bool
    reason: str


def evaluate_geo_gate(
    categories: Iterable[SensitiveCategory],
    requester_state: Optional[str],
    *,
    record_state: str = "CA",
    audit=None,
    actor: str = "system",
    resource_key: str = "",
) -> GeoGateDecision:
    """
    California AB 352's machine mandate (§6.1, verified): systems must
    prevent out-of-state disclosure of reproductive / gender-affirming /
    contraception data and automatically disable out-of-state access.
    Evaluated on REQUESTER location at query time; fails closed on an
    unresolvable location. Only bites for AB 352 categories on records
    governed by California — everything else passes through.
    """
    if SensitiveCategory.REPRODUCTIVE_HEALTH not in set(categories):
        return GeoGateDecision(allowed=True, reason="no AB 352 category present")
    if record_state != "CA":
        return GeoGateDecision(
            allowed=True, reason=f"record not governed by AB 352 ({record_state})"
        )

    def _refuse(reason: str) -> GeoGateDecision:
        if audit is not None:
            audit.record(
                actor=actor,
                action="segmentation.geo_gate.refused",
                resource_key=resource_key or "ab352/unattributed",
                purpose_of_use="operations",
            )
        return GeoGateDecision(allowed=False, reason=reason)

    if not requester_state or not requester_state.strip():
        return _refuse(
            "Requester location unresolved; the AB 352 geo-gate fails closed"
        )
    if requester_state.strip().upper() != "CA":
        return _refuse(
            f"Out-of-state requester ({requester_state}) refused access to "
            "AB 352-protected categories (Cal. Civ. Code §56.101(c))"
        )
    return GeoGateDecision(allowed=True, reason="in-state requester")
# Made by Ryan Gomez & Co. Inc.
