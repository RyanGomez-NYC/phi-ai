# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Verify every data flow in the PHI AI Platform, in one answer.

    python -m core.verify                      # everything runnable
    python -m core.verify --deep               # identifier-level ingestion check
    python -m core.verify --export-dir ./out   # also verify a bulk export

WHAT IT CHECKS, and what each failure would mean:

  object integrity   ciphertext no longer matching its recorded digest
  index <-> storage  drift between the queryable index and the objects
  audit chain        an entry removed, reordered or altered
  EMR -> store       records the source has that the store does not
  record freshness   stored records the source has since superseded
  store -> export    an export that silently omitted records

EXIT CODES: 0 clean, 1 warnings or an unchecked flow, 2 critical.

A SKIPPED FLOW NEVER YIELDS 0. A deployment nobody could verify must not
report success - "sound" and "unexamined" are different states, and a
runbook step or CI job keying on this needs to tell them apart.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from core.verify.base import FlowReport, Severity, VerificationReport

log = logging.getLogger("phi-ai.verify")

# Sampled rather than exhaustive by default. Re-reading and re-hashing
# every object in a large deployment is hours of work and full read
# access; a sample catches systemic corruption, which is the realistic
# failure, while --deep exists for when a specific suspicion needs
# settling.
INTEGRITY_SAMPLE = 50

# The freshness flow's name, defined once. build_verification_report()
# below constructs a stand-in FlowReport under this same name when the
# source EMR is unreachable, and the two MUST match: a report where a
# skipped run names the flow differently from a completed one is a report
# nobody can diff across runs.
FRESHNESS_FLOW = "Record freshness (superseded records)"


def verify_object_integrity(storage, sample_size: int, deep: bool) -> FlowReport:
    report = FlowReport(flow="Object integrity", source="storage", target="recorded digests")

    # Reservoir sampling over the key STREAM. Materialising every key
    # just to pick fifty of them is the same unbounded-memory mistake
    # reconciliation had: on a large deployment the listing alone is
    # gigabytes before a single digest is checked.
    #
    # A reservoir gives a uniform sample over the whole store in one
    # pass with O(sample) memory - so it still catches a fault introduced
    # by a recent change, which taking the first N would miss entirely
    # since keys list oldest-first.
    import random

    reservoir: list[str] = []
    total_keys = 0
    checked: list[str] = []

    for key in storage.iter_keys(prefix="fhir/"):
        total_keys += 1
        if deep:
            checked.append(key)
            continue
        if len(reservoir) < sample_size:
            reservoir.append(key)
        else:
            position = random.randrange(total_keys)
            if position < sample_size:
                reservoir[position] = key

    if not total_keys:
        report.skipped_reason = "no stored objects found"
        return report

    if not deep:
        checked = reservoir
    keys = checked if deep else reservoir

    corrupted = []
    unreadable = []
    for key in checked:
        try:
            if not storage.verify_integrity(key):
                corrupted.append(key)
        except Exception as exc:
            log.warning("could not verify %s: %s", key, exc)
            unreadable.append(key)

    if corrupted:
        report.add(
            Severity.CRITICAL, "integrity.digest",
            f"{len(corrupted)} object(s) no longer match their recorded digest",
            "The stored ciphertext has changed since it was written. Treat as a suspected "
            "security incident - see RUNBOOK_INCIDENT_RESPONSE.md.",
            examples=tuple(corrupted), count=len(corrupted),
        )
    if unreadable:
        report.add(
            Severity.WARNING, "integrity.unreadable",
            f"{len(unreadable)} object(s) could not be read to verify",
            examples=tuple(unreadable), count=len(unreadable),
        )
    if not corrupted and not unreadable:
        scope = ("every object" if deep
                 else f"a random sample of {len(checked)} of {total_keys:,}")
        report.add(Severity.OK, "integrity.digest",
                   f"{scope} verified against its recorded digest")
        if not deep:
            report.add(
                Severity.INFO, "integrity.scope",
                "This was a sample, not every stored object",
                "A sample catches systemic corruption. Use --deep to check every object "
                "when a specific suspicion needs settling.",
            )
    return report


def verify_index(storage, conn) -> FlowReport:
    report = FlowReport(flow="Index vs storage", source="storage", target="Postgres index")
    if conn is None:
        report.skipped_reason = "no Postgres index is configured"
        return report

    from core.db.reconcile import build_report_streaming

    # Streaming merge join: memory tracks discrepancies, not store size.
    drift = build_report_streaming(storage, conn)

    if drift.missing_index_rows:
        report.add(
            Severity.WARNING, "index.missing",
            f"{len(drift.missing_index_rows)} stored object(s) are not in the index",
            "The objects are safe - storage is the system of record - but they will not "
            "appear in search or restore-by-patient until the index is rebuilt.",
            examples=tuple(drift.missing_index_rows), count=len(drift.missing_index_rows),
        )
    if drift.orphaned_index_rows:
        report.add(
            Severity.WARNING, "index.orphaned",
            f"{len(drift.orphaned_index_rows)} index row(s) reference objects that do not exist",
            "Expected after a disposal run if the index delete failed. Anything else "
            "warrants investigation.",
            examples=tuple(drift.orphaned_index_rows), count=len(drift.orphaned_index_rows),
        )
    if not drift.missing_index_rows and not drift.orphaned_index_rows:
        report.add(Severity.OK, "index.reconcile", "index and storage agree exactly")
    return report


def verify_audit(audit_sink, conn=None, full: bool = False) -> FlowReport:
    """Verify the chain, incrementally by default.

    A full verification loads every event to prove each prev_hash
    resolves - inherent to the check, and O(log size). A deployment whose
    verification gets slower every day eventually stops being verified,
    so the routine path resumes from a checkpoint and reads only what is
    new. `full=True` (--deep) does the whole chain and is the
    authoritative answer; run it periodically and after any incident.

    Independent of action names: nothing here filters on `action`, so the
    chain verifies identically whatever action vocabulary it holds. See
    core/audit/log.py.
    """
    report = FlowReport(flow="Audit chain", source="audit log", target="hash chain")
    if audit_sink is None:
        report.skipped_reason = "no audit sink is configured"
        return report

    from core.audit.log import AuditLog

    if not full and hasattr(audit_sink, "iter_events"):
        from core.audit.checkpoint import (
            load_checkpoint,
            save_checkpoint,
            verify_incremental,
        )

        checkpoint = load_checkpoint(conn)
        diagnostics, updated = verify_incremental(audit_sink, checkpoint)
        if diagnostics.ok:
            save_checkpoint(conn, updated)
            report.add(
                Severity.OK, "audit.chain",
                f"{diagnostics.total_events} new event(s) verified; "
                f"{updated.events_verified} total since the first check",
            )
            if checkpoint is None:
                report.add(
                    Severity.INFO, "audit.checkpoint",
                    "First run verified the whole chain and recorded a checkpoint",
                    "Later runs read only what is new, so verification cost tracks what "
                    "was written rather than what exists.",
                )
            else:
                report.add(
                    Severity.INFO, "audit.scope",
                    "Incremental verification - only events since the last checkpoint",
                    "This shows nothing new has been tampered with. Only a full check "
                    "(--deep) confirms the whole chain; run one periodically.",
                )
            if conn is None:
                report.add(
                    Severity.WARNING, "audit.checkpoint",
                    "No index configured, so no checkpoint could be saved",
                    "Every run will re-read the entire chain, which gets slower as the "
                    "log grows.",
                )
            return report
        # Fall through to reporting the failure below.
    else:
        events = audit_sink.read_all()
        diagnostics = AuditLog.diagnose_chain(events, closed=True)

    if diagnostics.ok:
        report.add(
            Severity.OK, "audit.chain",
            f"{diagnostics.total_events} event(s) verified; the chain is intact",
        )
        if diagnostics.fork_points:
            report.add(
                Severity.INFO, "audit.forks",
                f"{diagnostics.fork_points} concurrent-writer fork(s) present",
                "Two events sharing a predecessor. Normal with more than one writer, and "
                "not evidence of tampering.",
            )
        return report

    if diagnostics.corrupted_event_hashes:
        report.add(
            Severity.CRITICAL, "audit.modified",
            f"{len(diagnostics.corrupted_event_hashes)} audit event(s) were modified after "
            "being written",
            "An event's contents no longer match its recorded hash. Treat as a suspected "
            "security incident.",
            examples=tuple(diagnostics.corrupted_event_hashes[:8]),
        )
    if diagnostics.unresolved_prev_hashes:
        report.add(
            Severity.CRITICAL, "audit.removed",
            f"{len(diagnostics.unresolved_prev_hashes)} event(s) reference a predecessor "
            "that is absent",
            "Entries appear to have been removed from the audit log.",
            examples=tuple(diagnostics.unresolved_prev_hashes[:8]),
        )
    return report


def build_verification_report(
    deep: bool = False,
    export_dir: Optional[str] = None,
    skip_source: bool = False,
    sample: int = INTEGRITY_SAMPLE,
    check_freshness: bool = True,
) -> VerificationReport:
    """Assemble every runnable flow into one report.

    Separated from main() so a scheduled run and a hand-run check share
    one definition of what "verified" means - and so the caller gets the
    report object rather than only an exit code, which is what recording
    a run in the audit log needs.
    """
    from core.config.settings import Settings
    from core.crypto.envelope import EnvelopeEncryptor
    from core.fhir.emr_profiles import profile_for
    from core.storage.factory import build_audit_sink, build_kms, build_storage

    settings = Settings.from_env()
    storage = build_storage(settings)
    audit_sink = build_audit_sink(settings)

    report = VerificationReport()
    report.add(verify_object_integrity(storage, sample, deep))

    conn = None
    try:
        from core.db.connection import connect

        # The INGEST role, not the reader, and the distinction is
        # load-bearing: verify_audit() writes its checkpoint back through
        # core/audit/checkpoint.py -> write_index_state(), which INSERTs
        # into index_state. core/db/bootstrap_aws.sql grants
        # INSERT/UPDATE on index_state to phi_ai_ingest and only
        # SELECT to phi_ai_reader, so connecting as the reader would
        # verify correctly and then fail to save the checkpoint - and the
        # comment below is explicit that a lost checkpoint silently
        # reverts verification to re-reading the whole chain every run.
        conn = connect(settings, settings.db_ingest_username)
    except Exception as exc:
        log.warning("no index connection: %s", exc)
    # The connection stays open across BOTH checks: audit verification
    # reads and writes its checkpoint through the index, and closing here
    # would have left the checkpoint unsaved on every run - so
    # verification would have silently reverted to reading the whole
    # chain each time, which is the exact cost this was meant to remove.
    try:
        report.add(verify_index(storage, conn))
        report.add(verify_audit(audit_sink, conn=conn, full=deep))
    finally:
        if conn is not None:
            conn.close()

    if skip_source:
        flow = FlowReport(flow="EMR ingestion completeness", source="source EMR",
                          target="object store")
        flow.skipped_reason = (
            "skipped at the operator's request. If the source EMR is already "
            "decommissioned this can never be checked - any gap is now permanent and "
            "undetectable."
        )
        report.add(flow)
    else:
        from core.verify.ingestion import verify_ingestion

        try:
            from core.audit.log import AuditLog
            from core.fhir.client import FHIRIngestionClient

            profile = profile_for(getattr(settings, "emr_vendor", "epic") or "epic")
            client = FHIRIngestionClient(
                base_url=settings.fhir_base_url, profile=profile, storage=storage,
                encryptor=EnvelopeEncryptor(kms=build_kms(settings)),
                audit=AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash()),
                retention_years=settings.retention_years,
            )
            # Dispatched on the vendor profile's auth_flow, exactly as
            # the schedulers do - a client-secret vendor (athenahealth)
            # rejects the JWT-assertion flow outright, and Oracle Health
            # rejects a token request without explicit system scopes. See
            # FHIRIngestionClient.authenticate_from_settings(). (An
            # earlier version of this call also read the non-existent
            # settings.fhir_private_key_path - found by
            # tests/test_entrypoints.py; from_env() reads that file once
            # at startup and exposes the bytes as fhir_private_key_pem.)
            client.authenticate_from_settings(settings)
            report.add(verify_ingestion(storage, client, profile.supported_resources,
                                        deep=deep))

            if check_freshness:
                from core.verify.freshness import verify_freshness
                from core.web.data import LiveRecordReader

                reader = LiveRecordReader(
                    connection_factory=lambda: None, storage=storage,
                    encryptor=EnvelopeEncryptor(kms=build_kms(settings)),
                    audit_sink=audit_sink,
                )
                report.add(verify_freshness(storage, reader, client,
                                            profile.supported_resources, deep=deep))
        except Exception as exc:
            flow = FlowReport(flow="EMR ingestion completeness", source="source EMR",
                              target="object store")
            flow.skipped_reason = (
                f"could not reach the source EMR: {exc}. This is the one check that "
                "expires - it cannot be run after the source is decommissioned."
            )
            report.add(flow)

            freshness = FlowReport(flow=FRESHNESS_FLOW,
                                   source="source EMR", target="object store")
            freshness.skipped_reason = "the source EMR was not reachable"
            report.add(freshness)

    if export_dir:
        from core.verify.export import verify_export

        profile = profile_for(getattr(settings, "emr_vendor", "epic") or "epic")
        report.add(verify_export(storage, export_dir, profile.supported_resources))

    return report


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        prog="python -m core.verify",
        description="Verify every data flow in the PHI AI Platform.",
    )
    parser.add_argument("--deep", action="store_true",
                        help="check every object, compare EMR identifiers rather than "
                             "counts, and compare every stored version against the "
                             "source. Slower; definitive.")
    parser.add_argument("--export-dir", help="also verify a bulk export directory")
    parser.add_argument("--skip-source", action="store_true",
                        help="skip checks that need the source EMR (e.g. it is already "
                             "decommissioned). Those checks cannot be run later.")
    parser.add_argument("--sample", type=int, default=INTEGRITY_SAMPLE,
                        help=f"objects to spot-check for integrity (default {INTEGRITY_SAMPLE})")
    args = parser.parse_args(argv)

    report = build_verification_report(
        deep=args.deep,
        export_dir=args.export_dir,
        skip_source=args.skip_source,
        sample=args.sample,
    )
    print(report.render())
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
