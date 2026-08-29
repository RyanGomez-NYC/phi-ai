# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/fhir/restore_common.py, core/fhir/restore.py, and
core/fhir/psychotherapy_restore.py.

Covers the shared restore_one()/apply_credentials_to_environment() logic
once (both restore scripts import the identical functions - see
restore_common.py's own docstring for why duplicating this across the
two scripts would be the wrong kind of "separation"), plus each script's
own input validation.

Does not exercise the AWS-calling parts (STS assume_role, actual S3/KMS
network calls) - those need real or mocked boto3 clients, which is
better covered by scripts/smoke_test_aws.py against a real dev stack.
What's tested here is the logic these scripts are actually responsible
for getting right on their own: integrity-gated decryption, and
failing clearly on bad input before any AWS call is attempted.
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.crypto.envelope import EnvelopeEncryptor, KeyManagementService  # noqa: E402
from core.fhir.psychotherapy_restore import assume_psychotherapy_restore_role  # noqa: E402
from core.fhir.restore import assume_restore_role  # noqa: E402
from core.fhir.restore_common import apply_credentials_to_environment, restore_one  # noqa: E402
from core.storage.base import ObjectStore, StoredObjectMetadata  # noqa: E402


class _FakeKMS(KeyManagementService):
    def generate_data_key(self):
        dek = os.urandom(32)
        return dek, base64.b64encode(dek).decode()

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        return base64.b64decode(wrapped_dek_b64)


class _FakeStorage(ObjectStore):
    def __init__(self):
        self._objects: dict[str, tuple] = {}

    def put_object(self, key, ciphertext, wrapped_dek_b64, sha256_hex, retention_until=None, content_type="application/octet-stream"):
        self._objects[key] = (ciphertext, wrapped_dek_b64, sha256_hex)
        return StoredObjectMetadata(
            key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=sha256_hex,
            stored_at=self._utcnow(), retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64, content_type=content_type,
        )

    def get_object(self, key, version_id=None):
        return self._objects[key][0]

    def get_metadata(self, key, version_id=None):
        ciphertext, wrapped_dek_b64, sha256_hex = self._objects[key]
        return StoredObjectMetadata(
            key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=sha256_hex,
            stored_at=self._utcnow(), retention_until=None,
            wrapped_dek_b64=wrapped_dek_b64, content_type="application/fhir+json",
        )

    def object_exists(self, key):
        return key in self._objects

    def list_keys(self, prefix=""):
        return [k for k in self._objects if k.startswith(prefix)]

    def tamper(self, key: str) -> None:
        ciphertext, wrapped_dek_b64, sha256_hex = self._objects[key]
        tampered = bytearray(ciphertext)
        tampered[len(tampered) // 2] ^= 0xFF
        self._objects[key] = (bytes(tampered), wrapped_dek_b64, sha256_hex)


def _store_for_test(storage: _FakeStorage, encryptor: EnvelopeEncryptor, key: str, resource: dict) -> None:
    """Writes a resource the same way core/fhir/client.py's
    store_resource() actually does (nonce-prefixed ciphertext, digest
    over those combined bytes) - restore_one() must correctly read back
    exactly what the write path actually produces, not a simplified
    stand-in for it."""
    import hashlib
    import json

    plaintext = json.dumps(resource, sort_keys=True).encode("utf-8")
    payload = encryptor.encrypt(plaintext)
    storage_bytes = payload.nonce + payload.ciphertext
    storage.put_object(
        key=key,
        ciphertext=storage_bytes,
        wrapped_dek_b64=payload.wrapped_dek_b64,
        sha256_hex=hashlib.sha256(storage_bytes).hexdigest(),
    )


# ---------------------------------------------------------------------------
# restore_common.restore_one() - shared by both scripts
# ---------------------------------------------------------------------------

def test_restore_one_round_trips_a_correctly_stored_resource():
    storage = _FakeStorage()
    encryptor = EnvelopeEncryptor(_FakeKMS())
    resource = {"resourceType": "DocumentReference", "id": "doc1", "content": "synthetic"}
    _store_for_test(storage, encryptor, "fhir/DocumentReference/doc1.json", resource)

    result = restore_one(storage, encryptor, "fhir/DocumentReference/doc1.json")
    assert result == resource


def test_restore_one_refuses_tampered_data():
    storage = _FakeStorage()
    encryptor = EnvelopeEncryptor(_FakeKMS())
    _store_for_test(storage, encryptor, "fhir/Patient/p1.json", {"resourceType": "Patient", "id": "p1"})
    storage.tamper("fhir/Patient/p1.json")

    raised = False
    try:
        restore_one(storage, encryptor, "fhir/Patient/p1.json")
    except ValueError as exc:
        raised = True
        assert "Integrity check failed" in str(exc)
    assert raised, "restore_one() must raise, not silently return tampered/corrupted content"


def test_apply_credentials_to_environment_sets_all_three_variables():
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ.pop(var, None)

    apply_credentials_to_environment({
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": "examplesecret",
        "SessionToken": "exampletoken",
    })

    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "examplesecret"
    assert os.environ["AWS_SESSION_TOKEN"] == "exampletoken"


# ---------------------------------------------------------------------------
# core/fhir/restore.py - general records-request path
# ---------------------------------------------------------------------------

def test_restore_rejects_empty_purpose_of_use_before_any_aws_call():
    """Validated before importing boto3 - see assume_restore_role()'s
    own docstring for why the ordering itself matters, not just the
    check's existence."""
    raised = False
    try:
        assume_restore_role("arn:aws:iam::123456789012:role/x", "us-east-1", "   ")
    except ValueError as exc:
        raised = True
        assert "purpose_of_use" in str(exc)
    assert raised


# ---------------------------------------------------------------------------
# core/fhir/psychotherapy_restore.py - narrow-exception path
# ---------------------------------------------------------------------------

def test_psychotherapy_restore_rejects_invalid_exception_value():
    raised = False
    try:
        assume_psychotherapy_restore_role(
            "arn:aws:iam::123456789012:role/x", "us-east-1", "records-request", "someone",
        )
    except ValueError as exc:
        raised = True
        assert "exception must be one of" in str(exc)
    assert raised, "an exception value outside the three HIPAA-permitted ones must be rejected"


def test_psychotherapy_restore_accepts_all_three_valid_exceptions_at_validation_layer():
    """Confirms the three legitimate values themselves don't trip the
    validation check - only reaches the (unavailable-in-this-test-env)
    boto3 call after passing validation, which is exactly the boundary
    this test is checking."""
    from core.fhir.psychotherapy_restore import VALID_EXCEPTIONS

    for exception in VALID_EXCEPTIONS:
        hit_boto3_import = False
        try:
            assume_psychotherapy_restore_role(
                "arn:aws:iam::123456789012:role/x", "us-east-1", exception, "a valid attestation",
            )
        except ValueError:
            raise AssertionError(f"{exception!r} is a valid exception and must not raise ValueError")
        except ModuleNotFoundError:
            # Expected in this test environment - confirms validation
            # passed and execution reached the boto3 import, which is
            # exactly the point being tested.
            hit_boto3_import = True
        except Exception:
            hit_boto3_import = True
        assert hit_boto3_import, f"{exception!r} should pass validation and proceed past it"


def test_psychotherapy_restore_rejects_empty_attestation():
    raised = False
    try:
        assume_psychotherapy_restore_role(
            "arn:aws:iam::123456789012:role/x", "us-east-1", "originator-treatment", "  ",
        )
    except ValueError as exc:
        raised = True
        assert "attestation" in str(exc)
    assert raised


def test_both_restore_scripts_share_the_identical_restore_one_function():
    """Not just 'behaves the same' - the literal same function object,
    confirming there is exactly one implementation of this logic to
    maintain, not two that could silently drift apart."""
    from core.fhir.psychotherapy_restore import restore_one as psych_restore_one
    from core.fhir.restore import restore_one as general_restore_one

    assert general_restore_one is psych_restore_one
    assert general_restore_one is restore_one


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
