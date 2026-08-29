# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Extract the clinically meaningful date from a FHIR resource.

WHY THIS EXISTS RATHER THAN A SQL COLUMN. A records request scoped to
"2019 through 2021" means DATES OF SERVICE, not when this system happened
to store the record. Those are completely different: an EHR retired in
2026 gets its entire twenty-year history stored over a few days, so
`stored_at` (a real column - see core/db/schema.sql) would put every
record in 2026 and a date-scoped release would return either everything
or nothing. Filtering on the stored-at timestamp is not an approximation
of clinical date - it is unrelated to it, and using it would silently
produce a wrong record set for a legal request.

The correct date lives inside the resource, which means the index cannot
hold it. Under HIPAA's Safe Harbor de-identification standard (45 CFR
164.514(b)(2)), dates directly related to an individual are themselves
identifiers - so a `service_date` column next to `patient_reference`
would put PHI in the index and break the rule core/db/schema.sql exists
to enforce.

The consequence, stated plainly because it is a real cost: date-scoped
release reads and decrypts every candidate resource for that patient
rather than filtering in SQL. That is slower. It is correct, and it keeps
the index free of clinical content. For one patient's records - the only
scope a release ever has - it is a bounded cost.

WHAT THIS DOES NOT DO: reconcile disagreeing dates. A Condition carries
both an onset and a recorded date, and they can be years apart. This
returns the one most defensible as "when this clinically happened",
preferring onset/effective/performed over administrative timestamps, and
records which field it used so a reviewer can see the basis.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

# Per-type preference order. Clinical fact first, administrative
# timestamp second - "when did this happen" beats "when was it typed in",
# which is what a request for records of a period is asking about.
DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "Encounter": ("period.start", "period.end"),
    "Observation": ("effectiveDateTime", "effectivePeriod.start", "issued"),
    "Condition": ("onsetDateTime", "onsetPeriod.start", "recordedDate"),
    "MedicationRequest": ("authoredOn",),
    "MedicationAdministration": ("effectiveDateTime", "effectivePeriod.start"),
    "DocumentReference": ("context.period.start", "date"),
    "AllergyIntolerance": ("onsetDateTime", "recordedDate"),
    "Immunization": ("occurrenceDateTime",),
    "Procedure": ("performedDateTime", "performedPeriod.start"),
    "DiagnosticReport": ("effectiveDateTime", "effectivePeriod.start", "issued"),
    "ServiceRequest": ("occurrenceDateTime", "occurrencePeriod.start", "authoredOn"),
    "AdverseEvent": ("date", "detected", "recordedDate"),
    "Consent": ("dateTime",),
    "ExplanationOfBenefit": ("billablePeriod.start", "created"),
    # Patient is demographic, not an event. It has no service date, and
    # scoping it out of a date-limited release would produce a record set
    # with no patient in it - see resource_in_scope().
    "Patient": (),
}

# Tried for any resource type not listed above, so a newly supported type
# still yields a date instead of silently dropping out of scoped releases.
GENERIC_FIELDS = (
    "effectiveDateTime", "effectivePeriod.start", "occurrenceDateTime",
    "performedDateTime", "onsetDateTime", "recordedDate", "authoredOn",
    "date", "created", "issued", "period.start",
)

# Types with no meaningful service date. Always in scope for a date-bound
# release, because excluding them yields a record set that cannot be read:
# a bundle of observations with no patient resource identifies nobody.
ALWAYS_IN_SCOPE = frozenset({"Patient"})


def _dig(resource: dict, path: str) -> Optional[str]:
    node: Any = resource
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node if isinstance(node, str) else None


def parse_fhir_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse a FHIR date, dateTime or instant into an aware datetime.

    FHIR permits partial precision - "2019", "2019-07", "2019-07-04" are
    all valid dateTimes. A partial date is anchored to its EARLIEST
    instant, so a resource dated "2019" falls inside a scope starting
    2019-01-01. Anchoring to the latest instant instead would drop
    genuinely in-scope records from a legal production, which is the
    worse error of the two.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    for candidate, fmt in ((text, None), (f"{text}-01-01", None), (f"{text}-01", None)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # FHIR allows a date with no zone. Treating it as UTC is a
            # stated assumption, not a silent one - a scope boundary can
            # shift by hours across zones, which matters far less than
            # dropping the record entirely.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def clinical_date(resource: dict) -> tuple[Optional[datetime], Optional[str]]:
    """(date, field_it_came_from). Both None when the resource carries none.

    Returning the source field is not decoration: a reviewer asked why a
    record was included in - or left out of - a date-scoped legal release
    needs to see which field decided it, particularly for a Condition
    whose onset and recorded dates differ by years.
    """
    resource_type = resource.get("resourceType", "")
    candidates = DATE_FIELDS.get(resource_type, GENERIC_FIELDS)
    if resource_type in DATE_FIELDS and not candidates:
        return (None, None)

    for path in candidates:
        parsed = parse_fhir_datetime(_dig(resource, path))
        if parsed is not None:
            return (parsed, path)

    # Fall through to the generic list for a listed type whose preferred
    # fields were all absent - a sparse resource should not drop out of a
    # release just because its usual field is empty.
    if resource_type in DATE_FIELDS:
        for path in GENERIC_FIELDS:
            parsed = parse_fhir_datetime(_dig(resource, path))
            if parsed is not None:
                return (parsed, path)

    return (None, None)


def resource_in_scope(
    resource: dict,
    scope_start: Optional[datetime] = None,
    scope_end: Optional[datetime] = None,
    resource_types: Optional[frozenset[str]] = None,
) -> tuple[bool, str]:
    """(included, reason). The reason is recorded in the export manifest.

    UNDATED RESOURCES ARE INCLUDED, not excluded, and this is the one
    judgement call in the module. A resource whose date cannot be
    determined might fall inside the requested period or outside it -
    nothing here knows. Excluding it silently under-produces a legal
    record set, which is the error with consequences; including it
    over-produces, which a reviewer can see and set aside. The manifest
    marks these explicitly so the decision is visible rather than
    disguised as a match.
    """
    resource_type = resource.get("resourceType", "")

    if resource_types is not None and resource_type not in resource_types:
        return (False, f"resource type {resource_type} not in requested scope")

    if resource_type in ALWAYS_IN_SCOPE:
        return (True, "demographic resource - included in every release")

    if scope_start is None and scope_end is None:
        return (True, "no date scope requested")

    when, field = clinical_date(resource)
    if when is None:
        return (True, "INCLUDED WITHOUT A DATE - no clinical date found; review for relevance")

    if scope_start is not None and when < scope_start:
        return (False, f"{field}={when.date().isoformat()} is before the requested period")
    if scope_end is not None and when > scope_end:
        return (False, f"{field}={when.date().isoformat()} is after the requested period")

    return (True, f"{field}={when.date().isoformat()}")


def coerce_scope_bound(raw: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """Parse an operator-entered YYYY-MM-DD boundary.

    `end_of_day` makes an end bound inclusive of its whole day: a request
    for records "through 2021-12-31" means everything that day, not
    everything before midnight that morning.
    """
    if not raw or not raw.strip():
        return None
    parsed = parse_fhir_datetime(raw.strip())
    if parsed is None:
        raise ValueError(f"could not read {raw!r} as a date - use YYYY-MM-DD")
    if end_of_day and parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed
# Made by Ryan Gomez & Co. Inc.
