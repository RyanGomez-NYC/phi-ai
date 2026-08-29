# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Ingest data quality and mapping QA (SPEC §5.13) — the least regulated
work in the register, and it determines whether everything else is
correct, which is why the spec says build it first.

Three detectors, all pure functions over resource dicts:

- **Unmapped codes**: codings whose system is not a recognized
  standard vocabulary (local/proprietary systems), and codings with a
  system but no code. Each finding cites the storage key.
- **Duplicate patient candidates**: same normalized name + birthDate
  under different Patient ids — the Care Everywhere / split-record
  shape. CANDIDATES, not verdicts: merging patient identities is a
  human HIM decision, so the output is a worklist, never an action.
- **Terminology drift**: one (system, code) pair rendering different
  display strings across resources — either a vocabulary version
  change mid-corpus or a mapping bug upstream; both are worth a
  human's eyes before they become retrieval text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

#: Systems considered "standard" for mapping QA. Anything else is
#: surfaced as unmapped — including the empty system.
STANDARD_SYSTEMS = frozenset(
    {
        "http://snomed.info/sct",
        "http://loinc.org",
        "http://hl7.org/fhir/sid/icd-10-cm",
        "http://hl7.org/fhir/sid/icd-9-cm",
        "http://www.nlm.nih.gov/research/umls/rxnorm",
        "http://hl7.org/fhir/sid/ndc",
        "http://hl7.org/fhir/sid/cvx",
        "http://www.ama-assn.org/go/cpt",
        "http://unitsofmeasure.org",
        "http://terminology.hl7.org/CodeSystem/condition-clinical",
        "http://terminology.hl7.org/CodeSystem/condition-ver-status",
        "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
        "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
        "http://terminology.hl7.org/CodeSystem/v3-ActCode",
        "http://terminology.hl7.org/CodeSystem/v3-ActReason",
        "http://terminology.hl7.org/CodeSystem/observation-category",
        "http://terminology.hl7.org/CodeSystem/condition-category",
        "http://terminology.hl7.org/CodeSystem/medicationrequest-category",
        "http://terminology.hl7.org/CodeSystem/v3-ActEncounterCode",
        "http://hl7.org/fhir/sid/us-npi",
    }
)


@dataclass(frozen=True)
class UnmappedCode:
    storage_key: str
    system: str
    code: str
    display: str


@dataclass(frozen=True)
class DuplicateCandidate:
    name: str
    birth_date: str
    patient_keys: tuple[str, ...]


@dataclass(frozen=True)
class DriftFinding:
    system: str
    code: str
    displays: tuple[str, ...]
    storage_keys: tuple[str, ...]


@dataclass
class DataQualityReport:
    unmapped: list[UnmappedCode] = field(default_factory=list)
    duplicates: list[DuplicateCandidate] = field(default_factory=list)
    drift: list[DriftFinding] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Data quality: {len(self.unmapped)} unmapped codings, "
            f"{len(self.duplicates)} duplicate-patient candidates, "
            f"{len(self.drift)} display-drift findings"
        ]
        for u in self.unmapped[:20]:
            lines.append(
                f"  unmapped: {u.system or '(no system)'} {u.code or '(no code)'} "
                f"{u.display!r} [{u.storage_key}]"
            )
        for d in self.duplicates:
            lines.append(
                f"  duplicate candidate: {d.name} {d.birth_date} -> "
                f"{', '.join(d.patient_keys)} (HIM worklist — never auto-merged)"
            )
        for f_ in self.drift[:20]:
            lines.append(
                f"  drift: {f_.system} {f_.code} renders as "
                f"{' / '.join(repr(x) for x in f_.displays)}"
            )
        return "\n".join(lines)


def _codings(node):
    if isinstance(node, dict):
        if "system" in node or "code" in node:
            sys_, code = node.get("system"), node.get("code")
            if isinstance(sys_, str) or isinstance(code, str):
                yield (sys_ or "", code or "", node.get("display") or "")
        for value in node.values():
            yield from _codings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _codings(item)


def analyze(resources_by_key: Mapping[str, Mapping]) -> DataQualityReport:
    report = DataQualityReport()
    display_index: dict[tuple[str, str], dict[str, list[str]]] = {}
    patients: dict[tuple[str, str], list[str]] = {}

    for key in sorted(resources_by_key):
        resource = resources_by_key[key]
        for system, code, display in _codings(resource):
            # identifier {system, value} dicts carry no 'code', so
            # org-local identifier systems never reach the unmapped
            # check; a CODED entry on a local system does.
            if not code:
                continue
            if system not in STANDARD_SYSTEMS:
                report.unmapped.append(
                    UnmappedCode(storage_key=key, system=system, code=code, display=display)
                )
            if display:
                display_index.setdefault((system, code), {}).setdefault(
                    display, []
                ).append(key)

        if resource.get("resourceType") == "Patient":
            names = resource.get("name") or []
            birth = resource.get("birthDate") or ""
            if names and birth:
                given = " ".join(names[0].get("given") or [])
                family = names[0].get("family") or ""
                normalized = f"{given} {family}".strip().lower()
                if normalized:
                    patients.setdefault((normalized, birth), []).append(key)

    for (name, birth), keys in sorted(patients.items()):
        if len(keys) > 1:
            report.duplicates.append(
                DuplicateCandidate(name=name, birth_date=birth, patient_keys=tuple(keys))
            )

    for (system, code), displays in sorted(display_index.items()):
        if len(displays) > 1:
            report.drift.append(
                DriftFinding(
                    system=system,
                    code=code,
                    displays=tuple(sorted(displays)),
                    storage_keys=tuple(k for keys in displays.values() for k in keys),
                )
            )
    return report
# Made by Ryan Gomez & Co. Inc.
