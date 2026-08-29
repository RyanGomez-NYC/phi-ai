# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Read/write operations against the Postgres index.

See core/db/schema.sql for the hard rule this module enforces in code as
well as in the table definition: no clinical content, ever. Every value
written here is either a structural fact about the stored object
(resource type, storage key, hash, timestamps) or an opaque,
EMR-internal FHIR reference - never a name, MRN, DOB, SSN, or any field
whose value came from inside the clinical resource body itself.

CONNECTIONS ARE NOT ALWAYS psycopg. core/db/connection.py returns a
psycopg connection on AWS/Azure and a pg8000 connection on GCP (the
Cloud SQL Python Connector does not support psycopg's underlying
driver). This module therefore restricts itself to the DB-API 2.0
surface both drivers guarantee - cursor()/execute()/commit()/rollback()/
fetchall()/description/close() - and classifies database errors through
core/db/pg_errors.py rather than any driver's own exception classes.
Cursor cleanup uses explicit close() in finally blocks, not
`with conn.cursor()`: DB-API 2.0 does not promise cursors are context
managers, and depending on a per-driver extension here is exactly the
kind of coupling that already broke this module once on GCP (see
write_index_entry()'s FIXED note).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from core.db.pg_errors import is_unique_violation


@dataclass(frozen=True)
class IndexEntry:
    resource_type: str
    resource_id: str
    storage_key: str
    sha256_hex: str
    resource_count: int = 1
    patient_reference: Optional[str] = None
    storage_version_id: Optional[str] = None
    retention_until: Optional[datetime] = None


def extract_patient_reference(resource: dict) -> Optional[str]:
    """
    Pull the internal FHIR reference linking a resource to a patient,
    e.g. "Patient/eAB12cd3". This is the source EMR's own opaque
    server-assigned ID (Epic's today; structurally the same format any
    FHIR R4-compliant EMR uses - this function reads the standard
    resourceType/id/subject/patient fields the specification defines,
    nothing Epic-specific), structurally identical to what already
    appears in every stored object's storage key - not a real-world
    identifier like an MRN or name.

    Returns None for resources with no patient linkage (e.g. some
    administrative resource types), which is a normal, expected outcome,
    not an error.
    """
    if resource.get("resourceType") == "Patient":
        rid = resource.get("id")
        return f"Patient/{rid}" if rid else None

    for field in ("subject", "patient"):
        ref = resource.get(field, {}).get("reference")
        if ref and ref.startswith("Patient/"):
            return ref

    return None


def write_index_entry(conn: Any, entry: IndexEntry) -> None:
    """
    Insert one index row. Idempotent under retry (e.g. the scheduler
    re-processing a resource after a partial prior failure) the same way
    writes already are at the storage layer - a duplicate
    (resource_type, resource_id) is silently absorbed, not raised as an
    error.

    DELIBERATELY NOT using INSERT ... ON CONFLICT DO NOTHING for this.
    ON CONFLICT requires the SELECT privilege on the table, in addition
    to INSERT - Postgres has to be able to check whether a conflicting
    row exists, which means being able to read the table, not just write
    to it. phi_ai_ingest (the role this runs as - see
    core/db/bootstrap_aws.sql/bootstrap_gcp.sql/bootstrap_azure.sql and
    the ingest identity's IAM/service-account grants in each cloud's
    deploy/ stack) is deliberately INSERT-only, by
    the same design principle used everywhere else in this project: the
    ingest role can never decrypt PHI, can only append to the audit
    chain tip it needs, and so on. Granting it SELECT here just to
    satisfy ON CONFLICT would quietly reopen a read path that was closed
    on purpose, for a convenience that has a plain-INSERT equivalent - a
    unique constraint violation on a duplicate insert is a specific,
    detectable error under every Postgres client library, and catching
    it achieves the identical idempotent behavior using only the INSERT
    privilege already granted.

    FIXED - the duplicate detection here was previously
    `except psycopg.errors.UniqueViolation`, after an unconditional
    `import psycopg` at the top of this function. Both halves were
    wrong on GCP, where core/db/connection.py returns a pg8000
    connection, not a psycopg one (the Cloud SQL Python Connector does
    not support psycopg's driver): a GCP duplicate insert raises
    pg8000's own exception class, which fell through to the generic
    rollback-and-raise below and propagated as a real index failure -
    so every re-run or backfill on GCP produced one spurious error per
    already-indexed resource, and the "duplicates are absorbed"
    recovery contract core/db/reconcile.py documents was false on GCP
    specifically. (The stray `import psycopg` also meant a GCP image
    without psycopg installed died on the import itself, before any
    query ran - masked today only because psycopg happens to ship in
    the shared requirements.) Classification now goes through
    core/db/pg_errors.py's is_unique_violation(), which reads the
    SQLSTATE each driver carries structurally - verified against live
    PostgreSQL 16 (psycopg) and pg8000's documented server-error shape;
    see that module's own docstring.

    Callers should treat failures here as non-fatal to the overall
    ingestion operation - the storage backend and the audit log are the
    system of record; this index is a derived, rebuildable
    convenience. See core/fhir/client.py for how the ingestion client
    applies that policy.

    Rolls back on any failure (including the expected unique-violation
    case) before returning or re-raising. Without this, a failed write
    leaves the connection's transaction aborted, and - since
    scheduler.py holds one connection open for the entire run rather
    than reconnecting per resource - every subsequent write on that same
    run fails too, with Postgres's generic "current transaction is
    aborted" message rather than the real underlying error.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO stored_resources
                    (resource_type, resource_id, patient_reference, storage_key,
                     storage_version_id, sha256_hex, retention_until, resource_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entry.resource_type,
                    entry.resource_id,
                    entry.patient_reference,
                    entry.storage_key,
                    entry.storage_version_id,
                    entry.sha256_hex,
                    entry.retention_until,
                    entry.resource_count,
                ),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception as exc:
        # Roll back either way: the failed statement left the
        # transaction aborted, and this connection is reused for every
        # subsequent write this run.
        conn.rollback()
        if not is_unique_violation(exc):
            raise
        # Already indexed - an idempotent retry, not a real error.


def delete_index_entry(conn: Any, storage_key: str) -> bool:
    """
    Deletes the index row for storage_key, if one exists. Added for
    core/fhir/purge.py's disposal completeness fix (2026-08-17 audit,
    C4): every other write path into this table is INSERT-only by
    design (see write_index_entry() above) - this is the one
    deliberate exception, granted ONLY to a separate, narrowly-scoped
    phi_ai_disposition role (core/db/bootstrap_aws.sql), never to
    phi_ai_ingest or phi_ai_reader. Before this function
    existed, a purged object left its index row behind
    permanently: a later restore-by-patient would find a storage_key
    pointing at nothing, and core/db/reconcile.py would report it as an
    orphaned row indistinguishable from actual tampering (see
    runbooks/RUNBOOK_INDEX_MAINTENANCE.md) - a false tampering finding
    triggered by routine, authorized disposition.

    Returns True if a row was deleted, False if storage_key had no
    index row to begin with - a normal, expected outcome when the
    Postgres index isn't configured, or the disposed resource was
    stored before indexing was enabled, not an error condition
    callers need to treat specially.

    The `WHERE storage_key = %s` clause requires the connecting role to
    hold SELECT on that column specifically - Postgres requires SELECT
    on any column a DELETE's WHERE clause reads, the identical rule
    already documented for the re-ETL UPDATE path in
    core/db/omop_bootstrap_aws.sql (2026-08-17 audit, C1b). Granted,
    column-scoped only, in core/db/bootstrap_aws.sql - this role cannot
    read resource_type, patient_reference, sha256_hex, or any other
    column through this grant.

    Cursor cleanup is explicit close() in finally, not
    `with conn.cursor()` - see this module's own docstring on why.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM stored_resources WHERE storage_key = %s", (storage_key,))
            deleted = cur.rowcount > 0
        finally:
            cur.close()
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise


def read_index_state(conn: Any, key: str) -> Optional[str]:
    """
    Reads one row's value from the index_state table (core/db/schema.sql)
    by key, or None if no row exists for that key yet - a normal,
    expected condition (e.g. the scheduler's first-ever run), not an
    error.

    Added for core/fhir/scheduler.py's watermark fix (2026-08-17 audit,
    H1; implemented 2026-08-18) - see that module's own
    SCHEDULER_WATERMARK_KEY comment for the full account of why the
    watermark lives here rather than in the storage backend. index_state
    is a generic key/value table (core/db/schema.sql), not
    scheduler-specific by construction, so this function takes an
    arbitrary key rather than hardcoding the scheduler's own key name -
    a future caller with its own small, durable piece of operational
    state (not PHI - see schema.sql's own header on what belongs in
    this database at all) can reuse this directly instead of growing a
    second key/value table.

    Cursor cleanup is explicit close() in finally, not
    `with conn.cursor()` - see this module's own docstring on why.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM index_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row is not None else None
    finally:
        cur.close()


def write_index_state(conn: Any, key: str, value: str) -> None:
    """
    Upserts one row in the index_state table. Unlike
    write_index_entry()'s deliberate avoidance of INSERT ... ON
    CONFLICT above, that constraint doesn't apply here: phi_ai_ingest
    is explicitly granted SELECT on index_state (core/db/bootstrap_aws.sql),
    in addition to INSERT and UPDATE, specifically because index_state is
    a genuine key/value store meant to be overwritten - unlike
    stored_resources, which is append-only by design and where granting
    SELECT was the thing being deliberately avoided. ON CONFLICT is the
    correct, simplest tool for this table's shape, not a reversal of that
    earlier decision - the two tables have different access patterns and
    different grants for exactly that reason.

    Added for core/fhir/scheduler.py's watermark fix (2026-08-17 audit,
    H1; implemented 2026-08-18) - see read_index_state() above and that
    module's own SCHEDULER_WATERMARK_KEY comment. Proven against live
    PostgreSQL 16 running the real bootstrap_aws.sql grants as the
    phi_ai_ingest role: an empty first read, a first INSERT, and a
    same-key second write that upserts in place rather than erroring or
    duplicating (2026-08-17 audit execution notes).

    Rolls back on any failure before re-raising, matching every other
    write function in this module - a failed statement here would
    otherwise leave the connection's transaction aborted for the rest of
    a long-running scheduler cycle (see write_index_entry()'s own
    docstring for the full reasoning, identical here).
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO index_state (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def find_by_patient_reference(conn: Any, patient_reference: str) -> list[dict]:
    """
    Every stored resource linked to a given internal Patient reference
    (e.g. "Patient/eAB12cd3"). Typical use: a records request needs
    "everything held for this patient" as the starting point for a
    restore - see runbooks/RUNBOOK_DATA_RESTORE.md.

    Returns index rows only (storage keys, types, timestamps) - never
    resource content. Restoring the actual data requires the separate
    restore role and its decrypt path.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT resource_type, resource_id, storage_key, storage_version_id,
                   sha256_hex, stored_at, retention_until
            FROM stored_resources
            WHERE patient_reference = %s
            ORDER BY stored_at
            """,
            (patient_reference,),
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def find_by_type(
    conn: Any, resource_type: str, since: Optional[datetime] = None
) -> list[dict]:
    """All stored resources of a given FHIR type, optionally filtered
    to those stored after `since`. Useful for volume reporting and
    spot-checking a resource type's coverage."""
    cur = conn.cursor()
    try:
        if since:
            cur.execute(
                """
                SELECT resource_type, resource_id, storage_key, storage_version_id,
                       sha256_hex, stored_at, retention_until
                FROM stored_resources
                WHERE resource_type = %s AND stored_at >= %s
                ORDER BY stored_at
                """,
                (resource_type, since),
            )
        else:
            cur.execute(
                """
                SELECT resource_type, resource_id, storage_key, storage_version_id,
                       sha256_hex, stored_at, retention_until
                FROM stored_resources
                WHERE resource_type = %s
                ORDER BY stored_at
                """,
                (resource_type,),
            )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def iter_indexed_keys(conn: Any, batch_size: int = 10_000):
    """Yield every indexed storage_key in sorted order, in batches.

    The streaming counterpart to list_indexed_keys(), for
    core/db/reconcile.py's merge-join reconciliation. Keyset pagination
    rather than OFFSET: OFFSET makes the database scan and discard every
    preceding row, so page N costs O(N) and a full walk is quadratic -
    which is precisely the shape that fails on a large deployment.
    """
    last = ""
    while True:
        conn_cursor = conn.cursor()
        try:
            conn_cursor.execute(
                "SELECT storage_key FROM stored_resources "
                "WHERE storage_key > %s ORDER BY storage_key LIMIT %s",
                (last, batch_size),
            )
            rows = conn_cursor.fetchall()
        finally:
            conn_cursor.close()

        if not rows:
            return
        for row in rows:
            yield row[0]
        last = rows[-1][0]


def list_indexed_keys(conn: Any) -> set[str]:
    """
    Every storage_key currently present in the index, as a set. Used for
    reconciliation against the actual contents of the storage backend
    (core/db/reconcile.py) - the storage backend is the system of
    record, so this is compared against a live listing to find
    drift, never treated as authoritative on its own.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT storage_key FROM stored_resources")
        return {row[0] for row in cur.fetchall()}
    finally:
        cur.close()
# Made by Ryan Gomez & Co. Inc.
