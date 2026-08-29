# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Install-time terminology loader (SPEC §7.4).

One manifest, one licence class per terminology, one behavior on
missing credentials: fail loud. The classes:

- **COMMITTED** — content may live in this repository (with the
  required notice where one exists): LOINC, ICD-10-CM/PCS (from
  CMS/CDC, never the UMLS copy), CVX, NDC.
- **OPERATOR_LICENSED** — content is fetched or mounted at install
  time under the DEPLOYMENT'S licence, which this project cannot
  confer: SNOMED CT US (Affiliate/UMLS), RxNorm full release (UMLS).
  The loader requires the operator-supplied credential or file path
  and refuses without it.
- **ATTESTED_LICENSE_ID** — CPT: disabled by default, enabled only
  with an operator-attested AMA licence ID. The AMA additionally
  prohibits training or fine-tuning AI models against the CPT data
  file; retrieval-based reference is covered under licence — recorded
  here and in the runbook's Known gaps.

The loader's second product is the EXPANSION AVAILABILITY report: which
5.1(c) query expansions (RxNorm ingredient↔brand, SNOMED subsumption,
LOINC value sets) this deployment actually has. A deployment without a
SNOMED licence has subsumption expansion DISABLED EXPLICITLY WITH A
NAMED REASON — surfaced through §6.7's conformance report — never
silently degraded, and retrieval quality numbers are reported per
expansion configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

import yaml

from core.governance.segmentation import CategoryValueSets, SensitiveCategory


class TerminologyError(Exception):
    """A licensing or configuration failure. Always loud: the loader
    never substitutes an empty vocabulary for a missing one."""


class LicenseClass(Enum):
    COMMITTED = "committed"
    OPERATOR_LICENSED = "operator_licensed"
    ATTESTED_LICENSE_ID = "attested_license_id"


@dataclass(frozen=True)
class TerminologySource:
    name: str
    license_class: LicenseClass
    notice: str  # the licence/attribution note that must travel with it
    expansion: Optional[str] = None  # which 5.1(c) expansion it powers


SOURCES: dict[str, TerminologySource] = {
    "loinc": TerminologySource(
        "loinc",
        LicenseClass.COMMITTED,
        "This material contains content from LOINC (http://loinc.org), "
        "used under its licence; not relicensed under this project's "
        "licence; field names unchanged.",
        expansion="loinc_value_sets",
    ),
    "icd10cm": TerminologySource(
        "icd10cm",
        LicenseClass.COMMITTED,
        "ICD-10-CM from CMS/CDC public releases — never the UMLS copy, "
        "which carries Category 4 restrictions.",
    ),
    "cvx": TerminologySource(
        "cvx",
        LicenseClass.COMMITTED,
        "CVX (CDC/NCIRD), public domain; attribution, no endorsement implied.",
    ),
    "ndc": TerminologySource(
        "ndc", LicenseClass.COMMITTED, "NDC via openFDA, CC0 1.0."
    ),
    "snomed": TerminologySource(
        "snomed",
        LicenseClass.OPERATOR_LICENSED,
        "SNOMED CT US requires each deployment's own NLM/UMLS Affiliate "
        "licence; this project cannot confer it (SPEC §7.4).",
        expansion="snomed_subsumption",
    ),
    "rxnorm": TerminologySource(
        "rxnorm",
        LicenseClass.OPERATOR_LICENSED,
        "RxNorm full release embeds restricted proprietary sources; requires "
        "the deployment's UMLS licence. RxNav REST is public reference only.",
        expansion="rxnorm_ingredient_brand",
    ),
    "cpt": TerminologySource(
        "cpt",
        LicenseClass.ATTESTED_LICENSE_ID,
        "CPT (AMA) is disabled by default; enabling requires an "
        "operator-attested AMA licence ID. The AMA prohibits training or "
        "fine-tuning AI models against the CPT data file; retrieval-based "
        "reference is covered under licence.",
    ),
}


@dataclass(frozen=True)
class LoadedTerminologies:
    available: frozenset[str]
    expansions_enabled: Mapping[str, bool]
    disabled_reasons: Mapping[str, str]
    value_sets: CategoryValueSets

    def expansion_report(self) -> str:
        """Feeds the §6.7 conformance report."""
        lines = ["Query expansion availability (deployment-time property, SPEC §7.4):"]
        for name, enabled in sorted(self.expansions_enabled.items()):
            if enabled:
                lines.append(f"  [ENABLED ] {name}")
            else:
                lines.append(f"  [DISABLED] {name} — {self.disabled_reasons[name]}")
        return "\n".join(lines)


def load_value_sets(path: Path) -> CategoryValueSets:
    """
    Reads the operator's sensitive-category value-set file (see
    config/sensitive_value_sets.example.yaml). Structure over content:
    the file's codes were curated by whoever owns the deployment's
    segmentation review; this loader validates shape, maps category
    names, and refuses unknown categories rather than dropping them.
    """
    if not path.exists():
        raise TerminologyError(
            f"Sensitive-category value-set file not found: {path}. "
            "Segmentation cannot run on an empty vocabulary; see "
            "config/sensitive_value_sets.example.yaml (SPEC §6.1)."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    by_name = {c.value: c for c in SensitiveCategory}
    codes: dict[SensitiveCategory, frozenset[tuple[str, str]]] = {}
    for category_name, entries in (raw.get("categories") or {}).items():
        category = by_name.get(category_name)
        if category is None:
            raise TerminologyError(
                f"Unknown sensitive category {category_name!r} in {path}; "
                f"known: {sorted(by_name)}"
            )
        pairs = set()
        for entry in entries or []:
            system, code = entry.get("system"), entry.get("code")
            if not system or not code:
                raise TerminologyError(
                    f"Value-set entry under {category_name!r} needs both "
                    f"'system' and 'code': {entry!r}"
                )
            pairs.add((str(system), str(code)))
        codes[category] = frozenset(pairs)
    departments = {
        str(dept): by_name[cat]
        for dept, cat in (raw.get("departments") or {}).items()
        if cat in by_name
    }
    unknown_depts = {
        dept: cat
        for dept, cat in (raw.get("departments") or {}).items()
        if cat not in by_name
    }
    if unknown_depts:
        raise TerminologyError(
            f"Department mappings reference unknown categories: {unknown_depts}"
        )
    return CategoryValueSets(codes=codes, departments=departments)


def load(
    *,
    value_sets_path: Path,
    umls_api_key: Optional[str] = None,
    snomed_release_path: Optional[Path] = None,
    cpt_license_id: Optional[str] = None,
) -> LoadedTerminologies:
    """
    The install-time entry. COMMITTED sources are always available;
    OPERATOR_LICENSED ones require their credential/path;
    CPT requires the attested licence ID. Missing credentials disable
    the dependent expansion WITH A NAMED REASON — and only expansions
    can degrade this way; segmentation value sets are required
    outright, because a missing exclusion list fails unsafe rather
    than inconvenient.
    """
    value_sets = load_value_sets(value_sets_path)

    available: set[str] = set()
    expansions: dict[str, bool] = {}
    reasons: dict[str, str] = {}

    for name, source in SOURCES.items():
        if source.license_class is LicenseClass.COMMITTED:
            available.add(name)
        elif source.license_class is LicenseClass.OPERATOR_LICENSED:
            has_credential = bool(umls_api_key) or (
                name == "snomed" and snomed_release_path is not None
            )
            if has_credential:
                available.add(name)
        elif source.license_class is LicenseClass.ATTESTED_LICENSE_ID:
            if cpt_license_id and cpt_license_id.strip():
                available.add(name)

        if source.expansion:
            enabled = name in available
            expansions[source.expansion] = enabled
            if not enabled:
                reasons[source.expansion] = (
                    f"{name} not licensed for this deployment — "
                    f"{source.notice.splitlines()[0]}"
                )

    return LoadedTerminologies(
        available=frozenset(available),
        expansions_enabled=expansions,
        disabled_reasons=reasons,
        value_sets=value_sets,
    )
# Made by Ryan Gomez & Co. Inc.
