#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
End-to-end smoke test against a real (dev) Azure deployment.

    set -a && . ./.env && . ./.env.azure && set +a
    .venv/bin/python scripts/smoke_test_azure.py

Sourcing BOTH files in that order is deliberate and is the whole reason
this script does not simply say `. ./.env`. `deploy/azure`'s
`env_fragment` output is a FRAGMENT - eight lines covering the container
names, region, key name, account URL and vault URI - not a complete
configuration. Everything else this test reads (retention_years above
all) lives in the main `.env`. Sourcing `.env` first and `.env.azure`
second layers the Azure cloud settings over an otherwise-complete
config, so the run targets Azure WITHOUT editing `.env` and without
switching the rest of the platform off whatever cloud it currently
points at. See runbooks/RUNBOOK_AZURE_SETUP.md Step 4.

Uses SYNTHETIC patient data only. Never point this at real PHI - it
writes an object under `fhir/Patient/smoketest-*`. Nothing here is
immutable (this stack provisions no immutability policy at all), so
cleanup is an ordinary delete - though blob soft delete will retain the
deleted blob for the account's configured window, 7 days on this stack.

What it proves, in order:
  1. Config loads and Azure AD credentials resolve.
  2. A DEK can be generated and wrapped by Key Vault.
  3. The wrapped DEK records the EXACT key VERSION that wrapped it.
  4. Ciphertext lands in Blob Storage and is genuinely not plaintext.
  5. The declared retain-until was recorded, and nothing was locked.
  6. Round-trip decrypt returns the original resource.
  7. An audit record was written and the chain verifies.

Step 3 has no AWS or GCP counterpart and is not ceremony. Azure Key
Vault's RSA-OAEP unwrap performs NO server-side version resolution: a
wrapped DEK carries no metadata saying which version wrapped it, and a
different version's key material genuinely cannot unwrap it. Before the
2026-08-17 audit's H5 fix, core/crypto/envelope.py's AzureKMS bound one
CryptographyClient to whatever Key Vault reported as latest and reused
it for unwrap, so rotating the key would have permanently broken restore
of every object stored before the rotation. keyvault.tf now configures
90-day automatic rotation, which is safe ONLY because of that fix. This
step asserts the fix is actually present in the deployment being tested,
rather than trusting that the file on disk is the code that ran.

If step 4 or 5 fails, stop - those are the two properties the whole
compliance story rests on.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.config.settings import Settings  # noqa: E402
from core.crypto.envelope import EnvelopeEncryptor  # noqa: E402
from core.storage.factory import build_audit_sink, build_kms, build_storage  # noqa: E402

# Obviously synthetic. No real identifiers.
SYNTHETIC_PATIENT = {
    "resourceType": "Patient",
    "id": "smoketest-azure-0001",
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
    if settings.cloud_provider != "azure":
        print(
            f"  This smoke test targets Azure; PHI_AI_CLOUD_PROVIDER="
            f"{settings.cloud_provider}. Source .env.azure over .env - see this "
            "script's own docstring."
        )
        return 2
    print(f"  account={settings.azure_account_url}")
    print(f"  vault  ={settings.azure_vault_url}")
    print(f"  containers: store={settings.storage_bucket} audit={settings.audit_bucket}")

    storage = build_storage(settings)
    kms = build_kms(settings)
    encryptor = EnvelopeEncryptor(kms)

    step(2, "Generating and wrapping a data encryption key via Key Vault")
    plaintext = json.dumps(SYNTHETIC_PATIENT, sort_keys=True).encode("utf-8")
    payload = encryptor.encrypt(plaintext)
    print(f"  wrapped DEK length: {len(payload.wrapped_dek_b64)} chars")
    print(f"  ciphertext sha256:  {payload.sha256_hex[:32]}...")

    if payload.ciphertext == plaintext:
        print("  FAIL: ciphertext is identical to plaintext")
        return 1
    if b"Testpatient" in payload.ciphertext:
        print("  FAIL: plaintext identifiers leaked into ciphertext")
        return 1
    print("  OK: ciphertext contains no plaintext identifiers")

    step(3, "Verifying the wrapped DEK pins an exact Key Vault key VERSION")
    # AzureKMS.generate_data_key() returns "<versioned key id>|<wrapped b64>".
    # A bare base64 blob with no "|" is a PRE-FIX wrapped DEK: it records no
    # version, so a future rotation leaves it unrecoverable. That is exactly
    # the H5 condition, and on a stack with rotation_policy configured it is
    # a live hazard rather than a historical note.
    key_id, sep, _ = payload.wrapped_dek_b64.rpartition("|")
    if not sep or not key_id:
        print("  FAIL: wrapped DEK records no key version (pre-H5-fix format).")
        print("        keyvault.tf configures 90-day rotation; with DEKs in this")
        print("        format, the first rotation permanently breaks restore of")
        print("        everything stored before it. Do not ingest against this build.")
        return 1
    # https://<vault>.vault.azure.net/keys/<name>/<32-hex-version>
    parts = key_id.rstrip("/").split("/")
    if len(parts) < 3 or parts[-3] != "keys" or not parts[-1]:
        print(f"  FAIL: recorded key id is not a versioned Key Vault key URL: {key_id}")
        return 1
    print(f"  key name:    {parts[-2]}")
    print(f"  key version: {parts[-1]}")
    print("  OK: unwrap is bound to this exact version, so rotation is safe")

    step(4, "Writing encrypted object to Blob Storage")
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
    print(f"  {settings.azure_account_url}{settings.storage_bucket}/{key}")
    print(f"  version: {stored.version_id}")

    step(5, "Verifying stored bytes are not readable plaintext")
    raw = storage.get_object(key)
    if b"Testpatient" in raw:
        print("  FAIL: PLAINTEXT PHI FOUND IN STORAGE - STOP")
        return 1
    print("  OK: stored object is opaque ciphertext")

    step(6, "Verifying retain-until was recorded (and that nothing was locked)")
    meta = storage.get_metadata(key)
    if meta.retention_until is None:
        print("  FAIL: no retain-until recorded in blob metadata")
        return 1
    print(f"  retain_until={meta.retention_until.isoformat()} (recorded, NOT enforced)")

    # The direct-SDK check below mirrors smoke_test_aws.py reaching for boto3
    # to read Object Lock state: the ObjectStore interface deliberately does
    # not expose immutability, because this stack sets none, so proving the
    # absence means asking Azure rather than asking our own abstraction.
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    blob = BlobServiceClient(
        account_url=settings.azure_account_url, credential=DefaultAzureCredential()
    ).get_blob_client(container=settings.storage_bucket, blob=key)
    props = blob.get_blob_properties()
    policy = getattr(props, "immutability_policy", None)
    expiry = getattr(policy, "expiry_time", None) if policy else None
    if expiry or getattr(props, "has_legal_hold", False):
        print(
            f"  FAIL: an immutability policy or legal hold is active "
            f"(expiry={expiry}, legal_hold={getattr(props, 'has_legal_hold', False)}). "
            "This stack is not supposed to lock anything - see deploy/azure/README.md. "
            "If its state is Locked the container must be replaced; it cannot be undone."
        )
        return 1
    print("  OK: no immutability policy and no legal hold, as expected")

    step(7, "Round-trip decrypt")
    nonce, ciphertext = raw[:12], raw[12:]
    recovered = encryptor.decrypt(ciphertext, nonce, payload.wrapped_dek_b64)
    if json.loads(recovered) != SYNTHETIC_PATIENT:
        print("  FAIL: round-trip mismatch")
        return 1
    print("  OK: decrypted resource matches original exactly")

    step(8, "Writing and verifying an audit record")
    # build_audit_sink() rather than a hardcoded sink class - the same
    # provider-dispatch both schedulers were fixed to use. Constructing
    # AzureBlobAuditSink directly here would test a class this deployment's
    # own code path might not even select.
    sink = build_audit_sink(settings)
    audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())
    # "record.write" is what core/fhir/client.py's store_resource() actually
    # records for this operation - matched to the real client, not invented,
    # so an operator grepping a freshly-verified stack for record.write finds
    # this run rather than nothing.
    event = audit.record(
        actor="smoke-test",
        action="record.write",
        resource_key=key,
        purpose_of_use="deployment_verification",
    )
    print(f"  event hash: {event.event_hash[:32]}...")

    events = sink.read_all()
    if not AuditLog.verify_chain(events):
        print("  FAIL: audit chain does not verify")
        return 1
    print(f"  OK: audit chain intact across {len(events)} events")

    print("\n" + "=" * 60)
    print("SMOKE TEST PASSED")
    print("=" * 60)
    print("\nWhat this did NOT prove: role separation. Everything above ran as")
    print("your own az login identity, which holds every grant at once. Azure")
    print("managed identities cannot be assumed from a laptop the way AWS IAM")
    print("roles can, so the ingest/restore/auditor boundary is only genuinely")
    print("exercised once each identity is attached to its own compute - see")
    print("runbooks/RUNBOOK_AZURE_SETUP.md Step 12 and its Known gaps item 1.")
    print("\nThe object written above carries a recorded retain-until but no")
    print("immutability policy - step 6 fails the run if one is found - so it")
    print("deletes normally. Clean it up: it is synthetic, but it sits in the")
    print("same container real records would.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
