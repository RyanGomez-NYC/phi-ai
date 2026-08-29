# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Envelope encryption for stored PHI objects.

Pattern (standard AWS/GCP/Azure-recommended envelope encryption):
  1. Generate a random 256-bit Data Encryption Key (DEK) locally.
  2. Encrypt the plaintext with the DEK using AES-256-GCM (authenticated
     encryption — protects both confidentiality and integrity).
  3. Ask the cloud KMS to "wrap" (encrypt) the DEK using a Key Encryption
     Key (KEK) that never leaves the KMS.
  4. Discard the plaintext DEK immediately. Store only the ciphertext and
     the wrapped DEK.
  5. To decrypt: ask KMS to unwrap the DEK (requires IAM permission on the
     KMS key — this is where access control is actually enforced), then
     use the unwrapped DEK to decrypt the ciphertext locally.

This means the storage layer (core/storage) never sees plaintext, and a
compromised storage bucket alone does not expose PHI — the attacker would
also need KMS decrypt permission, which is where audit-logged access
control lives.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek_b64: str
    sha256_hex: str  # digest of the *ciphertext*, for storage-layer integrity checks


class KeyManagementService(Protocol):
    """Interface every cloud KMS wrapper must implement."""

    def generate_data_key(self) -> tuple[bytes, str]:
        """Return (plaintext_dek, wrapped_dek_b64)."""
        ...

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        """Return the plaintext DEK for a previously wrapped key."""
        ...


class EnvelopeEncryptor:
    def __init__(self, kms: KeyManagementService):
        self._kms = kms

    def encrypt(self, plaintext: bytes) -> EncryptedPayload:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek, wrapped_dek_b64 = self._kms.generate_data_key()
        try:
            aesgcm = AESGCM(dek)
            nonce = os.urandom(12)
            ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
        finally:
            # Best-effort scrub of the plaintext key from the local
            # variable; Python can't guarantee memory zeroing, but we
            # avoid retaining any reference beyond this scope.
            dek = b"\x00" * len(dek)
            del dek

        return EncryptedPayload(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_dek_b64=wrapped_dek_b64,
            sha256_hex=hashlib.sha256(ciphertext).hexdigest(),
        )

    def decrypt(self, ciphertext: bytes, nonce: bytes, wrapped_dek_b64: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek = self._kms.unwrap_data_key(wrapped_dek_b64)
        try:
            aesgcm = AESGCM(dek)
            return aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        finally:
            dek = b"\x00" * len(dek)
            del dek


class AWSKMS:
    def __init__(self, key_id: str, region: str):
        import boto3

        self.key_id = key_id
        self._client = boto3.client("kms", region_name=region)

    def generate_data_key(self) -> tuple[bytes, str]:
        resp = self._client.generate_data_key(KeyId=self.key_id, KeySpec="AES_256")
        return resp["Plaintext"], base64.b64encode(resp["CiphertextBlob"]).decode()

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        blob = base64.b64decode(wrapped_dek_b64)
        resp = self._client.decrypt(CiphertextBlob=blob, KeyId=self.key_id)
        return resp["Plaintext"]


class GCPKMS:
    def __init__(self, key_name: str):
        # key_name format: projects/*/locations/*/keyRings/*/cryptoKeys/*
        from google.cloud import kms

        self.key_name = key_name
        self._client = kms.KeyManagementServiceClient()

    def generate_data_key(self) -> tuple[bytes, str]:
        dek = os.urandom(32)
        resp = self._client.encrypt(request={"name": self.key_name, "plaintext": dek})
        wrapped_b64 = base64.b64encode(resp.ciphertext).decode()
        return dek, wrapped_b64

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        ciphertext = base64.b64decode(wrapped_dek_b64)
        resp = self._client.decrypt(request={"name": self.key_name, "ciphertext": ciphertext})
        return resp.plaintext


class AzureKMS:
    """Wraps DEKs using Azure Key Vault keys (RSA-OAEP wrap of a locally
    generated AES-256 DEK, following the same envelope pattern).

    FIXED (2026-08-17 audit, H5). AWS's `kms.decrypt(CiphertextBlob=...)`
    and GCP's `client.decrypt(ciphertext=...)` above both resolve which
    KEY VERSION performed the original wrap SERVER-SIDE, from metadata
    embedded in the ciphertext blob itself - the caller never needs to
    know or track which version was current at wrap time, encrypt and
    decrypt both "just work" regardless of how many times the key has
    rotated since. Azure Key Vault's RSA-OAEP wrap/unwrap does NOT work
    this way: `wrap_key`/`unwrap_key` operate against whichever specific
    key VERSION the CryptographyClient is bound to, and a wrapped-DEK
    blob carries no version metadata of its own for Key Vault to resolve
    automatically. Each Key Vault key version has genuinely different RSA
    key material (this is what makes rotation cryptographically
    meaningful at all) - a version other than the one that performed the
    wrap CANNOT unwrap it, not "usually can't," structurally cannot,
    regardless of any permission or configuration.

    The class previously bound its ONE CryptographyClient to whatever
    `key_client.get_key(key_name)` returned as "latest" at construction
    time, and used that same client for BOTH wrap (generate_data_key)
    and unwrap (unwrap_data_key). That is correct for wrap - new writes
    should always use the current key version - but wrong for unwrap:
    every AzureKMS instance constructed after a key rotation binds to
    the NEW latest version, and using it to unwrap a DEK that was
    wrapped under the OLD version fails outright. Concretely: store an
    object today, rotate the Key Vault key next month (whether triggered
    manually or by the rotation_policy this fix's Terraform half adds -
    see deploy/azure/keyvault.tf), then try to restore that object -
    the restore fails, permanently, for every object stored before
    the rotation, with no way to recover the version that actually
    wrapped them after the fact, because it was never recorded anywhere.
    This was previously masked only by deploy/azure/keyvault.tf having
    NO rotation_policy configured at all - an undocumented parity gap
    versus GCP's enforced 90-day rotation and AWS's enforced rotation
    precondition (deploy/aws/kms.tf), not a deliberate safety choice.

    Fix: `generate_data_key()` now packs the FULL VERSIONED key ID
    (`https://<vault>/keys/<name>/<version>`, captured from Key Vault's
    own response at wrap time) alongside the wrapped bytes, delimited by
    "|" - a character that can never appear in either half (base64's
    alphabet is A-Za-z0-9+/= only; Azure resource ID URLs don't contain
    it either). `unwrap_data_key()` parses that prefix back out and
    builds (and caches) a CryptographyClient bound to THAT EXACT
    version, not whatever is currently latest - proven safe for both
    halves against a hand-rolled stub asserting the same packing/parsing
    contract (2026-08-17 audit execution notes).

    BACKWARD COMPATIBILITY: an object stored before this fix has a
    bare base64 wrapped DEK with no packed version (no "|" present) -
    `rpartition("|")` on such a string returns an empty key_id, which
    unwrap_data_key() below treats as "no version recorded" and falls
    back to the pre-fix behavior: unwrap using whatever is CURRENT
    latest. This works only if no rotation has happened between that
    object being stored and this restore attempt - if one has, the
    restore still fails, exactly as it already could have before this
    fix existed. This fix cannot retroactively recover a version
    identifier that was never recorded for already-stored objects; it
    only prevents the failure for everything stored from this point
    forward. There is no migration step that closes this gap for
    pre-fix objects - the version they were wrapped under is genuinely
    unrecoverable metadata, not something this code can reconstruct.
    """

    def __init__(self, vault_url: str, key_name: str):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
        from azure.keyvault.keys import KeyClient

        self._wrap_alg = KeyWrapAlgorithm.rsa_oaep_256
        self._credential = DefaultAzureCredential()
        key_client = KeyClient(vault_url=vault_url, credential=self._credential)
        key = key_client.get_key(key_name)
        # key.id is the FULL VERSIONED identifier
        # ("https://<vault>/keys/<name>/<version>"), not just the key
        # name - this is what gets packed alongside every wrapped DEK
        # below, and what makes unwrap_data_key() able to bind to the
        # exact version that performed a given wrap regardless of what
        # has rotated since. See this class's own docstring.
        self._latest_key_id = key.id
        # Bound to whatever was latest AT CONSTRUCTION TIME - used for
        # wrap (new writes always use the current version, which is
        # correct) and as the BACKWARD-COMPATIBILITY fallback for
        # unwrapping pre-fix DEKs that carry no packed version. NOT
        # reused for unwrapping any DEK that DOES carry a packed
        # version - see unwrap_data_key() below, which builds a
        # separately-scoped client for those.
        self._crypto_client = CryptographyClient(key, credential=self._credential)
        # One CryptographyClient per distinct key version actually
        # needed for an unwrap, built lazily and cached for the
        # lifetime of this AzureKMS instance - avoids re-authenticating
        # per call when a single restore run unwraps many DEKs that
        # all happen to share one (pre- or post-rotation) version.
        self._unwrap_clients_by_version: dict[str, "CryptographyClient"] = {}

    def generate_data_key(self) -> tuple[bytes, str]:
        dek = os.urandom(32)
        result = self._crypto_client.wrap_key(self._wrap_alg, dek)
        wrapped_b64 = base64.b64encode(result.encrypted_key).decode()
        return dek, f"{self._latest_key_id}|{wrapped_b64}"

    def unwrap_data_key(self, wrapped_dek_b64: str) -> bytes:
        from azure.keyvault.keys.crypto import CryptographyClient

        # rpartition("|") returns ("", "", original_string) when no "|"
        # is present - i.e. a pre-fix, unversioned wrapped DEK. See this
        # class's docstring's "BACKWARD COMPATIBILITY" section.
        key_id, _, wrapped_b64 = wrapped_dek_b64.rpartition("|")
        wrapped = base64.b64decode(wrapped_b64)

        if not key_id:
            client = self._crypto_client
        else:
            client = self._unwrap_clients_by_version.get(key_id)
            if client is None:
                # A bare versioned key ID string is a valid `key`
                # argument to CryptographyClient - no need to re-fetch
                # via KeyClient.get_key() first, which would also
                # require guessing/parsing a version query param rather
                # than just handing Key Vault the exact ID it already
                # gave us at wrap time.
                client = CryptographyClient(key_id, credential=self._credential)
                self._unwrap_clients_by_version[key_id] = client

        result = client.unwrap_key(self._wrap_alg, wrapped)
        return result.key
# Made by Ryan Gomez & Co. Inc.
