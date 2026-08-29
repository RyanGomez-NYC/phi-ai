# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Removes stored objects. The only tool in this codebase that does -
every ingest, restore, and audit code path elsewhere is deliberately
append-only. Two sharply separated modes, on purpose: conflating them
is exactly how a system like this gets quietly weakened. See
runbooks/RUNBOOK_DATA_RESTORE.md, RUNBOOK_DISPOSITION.md, and
RUNBOOK_INCIDENT_RESPONSE.md for the broader operational context this
tool sits inside.

    python -m core.fhir.purge expired --role-arn <disposition-role-arn>
    python -m core.fhir.purge expired --role-arn <disposition-role-arn> --confirm

"expired" mode - the routine, expected case. HIPAA's own disposal
requirement (45 CFR 164.310(d)(2)(i)) contemplates exactly this: secure,
documented destruction once a record's retention obligation has actually
been satisfied, not indefinite retention forever. This mode can ONLY
ever touch an object whose recorded retention_until date has already
passed.

READ THIS BEFORE TRUSTING THAT SENTENCE. It used to be backed by S3
Object Lock: the retention date was enforced by the storage service, so
this tool structurally could not delete something inside its window even
if this script were buggy. Object Lock has been removed from this
deployment, and with it that backstop. The retention check in this
module is now the ONLY thing preventing premature deletion of stored
PHI - a bug here deletes real records early, silently, and permanently.

That check is therefore written defensively rather than conveniently:
the candidate list is built from recorded retention metadata, and then
_dispose_one re-reads and re-verifies each object's retention date
immediately before deleting it (require_expired=True), so a stale
candidate list cannot cause a premature delete. Objects carrying NO
recorded retention date are skipped entirely, never treated as expired -
and, as of the 2026-08-17 audit's MEDIUM retention-math fix, reported
explicitly rather than silently dropped, since an object with no
retention date at all is a compliance misconfiguration, not a routine
state; see _run_expired() below.

Defaults to a dry run - prints exactly what would be deleted and why,
deletes nothing, until --confirm is passed. Every deletion is
audit-logged (action="record.dispose") BEFORE it happens, recording
the resource key and the retention date that was satisfied.

    python -m core.fhir.purge admin-order \\
        --role-arn <disposition-role-arn> \\
        --resource-type DocumentReference --resource-id eSyn0001Note \\
        --admin-basis "Subpoena, Case No. 2026-CV-1234, Superior Court of Example County, dated 2026-08-15" \\
        --confirm

    python -m core.fhir.purge admin-order \\
        --role-arn <disposition-role-arn> \\
        --resource-list ./records-to-remove.csv \\
        --admin-basis "Documented wind-down, no successor entity - see [board resolution/date]" \\
        --confirm

"admin-order" mode - the rare, exceptional case: an administrator, under
a stated basis, removes one or more specific records before their
retention period would otherwise allow. Named for who invokes it -
someone holding the disposition role, not a patient or an automated
process - and requires that invocation to be justified and logged, not
anonymous or silent. Deliberately narrow by construction, not just by
policy:

  - Specific, named records only - never a wildcard, never "everything
    of this type," never the whole record store. One record via
    --resource-type/--resource-id, or several via --resource-list (a
    file of "ResourceType,resource_id" pairs, one per line - blank
    lines and lines starting with # are skipped, so the file itself can
    carry a per-entry note reviewable before anyone runs it). Every
    entry in a --resource-list batch is validated to exist BEFORE
    anything is deleted; if any entry is missing, the whole batch is
    refused rather than partially applied - the operator's list should
    be accurate, not best-effort.
  - NOTHING CATEGORICALLY PREVENTS THIS MODE ANYMORE. It previously
    refused outright, before assuming any role, when the deployment ran
    in COMPLIANCE mode - and that refusal was real, because COMPLIANCE
    mode meant S3 itself would reject the delete regardless of what this
    script decided. With Object Lock removed there is no such deployment
    posture left to detect and no storage-layer refusal behind it, so
    the check is gone rather than left in place as a reassuring no-op.
    An administrator holding the disposition role can now remove any
    named record at any point inside its retention period. What stands
    in the way is IAM scoping, the required --admin-basis, the
    audit-logged record of the act, and the operator's own governance -
    not the platform.
  - Requires the disposition role, which exists only when
    deploy/aws/variables.tf's enable_admin_order_purge is explicitly set
    to true - off by default, the same "no hardcoded, silent defaults
    for irreversible settings" posture this project uses throughout. The
    s3:BypassGovernanceRetention permission that used to gate this is
    gone from the role; it referred to a lock that no longer exists, and
    keeping it would have implied a protection that isn't there.
  - Requires --admin-basis as free text, becomes a required session tag
    (AdminBasis) the IAM policy itself checks is present before
    allowing the bypass at all - not just documentation of intent, the
    same pattern core/fhir/psychotherapy_restore.py's
    PsychotherapyException/PsychotherapyAttestation tags already
    establish for a different narrow-exception scenario. One stated
    basis covers an entire --resource-list batch; it is not
    independently validated as any particular kind of authority (a
    court order, an internal decision, anything else) - it is a
    required, permanently-logged justification, not a credential this
    tool checks against an external source. See "What this tool does
    NOT do" below.
  - Requires typed, interactive confirmation before deleting - not just
    a --confirm flag - specifically so this cannot be silently scripted
    or run unattended. For a single record, type the exact resource key.
    For a --resource-list batch, type the exact count of records about
    to be removed, after seeing the full list printed - proportionate
    to the batch rather than requiring every key to be retyped, while
    still forcing the operator to have actually seen what they're
    confirming.

Audit-logged (action="record.purge.admin_order") BEFORE each delete,
one entry per record even within a batch - the same per-object
granularity core/fhir/bulk_export.py already uses, deliberately not
collapsed into a single summary event, so a later audit-trail review
can see exactly which resource keys an admin-order run actually
touched. Each entry records the resource key, the retention date that
was bypassed, and the full admin-basis text - so even though the record
itself is gone, the fact that it was removed, by whom, when, and citing
what basis, remains permanently in the tamper-evident audit trail.

DISPOSAL COMPLETENESS (2026-08-17 audit, C4). Earlier versions of this
tool deleted only the stored object - the lightweight Postgres
index row and any OMOP CDM row(s) derived from the same resource
(core/db/omop_schema.sql; holds IDENTIFIED PHI - DOB, diagnoses,
medication exposures) both survived every disposal, indefinitely. For
an admin-order removal that meant the very record ordered removed
stayed fully queryable via OMOP, traceable back to the "removed" object
through its own source_storage_key provenance column. Both modes now
delete, in order, for every resource key: (1) any OMOP row (see
core/db/omop_purge.py - only when this deployment's OMOP layer is
configured), (2) the index row (core/db/index.py's delete_index_entry -
only when the index is configured), (3) every stored VERSION of the
object itself (core/storage/aws_s3.py's delete_all_versions - not just
the current version, so a key that was ever overwritten doesn't leave
earlier versions recoverable after "disposal"). Each resource's three
deletes are attempted in that order and, if any step fails, the
STORAGE object is deliberately left intact rather than partially
disposed - see _dispose_one()'s own docstring below. A batch spanning
multiple resource types (e.g. a whole patient's record set disposed
together) is sorted into FK-safe order first - Encounter before
Patient, everything else before both - matching core/db/omop_purge.py's
own table-delete order, so a same-run "dispose the whole family"
admin-order batch does not fail on its own ordering. One item's failure
never aborts the rest of a run: failures are collected, reported
plainly at the end, and produce a non-zero exit code - the same
had_errors discipline core/fhir/bulk_scheduler.py's own 2026-08-17 H2
fix established, applied here to the same "a partial failure must never
be reported as a clean success" invariant.

Psychotherapy notes have their OWN, separate disposition path -
core/fhir/psychotherapy_purge.py, not this tool. This tool's storage
client is bound to settings.storage_bucket (the general record store)
only; it has no access to, and makes no attempt to touch, the
psychotherapy bucket - see runbooks/RUNBOOK_DISPOSITION.md for why
psychotherapy notes needed their own disposition role rather than a
--target flag on this one.

What this tool does NOT do: decide whether a stated basis is valid,
whether a retention period has been correctly configured, or whether
removal is actually appropriate for a given record. Those are
organizational and legal judgments this tool records evidence of, not
makes on its own - the same boundary runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md
draws for its own exception-attestation mechanism.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# FOUND AND FIXED: this module used `log` in two places - _dispose_one()'s
# bundle notice and _write_certificate()'s failure handler - while never
# importing logging or defining a logger anywhere. Both were live
# NameError paths, not dead code: disposing any .ndjson bundle key raised
# before the retention re-check, and _write_certificate()'s handler -
# whose docstring promises it never raises past the caller - raised while
# trying to report that a certificate could not be written, for a record
# that had already been destroyed. Named following this project's
# convention, matching core/fhir/client.py's "phi-ai.fhir.client".
log = logging.getLogger("phi-ai.fhir.purge")

# Encounter's OMOP row (cdm.visit_occurrence) and Patient's (cdm.person)
# are REFERENCED BY every other event table with no ON DELETE CASCADE
# (core/db/omop_schema.sql) - see core/db/omop_purge.py's own docstring
# for why that's deliberate. A disposal batch spanning multiple
# resource types is sorted so leaf types go first, Encounter next,
# Patient last - the identical FK-safe order omop_purge.py's
# _OMOP_DELETE_ORDER encodes at the table level, applied here at the
# resource-key level before any deletes are attempted.
_DISPOSAL_ORDER_LAST = {"Encounter": 1, "Patient": 2}


def _disposal_order_key(resource_type: str) -> int:
    return _DISPOSAL_ORDER_LAST.get(resource_type, 0)


def _resource_type_from_key(key: str) -> str:
    """fhir/{ResourceType}/{id}.json -> ResourceType. Every key this
    tool ever handles was constructed by core/fhir/client.py in exactly
    this shape - not a general-purpose parser."""
    parts = key.split("/")
    return parts[1] if len(parts) > 1 else ""


def assume_disposition_role(
    role_arn: str,
    region: str,
    admin_basis: str | None = None,
    session_name: str = "purge",
):
    """
    Assumes the disposition role. admin_basis, when provided, becomes a
    required AdminBasis session tag - the disposition role's own IAM
    policy (deploy/aws/iam.tf) denies s3:DeleteObject and
    s3:DeleteObjectVersion outright unless this tag is present, mirroring
    core/fhir/psychotherapy_restore.py's PsychotherapyException/
    PsychotherapyAttestation tags: enforced at the AWS layer, not just
    documented as a convention, so it holds even if this script were
    bypassed and the role assumed directly.

    The tag condition moved from s3:BypassGovernanceRetention to the
    delete actions themselves when Object Lock was removed. That was not
    a cosmetic rename: bypass-retention was a permission that only meant
    anything while a lock existed, so leaving the condition attached to
    it would have left admin-order deletes governed by nothing at all.
    Attaching it to the deletes keeps the AdminBasis tag load-bearing.

    Validates BEFORE importing boto3 or making any AWS call, same
    ordering discipline as every other restore-family script in this
    project.
    """
    import boto3

    tags = []
    if admin_basis is not None:
        if not admin_basis.strip():
            raise ValueError("admin_basis must be a non-empty string when provided")
        tags.append({"Key": "AdminBasis", "Value": admin_basis})

    sts = boto3.client("sts", region_name=region)
    # 30 minutes, not the 1-hour default. The role's max_session_duration
    # is only a ceiling (and AWS forbids setting it below 3600), so this is
    # where a genuinely short session for a sensitive, narrowly-justified
    # action gets requested.
    kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": 1800,
    }
    if tags:
        kwargs["Tags"] = tags
    resp = sts.assume_role(**kwargs)
    return resp["Credentials"]


def _open_disposition_db_connection(settings) -> "tuple[Optional[Any], bool, bool]":
    """
    Opens the disposition Postgres connection this deployment's
    disposal completeness (2026-08-17 audit, C4) needs, or returns
    (None, False) if the Postgres index isn't configured at all -
    storage-only deployments dispose of objects identically either way,
    the same graceful-skip posture db_target_configured() already
    documents for every other optional-database code path in this
    project.

    Returns (connection_or_None, omop_enabled, retrieval_enabled) -
    omop_enabled reuses settings.omop_target_configured() as the signal
    for "does this deployment's Postgres also have the OMOP schema/roles
    set up," rather than inventing a second, parallel flag: if a
    deployment runs the OMOP scheduler wiring, it has
    PHI_AI_OMOP_ETL_USERNAME set, and this tool's OMOP-delete step
    should be attempted under the identical condition.
    retrieval_enabled follows the same rule for the clinical retrieval
    index (settings.retrieval_configured(): a deployment whose ETL
    writes retrieval.clinical_text is exactly the deployment whose
    disposals must also delete from it - see
    core/db/retrieval_schema.sql's DISPOSAL note).

    The disposition role connects as a NEW, separate Postgres role
    (phi_ai_disposition - core/db/bootstrap_aws.sql,
    core/db/omop_bootstrap_aws.sql), not as the ingest or omop_etl role -
    one role, spanning both schemas, since a single disposal operation
    needs to remove rows from both. Its username is independently
    configurable
    (PHI_AI_DISPOSITION_DB_USERNAME/settings.disposition_db_username),
    matching db_ingest_username/db_reader_username/omop_etl_username's
    existing pattern rather than hardcoding the bootstrap SQL's role
    name into this script.
    """
    if not settings.disposition_db_configured():
        return None, False, False

    from core.db.connection import connect

    conn = connect(settings, settings.disposition_db_username)
    return conn, settings.omop_target_configured(), settings.retrieval_configured()


def _disposed_by(args) -> str:
    """Who performed the disposal, for the certificate.

    The role ARN rather than a human name: that is what this tool
    actually knows, and a certificate naming a person it cannot verify
    would be worse than one naming the credential that did the work.
    """
    return getattr(args, "role_arn", None) or "phi-ai-disposition"


def _write_certificate(
    certificates_dir: "Optional[str]",
    storage,
    key: str,
    versions_destroyed: int,
    mode: str,
    reason: str,
    disposed_by: str,
    audit_event_hash: str,
    retention_until,
) -> None:
    """Emit a certificate of destruction for one disposed record.

    Written AFTER the delete, unlike the audit entry which precedes it -
    a certificate asserts that destruction happened, so issuing one
    before it has would be a false statement if the delete then failed.

    Never raises past the caller: the record IS destroyed by this point
    and the audit log already says so. Failing the whole run because a
    certificate could not be written would leave the operator worse off,
    with a partially-disposed batch and no clear place to resume.
    """
    if not certificates_dir:
        return

    from pathlib import Path

    from core.fhir.disposal_certificate import build_certificate

    try:
        resource_type, _, resource_id = key.partition("/")[2].partition("/")
        certificate = build_certificate(
            resource_type=key.split("/")[1] if "/" in key else "?",
            resource_id=key.rsplit("/", 1)[-1].removesuffix(".json"),
            storage_key=key,
            stored_sha256_hex="",  # object is gone; digest is in the audit trail
            versions_destroyed=versions_destroyed,
            disposal_mode=mode,
            disposal_reason=reason,
            disposed_by=disposed_by,
            audit_event_hash=audit_event_hash,
            retention_until=retention_until.isoformat() if retention_until else None,
        )
        directory = Path(certificates_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{certificate.certificate_id}.txt").write_text(
            certificate.to_text(), encoding="utf-8"
        )
    except Exception as exc:
        log.error(
            "record %s WAS destroyed but its certificate could not be written: %s. The "
            "disposal is still recorded in the audit log.", key, exc,
        )


def _dispose_one(
    key: str,
    storage,
    db_conn: "Optional[Any]",
    omop_enabled: bool,
    retrieval_enabled: bool = False,
    require_expired: bool = False,
) -> int:
    """
    Deletes every trace of one stored resource, in the order
    disposal completeness requires (2026-08-17 audit, C4): any OMOP
    row first, then the index row, then EVERY stored version of the
    object itself. Raises on any failure without touching the storage
    object - if the OMOP or index delete fails (most likely an
    unexpected foreign-key violation - see core/db/omop_purge.py's own
    docstring on when that's the CORRECT outcome, not a bug), the
    stored object is left exactly as it was. The alternative -
    deleting storage first - would risk the opposite failure mode: an
    OMOP/index row surviving with no backing object, which
    core/db/reconcile.py would then report as a tampering-shaped
    "orphaned row" finding for something that was actually a routine,
    intended disposal. Under-deleting on a failure is the safer
    direction to fail in; callers should catch, log clearly, and
    continue to the next item rather than let this abort an entire
    disposal run.

    require_expired=True re-reads the object's recorded retention date
    and refuses to delete unless it has genuinely elapsed. This is the
    guard that S3 Object Lock used to provide for free; expired mode
    passes it on every call. It re-reads rather than trusting the
    caller's candidate list precisely so that a stale or mis-built list
    cannot cause a premature delete, and it runs BEFORE any OMOP/index
    row is touched, so a refusal leaves every trace of the resource
    intact rather than half-removed.
    """
    if key.endswith(".ndjson"):
        # A bundle holds every resource of one type for one patient. That
        # is the right unit for routine disposal - retention is uniform
        # within a bundle by construction, and disposal is normally
        # per-patient or per-retention-period.
        #
        # It is the WRONG unit for removing one named record: that would
        # need read-modify-write of the bundle, re-encrypting and
        # rewriting the remainder, which is a different operation with
        # different failure modes. Refused here rather than half-done, so
        # an operator finds out before a partial rewrite rather than
        # after.
        log.info("disposing bundle %s - every resource it holds is removed together", key)

    if require_expired:
        meta = storage.get_metadata(key)
        now = datetime.now(timezone.utc)
        if meta.retention_until is None:
            raise RuntimeError(
                f"Refusing to dispose of {key}: no retention date is recorded on the "
                "object, so there is nothing to confirm has elapsed. An object with no "
                "recorded retention is never treated as expired."
            )
        if meta.retention_until >= now:
            raise RuntimeError(
                f"Refusing to dispose of {key}: recorded retention runs until "
                f"{meta.retention_until.isoformat()}, which has not elapsed. Nothing in "
                "storage would have stopped this delete - this check is the only guard - "
                "so treat reaching it as a bug in the caller, not a transient condition."
            )

    if db_conn is not None:
        if omop_enabled:
            from core.db.omop_purge import delete_by_source_storage_key

            delete_by_source_storage_key(db_conn, key)
        if retrieval_enabled:
            # Extracted clinical text dies with its source, before the
            # index row and the object - retrieval_schema.sql's own
            # DISPOSAL rule. Same failure direction as OMOP above: an
            # error here aborts before storage is touched.
            from core.db.retrieval_purge import delete_clinical

            delete_clinical(db_conn, key)
        from core.db.index import delete_index_entry

        delete_index_entry(db_conn, key)

    return storage.delete_all_versions(key)


def _load_settings_and_storage(role_arn: str, admin_basis: str | None, session_name: str):
    """Shared setup for both modes: load Settings, assume the disposition
    role, and construct the storage/audit/database clients every mode
    needs. Returns (settings, storage, audit, db_conn, omop_enabled,
    retrieval_enabled) - db_conn is None (and both flags False) when the
    Postgres index isn't configured for this deployment; see
    _open_disposition_db_connection()'s own docstring."""
    from core.config.settings import Settings

    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"This tool currently supports AWS only; got {settings.cloud_provider}.", file=sys.stderr)
        sys.exit(2)

    print(f"Assuming {role_arn}...", file=sys.stderr)
    creds = assume_disposition_role(role_arn, settings.storage_region, admin_basis=admin_basis, session_name=session_name)

    from core.fhir.restore_common import apply_credentials_to_environment

    apply_credentials_to_environment(creds)

    from core.audit.log import AuditLog
    from core.audit.sink import S3AuditSink
    from core.storage.aws_s3 import S3Storage

    storage = S3Storage(
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        kms_key_id=settings.kms_key_id,
    )
    audit_sink = S3AuditSink(
        bucket=settings.audit_bucket,
        region=settings.storage_region,
        kms_key_id=settings.audit_kms_key_id,
    )
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())

    db_conn, omop_enabled, retrieval_enabled = _open_disposition_db_connection(settings)

    return settings, storage, audit, db_conn, omop_enabled, retrieval_enabled


def _parse_resource_list(path: str) -> list[tuple[str, str]]:
    """
    Parses a --resource-list file: one "ResourceType,resource_id" pair
    per line. Blank lines and lines starting with # are skipped, so the
    file can carry a per-entry note as a comment - a natural place to
    record why each specific record is included, reviewable in the file
    itself before anyone runs a command against it.

    Raises ValueError on any malformed line, naming the exact line
    number - the whole file should be correct before anything is
    deleted, not "mostly correct."
    """
    pairs: list[tuple[str, str]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) != 2:
                raise ValueError(f"{path}:{line_num}: expected 'ResourceType,resource_id', got {raw_line.strip()!r}")
            resource_type, resource_id = parts[0].strip(), parts[1].strip()
            if not resource_type or not resource_id:
                raise ValueError(f"{path}:{line_num}: empty resource type or id in {raw_line.strip()!r}")
            pairs.append((resource_type, resource_id))
    return pairs


def _run_expired(args: argparse.Namespace) -> int:
    settings, storage, audit, db_conn, omop_enabled, retrieval_enabled = _load_settings_and_storage(
        args.role_arn, admin_basis=None, session_name="purge-expired"
    )
    try:
        print("Listing stored objects...", file=sys.stderr)
        keys = storage.list_keys(prefix="fhir/")

        now = datetime.now(timezone.utc)
        expired: list[tuple[str, datetime]] = []
        # FOUND AND FIXED (2026-08-17 audit, MEDIUM): this loop previously
        # only ever appended to `expired` when meta.retention_until was
        # both present AND already past - an object carrying NO recorded
        # retention date at all simply never appeared anywhere, in any
        # list, with any message. That's backwards for a compliance tool:
        # every object stored through core/fhir/client.py records a
        # retention_until (see _retention_until() there), so an object
        # missing one is itself evidence of a misconfiguration - a
        # storage backend that dropped metadata, a direct PutObject that
        # bypassed this codebase, a bug in an older version of this
        # client - and it is also permanently ineligible for `expired`
        # disposal (_dispose_one's require_expired refuses an object with
        # no retention date, correctly - "no date" must never be treated
        # as "expired"). Silently doing nothing for such an object made
        # it invisible to the one tool an operator would run to notice
        # it. Now collected and reported explicitly below, still with
        # zero effect on what gets deleted.
        missing_retention: list[str] = []
        for key in keys:
            meta = storage.get_metadata(key)
            if meta.retention_until is None:
                missing_retention.append(key)
            elif meta.retention_until < now:
                expired.append((key, meta.retention_until))

        if missing_retention:
            print(
                f"\n{len(missing_retention)} object(s) carry NO recorded retention date. This is "
                "a compliance misconfiguration, not a routine state - every object stored "
                "through core/fhir/client.py records one. They are NOT eligible for disposal "
                "under this mode (an object with no recorded retention is never treated as "
                "expired) and will not appear here again until one is set. Investigate rather "
                "than ignore - see runbooks/RUNBOOK_DISPOSITION.md:",
                file=sys.stderr,
            )
            for key in missing_retention:
                print(f"  {key}", file=sys.stderr)

        if not expired:
            print("\nNo objects have passed their retention date. Nothing to dispose of.", file=sys.stderr)
            return 0

        # FK-safe order across the whole batch - see this module's own
        # docstring on _DISPOSAL_ORDER_LAST.
        expired.sort(key=lambda item: (_disposal_order_key(_resource_type_from_key(item[0])), item[0]))

        print(f"\n{len(expired)} object(s) have passed their retention date:", file=sys.stderr)
        for key, retention_until in expired:
            age = now - retention_until
            print(f"  {key}  (retention ended {retention_until.isoformat()}, {age.days} day(s) ago)", file=sys.stderr)

        if not args.confirm:
            print(
                f"\nDRY RUN - nothing deleted. {len(expired)} object(s) above would be permanently removed "
                "(every stored version, plus any index/OMOP rows derived from them). "
                "Re-run with --confirm to actually dispose of them.",
                file=sys.stderr,
            )
            return 0

        print(f"\nDisposing of {len(expired)} object(s)...", file=sys.stderr)
        disposed = 0
        failures: list[tuple[str, str]] = []
        for key, retention_until in expired:
            # BOUND, not discarded. The certificate below cites
            # event.event_hash as the audit entry proving this disposal
            # happened; this call's return value was previously thrown
            # away, so that reference raised NameError AFTER the record
            # had already been permanently destroyed - reported to the
            # operator as a failure for a delete that had in fact
            # succeeded, with no certificate written.
            event = audit.record(
                actor="phi-ai-purge-cli",
                action="record.dispose",
                resource_key=key,
                purpose_of_use=f"Retention period expired {retention_until.isoformat()}; routine disposition.",
            )
            try:
                versions = _dispose_one(key, storage, db_conn, omop_enabled,
                                        retrieval_enabled=retrieval_enabled,
                                        require_expired=True)
                _write_certificate(
                    getattr(args, "certificates_dir", None), storage, key,
                    versions or 0, "expired",
                    f"Retention period expired {retention_until.isoformat()}; routine "
                    "disposition.",
                    disposed_by=_disposed_by(args), audit_event_hash=event.event_hash,
                    retention_until=retention_until,
                )
            except Exception as exc:
                failures.append((key, str(exc)))
                print(f"  FAILED: {key}: {exc}", file=sys.stderr)
                continue
            disposed += 1
            print(f"  disposed: {key}", file=sys.stderr)

        print(f"\nDisposed of {disposed} object(s). Recorded in the audit trail as action=record.dispose.", file=sys.stderr)
        if failures:
            print(
                f"\n{len(failures)} object(s) FAILED disposal and were left untouched in storage - see "
                "the FAILED lines above. This is NOT a clean run; investigate before re-running "
                "(a foreign-key failure usually means a dependent resource - e.g. this Patient's "
                "Encounters/Conditions - needs disposing in the same run; see "
                "runbooks/RUNBOOK_DISPOSITION.md).",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        if db_conn is not None:
            db_conn.close()


def _run_admin_order(args: argparse.Namespace) -> int:
    from core.config.settings import Settings

    # NOTE: there was a refusal here, before any AWS call, when the
    # deployment ran with S3 Object Lock in COMPLIANCE mode. It is gone
    # because the condition it tested no longer exists - Object Lock was
    # removed from this deployment, so there is no mode in which S3
    # would refuse this delete and therefore nothing for this check to
    # detect. It was deliberately NOT replaced with a softer warning: a
    # check that always passes reads like a safeguard while providing
    # none, which is worse than its absence. The real controls for this
    # mode are the disposition role's IAM scope, the required
    # --admin-basis, the pre-deletion audit record, and --confirm.

    # Exactly one input mode: a single record via --resource-type +
    # --resource-id together, or a batch via --resource-list. Not both,
    # not neither, and not one half of the single-record pair.
    single_type_or_id_given = args.resource_type is not None or args.resource_id is not None
    if single_type_or_id_given and not (args.resource_type and args.resource_id):
        print("--resource-type and --resource-id must be given together.", file=sys.stderr)
        return 2
    if single_type_or_id_given and args.resource_list:
        print(
            "Specify either --resource-type/--resource-id for one record, or --resource-list "
            "for multiple - not both.",
            file=sys.stderr,
        )
        return 2
    if not single_type_or_id_given and not args.resource_list:
        print(
            "Specify either --resource-type/--resource-id for one record, or --resource-list "
            "for multiple.",
            file=sys.stderr,
        )
        return 2

    if args.resource_list:
        try:
            pairs = _parse_resource_list(args.resource_list)
        except (OSError, ValueError) as exc:
            print(f"Could not read --resource-list: {exc}", file=sys.stderr)
            return 2
        if not pairs:
            print(f"{args.resource_list} contains no resource entries. Nothing to do.", file=sys.stderr)
            return 0
    else:
        pairs = [(args.resource_type, args.resource_id)]

    settings, storage, audit, db_conn, omop_enabled, retrieval_enabled = _load_settings_and_storage(
        args.role_arn, admin_basis=args.admin_basis, session_name="purge-admin-order"
    )
    try:
        # Validate every entry exists and fetch its metadata BEFORE deleting
        # anything. Refuse the whole batch rather than partially apply it if
        # any entry is wrong - the operator's list should be accurate
        # up front, not corrected by watching what fails partway through.
        targets = []
        missing = []
        for resource_type, resource_id in pairs:
            key = f"fhir/{resource_type}/{resource_id}.json"
            if not storage.object_exists(key):
                missing.append(key)
                continue
            targets.append((key, storage.get_metadata(key)))

        if missing:
            print(f"\n{len(missing)} entry(ies) do not exist in the record store - refusing the whole batch:", file=sys.stderr)
            for key in missing:
                print(f"  {key}", file=sys.stderr)
            print("\nCorrect the list and re-run. Nothing was deleted.", file=sys.stderr)
            return 1

        # FK-safe order across the whole batch - see this module's own
        # docstring on _DISPOSAL_ORDER_LAST. Applied before display, so
        # what's printed for confirmation matches processing order.
        targets.sort(key=lambda item: (_disposal_order_key(_resource_type_from_key(item[0])), item[0]))

        print(
            f"\nAbout to permanently remove {len(targets)} record(s) under admin order, BEFORE their "
            "retention date(s) - every stored version, plus any index/OMOP rows derived from them:",
            file=sys.stderr,
        )
        for key, meta in targets:
            retention_str = meta.retention_until.isoformat() if meta.retention_until else "unknown"
            print(f"  {key}  (retention ends {retention_str})", file=sys.stderr)
        print(f"\nAdmin basis: {args.admin_basis}\n", file=sys.stderr)

        if not args.confirm:
            print("DRY RUN - nothing deleted. Re-run with --confirm to proceed.", file=sys.stderr)
            return 0

        if len(targets) == 1:
            key = targets[0][0]
            typed = input(f"Type the exact resource key to confirm permanent removal ({key}): ").strip()
            if typed != key:
                print("Confirmation did not match the resource key exactly. Aborting - nothing deleted.", file=sys.stderr)
                return 1
        else:
            typed = input(
                f"Type the number of records above to confirm permanent removal of all {len(targets)}: "
            ).strip()
            if typed != str(len(targets)):
                print("Confirmation did not match the record count exactly. Aborting - nothing deleted.", file=sys.stderr)
                return 1

        removed = 0
        failures: list[tuple[str, str]] = []
        for key, meta in targets:
            retention_str = meta.retention_until.isoformat() if meta.retention_until else "unknown"
            # BOUND, not discarded - same fix and same reasoning as
            # _run_expired() above. This mode's certificate cites the
            # admin-order audit entry, which is the permanent record that
            # a named record was removed inside its retention period and
            # on what stated basis.
            event = audit.record(
                actor="phi-ai-purge-cli",
                action="record.purge.admin_order",
                resource_key=key,
                purpose_of_use=(
                    f"Admin order early removal. Admin basis: {args.admin_basis}. "
                    f"Retention date bypassed: {retention_str}."
                ),
            )
            try:
                if key.endswith(".ndjson"):
                    # Removing ONE named record from a bundle needs a
                    # read-modify-write this tool does not implement.
                    # Refusing is correct: disposing the whole bundle
                    # would destroy other records the order does not
                    # cover, which is a disclosure-shaped error in the
                    # opposite direction.
                    message = (
                        f"{key} is a bundle holding every {key.split('/')[1]} for this "
                        "patient. Admin-order purge removes ONE named record, and this "
                        "tool will not delete the whole bundle to do it - that would "
                        "destroy records the order does not cover. Use expired mode for "
                        "routine disposal, or dispose the patient's records as a whole."
                    )
                    failures.append((key, message))
                    print(f"  REFUSED: {message}", file=sys.stderr)
                    continue

                # No require_expired here, deliberately: removing a record
                # BEFORE its retention date is precisely what this mode is
                # for. That is why it demands a named record, a stated
                # --admin-basis, an audit entry written before the delete,
                # and --confirm - none of which expired mode needs.
                versions = _dispose_one(key, storage, db_conn, omop_enabled,
                                        retrieval_enabled=retrieval_enabled)
                _write_certificate(
                    getattr(args, "certificates_dir", None), storage, key,
                    versions or 0, "admin-order",
                    f"Admin order early removal. Admin basis: {args.admin_basis}.",
                    disposed_by=_disposed_by(args), audit_event_hash=event.event_hash,
                    retention_until=None,
                )
            except Exception as exc:
                failures.append((key, str(exc)))
                print(f"  FAILED: {key}: {exc}", file=sys.stderr)
                continue
            removed += 1
            print(f"  removed: {key}", file=sys.stderr)

        print(
            f"\nRemoved {removed} record(s). Recorded in the audit trail as action=record.purge.admin_order.",
            file=sys.stderr,
        )
        if failures:
            print(
                f"\n{len(failures)} record(s) in this admin-order batch FAILED and were left untouched in "
                "storage - see the FAILED lines above. The typed confirmation covered the whole batch, but "
                "this is NOT a clean run; investigate before considering this order fulfilled (see "
                "runbooks/RUNBOOK_DISPOSITION.md).",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        if db_conn is not None:
            db_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove stored objects. See this module's own docstring before using either mode."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    expired = subparsers.add_parser(
        "expired", help="Dispose of objects whose retention period has already passed. Routine case."
    )
    expired.add_argument("--role-arn", required=True, help="ARN of the disposition IAM role.")
    expired.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag, prints what would be deleted and deletes nothing.",
    )
    expired.add_argument(
        "--certificates-dir",
        help="Write a certificate of destruction per disposed record into this directory. "
             "Each names the record, the reason, the time and the audit event that proves "
             "it - see core/fhir/disposal_certificate.py.",
    )
    expired.set_defaults(func=_run_expired)

    admin_order = subparsers.add_parser(
        "admin-order",
        help=(
            "Remove one or more specific records before their retention date, under a stated "
            "administrative basis. Exceptional case."
        ),
    )
    admin_order.add_argument("--role-arn", required=True, help="ARN of the disposition IAM role.")
    admin_order.add_argument(
        "--resource-type",
        default=None,
        help="FHIR resource type, e.g. DocumentReference. Use together with --resource-id for a single record.",
    )
    admin_order.add_argument(
        "--resource-id",
        default=None,
        help="The single resource ID to remove. Use together with --resource-type.",
    )
    admin_order.add_argument(
        "--resource-list",
        default=None,
        help=(
            "Path to a file listing multiple records to remove, one 'ResourceType,resource_id' pair per "
            "line (blank lines and lines starting with # are skipped). Use instead of "
            "--resource-type/--resource-id to remove more than one record under a single stated basis."
        ),
    )
    admin_order.add_argument(
        "--admin-basis",
        required=True,
        help=(
            "Free text stating the administrative basis for early removal - e.g. a legal order, a "
            "documented wind-down decision, or another specific justification. Becomes a required "
            "session tag and a permanent audit-log entry for every record removed."
        ),
    )
    admin_order.add_argument(
        "--confirm",
        action="store_true",
        help="Proceed to the confirmation step. Without this flag, prints what would happen and deletes nothing.",
    )
    admin_order.add_argument(
        "--certificates-dir",
        help="Write a certificate of destruction per disposed record into this directory.",
    )
    admin_order.set_defaults(func=_run_admin_order)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
