# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Bulk Data Export scheduler entry point.

Distinct from core/fhir/scheduler.py (per-type incremental search)
because bulk export is a fundamentally different retrieval model - see
core/fhir/bulk_client.py's module docstring for the full citations from
Epic's own documentation. Briefly: Epic supports ONLY Group-level bulk
export, with no incremental/delta capability at all, and rate-limits
kickoff to once per 24 hours per group+client ID by default. This is why
the default interval below is a day, not scheduler.py's hour - running
this more often will simply get rejected by Epic.

Requires PHI_AI_FHIR_GROUP_ID - a Group FHIR ID that has to come
from Epic directly (email openepic@epic.com for a sandbox one) or from
the healthcare organization in a real deployment; it cannot be
discovered through any API. See core/config/settings.py.

    python -m core.fhir.bulk_scheduler --once
    python -m core.fhir.bulk_scheduler
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from core.audit.log import AuditLog
from core.config.settings import ConfigError, Settings, env_var
from core.crypto.envelope import EnvelopeEncryptor
from core.db import connection as db_connection
from core.db import index as db_index
from core.db import omop_etl
from core.fhir import bulk_client
from core.fhir.client import FHIRIngestionClient
from core.config.scale_profile import profile_from_env
from core.fhir.emr_profiles import profile_for
from core.storage.factory import build_audit_sink, build_kms, build_storage

# PHI must never reach application logs - same rule as scheduler.py. Log
# resource COUNTS and types, never resource bodies or identifiers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phi-ai.bulk_scheduler")


def run_once(settings: Settings) -> int:
    if not settings.fhir_group_id:
        log.error(
            "PHI_AI_FHIR_GROUP_ID is not set - bulk export requires a Group FHIR ID. "
            "This has to come from Epic directly (email openepic@epic.com for a sandbox "
            "one) or from the healthcare organization in a real deployment; it is not "
            "discoverable through any API. See core/fhir/bulk_client.py's module docstring."
        )
        return 1

    storage = build_storage(settings)
    kms = build_kms(settings)
    encryptor = EnvelopeEncryptor(kms)

    try:
        # FIXED: this previously constructed S3AuditSink directly and
        # unconditionally, regardless of settings.cloud_provider - the
        # identical bug core/fhir/scheduler.py had. See
        # build_audit_sink()'s own docstring (core/storage/factory.py)
        # for the full account of what that meant in practice.
        sink = build_audit_sink(settings)
        last_hash = sink.last_hash()
    except Exception as exc:
        log.error("Could not initialize durable audit sink: %s", exc)
        log.error("Refusing to store PHI without a durable audit trail.")
        return 1

    audit = AuditLog(sink=sink, last_known_hash=last_hash)
    # Vendor-selected via PHI_AI_EMR_VENDOR (default epic).
    profile = profile_for(settings.emr_vendor)
    if not profile.supports_bulk_export:
        # Refuse, don't improvise: silently falling back to per-type paged
        # search would turn a planned $export run into a much longer,
        # rate-limited job nobody budgeted for (see the eClinicalWorks and
        # NextGen notes in emr_profiles.py). The paged path exists - it is
        # core/fhir/scheduler.py - and choosing it should be a decision,
        # not a surprise.
        log.error(
            "%s's profile records no Bulk Data Export support. Use "
            "core/fhir/scheduler.py (paged search) for this vendor, or correct "
            "the profile if the instance's CapabilityStatement proves otherwise.",
            profile.name,
        )
        return 1

    # Postgres index wiring - mirrors core/fhir/scheduler.py's identical
    # pattern exactly.
    #
    # FIXED: this was previously entirely absent from this file. Bulk
    # Data Export is this project's OWN documented way to ingest an
    # entire patient population (see this module's docstring above and
    # docs/EMR_CONNECTORS.md) - a real, expected primary ingestion path
    # for many deployments, not a rarely-used fallback. Without an
    # index_writer wired up here, every resource ingested via bulk
    # export was safely written to the object store but never indexed in
    # Postgres - meaning core/fhir/restore.py's patient lookup
    # (find_by_patient_reference(), which the entire records-request
    # workflow in runbooks/RUNBOOK_DATA_RESTORE.md depends on) would
    # report "nothing stored for this patient" even when their data
    # genuinely exists, for any deployment relying on
    # bulk export. Data safely retained but functionally undiscoverable
    # for the exact workflow this project exists to support.
    #
    # Connection lifetime and cleanup follow scheduler.py's same fixed
    # pattern too: opened here, closed in the `finally` block below so a
    # failure partway through a run (or Epic auth failing before any
    # resource is even fetched) doesn't leak a connection - this
    # function runs once every ~24 hours indefinitely (see main()'s
    # loop), so a leak here would be slow to notice but just as real as
    # the one already fixed in scheduler.py.
    #
    # FIXED: this previously checked `settings.db_host` directly - see
    # scheduler.py's identical fix and core/config/settings.py's
    # db_target_configured() for the full account of why that was wrong
    # for GCP specifically.
    db_conn = None
    if settings.db_target_configured() and settings.db_ingest_username:
        try:
            db_conn = db_connection.connect(settings, username=settings.db_ingest_username)
            log.info("Connected to Postgres index")
        except Exception as exc:
            log.warning("Could not connect to Postgres index (continuing without it): %s", exc)
            db_conn = None
    else:
        log.info("No PHI_AI_DB_* settings present; running without a Postgres index.")

    # OMOP analytics layer connection - same independent-from-db_conn
    # design as core/fhir/scheduler.py's identical setup; see that
    # file's own comment for the full reasoning.
    omop_conn = None
    if settings.omop_target_configured():
        try:
            omop_conn = db_connection.connect(settings, username=settings.omop_etl_username)
            log.info("Connected to OMOP analytics layer")
        except Exception as exc:
            log.warning("Could not connect to OMOP analytics layer (continuing without it): %s", exc)
            omop_conn = None
    else:
        log.info("No PHI_AI_OMOP_ETL_USERNAME set; running without the OMOP analytics layer.")

    try:
        def index_writer(result, resource: dict) -> None:
            if db_conn is None:
                return
            entry = db_index.IndexEntry(
                resource_type=result.resource_type,
                resource_id=result.resource_id,
                storage_key=result.storage_key,
                storage_version_id=result.version_id,
                sha256_hex=result.sha256_hex,
                patient_reference=db_index.extract_patient_reference(resource),
                retention_until=result.retention_until,
            )
            db_index.write_index_entry(db_conn, entry)

        def omop_writer(result, resource: dict) -> None:
            if omop_conn is None:
                return
            omop_etl.etl_resource(omop_conn, resource, result.storage_key)

        client = FHIRIngestionClient(
            base_url=settings.fhir_base_url,
            profile=profile,
            storage=storage,
            encryptor=encryptor,
            audit=audit,
            retention_years=settings.retention_years,
            retention_years_overrides=settings.retention_years_overrides,
            index_writer=index_writer if db_conn is not None else None,
            omop_writer=omop_writer if omop_conn is not None else None,
            profile_config=profile_from_env(),
        )

        log.info("Authenticating to %s FHIR endpoint", profile.name)
        # Dispatched on the vendor profile's auth_flow, not hardcoded to
        # the JWT-assertion flow - see
        # FHIRIngestionClient.authenticate_from_settings().
        client.authenticate_from_settings(settings)

        resource_types = list(profile.supported_resources)
        log.info(
            "Kicking off bulk export for group %s (%d resource types)",
            settings.fhir_group_id, len(resource_types),
        )

        job = bulk_client.kickoff_export(
            base_url=settings.fhir_base_url,
            group_id=settings.fhir_group_id,
            access_token=client.access_token,
            resource_types=resource_types,
        )

        # KNOWN GAP, deliberate and disclosed rather than silently
        # risked: authenticate() runs once above, but wait_for_export
        # can poll for hours and Epic backend-services tokens live ~1
        # hour with no refresh-on-401 implemented anywhere yet. For a
        # large patient population, a mid-run 401 during file download
        # is an EXPECTED failure mode, not an edge case - it now
        # surfaces as a failed run (had_errors below) instead of a
        # silent exit 0, but re-authentication on 401 is the real fix
        # and still needs building.
        manifest = bulk_client.wait_for_export(
            job,
            client.access_token,
            poll_interval_seconds=settings.bulk_poll_interval_seconds,
            max_wait_seconds=settings.bulk_max_wait_seconds,
        )

        # FIXED - failure masking (2026-08-17 audit, H2). This loop
        # previously had no error flag at all: per-file failures logged,
        # audited, and `continue`d; then delete_export() ran
        # unconditionally and the function returned 0 ("Bulk export run
        # complete") - so a run where EVERY file failed looked like
        # success to monitoring, and the server-side export
        # (rate-limited to ~one per day) was deleted before its files
        # had actually been processed, destroying the only retry
        # opportunity until the next day's window. Mirrors
        # scheduler.py's had_errors discipline: the same
        # partial-failure-must-not-look-clean invariant the incremental
        # watermark enforces, applied to the bulk path's one
        # destructive server-side action.
        had_errors = False
        total = 0
        for entry in manifest.get("output", []):
            rtype = entry["type"]
            file_url = entry["url"]
            count = 0
            try:
                for resource in bulk_client.iter_ndjson_resources(file_url, client.access_token):
                    client.store_resource(resource)
                    count += 1
            except Exception as exc:
                # Log the type and error, never resource contents - same
                # policy as scheduler.py's per-type error handling.
                had_errors = True
                log.error("Failed processing bulk file for %s after %d resources: %s", rtype, count, exc)
                audit.record(
                    actor="phi-ai-bulk-scheduler",
                    action="record.error",
                    resource_key=f"fhir-bulk/{rtype}",
                    purpose_of_use="scheduled_ingestion",
                )
                continue
            log.info("Stored %s from bulk export: %d resources", rtype, count)
            total += count

        # FIXED - manifest-level errors (same audit finding). The
        # server reports resources it FAILED to export as
        # OperationOutcome entries under the manifest's "error" key;
        # these were previously invisible - resources the server
        # skipped never even reached the loop above. They now count as
        # a failed run: the object store did not receive everything the
        # export was asked for, and pretending otherwise is exactly the
        # silent-partial-success failure mode this project forbids.
        error_entries = manifest.get("error", [])
        if error_entries:
            had_errors = True
            log.error(
                "Bulk export manifest reported %d error file(s) from the server - "
                "resources the server itself failed to export. The object store is "
                "missing data this run was supposed to capture.",
                len(error_entries),
            )
            audit.record(
                actor="phi-ai-bulk-scheduler",
                action="record.error",
                resource_key="fhir-bulk/_manifest_errors",
                purpose_of_use="scheduled_ingestion",
            )

        if had_errors:
            # Deliberately do NOT delete the server-side export on a
            # failed run: its files are the only retry material until
            # Epic's ~daily rate limit allows a fresh kickoff. Epic
            # expires exports server-side on its own schedule, and the
            # next successful run's delete_export() cleans up whatever
            # remains; an operator can also re-process the surviving
            # files manually (see runbooks) before the next window.
            log.error(
                "Bulk export run finished WITH ERRORS: %d resources stored before/around "
                "the failure(s) above. Server-side export NOT deleted, so its files remain "
                "available for retry/inspection. This run must not be read as complete.",
                total,
            )
            return 1

        bulk_client.delete_export(job.status_url, client.access_token)
        log.info("Bulk export run complete: %d resources stored", total)
        return 0
    finally:
        if db_conn is not None:
            db_conn.close()
        if omop_conn is not None:
            omop_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="PHI AI Platform bulk data export scheduler.")
    parser.add_argument("--once", action="store_true", help="Run a single export and exit.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        # 24 hours - matches Epic's default rate limit of one bulk
        # export per group+client per day (see bulk_client.py's module
        # docstring for the citation). Running this more often than that
        # will simply get rejected by Epic, not produce fresher data.
        #
        # Read through env_var() rather than os.environ.get() so an
        # installer-produced .env (which sets PHI_AI_BULK_INTERVAL_SECONDS)
        # is actually honoured.
        default=int(
            env_var("BULK_INTERVAL_SECONDS", str(24 * 60 * 60)) or str(24 * 60 * 60)
        ),
    )
    args = parser.parse_args()

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    if args.once:
        return run_once(settings)

    log.info(
        "Bulk export scheduler started; interval=%ds (%.1f hours)",
        args.interval_seconds, args.interval_seconds / 3600,
    )
    while True:
        try:
            # FIXED: the return value was previously discarded here, so
            # in loop mode even an every-file-failed run (exit status 1
            # from run_once) left no scheduler-level trace beyond the
            # per-file logs. Monitoring watching this process's summary
            # lines now gets an unambiguous per-cycle verdict.
            rc = run_once(settings)
            if rc != 0:
                log.error("Bulk export cycle finished with errors (status %d) - see above.", rc)
        except Exception as exc:
            log.exception("Bulk export cycle failed: %s", exc)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
