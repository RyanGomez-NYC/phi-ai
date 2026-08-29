# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Resolve which encounter a FHIR resource belongs to.

Used for in-context launch: when an EMR launches this platform from an
open encounter, the token response carries an `encounter` id alongside the
patient, and the clinician expects to land on THAT visit - not the
patient's entire twenty-year history with the relevant visit somewhere in
it.

SAME CONSTRAINT AS CLINICAL DATES, for the same reason. The encounter
reference lives inside the resource and cannot be lifted into the index:
an encounter id ties a patient to a specific episode of care on a
specific date, which is exactly the kind of linkage core/db/schema.sql
keeps out. So encounter filtering reads the resources, like date scoping
in core/fhir/clinical_dates.py. Bounded by one patient's record count.

WHAT AN ENCOUNTER-SCOPED VIEW IS AND IS NOT. It is a convenience for a
clinician who launched from a specific visit. It is NOT a claim that
these are all the records for that visit - resources whose encounter link
is absent or recorded differently will not appear. The interface says so
and offers the full record one click away, because a clinician who
believes they are seeing a complete visit when they are not is worse off
than one who knows they are seeing a filtered view.
"""

from __future__ import annotations

from typing import Any, Optional

# Where each resource type records its encounter. R4 is not uniform here:
# most use `encounter`, DocumentReference nests it under `context`, and
# MedicationAdministration calls the field `context` outright.
ENCOUNTER_FIELDS: dict[str, tuple[str, ...]] = {
    "Observation": ("encounter",),
    "Condition": ("encounter",),
    "Procedure": ("encounter",),
    "MedicationRequest": ("encounter",),
    "MedicationAdministration": ("context",),
    "DiagnosticReport": ("encounter",),
    "ServiceRequest": ("encounter",),
    "DocumentReference": ("context.encounter", "context.encounter.0"),
    "AdverseEvent": ("encounter",),
    "Immunization": ("encounter",),
    "ExplanationOfBenefit": (),
    "AllergyIntolerance": ("encounter",),
    "Consent": (),
    "Patient": (),
}

GENERIC_FIELDS = ("encounter", "context.encounter", "context")


def _reference_at(resource: dict, path: str) -> Optional[str]:
    node: Any = resource
    for part in path.split("."):
        if isinstance(node, list):
            if not node:
                return None
            node = node[0]
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None

    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        reference = node.get("reference")
        return reference if isinstance(reference, str) else None
    return None


def encounter_reference(resource: dict) -> Optional[str]:
    """The 'Encounter/<id>' this resource belongs to, or None.

    An Encounter resource resolves to ITSELF, so launching into an
    encounter shows the encounter alongside the observations and orders
    recorded during it rather than omitting the visit it is scoped to.
    """
    resource_type = resource.get("resourceType", "")

    if resource_type == "Encounter":
        rid = resource.get("id")
        return f"Encounter/{rid}" if rid else None

    paths = ENCOUNTER_FIELDS.get(resource_type)
    if paths is None:
        paths = GENERIC_FIELDS
    elif not paths:
        return None

    for path in paths:
        reference = _reference_at(resource, path)
        if reference and reference.startswith("Encounter/"):
            return reference

    for path in GENERIC_FIELDS:
        reference = _reference_at(resource, path)
        if reference and reference.startswith("Encounter/"):
            return reference

    return None


def resource_in_encounter(resource: dict, encounter_id: str) -> bool:
    if not encounter_id:
        return True
    reference = encounter_reference(resource)
    return reference == f"Encounter/{encounter_id}"
# Made by Ryan Gomez & Co. Inc.
