# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Envelope encryption tests.

The property that matters most is negative: plaintext PHI must not be
recoverable from stored bytes. Testing that a round-trip works only
proves the happy path; testing that identifiers are absent from the
ciphertext is what actually validates the security claim.
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog, GENESIS_HASH  # noqa: E402
from core.crypto.envelope import EnvelopeEncryptor  # noqa: E402


class MockKMS:
    """Stands in for a cloud KMS. Wrapping is not real encryption here -
    this tests the envelope *mechanics*, not KMS itself."""

    def __init__(self):
        self._wrapped: dict[str, bytes] = {}
        self.generate_calls = 0
        self.unwrap_calls = 0

    def generate_data_key(self) -> tuple[bytes, str]:
        self.generate_calls += 1
        dek = os.urandom(32)
        handle = base64.b64encode(os.urandom(16)).decode()
        self._wrapped[handle] = dek
        return dek, handle

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        self.unwrap_calls += 1
        return self._wrapped[wrapped_dek_b64]


SYNTHETIC_RESOURCE = (
    b'{"resourceType":"Patient","id":"abc-123",'
    b'"name":[{"family":"Testpatient","given":["Synthetic"]}],'
    b'"birthDate":"1970-01-01"}'
)


def test_round_trip():
    enc = EnvelopeEncryptor(MockKMS())
    payload = enc.encrypt(SYNTHETIC_RESOURCE)
    recovered = enc.decrypt(payload.ciphertext, payload.nonce, payload.wrapped_dek_b64)
    assert recovered == SYNTHETIC_RESOURCE


def test_ciphertext_contains_no_plaintext_identifiers():
    enc = EnvelopeEncryptor(MockKMS())
    payload = enc.encrypt(SYNTHETIC_RESOURCE)

    for identifier in (b"Testpatient", b"Synthetic", b"abc-123", b"1970-01-01", b"Patient"):
        assert identifier not in payload.ciphertext, f"{identifier!r} leaked into ciphertext"


def test_unique_dek_per_object():
    """Key reuse across objects would mean one compromised DEK exposes
    many records, and AES-GCM nonce reuse under the same key is
    catastrophic. Each encrypt call must request a fresh key."""
    kms = MockKMS()
    enc = EnvelopeEncryptor(kms)

    a = enc.encrypt(b"record one")
    b = enc.encrypt(b"record two")

    assert kms.generate_calls == 2
    assert a.wrapped_dek_b64 != b.wrapped_dek_b64
    assert a.nonce != b.nonce


def test_tampered_ciphertext_is_rejected():
    """AES-GCM is authenticated: a modified ciphertext must fail to
    decrypt rather than silently returning corrupted data."""
    enc = EnvelopeEncryptor(MockKMS())
    payload = enc.encrypt(SYNTHETIC_RESOURCE)

    tampered = bytearray(payload.ciphertext)
    tampered[5] ^= 0xFF

    from cryptography.exceptions import InvalidTag

    try:
        enc.decrypt(bytes(tampered), payload.nonce, payload.wrapped_dek_b64)
        raise AssertionError("tampered ciphertext must not decrypt")
    except InvalidTag:
        pass  # correct: authentication tag check caught the modification


def test_wrong_nonce_fails():
    enc = EnvelopeEncryptor(MockKMS())
    payload = enc.encrypt(SYNTHETIC_RESOURCE)
    try:
        enc.decrypt(payload.ciphertext, os.urandom(12), payload.wrapped_dek_b64)
        raise AssertionError("decryption with wrong nonce should fail")
    except Exception:
        pass


def test_sha256_matches_ciphertext():
    import hashlib

    enc = EnvelopeEncryptor(MockKMS())
    payload = enc.encrypt(SYNTHETIC_RESOURCE)
    assert payload.sha256_hex == hashlib.sha256(payload.ciphertext).hexdigest()


def test_audit_chain_resumes_across_restarts():
    """A chain that restarts from GENESIS on every process start would let
    an attacker delete an entire run's records undetected."""
    events = []

    log1 = AuditLog(sink=events.append)
    log1.record(actor="svc", action="record.write", resource_key="fhir/Patient/1.json")
    log1.record(actor="svc", action="record.write", resource_key="fhir/Patient/2.json")

    # Simulate restart: new AuditLog resuming from the persisted tip.
    last_hash = events[-1]["event_hash"]
    log2 = AuditLog(sink=events.append, last_known_hash=last_hash)
    log2.record(actor="svc", action="record.write", resource_key="fhir/Patient/3.json")

    assert events[2]["prev_hash"] == events[1]["event_hash"]
    assert AuditLog.verify_chain(events) is True


def test_fresh_chain_starts_at_genesis():
    events = []
    AuditLog(sink=events.append).record(
        actor="svc", action="record.write", resource_key="fhir/Patient/1.json"
    )
    assert events[0]["prev_hash"] == GENESIS_HASH


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
