# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Shared logic between core/fhir/restore.py (general records requests) and
core/fhir/psychotherapy_restore.py (the narrow-exception path for
psychotherapy notes).

WHAT'S SHARED HERE, AND WHY THAT'S SAFE: only the purely mechanical
fetch-verify-decrypt logic and STS-credential plumbing - nothing here
knows or cares which bucket, key, or IAM role a caller is using. The
actual separation this project relies on for psychotherapy notes (see
runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md) is about storage location and
access control - a different S3 bucket, KMS key, and IAM role, with each
restore script's caller-provided storage/encryptor objects bound to its
own - not about whether the mechanical "given a storage and an
encryptor, safely produce plaintext" routine is typed out twice.
Duplicating that logic would only reintroduce the exact class of risk
this module exists to avoid: two copies of security-critical logic that
can silently drift out of sync if one is ever fixed and the other is
forgotten. See core/fhir/client.py's _stored_sha256_hex() docstring for
a real example of exactly that kind of drift, found and fixed in the
same cleanup pass that produced this module.
"""

from __future__ import annotations

import json
import os


def apply_credentials_to_environment(creds: dict) -> None:
    """
    Sets assumed-role temporary credentials as process environment
    variables, so every downstream boto3.client() call in this process -
    inside core.storage.aws_s3.S3Storage, core.crypto.envelope.AWSKMS,
    and (for core/fhir/restore.py specifically) core.db.connection's RDS
    IAM token generation - picks them up via boto3's standard credential
    chain, without any of those existing, already-tested modules needing
    to accept an explicit session parameter. Reusing them exactly as
    they are, rather than reimplementing S3/KMS/RDS-auth logic inside a
    restore script a second time, is deliberate.

    Scoped to this process only. Both callers of this function are
    short-lived, single-purpose CLI scripts that exist for exactly one
    restore operation before exiting - not something that would leak
    into a longer-running service's credential state.
    """
    os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["SessionToken"]


def restore_one(storage, encryptor, key: str) -> dict:
    """
    Fetch, integrity-check, and decrypt a single stored object.

    Raises ValueError on integrity failure - callers must not catch this
    and continue past it. Both restore scripts, and
    runbooks/RUNBOOK_DATA_RESTORE.md's step 3, depend on that: a broken
    integrity check is a possible-tampering finding to escalate, not
    something to skip past and keep going.
    """
    if not storage.verify_integrity(key):
        raise ValueError(
            f"Integrity check failed for {key}. This indicates possible tampering or "
            "corruption - do not proceed. Escalate per runbooks/RUNBOOK_INCIDENT_RESPONSE.md."
        )

    raw = storage.get_object(key)
    meta = storage.get_metadata(key)
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = encryptor.decrypt(ciphertext, nonce, meta.wrapped_dek_b64)
    return json.loads(plaintext)
# Made by Ryan Gomez & Co. Inc.
