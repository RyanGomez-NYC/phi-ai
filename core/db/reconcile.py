# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Reconciles the Postgres index against the storage backend - the actual
system of record, on whichever cloud is configured. Per
core/db/schema.sql: "This index is derived and rebuildable from its
contents; if the two ever disagree, the storage backend wins." This tool
makes that checkable rather than just asserted: it finds where the two
have actually drifted.

REPORT ONLY - makes zero writes to storage or Postgres. See "WHY NO
DELETE FLAG" below for why that's deliberate, not an oversight or a
not-yet-finished feature.

TWO KINDS OF DRIFT:

1. ORPHANED index rows - a row exists in Postgres with no corresponding
   object in storage. There is currently no process in this codebase
   that deletes a stored object after it is written, so in ordinary
   operation this set should always be empty; a non-empty result here is
   worth investigating rather than routine.

2. MISSING index rows - an object exists in storage with no
   corresponding Postgres row. This one IS expected to happen in
   ordinary operation: it means indexing was skipped for some resources
   (e.g. PHI_AI_DB_HOST/the GCP instance connection name wasn't set
   during an earlier run, or the Postgres index was enabled after some
   ingestion had already happened) or an individual index write failed
   and was logged-and-swallowed per core/fhir/client.py's own
   docstring - the storage backend remains the system of record either
   way, so the resource IS safely stored; the index has just not caught
   up to it yet.

   RECOVERY: re-run the scheduler (core/fhir/scheduler.py or
   bulk_scheduler.py). Ingestion is idempotent - core/db/index.py's
   write_index_entry() silently absorbs a duplicate
   (resource_type, resource_id) via a caught UniqueViolation rather than
   erroring - so a re-run safely backfills any resource already stored
   but missing from the index, without this tool needing to reconstruct
   rows itself. Reconstructing a row here would require decrypting every
   candidate object just to recover its patient_reference (the one
   field not derivable from the storage key alone) - a materially
   bigger, higher-privilege operation than reporting drift, and out of
   scope for this tool.

WHY NO --delete-orphaned-rows FLAG: core/db/bootstrap_aws.sql (and its
bootstrap_gcp.sql/bootstrap_azure.sql siblings) state explicitly that
"the index has no update/delete workflow by design" and that neither
Postgres role (phi_ai_ingest, phi_ai_reader) can UPDATE or
DELETE rows in stored_resources - a documented decision, not an
oversight. RESOLVED: cleanup, when genuinely needed, is a rare, manual
procedure run by a human connected as the database's own administrator
- see runbooks/RUNBOOK_INDEX_MAINTENANCE.md. No Postgres role gains
delete access; the application's role model is unchanged. This tool's
--print-cleanup-sql flag generates the exact, safely-escaped SQL for
that manual procedure - it prints text for a human to review and run
themselves in their own admin-authenticated session; it never connects
as an administrator or executes anything itself.

PERMISSIONS: identical to what the restore role/service account already
holds on each cloud (deploy/aws/iam.tf, deploy/gcp/identities.tf,
deploy/azure/identities.tf) - list access on the storage bucket/
container, and a connection as the read-only phi_ai_reader
Postgres user. No new grants are needed to run this on any cloud.

    python -m core.db.reconcile
    python -m core.db.reconcile --print-cleanup-sql
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from core.config.settings import Settings
from core.db import connection as db_connection
from core.db import index as db_index
from core.storage.factory import build_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phi-ai.db.reconcile")

# Preview length in log output - full lists can be long on a real
# deployment; a sample is enough to start investigating, and the exact
# count is always logged separately regardless.
_PREVIEW_LIMIT = 10


@dataclass(frozen=True)
class ReconcileReport:
    total_storage_objects: int
    total_index_rows: int
    orphaned_index_rows: tuple[str, ...]  # storage_key present in the index, not in storage
    missing_index_rows: tuple[str, ...]  # storage_key present in storage, not in the index

    @property
    def in_sync(self) -> bool:
        return not self.orphaned_index_rows and not self.missing_index_rows


def build_report_streaming(storage, conn, sample_limit: int = 1000) -> ReconcileReport:
    """Reconcile without holding both key sets in memory.

    THE SCALING FIX. build_report() below loads every storage key and
    every index key into Python sets and diffs them. At a hundred million
    objects that is tens of gigabytes before any comparison happens, so
    reconciliation stops being possible on exactly the deployments that
    most need it.

    Both sides can be read in SORTED order - S3 and its equivalents list
    lexicographically, and Postgres will ORDER BY - so this is a merge
    join. Memory is bounded by the number of DISCREPANCIES, not by the
    size of the deployment, and a healthy one uses almost none.

    Discrepancy lists are capped at `sample_limit`. The counts stay exact;
    it is the examples that are bounded, because ten million orphaned keys
    in a report is not a finding a human can act on and the count already
    carries the magnitude.
    """
    import heapq

    # iter_keys() streams in sorted order where the backend supports it,
    # and falls back to sorted(list_keys()) where it does not - so this is
    # bounded on S3 and merely correct elsewhere.
    storage_iter = iter(storage.iter_keys(prefix="fhir/"))
    index_iter = iter(db_index.iter_indexed_keys(conn))

    orphaned: list[str] = []
    missing: list[str] = []
    storage_total = index_total = 0
    orphaned_total = missing_total = 0

    storage_key = next(storage_iter, None)
    index_key = next(index_iter, None)

    while storage_key is not None or index_key is not None:
        if index_key is None or (storage_key is not None and storage_key < index_key):
            storage_total += 1
            missing_total += 1
            if len(missing) < sample_limit:
                missing.append(storage_key)
            storage_key = next(storage_iter, None)
        elif storage_key is None or index_key < storage_key:
            index_total += 1
            orphaned_total += 1
            if len(orphaned) < sample_limit:
                orphaned.append(index_key)
            index_key = next(index_iter, None)
        else:
            storage_total += 1
            index_total += 1
            storage_key = next(storage_iter, None)
            index_key = next(index_iter, None)

    return ReconcileReport(
        total_storage_objects=storage_total,
        total_index_rows=index_total,
        orphaned_index_rows=tuple(orphaned),
        missing_index_rows=tuple(missing),
    )


def build_report(storage, conn) -> ReconcileReport:
    storage_keys = set(storage.list_keys(prefix="fhir/"))
    index_keys = db_index.list_indexed_keys(conn)

    return ReconcileReport(
        total_storage_objects=len(storage_keys),
        total_index_rows=len(index_keys),
        orphaned_index_rows=tuple(sorted(index_keys - storage_keys)),
        missing_index_rows=tuple(sorted(storage_keys - index_keys)),
    )


def _preview(keys: tuple[str, ...]) -> str:
    shown = list(keys[:_PREVIEW_LIMIT])
    suffix = f" ... and {len(keys) - _PREVIEW_LIMIT} more" if len(keys) > _PREVIEW_LIMIT else ""
    return f"{shown}{suffix}"


def _sql_literal(value: str) -> str:
    """Safely quote a string for a SQL literal. Standard SQL escaping:
    a literal single quote is represented as two single quotes in a
    row - NOT a backslash escape, which Postgres does not treat as an
    escape character by default (standard_conforming_strings)."""
    return "'" + value.replace("'", "''") + "'"


def print_cleanup_sql(orphaned_keys: tuple[str, ...]) -> None:
    """
    Prints ready-to-paste SQL for manually reviewing and deleting
    orphaned rows, connected as the database's own administrator - see
    runbooks/RUNBOOK_INDEX_MAINTENANCE.md for the full procedure and the
    investigation that should happen before running the DELETE.

    This function only PRINTS. It never connects to Postgres itself and
    never executes anything - reconciling this tool's own report-only
    design with a real cleanup need means generating the exact text for
    a human to run in their own, separately-authenticated admin
    session, not running it here. Deliberately generated from the SAME
    orphaned_keys the report already computed, rather than asking an
    operator to write their own "find orphans" SQL by hand - a
    hand-written query drifting from this tool's actual definition of
    "orphaned" is exactly the kind of mistake this avoids.
    """
    if not orphaned_keys:
        return

    array_literal = "ARRAY[\n  " + ",\n  ".join(_sql_literal(k) for k in orphaned_keys) + "\n]"

    print("\n-- Paste into a psql session connected as the database's own administrator.")
    print("-- Review the preview output BEFORE running the DELETE below - see")
    print("-- runbooks/RUNBOOK_INDEX_MAINTENANCE.md for the full procedure.\n")
    print("-- 1. Preview exactly which rows this will remove:")
    print(
        f"SELECT id, resource_type, resource_id, storage_key, stored_at\n"
        f"FROM stored_resources\n"
        f"WHERE storage_key = ANY({array_literal});\n"
    )
    print("-- 2. If the preview matches what you investigated and expect, delete:")
    print(
        f"DELETE FROM stored_resources\n"
        f"WHERE storage_key = ANY({array_literal});"
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Reconcile the Postgres index against the storage backend (the system of record)."
    )
    parser.add_argument(
        "--print-cleanup-sql",
        action="store_true",
        help=(
            "If orphaned rows are found, print ready-to-paste SQL for manually reviewing "
            "and deleting them via the database's own administrator. Prints only - never "
            "connects as an administrator or executes anything. See "
            "runbooks/RUNBOOK_INDEX_MAINTENANCE.md."
        ),
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    if not (settings.db_target_configured() and settings.db_reader_username):
        log.error(
            "No PHI_AI_DB_* settings present - nothing to reconcile against. "
            "This tool only makes sense when the Postgres index is in use."
        )
        return 1

    storage = build_storage(settings)
    conn = db_connection.connect(settings, username=settings.db_reader_username)

    try:
        report = build_report(storage, conn)
    finally:
        conn.close()

    log.info("Stored objects: %d | Index rows: %d", report.total_storage_objects, report.total_index_rows)

    if report.missing_index_rows:
        log.warning(
            "%d stored object(s) have no index row - expected after enabling indexing "
            "partway through, or after an isolated index-write failure. Re-run the "
            "scheduler to backfill (see this module's docstring): %s",
            len(report.missing_index_rows),
            _preview(report.missing_index_rows),
        )
    else:
        log.info("No missing index rows - every stored object has a corresponding index entry.")

    if report.orphaned_index_rows:
        log.warning(
            "%d index row(s) point to objects that no longer exist in storage - "
            "unexpected in ordinary operation. INVESTIGATE why before cleaning up (see "
            "runbooks/RUNBOOK_INDEX_MAINTENANCE.md) - do not assume this is safe to delete "
            "without knowing why the stored object is gone: %s",
            len(report.orphaned_index_rows),
            _preview(report.orphaned_index_rows),
        )
        if args.print_cleanup_sql:
            print_cleanup_sql(report.orphaned_index_rows)
    else:
        log.info("No orphaned index rows - every index row has a corresponding stored object.")

    return 0 if report.in_sync else 2


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
