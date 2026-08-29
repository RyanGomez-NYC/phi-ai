# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/fhir/client.py.

The first group of tests here exists specifically to guard against a
real bug found during a cleanup pass: store_resource() and
store_psychotherapy_resource() were recording a SHA-256 digest of the
raw AES-GCM ciphertext alone (EnvelopeEncryptor.encrypt()'s
EncryptedPayload.sha256_hex), while actually storing a DIFFERENT byte
sequence - the nonce prefixed onto that ciphertext. ObjectStore.
verify_integrity() (core/storage/base.py) recomputes a digest over
whatever get_object() returns and compares it to the recorded one - so
this mismatch meant every single stored object would fail integrity
verification, including completely untampered ones. Not a rare edge
case: a systemic false positive on every object ever written through
this path. See core/fhir/client.py's _stored_sha256_hex() docstring for
the full account.

Uses a lightweight fake ObjectStore and a lightweight fake KMS (not
mocks of the real AWS classes) so these tests exercise real AES-256-GCM
encrypt/decrypt via the `cryptography` library and real SHA-256
computation - not a simulation of either. The one thing genuinely faked
is the network call a real KMS would make; wrapping/unwrapping a DEK
locally via base64 is a completely faithful stand-in for that, since
core.crypto.envelope.EnvelopeEncryptor never assumes anything about how
its KMS dependency implements wrap/unwrap beyond the two-method
KeyManagementService protocol.
"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.crypto.envelope import EnvelopeEncryptor, KeyManagementService  # noqa: E402
from core.fhir.client import FHIRIngestionClient  # noqa: E402
from core.fhir.emr_profiles import EPIC  # noqa: E402
from core.storage.base import ObjectStore, StoredObjectMetadata  # noqa: E402


class _FakeKMS(KeyManagementService):
    """Wraps by base64-encoding the raw DEK; unwrap is the exact
    inverse. No real KMS call, but EnvelopeEncryptor only depends on the
    two-method protocol, so this exercises its actual AES-GCM logic
    faithfully."""

    def generate_data_key(self):
        dek = os.urandom(32)
        return dek, base64.b64encode(dek).decode()

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        return base64.b64decode(wrapped_dek_b64)


class _FakeStorage(ObjectStore):
    """In-memory ObjectStore. put_object stores the exact bytes and
    exact sha256_hex string it's given, with no recomputation - matching
    core/storage/aws_s3.py's real S3Storage precisely, which just passes
    sha256_hex through into S3 object metadata verbatim. This fidelity
    matters: a fake that "helpfully" recomputed the hash itself would
    hide the exact bug these tests exist to catch."""

    def __init__(self):
        self._objects: dict[str, tuple] = {}

    def put_object(self, key, ciphertext, wrapped_dek_b64, sha256_hex, retention_until=None, content_type="application/octet-stream"):
        self._objects[key] = (ciphertext, wrapped_dek_b64, sha256_hex, retention_until, content_type)
        return StoredObjectMetadata(
            key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=sha256_hex,
            stored_at=self._utcnow(), retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64, content_type=content_type,
        )

    def get_object(self, key, version_id=None):
        return self._objects[key][0]

    def get_metadata(self, key, version_id=None):
        ciphertext, wrapped_dek_b64, sha256_hex, retention_until, content_type = self._objects[key]
        return StoredObjectMetadata(
            key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=sha256_hex,
            stored_at=self._utcnow(), retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64, content_type=content_type,
        )

    def object_exists(self, key):
        return key in self._objects

    def list_keys(self, prefix=""):
        return [k for k in self._objects if k.startswith(prefix)]

    def tamper(self, key: str) -> None:
        """Test helper: flip one bit in a stored object, to prove
        verify_integrity() still catches real corruption after the fix -
        the fix must not have replaced a false positive with a check
        that never fails at all."""
        ciphertext, wrapped_dek_b64, sha256_hex, retention_until, content_type = self._objects[key]
        tampered = bytearray(ciphertext)
        tampered[len(tampered) // 2] ^= 0xFF
        self._objects[key] = (bytes(tampered), wrapped_dek_b64, sha256_hex, retention_until, content_type)


class _FakeAudit(AuditLog):
    def __init__(self):
        self.records: list[tuple] = []

    def record(self, actor, action, resource_key, purpose_of_use):
        self.records.append((actor, action, resource_key, purpose_of_use))


def _make_client(**overrides) -> tuple[FHIRIngestionClient, _FakeStorage]:
    storage = overrides.pop("storage", None) or _FakeStorage()
    kwargs = dict(
        base_url="https://example.com",
        profile=EPIC,
        storage=storage,
        encryptor=EnvelopeEncryptor(_FakeKMS()),
        audit=_FakeAudit(),
        retention_years=10,
    )
    kwargs.update(overrides)
    return FHIRIngestionClient(**kwargs), storage


# ---------------------------------------------------------------------------
# The integrity fix - see module docstring.
# ---------------------------------------------------------------------------

def test_stored_object_passes_its_own_integrity_check():
    client, storage = _make_client()
    result = client.store_resource({"resourceType": "Patient", "id": "p1"})

    assert storage.verify_integrity(result.storage_key) is True


def test_stored_digest_matches_what_get_object_actually_returns():
    """The specific property that was broken: sha256(get_object(key))
    must equal get_metadata(key).sha256_hex. Checked directly, not just
    through verify_integrity(), so a future change to verify_integrity()
    itself can't accidentally mask a regression here."""
    client, storage = _make_client()
    result = client.store_resource({"resourceType": "Patient", "id": "p2"})

    raw = storage.get_object(result.storage_key)
    meta = storage.get_metadata(result.storage_key)
    assert hashlib.sha256(raw).hexdigest() == meta.sha256_hex


def test_integrity_check_still_detects_real_tampering():
    """The fix must produce a real check, not one that trivially always
    passes. If this test ever fails because verify_integrity() returns
    True for tampered data, that is at least as serious as the original
    bug this file guards against."""
    client, storage = _make_client()
    result = client.store_resource({"resourceType": "Patient", "id": "p3"})

    storage.tamper(result.storage_key)

    assert storage.verify_integrity(result.storage_key) is False


def test_decrypt_round_trip_still_works_after_the_fix():
    client, storage = _make_client()
    resource = {"resourceType": "Patient", "id": "p4", "name": [{"family": "Testworth"}]}
    result = client.store_resource(resource)

    raw = storage.get_object(result.storage_key)
    nonce, ciphertext = raw[:12], raw[12:]
    meta = storage.get_metadata(result.storage_key)
    decrypted = client.encryptor.decrypt(ciphertext, nonce, meta.wrapped_dek_b64)

    assert json.loads(decrypted) == resource


def test_store_result_sha256_hex_matches_stored_metadata():
    client, storage = _make_client()
    result = client.store_resource({"resourceType": "Patient", "id": "p5"})

    meta = storage.get_metadata(result.storage_key)
    assert result.sha256_hex == meta.sha256_hex


# ---------------------------------------------------------------------------
# Psychotherapy notes separation (core/fhir/client.py's
# store_psychotherapy_resource()) - see
# runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for why these properties exist.
# ---------------------------------------------------------------------------

def test_psychotherapy_resource_raises_without_configuration():
    """No fallback to the general store - misconfiguration must fail
    loudly, not silently degrade into writing psychotherapy content
    somewhere it doesn't belong."""
    client, general_storage = _make_client()

    raised = False
    try:
        client.store_psychotherapy_resource({"resourceType": "DocumentReference", "id": "n1"})
    except RuntimeError as exc:
        raised = True
        assert "no fallback" in str(exc).lower()
    assert raised, "store_psychotherapy_resource() must raise when unconfigured"
    assert general_storage.list_keys() == [], "general storage must remain completely untouched"


def test_psychotherapy_resource_never_touches_general_bucket():
    general_storage = _FakeStorage()
    psych_storage = _FakeStorage()
    client, _ = _make_client(
        storage=general_storage,
        psychotherapy_storage=psych_storage,
        psychotherapy_encryptor=EnvelopeEncryptor(_FakeKMS()),
    )

    client.store_psychotherapy_resource({"resourceType": "DocumentReference", "id": "n2"})

    assert psych_storage.list_keys() == ["notes/DocumentReference/n2.json"]
    assert general_storage.list_keys() == []


def test_psychotherapy_resource_also_passes_its_own_integrity_check():
    """The same fix applies to both write paths - this is the
    regression test for store_psychotherapy_resource() specifically,
    not just store_resource()."""
    psych_storage = _FakeStorage()
    client, _ = _make_client(
        psychotherapy_storage=psych_storage,
        psychotherapy_encryptor=EnvelopeEncryptor(_FakeKMS()),
    )

    result = client.store_psychotherapy_resource({"resourceType": "DocumentReference", "id": "n3"})

    assert psych_storage.verify_integrity(result.storage_key) is True


def test_psychotherapy_audit_action_is_distinct():
    psych_storage = _FakeStorage()
    client, _ = _make_client(
        psychotherapy_storage=psych_storage,
        psychotherapy_encryptor=EnvelopeEncryptor(_FakeKMS()),
    )

    client.store_psychotherapy_resource({"resourceType": "DocumentReference", "id": "n4"})

    assert client.audit.records[-1][1] == "record.write.psychotherapy"


def test_index_writer_never_fires_for_psychotherapy_notes():
    """Even structural metadata - that a note exists for a patient - is
    treated as sensitive; the general Postgres index must never learn
    about psychotherapy notes, even indirectly."""
    calls = []
    psych_storage = _FakeStorage()
    client, _ = _make_client(
        psychotherapy_storage=psych_storage,
        psychotherapy_encryptor=EnvelopeEncryptor(_FakeKMS()),
        index_writer=lambda result, resource: calls.append((result, resource)),
    )

    client.store_psychotherapy_resource({"resourceType": "DocumentReference", "id": "n5"})
    assert calls == [], "index_writer must never be called for a psychotherapy note"

    # The SAME client, SAME index_writer, used for a regular resource -
    # confirms the exclusion is specific to the psychotherapy path, not
    # an accident of index_writer being broken generally.
    client.store_resource({"resourceType": "Patient", "id": "p6"})
    assert len(calls) == 1, "index_writer SHOULD fire for a regular stored resource"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()


# ---------------------------------------------------------------------------
# Record scope: profile <-> mock-server parity
#
# docs/DATA_SCOPE_REVIEW.md maps this system's scope against CMS's
# hospital Conditions of Participation content list (42 CFR 482.24(c)(4)).
# Closing the gaps it identified meant adding resource types in two places
# at once - EPIC.supported_resources and the mock server's RESOURCES_BY_TYPE
# - and nothing structurally prevents the next addition from landing in only
# one of them. A type in the profile but not the mock is untestable; a type
# in the mock but not the profile is dead data the client will never request.
# ---------------------------------------------------------------------------

def _mock_server_module():
    """Load scripts/mock_epic_server.py by path - it's a script, not an
    importable package member, and importing it must not start a server."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "mock_epic_server.py"
    spec = importlib.util.spec_from_file_location("mock_epic_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_supported_resource_type_has_mock_data():
    mock = _mock_server_module()
    missing = [t for t in EPIC.supported_resources if t not in mock.RESOURCES_BY_TYPE]
    assert not missing, (
        f"supported_resources lists {missing} with no synthetic data in "
        "scripts/mock_epic_server.py - the pipeline cannot be exercised for those types"
    )


def test_mock_data_has_no_resource_types_the_profile_never_requests():
    mock = _mock_server_module()
    extra = [t for t in mock.RESOURCES_BY_TYPE if t not in EPIC.supported_resources]
    assert not extra, (
        f"mock server carries synthetic {extra} data that EPIC.supported_resources "
        "does not list, so the client will never request it"
    )


def test_every_synthetic_resource_resolves_to_a_real_patient():
    """The Postgres index links stored objects to patients solely via
    extract_patient_reference(). It reads `subject` and `patient` and
    nothing else, so a resource type that carries its patient link under a
    different field indexes with a NULL reference and silently drops out
    of restore-by-patient - exactly the failure a records request would
    surface at the worst moment.

    Checked across the whole synthetic dataset rather than per type, so a
    future addition is covered without anyone remembering to extend this."""
    from core.db.index import extract_patient_reference

    mock = _mock_server_module()
    patient_ids = {f"Patient/{p['id']}" for p in mock.RESOURCES_BY_TYPE["Patient"]}

    unlinked = []
    for rtype, resources in mock.RESOURCES_BY_TYPE.items():
        for resource in resources:
            reference = extract_patient_reference(resource)
            if reference not in patient_ids:
                unlinked.append((rtype, resource["id"], reference))

    assert not unlinked, f"resources with no resolvable patient reference: {unlinked}"


def test_unregistered_types_are_modelled_as_unauthorized_not_silent_success():
    """Types added to the profile without live Epic registration must show
    up as a 403 in the mock, not as an empty-but-successful response - the
    whole point of UNAUTHORIZED_TYPES. Discovering an unregistered type
    during testing is cheap; discovering it as a silently empty record
    set after an EMR retirement is not."""
    mock = _mock_server_module()

    assert mock.UNAUTHORIZED_TYPES, "UNAUTHORIZED_TYPES emptied - see the module docstring before doing that"
    assert mock.UNAUTHORIZED_TYPES <= set(EPIC.supported_resources), (
        "UNAUTHORIZED_TYPES names a type the profile does not list"
    )
# Made by Ryan Gomez & Co. Inc.
