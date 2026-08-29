# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Chunk serialization for the grounded assistant (SPEC §5.1 a, b, f).

The chunk unit is a clinically coherent unit rendered through a
DETERMINISTIC, VERSIONED template carrying subject reference, encounter
reference, effective date/period, code system + code + display, and the
storage object key. Serialization is idempotent — same resource, same
template version, same text, byte for byte — and the template version
is stored on every chunk so a partial re-embed is detectable (§9 makes
template churn a budget line for exactly this reason).

THE RULE THIS FILE EXISTS TO ENFORCE (5.1f): status and negation
survive into chunk text. A penicillin allergy marked `refuted` or
`entered-in-error` that serializes as "penicillin allergy" is a
data-integrity failure that looks exactly like a good retrieval. Every
status-bearing element is rendered explicitly, and the refuted /
entered-in-error states are rendered FIRST in the text so no truncation
or embedding pooling can lose them.

Two more spec rules live here:

- Sensitive-category exclusion happens HERE, at serialization time,
  never at retrieval (5.1b): classify() runs before any text is built,
  and an excluded resource produces no chunk at all — a chunk that was
  never embedded cannot be leaked by a retrieval bug.
- Absence is never a clinical negative (§7.3): a missing must-support
  element renders as "not recorded", never as "inactive", "none", or an
  omission that reads as one. "No known allergy" (SNOMED 716186003)
  and "allergy status not asked" (1631000175102) arrive as CODES and
  render distinctly — they are semantically different answers and must
  not collapse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from core.governance.segmentation import (
    CategoryValueSets,
    SegmentationDecision,
    classify,
)

#: Bumping this invalidates every stored vector (a full-corpus re-embed,
#: §9). Bump it for any change to rendered text; never silently reuse.
#: v2: medicationReference.display renders into chunk text (the
#: medication[x]-as-Reference must-support form).
TEMPLATE_VERSION = "2"


@dataclass(frozen=True)
class Chunk:
    storage_key: str
    resource_type: str
    subject_reference: Optional[str]
    encounter_reference: Optional[str]
    effective: Optional[str]  # ISO date/datetime, or None -> "not recorded"
    clinical_status: Optional[str]
    verification_status: Optional[str]
    codes: tuple[tuple[str, str, str], ...]  # (system, code, display)
    template_version: str
    text: str


@dataclass(frozen=True)
class SerializationResult:
    """Either a chunk or an exclusion — never both, never neither. The
    exclusion carries the segmentation decision so the ETL can count it
    (SegmentationStats) without re-classifying."""

    chunk: Optional[Chunk] = None
    excluded: Optional[SegmentationDecision] = None


def _first_coding_field(resource: Mapping, field: str) -> Optional[str]:
    """The code of the first coding under `resource[field]` — the shape
    CodeableConcept status fields (clinicalStatus, verificationStatus)
    always take in R4."""
    node = resource.get(field)
    if isinstance(node, dict):
        for coding in node.get("coding", []):
            code = coding.get("code")
            if isinstance(code, str):
                return code
    if isinstance(node, str):  # some status fields are plain codes
        return node
    return None


def _reference(resource: Mapping, field: str) -> Optional[str]:
    node = resource.get(field)
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            return ref
    return None


#: Fields checked in order for the resource's effective time. First hit
#: wins; the order prefers clinically-effective over administratively-
#: recorded times.
_EFFECTIVE_FIELDS = (
    "onsetDateTime",
    "effectiveDateTime",
    "occurrenceDateTime",
    "authoredOn",
    "recordedDate",
    "date",
)


def _effective(resource: Mapping) -> Optional[str]:
    for field in _EFFECTIVE_FIELDS:
        value = resource.get(field)
        if isinstance(value, str) and value:
            return value
    period = resource.get("effectivePeriod") or resource.get("onsetPeriod")
    if isinstance(period, dict):
        start = period.get("start")
        if isinstance(start, str) and start:
            return start
    return None


#: Code-bearing fields rendered into the chunk, per resource shape.
#: `medicationCodeableConcept` and `medicationReference` are BOTH
#: handled (§7.2's must-support fixture class: servers need support
#: only one form, so a consumer must handle both).
_CODE_FIELDS = (
    "code",
    "medicationCodeableConcept",
    "vaccineCode",
    "type",
    "category",
    "reasonCode",
)


def _codes(resource: Mapping) -> tuple[tuple[str, str, str], ...]:
    out: list[tuple[str, str, str]] = []
    # medication[x] as Reference (the form _CODE_FIELDS can't see):
    # the reference's display is the drug's only rendering, and a
    # medication chunk reading "no codes recorded" is a silent omission
    # waiting to happen — caught by the §7.2 must-support fixture.
    med_ref = resource.get("medicationReference")
    if isinstance(med_ref, dict) and isinstance(med_ref.get("display"), str):
        out.append(("", "", med_ref["display"]))
    for field in _CODE_FIELDS:
        node = resource.get(field)
        nodes = node if isinstance(node, list) else [node]
        for concept in nodes:
            if not isinstance(concept, dict):
                continue
            for coding in concept.get("coding", []):
                out.append(
                    (
                        coding.get("system") or "",
                        coding.get("code") or "",
                        coding.get("display") or coding.get("code") or "",
                    )
                )
            if not concept.get("coding") and isinstance(concept.get("text"), str):
                out.append(("", "", concept["text"]))
    return tuple(out)


#: verificationStatus values that mean "this assertion is NOT true".
#: They lead the rendered text, unconditionally.
NEGATING_VERIFICATIONS = frozenset({"refuted", "entered-in-error"})

#: clinicalStatus values that mean "not currently active".
INACTIVE_CLINICAL = frozenset({"inactive", "resolved", "remission"})


def _status_fields(resource: Mapping) -> tuple[Optional[str], Optional[str]]:
    resource_type = resource.get("resourceType")
    if resource_type in ("Condition", "AllergyIntolerance"):
        return (
            _first_coding_field(resource, "clinicalStatus"),
            _first_coding_field(resource, "verificationStatus"),
        )
    if resource_type in ("MedicationRequest", "MedicationStatement", "ServiceRequest"):
        status = resource.get("status")
        return (status if isinstance(status, str) else None, None)
    if resource_type in ("DocumentReference",):
        status = resource.get("status")
        doc_status = resource.get("docStatus")
        return (
            status if isinstance(status, str) else None,
            doc_status if isinstance(doc_status, str) else None,
        )
    status = resource.get("status")
    return (status if isinstance(status, str) else None, None)


def _not_recorded(value: Optional[str]) -> str:
    """§7.3's rule, in one place: a missing element is *data not present
    in the responder's system* — rendered as exactly that, never as a
    negative."""
    return value if value else "not recorded"


def _render(chunk_fields: dict) -> str:
    """The template. Deterministic: field order is fixed, every field
    always renders (with "not recorded" for absence), and negating
    verification states are promoted to the front of the text."""
    verification = chunk_fields["verification_status"]
    negation_banner = ""
    if verification in NEGATING_VERIFICATIONS:
        negation_banner = (
            f"[{verification.upper()} — this assertion is recorded as NOT true] "
        )

    code_text = (
        "; ".join(
            f"{display} ({system} {code})" if code else display
            for system, code, display in chunk_fields["codes"]
        )
        or "no codes recorded"
    )

    return (
        f"{negation_banner}"
        f"{chunk_fields['resource_type']} | "
        f"status: {_not_recorded(chunk_fields['clinical_status'])} | "
        f"verification: {_not_recorded(verification)} | "
        f"effective: {_not_recorded(chunk_fields['effective'])} | "
        f"{code_text}"
    )


def serialize_resource(
    resource: Mapping,
    storage_key: str,
    value_sets: CategoryValueSets,
    *,
    source_department: Optional[str] = None,
) -> SerializationResult:
    """
    One resource in, one chunk or one counted exclusion out.

    Segmentation runs FIRST (5.1b): if the resource is sensitive or
    unclassifiable, no text is ever built. The classify() engine is
    fail-closed (core/governance/segmentation.py), so this function
    inherits that posture for free.
    """
    decision = classify(resource, value_sets, source_department=source_department)
    if not decision.include:
        return SerializationResult(excluded=decision)

    clinical_status, verification_status = _status_fields(resource)
    fields = {
        "resource_type": resource["resourceType"],
        "clinical_status": clinical_status,
        "verification_status": verification_status,
        "effective": _effective(resource),
        "codes": _codes(resource),
    }
    chunk = Chunk(
        storage_key=storage_key,
        resource_type=fields["resource_type"],
        subject_reference=_reference(resource, "subject")
        or _reference(resource, "patient"),
        encounter_reference=_reference(resource, "encounter")
        or _reference(resource, "context"),
        effective=fields["effective"],
        clinical_status=clinical_status,
        verification_status=verification_status,
        codes=fields["codes"],
        template_version=TEMPLATE_VERSION,
        text=_render(fields),
    )
    return SerializationResult(chunk=chunk)
# Made by Ryan Gomez & Co. Inc.
