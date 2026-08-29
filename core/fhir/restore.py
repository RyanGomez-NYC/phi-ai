# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Retrieves and decrypts stored resources for an authorized records
request. See runbooks/RUNBOOK_DATA_RESTORE.md for the full workflow -
authorizing the request, this tool, verifying output, delivering it, and
recording fulfillment.

FOUND MISSING DURING A CLEANUP PASS: this module was documented in
RUNBOOK_DATA_RESTORE.md and referenced by the restore IAM role's own
comments (deploy/aws/iam.tf) but never actually existed in the repo -
running the documented command would have failed with
"No module named core.fhir.restore". This is that module.

Requires assuming the `restore` IAM role (deploy/aws/iam.tf) with a
PurposeOfUse session tag set from --purpose-of-use - the IAM policy
denies every S3 read outright unless that tag is present
(DenyReadWithoutPurposeOfUse), so this isn't just documentation of the
requirement, it's enforced at the AWS layer even if this script were
bypassed. MFA is required by the role's own trust policy.

Given --patient-id, queries the Postgres index
(core/db/index.py's find_by_patient_reference()) for every stored
resource linked to that patient, optionally narrowed to
--resource-type. For each match: verifies the stored object's integrity
BEFORE attempting decryption (see core/fhir/client.py's
_stored_sha256_hex() for exactly what that digest covers - a real,
previously-broken check, fixed in the same cleanup pass that produced
this module), decrypts via the same EnvelopeEncryptor/AWSKMS classes
every other part of this project uses (reused here deliberately, not
reimplemented - see the note on psychotherapy_restore.py's own history
for why that matters), and writes the plaintext resource to --output.
Stops immediately on any integrity failure rather than skipping the bad
object and continuing, per RUNBOOK_DATA_RESTORE.md step 3.

Records action="record.read" for every successful retrieval, via the
same AuditLog/audit bucket the rest of this project writes to - not a
separate log. Recorded BEFORE the plaintext resource is written to
--output (fixed 2026-08-17 audit, MEDIUM, "restore.py writes plaintext
to disk before recording the audit event") - see the ordering comment in
the main loop below for why that direction, not the reverse, is the safe
one.

    python -m core.fhir.restore \\
        --patient-id eSyn0001Patient \\
        --resource-type DocumentReference \\
        --purpose-of-use "Records request RR-2026-0142, patient right of access" \\
        --role-arn arn:aws:iam::123456789012:role/phi-ai-dev-restore \\
        --output ./restore-output/

--resource-type is optional - omit it to restore everything stored for
the patient. Bucket, region, and Postgres connection details are NOT
separate flags - they come from the same PHI_AI_* environment this
whole project already uses (Settings.from_env()), so this tool can never
drift from what's actually configured for the deployment it's run
against.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.fhir.restore_common import apply_credentials_to_environment, restore_one


def assume_restore_role(role_arn: str, region: str, purpose_of_use: str, session_name: str = "restore"):
    """
    Assumes the restore role with the required PurposeOfUse session tag.
    MFA is required by the role's trust policy (deploy/aws/iam.tf) - the
    caller's own AWS credentials must already have an MFA-verified
    session for this to succeed; that is not something this script can
    do on your behalf.

    Validates arguments BEFORE importing boto3 or making any AWS call -
    a malformed purpose_of_use should fail immediately and clearly, not
    after unrelated setup work.
    """
    if not purpose_of_use.strip():
        raise ValueError("purpose_of_use must be a non-empty string")

    import boto3

    sts = boto3.client("sts", region_name=region)
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        Tags=[{"Key": "PurposeOfUse", "Value": purpose_of_use}],
    )
    return resp["Credentials"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore and decrypt stored resources for an authorized records request."
    )
    parser.add_argument("--patient-id", required=True, help="Epic-internal patient ID, e.g. eSyn0001Patient.")
    parser.add_argument(
        "--resource-type",
        default=None,
        help="Restrict to one FHIR resource type. Omit to restore everything stored for this patient.",
    )
    parser.add_argument(
        "--purpose-of-use",
        required=True,
        help=(
            "Specific, documented reason for this retrieval - becomes a required AWS session "
            "tag and is recorded in the audit log. Keep it specific and accurate; see "
            "runbooks/RUNBOOK_DATA_RESTORE.md."
        ),
    )
    parser.add_argument("--role-arn", required=True, help="ARN of the restore IAM role.")
    parser.add_argument("--output", required=True, help="Directory to write restored plaintext JSON files into.")
    args = parser.parse_args()

    # Settings.from_env() needs no AWS credentials at all - just env vars
    # and a local file check - so this is safe to load before any role
    # assumption, and lets everything below use the SAME storage_region
    # for both the STS call and the storage/KMS/DB clients, rather than
    # risking a separate --region flag drifting from what's configured.
    from core.config.settings import Settings

    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"This tool currently supports AWS only; got {settings.cloud_provider}.", file=sys.stderr)
        return 2
    if not (settings.db_host and settings.db_name and settings.db_reader_username):
        print(
            "No PHI_AI_DB_* settings present. This tool needs the Postgres index to find "
            "what's stored for a given patient - without it there's no way to look up which "
            "S3 keys belong to --patient-id.",
            file=sys.stderr,
        )
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Assuming {args.role_arn} with PurposeOfUse={args.purpose_of_use!r}...", file=sys.stderr)
    creds = assume_restore_role(args.role_arn, settings.storage_region, args.purpose_of_use)
    apply_credentials_to_environment(creds)

    # Imported only after credentials are in place, so these modules'
    # internal boto3.client() calls pick up the assumed-role session.
    from core.audit.log import AuditLog
    from core.audit.sink import S3AuditSink
    from core.crypto.envelope import AWSKMS, EnvelopeEncryptor
    from core.db import connection as db_connection
    from core.db.index import find_by_patient_reference
    from core.storage.aws_s3 import S3Storage

    patient_reference = f"Patient/{args.patient_id}"
    print(f"Looking up stored resources for {patient_reference}...", file=sys.stderr)

    # FOUND AND FIXED: this called connect(host=, port=, dbname=,
    # username=, region=) and raised TypeError before opening anything.
    # core/db/connection.py's connect() takes (settings, username) and
    # derives host/port/dbname/region from settings itself - that is the
    # whole point of it, since only connect() knows GCP has no host:port
    # and Azure needs an Entra token. core/fhir/scheduler.py already
    # called it this way; this is now the same call.
    conn = db_connection.connect(settings, settings.db_reader_username)
    try:
        rows = find_by_patient_reference(conn, patient_reference)
    finally:
        conn.close()

    if args.resource_type:
        rows = [r for r in rows if r["resource_type"] == args.resource_type]

    if not rows:
        suffix = f" of type {args.resource_type}" if args.resource_type else ""
        print(f"No stored resources found for {patient_reference}{suffix}.", file=sys.stderr)
        return 0

    print(f"Found {len(rows)} resource(s). Restoring...", file=sys.stderr)

    storage = S3Storage(
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        kms_key_id=settings.kms_key_id,
    )
    encryptor = EnvelopeEncryptor(AWSKMS(key_id=settings.kms_key_id, region=settings.storage_region))
    audit_sink = S3AuditSink(
        bucket=settings.audit_bucket,
        region=settings.storage_region,
        kms_key_id=settings.audit_kms_key_id,
    )
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())

    restored = 0
    for row in rows:
        # FOUND AND FIXED: this read row["s3_key"], a column that does
        # not exist. find_by_patient_reference() selects storage_key and
        # builds its dicts from cur.description, so every row raised
        # KeyError: 's3_key' here - zero resources restored, and the
        # failure landed before the audit record rather than after it.
        # core/web/data.py reads the same column under the same name.
        key = row["storage_key"]
        print(f"  {key} ...", file=sys.stderr)

        try:
            resource = restore_one(storage, encryptor, key)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            print(
                f"Stopping - {restored} resource(s) were restored successfully before this "
                "failure; do not treat those as suspect, but do not continue past this one.",
                file=sys.stderr,
            )
            return 1

        # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "restore.py writes
        # plaintext to disk before recording the audit event"): the
        # write below used to happen BEFORE audit.record(). If the audit
        # sink write failed after the plaintext file was already on disk
        # - a transient network error to the audit bucket, a permissions
        # regression on the audit KMS key, anything
        # core/audit/sink.py's S3AuditSink.__call__ can raise - the
        # result was disclosed PHI on local disk with no corresponding
        # entry in the tamper-evident audit trail: exactly the gap
        # RUNBOOK_INCIDENT_RESPONSE.md's audit-trail-driven scoping
        # process depends on not existing. core/fhir/purge.py already
        # gets this right (see its own "Every deletion is audit-logged
        # ... BEFORE it happens" - purge.py's module docstring): audit
        # the sensitive action before performing it, so a failure here
        # aborts the action instead of letting it happen unrecorded.
        # Reordered to match. The residual risk moves in the safer
        # direction: an audit entry can now exist for a read whose write
        # to disk subsequently failed (a much rarer, non-PHI-disclosing
        # failure mode - permissions or disk space on --output), never
        # the reverse. Proven locally (no live cloud credentials in this
        # sandbox) against a fake audit sink that raises on call: the
        # old ordering left a plaintext file on disk despite the raise;
        # this ordering does not.
        try:
            audit.record(
                actor="phi-ai-restore-cli",
                action="record.read",
                resource_key=key,
                purpose_of_use=args.purpose_of_use,
            )
        except Exception as exc:
            print(f"Failed to record the audit event for {key}: {exc}", file=sys.stderr)
            print(
                f"Stopping - {restored} resource(s) were restored successfully before this "
                "failure. This resource was NOT written to disk, because it could not be "
                "recorded in the audit trail first. See runbooks/RUNBOOK_INCIDENT_RESPONSE.md "
                "if this persists.",
                file=sys.stderr,
            )
            return 1

        out_path = output_dir / f"{row['resource_type']}_{row['resource_id']}.json"
        out_path.write_text(json.dumps(resource, indent=2))
        restored += 1

    print(f"\nRestored {restored} resource(s) to {output_dir}/", file=sys.stderr)
    print(
        "Remember to delete the local plaintext copy once delivered, per "
        "runbooks/RUNBOOK_DATA_RESTORE.md step 4.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
