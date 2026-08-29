# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Removes stored psychotherapy notes - the psychotherapy-bucket twin of
core/fhir/purge.py, and (2026-08-17 audit, C4) the FIRST disposition
path this bucket has ever had. Before this module existed, purge.py's
storage client was bound to settings.storage_bucket (the general record
store) only - it never touched, and had no IAM grant to touch, the
psychotherapy bucket at all, and no role in deploy/aws/iam.tf held
delete there either. The most sensitive data class in this system was
also the one with no disposal path whatsoever: HIPAA's own disposal
requirement (45 CFR 164.310(d)(2)(i)) applies to psychotherapy notes
exactly as it does to everything else, and it was structurally
impossible to satisfy for this bucket.

DELIBERATELY A SEPARATE SCRIPT, not a --target flag on purge.py -
mirrors the existing psychotherapy_restore.py/restore.py split (see
that module's own docstring for the reasoning restated here): a
different bucket, KMS key, and now IAM role, with the general
disposition role holding zero access here and this role holding zero
access to the general record store - the same "genuinely separate, not
cosmetically separate" property runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md
insists on for storage and restore already.

SIMPLER than purge.py in one real way: psychotherapy notes are
deliberately never indexed and never ETL'd into OMOP (see
runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md's "Why the Postgres index never
sees this data") - so there is no derived-row cleanup step here at all,
unlike purge.py's OMOP/index deletes. Disposing a psychotherapy note is
exactly "delete every stored version of the object," nothing else.

    python -m core.fhir.psychotherapy_purge expired --role-arn <psychotherapy-disposition-role-arn>
    python -m core.fhir.psychotherapy_purge expired --role-arn <psychotherapy-disposition-role-arn> --confirm

"expired" mode - the routine case: only ever touches a note whose
recorded retention_until has already passed, and needs no special
permission beyond an ordinary delete grant.

NOTHING IN STORAGE ENFORCES THAT. This paragraph used to say the mode
"structurally cannot delete anything still under an active lock
(enforced by S3 Object Lock itself, not this script's logic)." That is
false and was a load-bearing false assurance: deploy/aws/s3_psychotherapy.tf
creates this bucket WITHOUT Object Lock (see its own "NO INFRASTRUCTURE
IMMUTABILITY" header), so no lock exists to refuse a delete. The
retention comparison in _run_expired() below is the only thing standing
between this tool and a premature deletion of a psychotherapy note.

KNOWN GAP, deliberate and stated rather than left to be discovered:
core/fhir/purge.py goes one step further than this file does. Its
_dispose_one() re-reads each object's recorded retention date and
re-verifies it has elapsed IMMEDIATELY BEFORE deleting
(require_expired=True), specifically so a stale candidate list cannot
cause an early delete. This file checks retention once, while building
its candidate list, and then deletes from that list. The window is small
- one process, one pass - but it is a real difference from the general
store's disposal path, and closing it means giving this file the same
re-verify-before-delete step rather than assuming the list is still
accurate.

    python -m core.fhir.psychotherapy_purge admin-order \\
        --role-arn <psychotherapy-disposition-role-arn> \\
        --resource-type DocumentReference --resource-id eSynNote0001 \\
        --admin-basis "Subpoena, Case No. 2026-CV-1234, Superior Court of Example County, dated 2026-08-15" \\
        --confirm

"admin-order" mode - identical semantics to purge.py's own, including
that NOTHING categorically prevents it anymore: the COMPLIANCE-mode
refusal is gone along with Object Lock itself, so a holder of the
psychotherapy disposition role can remove a named note at any point
inside its retention period. Requires the same enable_admin_order_purge
opt-in (reused, not a second variable - see deploy/aws/iam.tf), requires
--admin-basis as a required AdminBasis session tag enforced at the IAM
layer, and requires typed interactive confirmation before deleting.

What this tool does NOT do: everything purge.py's own "What this tool
does NOT do" section already states, unchanged here. It also does not,
and structurally cannot, touch the general record store bucket - this
script's storage client is bound to
settings.psychotherapy_storage_bucket only.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone


def assume_psychotherapy_disposition_role(
    role_arn: str,
    region: str,
    admin_basis: str | None = None,
    session_name: str = "psychotherapy-purge",
):
    """Identical mechanism to core/fhir/purge.py's
    assume_disposition_role() - see that function's own docstring. A
    separate function, not a shared import, only because the two roles
    (disposition, psychotherapy_disposition) are genuinely different
    IAM principals with no relationship to each other; the STS call
    shape is the only thing they share."""
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


def _load_settings_and_storage(role_arn: str, admin_basis: str | None, session_name: str):
    from core.config.settings import Settings

    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"This tool currently supports AWS only; got {settings.cloud_provider}.", file=sys.stderr)
        sys.exit(2)
    if not settings.psychotherapy_storage_bucket or not settings.psychotherapy_kms_key_id:
        print(
            "PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET and PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID "
            "must both be set - see runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Assuming {role_arn}...", file=sys.stderr)
    creds = assume_psychotherapy_disposition_role(
        role_arn, settings.storage_region, admin_basis=admin_basis, session_name=session_name
    )

    from core.fhir.restore_common import apply_credentials_to_environment

    apply_credentials_to_environment(creds)

    from core.audit.log import AuditLog
    from core.audit.sink import S3AuditSink
    from core.storage.aws_s3 import S3Storage

    storage = S3Storage(
        bucket=settings.psychotherapy_storage_bucket,
        region=settings.storage_region,
        kms_key_id=settings.psychotherapy_kms_key_id,
    )
    audit_sink = S3AuditSink(
        bucket=settings.audit_bucket,
        region=settings.storage_region,
        kms_key_id=settings.audit_kms_key_id,
    )
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())
    return settings, storage, audit


def _parse_resource_list(path: str) -> list[tuple[str, str]]:
    """Identical to core/fhir/purge.py's own - see that function's
    docstring."""
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


def _open_psych_retrieval_connection(settings):
    """A disposition-role connection for deleting a disposed note's
    retrieval rows (retrieval.psychotherapy_text), or None when this
    deployment never indexed psychotherapy text at all.

    Gated on psychotherapy_retrieval_configured() - the same "the
    deployment that writes the table is the deployment whose disposals
    must clean it" rule core/fhir/purge.py applies to the general
    clinical_text table - plus disposition_db_configured(), since the
    delete runs as the disposition role (whose column-scoped grants are
    in retrieval_bootstrap_<cloud>.sql)."""
    if not (settings.psychotherapy_retrieval_configured()
            and settings.disposition_db_configured()):
        return None
    from core.db.connection import connect

    return connect(settings, settings.disposition_db_username)


def _run_expired(args: argparse.Namespace) -> int:
    settings, storage, audit = _load_settings_and_storage(
        args.role_arn, admin_basis=None, session_name="psychotherapy-purge-expired"
    )

    print("Listing psychotherapy note contents...", file=sys.stderr)
    keys = storage.list_keys(prefix="notes/")

    now = datetime.now(timezone.utc)
    expired: list[tuple[str, datetime]] = []
    for key in keys:
        meta = storage.get_metadata(key)
        # A note carrying NO recorded retention date is never treated as
        # expired - "no date" is not "elapsed". With Object Lock gone,
        # this comparison is the only guard against a premature delete;
        # see this module's docstring.
        if meta.retention_until is not None and meta.retention_until < now:
            expired.append((key, meta.retention_until))

    if not expired:
        print("No notes have passed their retention date. Nothing to dispose of.", file=sys.stderr)
        return 0

    print(f"\n{len(expired)} note(s) have passed their retention date:", file=sys.stderr)
    for key, retention_until in sorted(expired):
        age = now - retention_until
        print(f"  {key}  (retention ended {retention_until.isoformat()}, {age.days} day(s) ago)", file=sys.stderr)

    if not args.confirm:
        print(
            f"\nDRY RUN - nothing deleted. {len(expired)} note(s) above would be permanently removed "
            "(every stored version). Re-run with --confirm to actually dispose of them.",
            file=sys.stderr,
        )
        return 0

    print(f"\nDisposing of {len(expired)} note(s)...", file=sys.stderr)
    retrieval_conn = _open_psych_retrieval_connection(settings)
    disposed = 0
    failures: list[tuple[str, str]] = []
    for key, retention_until in sorted(expired):
        audit.record(
            actor="phi-ai-psychotherapy-purge-cli",
            action="record.dispose.psychotherapy",
            resource_key=key,
            purpose_of_use=f"Retention period expired {retention_until.isoformat()}; routine disposition.",
        )
        try:
            if retrieval_conn is not None:
                # Indexed text dies with the note, BEFORE the storage
                # delete - the same failure direction core/fhir/purge.py
                # uses: on error the note is left exactly as it was.
                from core.db.retrieval_purge import delete_psychotherapy

                delete_psychotherapy(retrieval_conn, key)
            storage.delete_all_versions(key)
        except Exception as exc:
            failures.append((key, str(exc)))
            print(f"  FAILED: {key}: {exc}", file=sys.stderr)
            continue
        disposed += 1
        print(f"  disposed: {key}", file=sys.stderr)

    print(
        f"\nDisposed of {disposed} note(s). Recorded in the audit trail as action=record.dispose.psychotherapy.",
        file=sys.stderr,
    )
    if failures:
        print(f"\n{len(failures)} note(s) FAILED disposal and were left untouched - see FAILED lines above.", file=sys.stderr)
        return 1
    return 0


def _run_admin_order(args: argparse.Namespace) -> int:
    from core.config.settings import Settings

    # The COMPLIANCE-mode refusal that stood here is gone, for the
    # reasons core/fhir/purge.py's _run_admin_order documents at length:
    # Object Lock was removed, so the condition it tested cannot occur
    # and a check that always passes would imply a protection that no
    # longer exists.

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

    settings, storage, audit = _load_settings_and_storage(
        args.role_arn, admin_basis=args.admin_basis, session_name="psychotherapy-purge-admin-order"
    )

    targets = []
    missing = []
    for resource_type, resource_id in pairs:
        key = f"notes/{resource_type}/{resource_id}.json"
        if not storage.object_exists(key):
            missing.append(key)
            continue
        targets.append((key, storage.get_metadata(key)))

    if missing:
        print(f"\n{len(missing)} entry(ies) do not exist in the psychotherapy note store - refusing the whole batch:", file=sys.stderr)
        for key in missing:
            print(f"  {key}", file=sys.stderr)
        print("\nCorrect the list and re-run. Nothing was deleted.", file=sys.stderr)
        return 1

    print(
        f"\nAbout to permanently remove {len(targets)} note(s) under admin order, BEFORE their "
        "retention date(s) - every stored version:",
        file=sys.stderr,
    )
    for key, meta in sorted(targets):
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

    retrieval_conn = _open_psych_retrieval_connection(settings)
    removed = 0
    failures: list[tuple[str, str]] = []
    for key, meta in sorted(targets):
        retention_str = meta.retention_until.isoformat() if meta.retention_until else "unknown"
        audit.record(
            actor="phi-ai-psychotherapy-purge-cli",
            action="record.purge.admin_order.psychotherapy",
            resource_key=key,
            purpose_of_use=(
                f"Admin order early removal. Admin basis: {args.admin_basis}. "
                f"Retention date bypassed: {retention_str}."
            ),
        )
        try:
            # FOUND AND FIXED: this passed bypass_governance_retention=True,
            # a parameter core/storage/base.py's delete_all_versions(key)
            # does not accept and no storage backend implements - it was
            # removed along with Object Lock (see delete_object()'s own
            # docstring in base.py). Every admin-order deletion therefore
            # raised TypeError, AFTER the audit entry above had already
            # recorded the removal as about to happen, and the operator
            # saw a FAILED line for every note. The expired path above
            # always called this correctly, which is why the routine mode
            # worked and the exceptional one never did.
            if retrieval_conn is not None:
                # Same ordering and reasoning as the expired mode above.
                from core.db.retrieval_purge import delete_psychotherapy

                delete_psychotherapy(retrieval_conn, key)
            storage.delete_all_versions(key)
        except Exception as exc:
            failures.append((key, str(exc)))
            print(f"  FAILED: {key}: {exc}", file=sys.stderr)
            continue
        removed += 1
        print(f"  removed: {key}", file=sys.stderr)

    print(
        f"\nRemoved {removed} note(s). Recorded in the audit trail as action=record.purge.admin_order.psychotherapy.",
        file=sys.stderr,
    )
    if failures:
        print(f"\n{len(failures)} note(s) in this batch FAILED - see FAILED lines above. Not a clean run.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove stored psychotherapy notes. See this module's own docstring before using either mode."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    expired = subparsers.add_parser(
        "expired", help="Dispose of notes whose retention period has already passed. Routine case."
    )
    expired.add_argument("--role-arn", required=True, help="ARN of the psychotherapy_disposition IAM role.")
    expired.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag, prints what would be deleted and deletes nothing.",
    )
    expired.set_defaults(func=_run_expired)

    admin_order = subparsers.add_parser(
        "admin-order",
        help=(
            "Remove one or more specific notes before their retention date, under a stated "
            "administrative basis. Exceptional case."
        ),
    )
    admin_order.add_argument("--role-arn", required=True, help="ARN of the psychotherapy_disposition IAM role.")
    admin_order.add_argument("--resource-type", default=None)
    admin_order.add_argument("--resource-id", default=None)
    admin_order.add_argument("--resource-list", default=None)
    admin_order.add_argument(
        "--admin-basis",
        required=True,
        help="Free text stating the administrative basis for early removal. Becomes a required session tag and a permanent audit-log entry.",
    )
    admin_order.add_argument(
        "--confirm",
        action="store_true",
        help="Proceed to the confirmation step. Without this flag, prints what would happen and deletes nothing.",
    )
    admin_order.set_defaults(func=_run_admin_order)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
