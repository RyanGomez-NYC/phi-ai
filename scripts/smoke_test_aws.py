#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
End-to-end smoke test against a real (dev) AWS deployment.

    python scripts/smoke_test_aws.py

Uses SYNTHETIC patient data only. Never point this at real PHI - it writes
objects under a `fhir/Patient/smoketest-*` prefix. These objects are NOT
locked (this stack provisions no Object Lock), so cleaning them up
afterwards is an ordinary delete.

What it proves, in order:
  1. Config loads and AWS credentials resolve.
  2. A DEK can be generated and wrapped by KMS.
  3. Ciphertext lands in S3 and is genuinely not plaintext.
  4. The declared retain-until was recorded, and no lock was applied.
  5. Round-trip decrypt returns the original resource.
  6. An audit record was written and the chain verifies.

If step 3 or 4 fails, stop - those are the two properties the whole
compliance story rests on.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.audit.sink import S3AuditSink  # noqa: E402
from core.config.settings import Settings  # noqa: E402
from core.crypto.envelope import EnvelopeEncryptor  # noqa: E402
from core.storage.factory import build_kms, build_storage  # noqa: E402

# Obviously synthetic. No real identifiers.
SYNTHETIC_PATIENT = {
    "resourceType": "Patient",
    "id": "smoketest-0001",
    "name": [{"family": "Testpatient", "given": ["Synthetic"]}],
    "gender": "unknown",
    "birthDate": "1970-01-01",
    "meta": {"tag": [{"code": "SYNTHETIC", "display": "Test data - not real PHI"}]},
}


def step(n: int, msg: str) -> None:
    print(f"\n[{n}] {msg}")


def main() -> int:
    step(1, "Loading configuration")
    settings = Settings.from_env()
    if settings.cloud_provider != "aws":
        print(f"  This smoke test targets AWS; PHI_AI_CLOUD_PROVIDER={settings.cloud_provider}")
        return 2
    print(f"  bucket={settings.storage_bucket} region={settings.storage_region}")

    storage = build_storage(settings)
    kms = build_kms(settings)
    encryptor = EnvelopeEncryptor(kms)

    step(2, "Generating and wrapping a data encryption key via KMS")
    plaintext = json.dumps(SYNTHETIC_PATIENT, sort_keys=True).encode("utf-8")
    payload = encryptor.encrypt(plaintext)
    print(f"  wrapped DEK length: {len(payload.wrapped_dek_b64)} chars")
    print(f"  ciphertext sha256:  {payload.sha256_hex[:32]}...")

    assert payload.ciphertext != plaintext, "ciphertext must differ from plaintext"
    assert b"Testpatient" not in payload.ciphertext, "plaintext leaked into ciphertext"
    print("  OK: ciphertext contains no plaintext identifiers")

    step(3, "Writing encrypted object to S3")
    key = f"fhir/Patient/{SYNTHETIC_PATIENT['id']}.json"
    retain_until = datetime.now(timezone.utc) + timedelta(days=365 * settings.retention_years)

    stored = storage.put_object(
        key=key,
        ciphertext=payload.nonce + payload.ciphertext,
        wrapped_dek_b64=payload.wrapped_dek_b64,
        sha256_hex=payload.sha256_hex,
        retention_until=retain_until,
        content_type="application/fhir+json",
    )
    print(f"  s3://{settings.storage_bucket}/{key}")
    print(f"  version: {stored.version_id}")

    step(4, "Verifying stored bytes are not readable plaintext")
    raw = storage.get_object(key)
    assert b"Testpatient" not in raw, "PLAINTEXT PHI FOUND IN STORAGE - STOP"
    print("  OK: stored object is opaque ciphertext")

    step(5, "Verifying the declared retain-until was recorded (and nothing was locked)")
    meta = storage.get_metadata(key)
    if meta.retention_until is None:
        print("  FAIL: no retain-until recorded in object metadata")
        return 1
    print(f"  retain_until={meta.retention_until.isoformat()} (recorded, NOT enforced)")

    import boto3

    s3 = boto3.client("s3", region_name=settings.storage_region)
    try:
        ret = s3.get_object_retention(Bucket=settings.storage_bucket, Key=key)["Retention"]
        print(
            f"  FAIL: an Object Lock retention is active ({ret['Mode']} until "
            f"{ret['RetainUntilDate']}). This stack is not supposed to lock anything - "
            "the bucket predates that change and must be replaced.",
        )
        return 1
    except Exception:
        print("  OK: no Object Lock retention on the object, as expected")

    step(6, "Round-trip decrypt")
    nonce, ciphertext = raw[:12], raw[12:]
    recovered = encryptor.decrypt(ciphertext, nonce, stored.wrapped_dek_b64)
    assert json.loads(recovered) == SYNTHETIC_PATIENT, "round-trip mismatch"
    print("  OK: decrypted resource matches original exactly")

    step(7, "Writing and verifying an audit record")
    sink = S3AuditSink(
        bucket=settings.audit_bucket,
        region=settings.storage_region,
        kms_key_id=settings.audit_kms_key_id,
        retention_days=1,
    )
    audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())
    # Must match what core/fhir/client.py's store_resource() actually
    # records for the equivalent operation, which is "record.write"
    # (confirmed against the client, not assumed). A smoke test that
    # emits some other action still passes - the chain accepts any
    # action - but it would only prove the audit path accepts AN event
    # rather than the event this platform writes, and an operator
    # grepping a freshly-verified dev stack for record.write would find
    # nothing.
    event = audit.record(
        actor="smoke-test",
        action="record.write",
        resource_key=key,
        purpose_of_use="deployment_verification",
    )
    print(f"  event hash: {event.event_hash[:32]}...")

    events = sink.read_all()
    if AuditLog.verify_chain(events):
        print(f"  OK: audit chain intact across {len(events)} events")
    else:
        print("  FAIL: audit chain does not verify")
        return 1

    print("\n" + "=" * 60)
    print("SMOKE TEST PASSED")
    print("=" * 60)
    print("\nReminder: the objects written above carry a recorded retain-until")
    print("but no Object Lock - step 5 fails the run if a lock is found - so")
    print("they can be deleted normally. Clean them up: they are synthetic,")
    print("but they sit in the same bucket as real records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
