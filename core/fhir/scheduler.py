# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Ingestion scheduler.

Runs incremental FHIR ingestion on an interval. Tracks a high-water mark
so each run only pulls resources changed since the last successful run
(FHIR `_lastUpdated`), rather than re-storing everything.

    python -m core.fhir.scheduler
    python -m core.fhir.scheduler --once
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from core.audit.log import AuditLog
from core.config.settings import Settings, env_var
from core.crypto.envelope import EnvelopeEncryptor
from core.db import connection as db_connection
from core.db import index as db_index
from core.db import omop_etl
from core.fhir.client import FHIRIngestionClient
from core.config.scale_profile import profile_from_env
from core.fhir.emr_profiles import profile_for
from core.storage.factory import build_audit_sink, build_kms, build_storage

# PHI must never reach application logs. Log resource COUNTS and types,
# never resource bodies or identifiers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phi-ai.scheduler")

# Postgres index_state row key for this scheduler's incremental-ingestion
# high-water mark - see core/db/index.py's read_index_state()/
# write_index_state() and core/db/schema.sql's index_state table.
#
# THE WATERMARK LIVES IN POSTGRES, NOT IN OBJECT STORAGE, and that is a
# deliberate design decision rather than an implementation detail:
#
#   * The ingest role can PutObject but deliberately holds no
#     kms:Decrypt - it encrypts PHI and can never read it back. A
#     watermark stored as an SSE-KMS object in the PHI bucket could be
#     written and never read, so every run would silently start from
#     nothing.
#   * Operational bookkeeping has no business sharing a bucket with PHI,
#     inheriting whatever retention and immutability posture that bucket
#     carries. index_state is already granted to the ingest role with
#     INSERT/SELECT/UPDATE (core/db/bootstrap_aws.sql), is used for
#     nothing else, and is genuinely mutable.
#
# KNOWN GAP, stated rather than left implicit: a deployment with no
# Postgres index configured gets no persisted watermark at all, so every
# run is a full re-ingest. That is safe - ingestion is idempotent (see
# the had_errors comment further down) - but it is not cheap, and it is
# logged explicitly by load_watermark()/save_watermark() rather than
# passing unnoticed.
#
# This does not relax the "object storage backend is always the system of
# record" invariant: that invariant governs PHI content, not the
# scheduler's bookkeeping about its own progress. See core/db/schema.sql's
# index_state comment.
SCHEDULER_WATERMARK_KEY = "scheduler_last_successful_run"


def load_watermark(db_conn) -> Optional[datetime]:
    """
    Returns the last successful run's start time, or None if either no
    watermark has been recorded yet (first-ever run) or this deployment
    has no Postgres index configured (db_conn is None) - both are
    normal, expected conditions meaning "perform a full run,"
    not errors. See SCHEDULER_WATERMARK_KEY above for why this reads
    from Postgres rather than the object storage backend.
    """
    if db_conn is None:
        log.info(
            "No Postgres index connection available; no watermark can be read or persisted - "
            "performing a full ingestion run. Expected for a deployment without "
            "PHI_AI_DB_* configured, and safe (ingestion is idempotent), but means every "
            "run re-processes the entire resource history rather than only what changed since "
            "the last run."
        )
        return None
    raw = db_index.read_index_state(db_conn, SCHEDULER_WATERMARK_KEY)
    if raw is None:
        log.info("No previous watermark found in the Postgres index; performing a full ingestion run.")
        return None
    return datetime.fromisoformat(raw)


def save_watermark(db_conn, ts: datetime) -> None:
    """
    Persists ts as the new watermark in the Postgres index_state table.
    A no-op (logged, not silent) when db_conn is None - see
    load_watermark() above for why that's an expected, safe condition
    rather than an error.
    """
    if db_conn is None:
        log.info(
            "No Postgres index connection available; this run's watermark will not be "
            "persisted. The next run will start from wherever the last successfully persisted "
            "watermark left off, or perform a full re-ingest if none exists - see "
            "load_watermark()."
        )
        return
    db_index.write_index_state(db_conn, SCHEDULER_WATERMARK_KEY, ts.isoformat())


def run_once(settings: Settings) -> int:
    storage = build_storage(settings)
    kms = build_kms(settings)
    encryptor = EnvelopeEncryptor(kms)

    try:
        # FIXED: this previously constructed S3AuditSink directly and
        # unconditionally, regardless of settings.cloud_provider - see
        # build_audit_sink()'s own docstring (core/storage/factory.py)
        # for the full account of what that meant in practice for any
        # non-AWS deployment. Same fix applied to
        # core/fhir/bulk_scheduler.py, which had the identical bug.
        sink = build_audit_sink(settings)
        last_hash = sink.last_hash()
    except Exception as exc:
        log.error("Could not initialize durable audit sink: %s", exc)
        log.error("Refusing to store PHI without a durable audit trail.")
        return 1

    audit = AuditLog(sink=sink, last_known_hash=last_hash)
    # Vendor-selected via PHI_AI_EMR_VENDOR (default epic) - see
    # core/fhir/emr_profiles.py for what each profile changes.
    profile = profile_for(settings.emr_vendor)

    # Postgres index is optional and best-effort. Unlike the audit sink
    # above, a failure to connect here does not stop ingestion - the
    # object storage backend is already the system of record, and the
    # index is derived/rebuildable. See core/db/schema.sql.
    #
    # Opened here, but closed in the `finally` block below rather than
    # only at the very end of the happy path - a connection opened here
    # and left open on any early return (e.g. client.authenticate()
    # raising) would leak for the lifetime of that failed attempt.
    # Harmless once, but this function runs on every scheduler cycle
    # forever (see main()'s loop) - a persistent failure (an Epic auth
    # problem that doesn't clear on its own, say) would leak one more
    # connection every cycle, and every provider's managed Postgres has
    # a real, finite connection limit. A slow leak that only shows up
    # after enough repeated failures is exactly the kind of thing worth
    # closing off now rather than waiting to notice it in production.
    #
    # FIXED: this previously checked `settings.db_host` directly, which
    # is correct for AWS and Azure (both connect over host:port) but
    # always false for a correctly-configured GCP deployment - GCP's
    # Cloud SQL Connector uses an instance connection name instead of a
    # host at all (see core/db/connection.py's own module docstring).
    # A GCP deployment with the Postgres index genuinely enabled would
    # have silently skipped indexing entirely, every run, with no error
    # - db_target_configured() (core/config/settings.py) now checks the
    # right field per provider.
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

    # OMOP analytics layer connection - genuinely separate from db_conn
    # above (a different Postgres role, omop_etl, on the same
    # instance - core/db/omop_bootstrap_aws.sql), opened independently
    # so a failure connecting to one never affects the other. See
    # core/config/settings.py's omop_target_configured() and
    # core/db/omop_etl.py for the full design.
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
        # the JWT-assertion flow: athenahealth takes a client secret and
        # Oracle Health requires explicit system scopes - see
        # FHIRIngestionClient.authenticate_from_settings().
        client.authenticate_from_settings(settings)

        since = load_watermark(db_conn)
        started = datetime.now(timezone.utc)

        resource_types = list(profile.supported_resources)
        log.info("Ingesting %d resource types (since=%s)", len(resource_types), since)

        total = 0
        had_errors = False
        for rtype in resource_types:
            count = 0
            try:
                for resource in client.iter_resources(rtype, since=since):
                    client.store_resource(resource)
                    count += 1
            except Exception as exc:
                # Log the type and error, never the resource contents.
                had_errors = True
                log.error("Failed storing %s after %d resources: %s", rtype, count, exc)
                audit.record(
                    actor="phi-ai-scheduler",
                    action="record.error",
                    resource_key=f"fhir/{rtype}",
                    purpose_of_use="scheduled_ingestion",
                )
                continue
            log.info("Stored %s: %d resources", rtype, count)
            total += count

        # FIXED BUG: this previously called save_watermark() unconditionally,
        # even when one or more resource types failed partway through. A
        # type that fails after storing some but not all of its
        # resources this run means the REMAINING, un-stored resources
        # of that type were never even attempted - and advancing the
        # watermark to this run's start time means the NEXT run's Epic
        # query (_lastUpdated > new_watermark) would never ask for them
        # again, since they already existed before that new watermark.
        # Concretely: 500 of 1000 Observations stored, the 501st
        # raises, watermark still advances -> the other 500 are gone,
        # permanently and silently, from a system whose entire purpose
        # is not losing data. Now: the watermark only advances when every
        # resource type in this run completed without error. On a
        # partial failure, the OLD watermark is kept, so the next run
        # retries the full range again - safe, since ingestion is
        # idempotent (core/fhir/client.py writes with the same storage
        # key simply overwrite, and core/db/index.py's write_index_entry()
        # absorbs a duplicate insert rather than erroring), just
        # possibly redundant for whatever succeeded this run. A stuck
        # watermark under a PERSISTENT failure is a visible, log-
        # observable problem an operator can investigate; silent data
        # loss is not visible until someone goes looking for a record
        # that no longer exists - the wrong side of that tradeoff for a
        # system whose entire purpose is retaining data.
        if had_errors:
            log.warning(
                "One or more resource types failed this run - NOT advancing the watermark "
                "(staying at since=%s). The next run will retry from the same starting point; "
                "this is safe because ingestion is idempotent, so resources already stored "
                "this run will be re-processed, not duplicated, not lost.",
                since,
            )
        else:
            save_watermark(db_conn, started)

        log.info(
            "Run complete: %d resources stored%s",
            total,
            " (with errors this run - watermark not advanced, see warnings above)" if had_errors else "",
        )

        return 1 if had_errors else 0
    finally:
        if db_conn is not None:
            db_conn.close()
        if omop_conn is not None:
            omop_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="PHI AI Platform incremental ingestion scheduler.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        # env_var(), never os.environ.get(): an installer-produced .env
        # sets PHI_AI_INTERVAL_SECONDS, and a bare os.environ read would
        # not necessarily see it - it would fall through to the default
        # below and run on an interval the operator never chose, without
        # erroring.
        default=int(env_var("INTERVAL_SECONDS", "3600") or "3600"),
    )
    args = parser.parse_args()

    settings = Settings.from_env()

    if args.once:
        return run_once(settings)

    log.info("Scheduler started; interval=%ds", args.interval_seconds)
    while True:
        try:
            run_once(settings)
        except Exception as exc:
            log.exception("Ingestion cycle failed: %s", exc)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
