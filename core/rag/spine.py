# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The deterministic structured spine (SPEC §5.1g) — the highest-leverage
decision in the flagship capability, per the spec: for
summarization-shaped questions, the timeline — problems, medications,
allergies, recent labs with trends, procedures, encounters — is
assembled DIRECTLY from structured resources, with retrieval filling
narrative context around it. Chunk-level recall silently drops the
active medication that mattered while returning a fluent, well-cited,
wrong answer; a deterministic pass over the structured record cannot.

Every spine entry carries its storage key, so spine content meets the
same citation contract as retrieved content (5.1h) — the answer's
claims cite the spine rows exactly as they cite chunks.

The silent-omission guarantee this module makes, and the one §10 names
as the primary acceptance metric: every active medication, every
non-negated allergy, and every active problem present in the input
appears in the spine. No ranking, no top-k, no sampling — omission here
is a bug by construction, and tests/test_rag_spine.py holds it to that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.rag.serialization import (
    INACTIVE_CLINICAL,
    NEGATING_VERIFICATIONS,
    Chunk,
)


@dataclass(frozen=True)
class SpineEntry:
    storage_key: str
    effective: str | None
    label: str  # the rendered chunk text - status and negation included


@dataclass(frozen=True)
class LabSeries:
    """One lab analyte across time, oldest first, with the naive trend a
    reader would compute: the direction of the last step. Trend is a
    presentation aid, not an interpretation - the platform does not
    diagnose (Invariant 19)."""

    code: str
    display: str
    points: tuple[tuple[str, float], ...]  # (effective, value) oldest first
    trend: str  # "rising" | "falling" | "flat" | "single value"
    storage_keys: tuple[str, ...]


@dataclass(frozen=True)
class Spine:
    active_problems: tuple[SpineEntry, ...] = ()
    resolved_problems: tuple[SpineEntry, ...] = ()
    negated_assertions: tuple[SpineEntry, ...] = ()  # refuted/entered-in-error, shown AS negated
    active_medications: tuple[SpineEntry, ...] = ()
    inactive_medications: tuple[SpineEntry, ...] = ()
    allergies: tuple[SpineEntry, ...] = ()
    labs: tuple[LabSeries, ...] = ()
    procedures: tuple[SpineEntry, ...] = ()
    encounters: tuple[SpineEntry, ...] = ()

    def citation_keys(self) -> frozenset[str]:
        keys: set[str] = set()
        for entries in (
            self.active_problems,
            self.resolved_problems,
            self.negated_assertions,
            self.active_medications,
            self.inactive_medications,
            self.allergies,
            self.procedures,
            self.encounters,
        ):
            keys.update(e.storage_key for e in entries)
        for series in self.labs:
            keys.update(series.storage_keys)
        return frozenset(keys)


def _entry(chunk: Chunk) -> SpineEntry:
    return SpineEntry(
        storage_key=chunk.storage_key, effective=chunk.effective, label=chunk.text
    )


def _sorted(entries: list[SpineEntry]) -> tuple[SpineEntry, ...]:
    """Newest first; undated entries LAST but present — an undated
    active medication still appears (absence of a date never becomes
    absence of the medication), it just can't claim recency. Storage
    key is the deterministic tiebreak."""
    dated = sorted(
        (e for e in entries if e.effective is not None),
        key=lambda e: (e.effective, e.storage_key),
        reverse=True,
    )
    undated = sorted(
        (e for e in entries if e.effective is None),
        key=lambda e: e.storage_key,
    )
    return tuple(dated) + tuple(undated)


def _lab_value(chunk: Chunk, resources_by_key: dict) -> float | None:
    resource = resources_by_key.get(chunk.storage_key, {})
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict) and isinstance(quantity.get("value"), (int, float)):
        return float(quantity["value"])
    return None


def build_spine(
    chunks: list[Chunk],
    resources_by_key: dict | None = None,
) -> Spine:
    """
    Deterministic pass over serialized chunks (already segmentation-
    filtered and attribution-checked upstream). `resources_by_key`
    optionally maps storage key -> the resource dict, letting lab
    Observations contribute numeric points; without it labs still
    appear as dated entries in `procedures`-style form via their series
    with no values.
    """
    resources_by_key = resources_by_key or {}

    active_problems: list[SpineEntry] = []
    resolved_problems: list[SpineEntry] = []
    negated: list[SpineEntry] = []
    active_meds: list[SpineEntry] = []
    inactive_meds: list[SpineEntry] = []
    allergies: list[SpineEntry] = []
    procedures: list[SpineEntry] = []
    encounters: list[SpineEntry] = []
    lab_points: dict[tuple[str, str], list[tuple[str, float, str]]] = {}

    for chunk in chunks:
        negating = chunk.verification_status in NEGATING_VERIFICATIONS

        if chunk.resource_type == "Condition":
            if negating:
                negated.append(_entry(chunk))
            elif chunk.clinical_status in INACTIVE_CLINICAL:
                resolved_problems.append(_entry(chunk))
            else:
                active_problems.append(_entry(chunk))

        elif chunk.resource_type == "AllergyIntolerance":
            if negating:
                negated.append(_entry(chunk))
            else:
                allergies.append(_entry(chunk))

        elif chunk.resource_type in ("MedicationRequest", "MedicationStatement"):
            if chunk.clinical_status == "active":
                active_meds.append(_entry(chunk))
            else:
                inactive_meds.append(_entry(chunk))

        elif chunk.resource_type == "Observation":
            value = _lab_value(chunk, resources_by_key)
            code = chunk.codes[0] if chunk.codes else ("", "", "unknown")
            if value is not None and chunk.effective:
                lab_points.setdefault((code[1], code[2]), []).append(
                    (chunk.effective, value, chunk.storage_key)
                )
            else:
                procedures.append(_entry(chunk))

        elif chunk.resource_type == "Procedure":
            procedures.append(_entry(chunk))

        elif chunk.resource_type == "Encounter":
            encounters.append(_entry(chunk))

    labs: list[LabSeries] = []
    for (code, display), points in sorted(lab_points.items()):
        points.sort(key=lambda p: p[0])
        if len(points) == 1:
            trend = "single value"
        else:
            last, prev = points[-1][1], points[-2][1]
            trend = "rising" if last > prev else "falling" if last < prev else "flat"
        labs.append(
            LabSeries(
                code=code,
                display=display,
                points=tuple((p[0], p[1]) for p in points),
                trend=trend,
                storage_keys=tuple(p[2] for p in points),
            )
        )

    return Spine(
        active_problems=_sorted(active_problems),
        resolved_problems=_sorted(resolved_problems),
        negated_assertions=_sorted(negated),
        active_medications=_sorted(active_meds),
        inactive_medications=_sorted(inactive_meds),
        allergies=_sorted(allergies),
        labs=tuple(labs),
        procedures=_sorted(procedures),
        encounters=_sorted(encounters),
    )
# Made by Ryan Gomez & Co. Inc.
