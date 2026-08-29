# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Retrieves and decrypts a psychotherapy note - the only mechanism this
codebase provides for reading psychotherapy note content, and
deliberately separate from core/fhir/restore.py's general path. See
runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for the full picture.

Requires assuming the psychotherapy_restore IAM role (deploy/aws/iam.tf)
with two session tags this script sets directly from your command-line
arguments - the IAM policy denies the S3 read outright unless both are
present and PsychotherapyException is exactly one of the three values
below, so this isn't just documentation of intent, it's enforced at the
AWS layer even if this script were bypassed entirely:

  --exception     Must be exactly one of:
                    originator-treatment  (45 CFR 164.508(a)(2)(i)(A) -
                      use by the note's own author, for treatment)
                    training-program      (164.508(a)(2)(i)(B) - the
                      covered entity's own training programs)
                    legal-defense         (164.508(a)(2)(i)(C) -
                      defending itself in litigation the patient brought)
                  Nothing else satisfies HIPAA's actual exception list -
                  in particular, an ordinary records request does not,
                  which is why the general restore role has no access to
                  this bucket at all.

                  THESE THREE STRINGS ARE AN IAM CONTRACT, NOT LABELS.
                  They are sent verbatim as the PsychotherapyException
                  session tag and matched exactly by the role's policy,
                  so changing one here without changing the policy would
                  deny every retrieval using it.

  --attestation   Free text: who is attesting to this exception and why.
                  Required by IAM policy (see deploy/aws/iam.tf), but
                  NOT independently verified - this script and the IAM
                  policy behind it can confirm a claim was made and
                  recorded, not that a given requester genuinely is the
                  note's own original author. That verification is an
                  organizational/procedural control, not a technical
                  one - see the runbook for what that means in practice.

Every successful invocation is captured in TWO independent trails: the
AWS-level CloudTrail record (from the psychotherapy_restore role's own
session tags - visible regardless of anything this script does), and an
application-level record this script writes directly to the same
hash-chained audit log core/fhir/restore.py uses, under its own distinct
action: record.read.psychotherapy. Both matter - CloudTrail is
independent and out-of-band, but does not carry the hash-chain
tamper-evidence property described in core/audit/log.py; the application
log does. FIXED: this script previously only produced the CloudTrail
trail - the IAM role already held exactly the permissions needed for the
application log (AppendAuditRecords, UseAuditKey in deploy/aws/iam.tf),
matching restore.py's own pattern, but this script never used them, so
the most HIPAA-sensitive category of restore in this system was the one
path missing from the tamper-evident record every other restore
produces.

    python -m core.fhir.psychotherapy_restore \\
        --resource-type DocumentReference --resource-id eSynNote0001 \\
        --role-arn arn:aws:iam::123456789012:role/phi-ai-dev-psychotherapy-restore \\
        --exception originator-treatment \\
        --attestation "Dr. Jane Smith, treating clinician, retrieving own prior note" \\
        --output ./restore-output/

Bucket, region, and KMS key come from PHI_AI_PSYCHOTHERAPY_* -
Settings.from_env(), not a separate flag, so this can never point at a
different bucket than what the rest of the deployment is actually
configured with.

FOUND AND FIXED (2026-08-17 audit, MEDIUM, "psychotherapy_restore
prints the decrypted note to stdout"): this script used to print the
decrypted note directly to stdout instead of writing it to a file, the
one restore-family script in this project that did. stdout is not a
controlled destination the way a file under an operator-chosen
--output directory is - it lands in shell scrollback, tmux/screen
session logs, and (if this were ever wrapped by automation) CI logs,
none of which this project's threat model treats as an acceptable
resting place for decrypted PHI - see core/fhir/documents.py's own
"Neither mode prints extracted text - it is PHI and stdout ends up in
scrollback and CI logs" for the same concern applied elsewhere in this
codebase. Now writes to --output, matching core/fhir/restore.py's
existing pattern exactly, rather than being the one exception to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.fhir.restore_common import apply_credentials_to_environment, restore_one

# IAM-matched session tag values - see the module docstring. Not labels;
# do not reword.
VALID_EXCEPTIONS = ("originator-treatment", "training-program", "legal-defense")


def assume_psychotherapy_restore_role(
    role_arn: str,
    region: str,
    exception: str,
    attestation: str,
    session_name: str = "psychotherapy-restore",
):
    """
    Assumes the psychotherapy_restore role with the two required session
    tags. MFA is required by the role's trust policy (deploy/aws/iam.tf)
    - the caller's own AWS credentials must already have an MFA-verified
    session for this to succeed; that is not something this script can
    do on your behalf.

    Validates arguments BEFORE importing boto3 or making any AWS call -
    a malformed --exception or empty --attestation should fail
    immediately and clearly, not after unrelated setup work.

    Returns the raw temporary-credentials dict (not a boto3.Session) -
    matching core/fhir/restore.py's assume_restore_role() exactly, so
    both scripts hand off to the same apply_credentials_to_environment()
    in core/fhir/restore_common.py rather than each having their own way
    of getting credentials into effect.
    """
    if exception not in VALID_EXCEPTIONS:
        # argparse's choices= already prevents this in normal use: this
        # check exists so the function is also safe to call directly
        # (e.g. from a test or a future caller) without relying on
        # argparse having run first.
        raise ValueError(f"exception must be one of {VALID_EXCEPTIONS}, got {exception!r}")
    if not attestation.strip():
        raise ValueError("attestation must be a non-empty string")

    import boto3

    sts = boto3.client("sts", region_name=region)
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        Tags=[
            {"Key": "PsychotherapyException", "Value": exception},
            {"Key": "PsychotherapyAttestation", "Value": attestation},
        ],
        # 30 minutes, not the 1-hour default. The role's
        # max_session_duration is only a ceiling (and AWS forbids setting
        # it below 3600), so this is where a genuinely short session for a
        # sensitive, narrowly-justified action gets requested.
        DurationSeconds=1800,
    )
    return resp["Credentials"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore and decrypt a psychotherapy note. Requires one of three narrow HIPAA exceptions."
    )
    parser.add_argument("--resource-type", required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument(
        "--exception",
        required=True,
        choices=VALID_EXCEPTIONS,
        help="Which of the three HIPAA 164.508(a)(2) exceptions justifies this retrieval.",
    )
    parser.add_argument(
        "--attestation",
        required=True,
        help=(
            "Free text: who is attesting to this exception and why "
            "(e.g. 'Dr. Jane Smith, treating clinician, retrieving own prior note')."
        ),
    )
    parser.add_argument("--role-arn", required=True, help="ARN of the psychotherapy_restore IAM role.")
    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Directory to write the restored plaintext JSON file into - matching "
            "core/fhir/restore.py's --output, rather than printing decrypted PHI to stdout "
            "(fixed 2026-08-17 audit, MEDIUM, see this module's own docstring)."
        ),
    )
    args = parser.parse_args()

    # Settings.from_env() needs no AWS credentials at all, so this is
    # safe to load before role assumption, and lets the STS call and the
    # storage/KMS clients all use the same storage_region rather than a
    # separate --region flag that could drift from what's configured.
    from core.config.settings import Settings

    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"This tool currently supports AWS only; got {settings.cloud_provider}.", file=sys.stderr)
        return 2
    if not settings.psychotherapy_storage_bucket or not settings.psychotherapy_kms_key_id:
        print(
            "PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET and PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID "
            "must both be set - see runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md.",
            file=sys.stderr,
        )
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # "notes/" prefix, not "fhir/" - deploy/aws/iam.tf's
    # psychotherapy_restore role is scoped to exactly this prefix, so the
    # key shape is an access-control boundary and has to stay in step
    # with that policy.
    key = f"notes/{args.resource_type}/{args.resource_id}.json"

    print(f"Assuming {args.role_arn} with PsychotherapyException={args.exception!r}...", file=sys.stderr)
    creds = assume_psychotherapy_restore_role(
        args.role_arn, settings.storage_region, args.exception, args.attestation
    )
    apply_credentials_to_environment(creds)

    # Imported only after credentials are in place, so these modules'
    # internal boto3.client() calls pick up the assumed-role session.
    from core.audit.log import AuditLog
    from core.audit.sink import S3AuditSink
    from core.crypto.envelope import AWSKMS, EnvelopeEncryptor
    from core.storage.aws_s3 import S3Storage

    storage = S3Storage(
        bucket=settings.psychotherapy_storage_bucket,
        region=settings.storage_region,
        kms_key_id=settings.psychotherapy_kms_key_id,
    )
    encryptor = EnvelopeEncryptor(AWSKMS(key_id=settings.psychotherapy_kms_key_id, region=settings.storage_region))

    print(f"Fetching and decrypting {key}...", file=sys.stderr)
    try:
        resource = restore_one(storage, encryptor, key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    audit_sink = S3AuditSink(
        bucket=settings.audit_bucket,
        region=settings.storage_region,
        kms_key_id=settings.audit_kms_key_id,
    )
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())

    # Audit BEFORE writing the plaintext note to disk - already the
    # correct order here (unlike restore.py/bulk_export.py's ordering
    # bug fixed in the same audit pass), but now wrapped so a failed
    # audit write produces a clear, actionable message and a clean
    # non-zero exit instead of a bare traceback, matching the error-
    # handling style those two scripts now use for the same failure.
    try:
        audit.record(
            actor="phi-ai-psychotherapy-restore-cli",
            action="record.read.psychotherapy",
            resource_key=key,
            purpose_of_use=f"exception={args.exception}: {args.attestation}",
        )
    except Exception as exc:
        print(f"Failed to record the audit event for {key}: {exc}", file=sys.stderr)
        print(
            "This note was NOT written to disk, because it could not be recorded in the "
            "audit trail first. See runbooks/RUNBOOK_INCIDENT_RESPONSE.md if this persists.",
            file=sys.stderr,
        )
        return 1

    # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "psychotherapy_restore
    # prints the decrypted note to stdout"): see this module's own
    # docstring for the full reasoning. Written to --output instead,
    # matching core/fhir/restore.py's naming convention exactly.
    out_path = output_dir / f"{args.resource_type}_{args.resource_id}.json"
    out_path.write_text(json.dumps(resource, indent=2))
    print(f"Restored to {out_path}", file=sys.stderr)
    print(
        "Remember to delete the local plaintext copy once delivered, per "
        "runbooks/RUNBOOK_DATA_RESTORE.md step 4.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
