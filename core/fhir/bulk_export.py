# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Exports everything stored - every resource in the object store
container/bucket - to portable NDJSON files, one per FHIR resource type,
matching the same format Epic's own Bulk Data Export produces (see
core/fhir/bulk_client.py and docs/EMR_CONNECTORS.md). Built specifically
so this project never becomes a one-way door: whatever this stack
stores, a deployer can always get every bit of it back out, in a
format any other FHIR-aware system already knows how to read, without
needing this codebase at all on the receiving end.

This is a READ operation. It uses the exact same `restore` IAM role
core/fhir/restore.py already uses (read + decrypt, no write, no
delete), because reading in bulk requires nothing that reading one
record at a time didn't already require - see runbooks/RUNBOOK_DATA_RESTORE.md
and deploy/aws/iam.tf for what that role can and cannot do.

CORRECTED CLAIM: this docstring used to say "Nothing about immutability
changes because of this tool; an immutable object can be read as many
times as needed." The conclusion was right but the premise was false,
which made it a load-bearing false assurance rather than a harmless
aside. Stored objects are NOT immutable on any cloud this project
supports: no bucket in deploy/aws is created with S3 Object Lock
(see deploy/aws/s3_store.tf), deploy/gcp sets neither
`enable_object_retention` nor a bucket `retention_policy` nor Bucket
Lock, and deploy/azure provisions no container immutability policy and
has no version-level WORM. Retention is RECORDED as object/blob metadata
by application code and enforced by nothing. The narrower statement that
is actually true is enough for this tool's purposes: this is a pure read
path, so it grants no ability to delete or modify anything that did not
already exist elsewhere in the stack, and it neither depends on nor
weakens any storage-layer protection. This is deliberately unrelated to
core/fhir/purge.py, which is the tool for actually removing anything -
this one never deletes, modifies, or even touches the objects it reads.

Unlike core/fhir/restore.py, this does NOT use the Postgres index to
find what to export - it lists directly from storage
(ObjectStore.list_keys()), which is the system of record, so this
tool works correctly even in a deployment with no Postgres index
configured, and can never under-export due to index drift (see
core/db/reconcile.py for what that drift looks like and why S3/Blob
storage, never the index, is authoritative).

    python -m core.fhir.bulk_export \\
        --purpose-of-use "Entity migration to Example Health Partners, asset purchase agreement dated 2026-08-20" \\
        --role-arn arn:aws:iam::123456789012:role/phi-ai-dev-restore \\
        --output ./full-export/

--resource-type is optional - omit it to export every resource type
stored. Bucket, region, and KMS key come from the same PHI_AI_*
environment every other tool in this project uses - not a separate
flag, so this can never point at a different deployment than what's
actually configured.

Every exported object is individually audit-logged
(action="record.export") - the same per-object granularity
core/fhir/restore.py already uses, deliberately not collapsed into one
summary event for the whole run, so a later audit-trail review can see
exactly which resource keys a bulk export actually touched, the same way
it can for an individual records request. See
runbooks/RUNBOOK_INCIDENT_RESPONSE.md's own reliance on that
granularity. Recorded BEFORE the resource is written to its .ndjson file
(fixed 2026-08-17 audit, MEDIUM, "bulk_export.py writes plaintext to disk
before recording the audit event") - see the ordering comment in the main
loop below.

Each run exports EVERYTHING from scratch every time (per
"Unlike core/fhir/restore.py, this does NOT use the Postgres index"
above) - there is no incremental/resume state. FOUND AND FIXED
(2026-08-17 audit, MEDIUM, "bulk_export appends into existing files on
re-run"): the per-type .ndjson files used to be opened in append mode,
so re-running this tool against an --output directory from a prior run
silently duplicated every previously-exported resource into the same
growing file, forever, on every re-run - with no error, no warning, and
no way to tell from the file itself that it happened. Each run now
truncates its own .ndjson files, matching what "exports everything"
actually means: one full, self-contained snapshot per --output directory
per invocation, not an accumulating log.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from core.fhir.restore import assume_restore_role
from core.fhir.restore_common import apply_credentials_to_environment, restore_one


def _resource_type_from_key(key: str) -> str:
    """
    Storage keys are laid out as fhir/{ResourceType}/{resource_id}.json
    (see core/fhir/client.py) - this pulls the resource type back out
    without needing to have decrypted the object first, so progress
    output and per-type NDJSON file selection can happen before
    decryption completes.
    """
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "fhir":
        return parts[1]
    return "Unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export everything stored to portable NDJSON files, one per FHIR resource type."
    )
    parser.add_argument(
        "--resource-type",
        default=None,
        help="Restrict to one FHIR resource type. Omit to export every resource type stored.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an interrupted export instead of starting over. Skips objects "
             "already recorded in the output directory's .export-manifest and appends "
             "to the existing .ndjson files.",
    )
    parser.add_argument(
        "--purpose-of-use",
        required=True,
        help=(
            "Specific, documented reason for this export - becomes a required AWS session tag "
            "and is recorded in the audit log for every object exported, the same as "
            "core/fhir/restore.py's --purpose-of-use. For a migration, name the destination and "
            "the underlying business reason, e.g. 'Entity migration to Example Health Partners, "
            "asset purchase agreement dated 2026-08-20' - see runbooks/RUNBOOK_DATA_RESTORE.md."
        ),
    )
    parser.add_argument("--role-arn", required=True, help="ARN of the restore IAM role.")
    parser.add_argument(
        "--output", required=True, help="Directory to write exported NDJSON files into, one per resource type."
    )
    args = parser.parse_args()

    from core.config.settings import Settings

    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"This tool currently supports AWS only; got {settings.cloud_provider}.", file=sys.stderr)
        return 2

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Assuming {args.role_arn} with PurposeOfUse={args.purpose_of_use!r}...", file=sys.stderr)
    creds = assume_restore_role(args.role_arn, settings.storage_region, args.purpose_of_use, session_name="bulk-export")
    apply_credentials_to_environment(creds)

    # Imported only after credentials are in place, so these modules'
    # internal boto3.client() calls pick up the assumed-role session.
    from core.audit.log import AuditLog
    from core.audit.sink import S3AuditSink
    from core.crypto.envelope import AWSKMS, EnvelopeEncryptor
    from core.storage.aws_s3 import S3Storage

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

    # --- resume state ------------------------------------------------
    #
    # A full export of a large deployment runs for hours or days. Without
    # resume, a failure at 90% - a credential expiring, a network blip,
    # an operator's terminal closing - throws away every hour of it, and
    # the natural response is to stop attempting full exports at all.
    #
    # The manifest records which keys have been written. On restart,
    # already-exported keys are skipped and the .ndjson files are OPENED
    # FOR APPEND rather than truncated. That append is safe here and
    # nowhere else in this tool precisely because the manifest says what
    # is already in them - which is what the earlier append-mode bug
    # lacked, and why that bug silently doubled files on every re-run.
    manifest_path = output_dir / ".export-manifest"
    done_keys: set[str] = set()
    resuming = False

    if args.resume and manifest_path.is_file():
        with manifest_path.open("r", encoding="utf-8") as handle:
            done_keys = {line.strip() for line in handle if line.strip()}
        resuming = bool(done_keys)
        if resuming:
            print(f"Resuming: {len(done_keys):,} object(s) already exported.",
                  file=sys.stderr)
    elif manifest_path.is_file():
        # A manifest present without --resume means a previous run was
        # interrupted. Truncating now would discard a partial export the
        # operator may want; saying so is cheaper than silently redoing
        # hours of work or silently appending to it.
        print(
            f"WARNING: {manifest_path} exists, so a previous export was interrupted. "
            "This run will start from scratch and overwrite it. Re-run with --resume to "
            "continue that export instead.",
            file=sys.stderr,
        )
        manifest_path.unlink()

    print("Listing stored contents...", file=sys.stderr)
    keys = list(storage.iter_keys(prefix="fhir/"))

    if args.resource_type:
        keys = [k for k in keys if _resource_type_from_key(k) == args.resource_type]

    if not keys:
        suffix = f" of type {args.resource_type}" if args.resource_type else ""
        print(f"Nothing stored{suffix}. Nothing to export.", file=sys.stderr)
        return 0

    remaining = [k for k in keys if k not in done_keys]
    if resuming and not remaining:
        print(f"All {len(keys):,} object(s) already exported. Nothing to do.",
              file=sys.stderr)
        return 0

    print(f"Found {len(keys):,} object(s); {len(remaining):,} to export.", file=sys.stderr)

    # One open file handle per resource type, written to as matching
    # objects are decrypted - avoids holding the entire export in memory
    # at once, which matters once a deployment reaches real production
    # scale (tens or hundreds of thousands of resources).
    #
    # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "bulk_export appends
    # into existing files on re-run"): this open() call used mode "a"
    # (append). Every invocation of this tool re-lists and re-exports
    # EVERYTHING from scratch (see this module's own docstring -
    # there is no incremental/watermark state, unlike scheduler.py) -
    # so append mode meant a second run against the same --output
    # directory silently doubled every .ndjson file's contents, a third
    # run tripled it, and so on, with nothing in the tool's own output
    # to reveal it happened. Mode "w" (truncate) matches what this tool
    # actually promises: one complete, self-contained export per
    # invocation.
    open_files: dict[str, TextIO] = {}
    counts: dict[str, int] = defaultdict(int)

    def _file_for(resource_type: str):
        if resource_type not in open_files:
            # Append when resuming (the manifest records what is already
            # in the file); truncate otherwise, which is what a fresh
            # export promises.
            mode = "a" if resuming else "w"
            open_files[resource_type] = open(
                output_dir / f"{resource_type}.ndjson", mode, encoding="utf-8")
        return open_files[resource_type]

    exported = 0
    manifest = manifest_path.open("a", encoding="utf-8")
    try:
        for key in remaining:
            resource_type = _resource_type_from_key(key)
            print(f"  {key} ...", file=sys.stderr)

            try:
                resource = restore_one(storage, encryptor, key)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                print(
                    f"Stopping - {exported} resource(s) were exported successfully before this "
                    "failure; do not treat those as suspect, but do not continue past this one. "
                    "See runbooks/RUNBOOK_INCIDENT_RESPONSE.md.",
                    file=sys.stderr,
                )
                return 1

            # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "bulk_export.py
            # writes plaintext to disk before recording the audit
            # event"): the write below used to happen BEFORE
            # audit.record() - the identical bug and identical fix as
            # core/fhir/restore.py's main loop; see that file's own
            # comment on this same reordering for the full reasoning
            # (core/fhir/purge.py's audit-before-delete precedent, and
            # why "audit succeeds, write fails" is the safe failure
            # direction and "write succeeds, audit fails" is not).
            try:
                audit.record(
                    actor="phi-ai-bulk-export-cli",
                    action="record.export",
                    resource_key=key,
                    purpose_of_use=args.purpose_of_use,
                )
            except Exception as exc:
                print(f"Failed to record the audit event for {key}: {exc}", file=sys.stderr)
                print(
                    f"Stopping - {exported} resource(s) were exported successfully before this "
                    "failure. This resource was NOT written to its .ndjson file, because it "
                    "could not be recorded in the audit trail first. See "
                    "runbooks/RUNBOOK_INCIDENT_RESPONSE.md if this persists.",
                    file=sys.stderr,
                )
                return 1

            _file_for(resource_type).write(json.dumps(resource, sort_keys=True) + "\n")
            counts[resource_type] += 1
            exported += 1
            # Written and flushed per object: a process killed mid-export
            # should lose at most the object in flight, not the batch.
            manifest.write(key + "\n")
            manifest.flush()
    finally:
        for f in open_files.values():
            f.close()

    print(f"\nExported {exported} resource(s) to {output_dir}/:", file=sys.stderr)
    for resource_type, count in sorted(counts.items()):
        print(f"  {resource_type}.ndjson: {count}", file=sys.stderr)
    print(
        "\nEach .ndjson file is one JSON resource per line, matching Epic's own Bulk Data Export "
        "format - portable to any FHIR-aware system, not just this codebase. Handle the output "
        "with the same care as any other decrypted PHI: it is plaintext on local disk the moment "
        "this command finishes.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
