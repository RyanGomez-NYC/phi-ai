# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
How stored resources map onto storage objects.

Two layouts, selected by the scale profile (core/config/scale_profile.py).
Everything that derives a storage key goes through here, so the two shapes
cannot drift apart in different modules.

PER_RESOURCE - fhir/{Type}/{id}.json, one object per resource.
    Finest possible granularity: a digest per resource, disposal of one
    record without touching another, and an index row that points at
    exactly one thing. Correct until the object count is the problem.

BUNDLED - fhir/{Type}/{patientId}.ndjson, one object per (patient, type).
    Roughly 500x fewer objects at a large IDN's resource counts, and the
    index shrinks by the same factor.

THE KEY SHAPES ABOVE ARE FROZEN. They are not merely conventions: AWS IAM
policies scope permission on the `fhir/*` prefix, so the key layout is
part of the access-control model, and every object already written is
reachable only at the key it was written under. The product rename to the
PHI AI Platform changed no key, prefix or file extension here, and none
should be changed without treating it as a data migration plus an IAM
change rather than a rename.

WHY BUNDLE ON (PATIENT, TYPE) AND NOT PATIENT ALONE. Retention is
configurable per resource type. A patient-only bundle would put a
DocumentReference retained for ten years in the same object as an
Observation retained for six, and disposal could then express neither
without rewriting the bundle and splitting it. Bundling on the type as
well keeps every resource in an object subject to one retention period,
so the object is the unit disposal already works in.

WHY NDJSON. One resource per line, append-friendly, and the exact format
core/fhir/bulk_export.py already produces and every FHIR-aware system
already reads. A bundle is therefore readable by the receiving end of a
migration with no knowledge of this project - which is the same promise
the per-resource layout makes, kept at a different granularity.

WHAT BUNDLING COSTS, stated here because it is easy to discover late:

  * Integrity is per bundle. A digest mismatch identifies the patient and
    type affected, not the individual resource.
  * Disposing ONE resource means rewriting its bundle. Disposal is
    normally per-patient or per-retention-period, where a bundle is
    exactly the right unit - but admin-order purge of a single record
    becomes read-modify-write.
  * Lookup by resource id requires reading the patient's bundle. Every
    dominant read path here is already patient-scoped, so this is rarely
    the shape needed.

THIS IS SAFE ONLY BECAUSE THE SOURCE IS RETIRED. Bundling assumes the
data is static once migrated: each bundle is written once, at the end of
that patient's migration. Against a live, appending EMR this layout would
mean constant rewrites, and the per-resource layout would be correct.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

log = logging.getLogger("phi-ai.storage.layout")

FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class LayoutError(ValueError):
    pass


def _safe(component: str, what: str) -> str:
    """Reject anything that could escape the key namespace.

    A resource id arrives from an EMR and is therefore untrusted input.
    An id containing '../' or a slash would write outside its intended
    prefix - which in a bucket means one patient's bundle overwriting
    another's. FHIR already constrains ids to this character set, so
    rejecting is correct rather than merely cautious.
    """
    value = (component or "").strip()
    if not FHIR_ID.match(value):
        raise LayoutError(
            f"{what} {component!r} is not a valid FHIR id. Storage keys are derived from "
            "it, so anything outside [A-Za-z0-9-.] is refused rather than sanitised."
        )
    return value


@dataclass(frozen=True)
class ResourceLocation:
    """Where one resource lives, under whichever layout is in force."""

    storage_key: str
    bundled: bool
    # Under BUNDLED this identifies the group sharing the object; under
    # PER_RESOURCE it is the resource itself.
    group_key: str

    @property
    def content_type(self) -> str:
        return "application/fhir+ndjson" if self.bundled else "application/fhir+json"


def patient_id_of(resource: dict) -> Optional[str]:
    """The patient this resource belongs to, for bundling.

    Reuses core/db/index.py's extract_patient_reference so "which patient"
    has ONE definition in this codebase. A second implementation here
    would eventually disagree with the index, and resources would be
    bundled under one patient while indexed under another.
    """
    from core.db.index import extract_patient_reference

    reference = extract_patient_reference(resource)
    return reference.split("/", 1)[1] if reference else None


def locate(resource: dict, profile) -> ResourceLocation:
    """Where this resource should be stored under `profile`.

    `profile` is a core.config.scale_profile.ScaleProfile.
    """
    resource_type = _safe(resource.get("resourceType", ""), "resourceType")

    if not profile.bundles:
        resource_id = _safe(str(resource.get("id", "")), "resource id")
        key = f"fhir/{resource_type}/{resource_id}.json"
        return ResourceLocation(storage_key=key, bundled=False, group_key=key)

    patient_id = patient_id_of(resource)
    if patient_id is None:
        # A resource with no patient link cannot be bundled by patient.
        # Stored individually rather than dropped or forced into a
        # catch-all: an unlinked resource is rare, and losing it to make
        # the layout uniform would be the wrong trade.
        resource_id = _safe(str(resource.get("id", "")), "resource id")
        key = f"fhir/{resource_type}/_unlinked/{resource_id}.json"
        log.debug("%s/%s has no patient link; storing individually", resource_type, resource_id)
        return ResourceLocation(storage_key=key, bundled=False, group_key=key)

    patient_id = _safe(patient_id, "patient id")
    key = f"fhir/{resource_type}/{patient_id}.ndjson"
    return ResourceLocation(storage_key=key, bundled=True,
                            group_key=f"{resource_type}/{patient_id}")


def group_for_bundling(resources: Iterable[dict], profile) -> dict[str, list[dict]]:
    """Group resources by the object each will be written into.

    Returns {storage_key: [resource, ...]}. Under PER_RESOURCE every list
    holds exactly one, so a caller can use one code path for both layouts
    rather than branching on the profile at every write site.
    """
    grouped: dict[str, list[dict]] = {}
    for resource in resources:
        location = locate(resource, profile)
        grouped.setdefault(location.storage_key, []).append(resource)

    if profile.bundles:
        oversized = [
            (key, len(items)) for key, items in grouped.items()
            if len(items) > profile.max_bundle_resources
        ]
        for key, count in oversized:
            # Not split automatically: splitting would need a stable
            # naming scheme for the parts, and silently producing
            # `...ndjson.2` would break every reader that assumes one
            # bundle per (patient, type). Loud, and rare enough to be
            # worth an operator's attention.
            log.warning(
                "bundle %s holds %d resources, above max_bundle_resources=%d. It will be "
                "written whole; consider a time-windowed key scheme if this is common.",
                key, count, profile.max_bundle_resources,
            )
    return grouped


def serialise_bundle(resources: list[dict]) -> bytes:
    """NDJSON: one resource per line, keys sorted.

    Sorted keys make the bytes deterministic, so re-serialising an
    unchanged bundle produces an identical digest. Without that, every
    re-run would look like a content change to integrity verification.
    """
    lines = (json.dumps(r, sort_keys=True, separators=(",", ":")) for r in resources)
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_bundle(payload: bytes) -> Iterator[dict]:
    """Yield resources from an NDJSON bundle, skipping unreadable lines.

    A malformed line is logged and skipped rather than failing the whole
    bundle: one bad line must not make a patient's entire record
    unreadable, and the verification path reports the discrepancy
    separately.
    """
    for number, line in enumerate(payload.decode("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            log.error("bundle line %d is not valid JSON, skipping: %s", number, exc)
# Made by Ryan Gomez & Co. Inc.
