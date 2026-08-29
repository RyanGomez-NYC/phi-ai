# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deletes the OMOP CDM row(s) (core/db/omop_schema.sql) derived from a
disposed resource. Added for core/fhir/purge.py's disposal
completeness fix (2026-08-17 audit, C4): before this module existed,
purge.py deleted only the stored object - every OMOP row ETL'd from
that resource, holding identified PHI (DOB, diagnoses, medication
exposures - see omop_schema.sql's own header), survived indefinitely.
For an admin-order removal specifically this was the worst case: the
very record ordered removed remained fully queryable, traceable back to
the "removed" object via its own source_storage_key provenance column -
the exact mechanism that column exists for, working against the
disposal it should have supported instead of for it.

FK-SAFE DELETE ORDER. Each cdm table's source_storage_key column is
UNIQUE (omop_schema.sql), so a single disposed resource matches AT MOST
ONE row, in exactly one table - deleting a Condition's OMOP row never
touches cdm.person. The one real ordering hazard: cdm.person and
cdm.visit_occurrence are REFERENCED BY every event table via a plain FK
with no ON DELETE CASCADE (deliberately - see below), so disposing a
Patient or Encounter resource while OMOP rows for that same patient's
OTHER, still-retained resources still exist would hit a foreign-key
violation if attempted out of order. _OMOP_DELETE_ORDER below lists
child tables before the parents they reference; core/fhir/purge.py
sorts a multi-resource disposal batch to this same order before calling
delete_by_source_storage_key(), so the realistic "dispose this
patient's whole record set in one run" case (every child removed before
the person row) works cleanly - proven end-to-end against live
PostgreSQL 16 running the real omop_schema.sql/omop_bootstrap_aws.sql
as the phi_ai_disposition role (2026-08-17 audit execution notes).

Deliberately NOT ON DELETE CASCADE: a person/visit row being removed
should never silently take other, independently-retained clinical
events with it. If children still exist when a person/visit delete is
attempted, that foreign-key violation is exactly the correct outcome -
see delete_by_source_storage_key()'s own docstring below - not a bug to
design around with a broader cascade.
"""

from __future__ import annotations

from typing import Any, Optional

# Children before the parents they reference (cdm.person,
# cdm.visit_occurrence) - see this module's own docstring. Table names
# here are a fixed, hardcoded constant, never derived from caller
# input - the f-string below is safe from injection for exactly that
# reason, not because inputs are validated.
_OMOP_DELETE_ORDER: tuple[str, ...] = (
    "condition_occurrence",
    "procedure_occurrence",
    "drug_exposure",
    "measurement",
    "observation",
    "visit_occurrence",
    "person",
)


def delete_by_source_storage_key(conn: Any, source_storage_key: str) -> Optional[str]:
    """
    Deletes the single cdm.* row (if any) matching source_storage_key,
    checking tables in the FK-safe order above. Returns the table name
    a row was actually deleted from, or None if source_storage_key has
    no OMOP row at all - a normal outcome when OMOP is disabled, the
    disposed resource's type isn't OMOP-mapped (DocumentReference,
    AllergyIntolerance, ExplanationOfBenefit - see omop_schema.sql), or
    the resource was stored before OMOP was enabled.

    Raises on a foreign-key violation rather than catching and skipping
    it: a violation here means either the disposal batch omitted a
    still-existing dependent resource (dispose the Encounter/Condition
    rows in the same run, or first) or this function was called out of
    FK-safe order by a bug in the caller. Either way this is a "some
    clinical fact for this person is still retained; removing the
    person identity underneath it would orphan that fact" condition,
    which this project's no-silent-fallback invariant says must fail
    loud, not be silently skipped or cascade-deleted through.

    Each table's `WHERE source_storage_key = %s` requires SELECT on
    that column specifically - granted, column-scoped only, to the
    phi_ai_disposition role in core/db/omop_bootstrap_aws.sql, the
    same minimum-necessary shape as the ETL role's own pk-column grants
    (2026-08-17 audit, C1b).

    Cursor cleanup is explicit close() in finally, not
    `with conn.cursor()` - DB-API 2.0 does not promise cursors are
    context managers, and GCP connections elsewhere in this project are
    pg8000, not psycopg (see core/db/connection.py). Disposition is
    AWS-only today (core/fhir/purge.py), but this module follows the
    same discipline as core/db/index.py/omop_etl.py rather than
    assuming that stays true forever.
    """
    cur = conn.cursor()
    try:
        for table in _OMOP_DELETE_ORDER:
            cur.execute(
                f"DELETE FROM cdm.{table} WHERE source_storage_key = %s",
                (source_storage_key,),
            )
            if cur.rowcount > 0:
                conn.commit()
                return table
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
# Made by Ryan Gomez & Co. Inc.
