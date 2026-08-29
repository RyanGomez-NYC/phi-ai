# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Storage abstraction for the PHI AI Platform.

All backends store ciphertext only. Plaintext PHI must never be passed to
`put_object` — callers are expected to have already run objects through
`core.crypto.envelope.EnvelopeEncryptor` first. This module deliberately
does not know how to decrypt anything; separation of concerns between
"where bytes live" and "what the bytes mean" is intentional.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def encode_retention(retention_until: Optional[datetime]) -> Optional[str]:
    """ISO-8601, for storage as a plain metadata string.

    The metadata KEY differs per provider (S3 lowercases user metadata,
    Azure requires identifier-safe names), so each backend picks its own;
    the encoding is shared so a date written by one reads back the same.
    """
    return retention_until.isoformat() if retention_until else None


def decode_retention(raw: Optional[str]) -> Optional[datetime]:
    """Parse a retain-until string back out of object metadata.

    Returns None on anything unparseable rather than raising: this value
    is advisory, and a malformed date must never make a stored object
    unreadable.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class StoredObjectMetadata:
    key: str
    version_id: Optional[str]
    size_bytes: int
    sha256_hex: str
    stored_at: datetime

    # DECLARED retention only. No backend enforces this date - it is
    # written as ordinary object metadata and mirrored into the Postgres
    # index so disposition can be driven from it. An object whose
    # retention_until is years away is still deletable today by anyone
    # holding delete permission.
    retention_until: Optional[datetime]
    wrapped_dek_b64: str  # the wrapped (KMS-encrypted) data encryption key
    content_type: str = "application/octet-stream"


class ObjectStore(abc.ABC):
    """
    Common interface every cloud backend must implement.

    Implementations MUST enable, at the bucket/container level (not just in
    application logic):
      - Versioning
      - Server-side encryption using a organization-managed KMS key (defense
        in depth on top of the application-level envelope encryption)

    Implementations MUST NOT apply an immutability / WORM feature (S3
    Object Lock, Azure Locked immutability policies, GCS Bucket Lock or
    per-object retention). That was removed deliberately: retention in
    this system is a configuration value - see
    core/config/retention_rules.py - recorded as metadata, not a control
    the storage layer enforces. A backend that silently applies a lock
    reintroduces exactly the irreversibility this design gave up, and in
    COMPLIANCE/Locked modes does so permanently.

    Integrity therefore rests on DETECTION rather than PREVENTION:
    versioning, the recorded SHA-256 digest, the hash-chained audit log,
    and provider-side access logs. Every caller that used to rely on a
    lock refusing a delete must now rely on IAM scoping instead.
    """

    @abc.abstractmethod
    def put_object(
        self,
        key: str,
        ciphertext: bytes,
        wrapped_dek_b64: str,
        sha256_hex: str,
        retention_until: Optional[datetime] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObjectMetadata:
        """Write an already-encrypted object. Returns metadata including
        the storage-assigned version id, used for integrity checks and
        for audit-log cross-referencing.

        `retention_until` is RECORDED as metadata, not enforced."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_object(self, key: str, version_id: Optional[str] = None) -> bytes:
        """Return raw ciphertext bytes for the given key/version."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_metadata(self, key: str, version_id: Optional[str] = None) -> StoredObjectMetadata:
        raise NotImplementedError

    @abc.abstractmethod
    def object_exists(self, key: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def list_keys(self, prefix: str = "") -> list[str]:
        """
        All object keys currently stored under the given prefix.

        Added for core/db/reconcile.py, which needs to enumerate what's
        actually in storage to check the Postgres index against it - S3
        (or the equivalent backend) is the system of record, so this is
        the ground truth reconciliation compares against, never the
        index. Implementations must page through the full result set
        internally rather than truncating at a provider's per-call
        limit (e.g. S3's list_objects_v2 caps at 1000 keys/call) - a
        caller getting a silently incomplete list back would be worse
        than the call being slow.
        """
        raise NotImplementedError

    def iter_keys(self, prefix: str = ""):
        """Yield keys in sorted order without materialising them all.

        The streaming counterpart to list_keys(), for reconciliation and
        verification over large deployments where the full key list does
        not fit in memory. S3, GCS and Azure Blob all list
        lexicographically, so a backend that pages its listing API yields
        sorted keys for free.

        The default implementation falls back to sorting list_keys(),
        which is correct but not memory-bounded - a backend that has not
        overridden this still works, just without the benefit.
        """
        for key in sorted(self.list_keys(prefix=prefix)):
            yield key

    def delete_object(
        self,
        key: str,
        version_id: Optional[str] = None,
    ) -> None:
        """
        Permanently delete an object version. Added for
        core/fhir/purge.py - no other part of this project calls this;
        every ingest/restore/audit code path is deliberately append-only.

        Deliberately a concrete method with a NotImplementedError
        default, NOT an abc.abstractmethod - making it abstract would
        have broken instantiation of every existing backend
        (AzureBlobStorage, GCSStorage) that predates this method and
        doesn't yet implement it, the moment this file shipped, for
        code paths that never call delete_object at all. Same pattern
        core/storage/factory.py's own docstring describes for
        build_audit_storage()'s provider gaps: fail clearly and
        specifically if this is ever actually called on a backend that
        hasn't implemented it yet, rather than silently breaking every
        other use of that backend in the meantime. core/storage/aws_s3.py
        overrides this; Azure/GCP do not yet.

        NO LOCK STANDS IN THE WAY OF THIS CALL. It previously took a
        `bypass_governance_retention` flag, and its contract described
        two narrow conditions under which a delete could succeed: an
        already-elapsed retention period, or an explicit GOVERNANCE-mode
        bypass held by the caller's own IAM identity. Neither condition
        exists anymore. With Object Lock removed there is nothing to
        bypass and nothing to wait out, so the parameter is gone rather
        than left as a no-op argument that would read like a safeguard.

        What that means for callers: this call now succeeds whenever the
        caller's IAM identity permits a delete, at any time, including
        well inside the declared retention period. The storage layer will
        not second-guess it. Authorization for disposal is entirely a
        matter of IAM scoping plus whatever approval gate the calling
        tool implements - see core/fhir/purge.py.
        """
        raise NotImplementedError(
            f"delete_object is not implemented for {type(self).__name__}. "
            "This is a provider gap, not something purge.py can work around - "
            "see core/storage/aws_s3.py for the only current implementation."
        )

    def list_object_versions(self, key: str) -> list[str]:
        """
        Every version_id currently stored for this exact key. Added for
        delete_all_versions() below (2026-08-17 audit, C4 - disposal
        completeness): deciding what "every version" means requires
        enumerating them first, the same way delete_object() requires a
        caller to already have a version_id from get_metadata() before
        calling it for a single version.

        Same NotImplementedError-by-default shape as delete_object()
        above, for the identical reason - see that method's docstring;
        it applies unchanged here. core/storage/aws_s3.py overrides
        this; Azure/GCP do not yet, matching delete_object()'s own gap
        (core/fhir/purge.py and psychotherapy_purge.py are AWS-only
        today - see those modules' own docstrings).
        """
        raise NotImplementedError(
            f"list_object_versions is not implemented for {type(self).__name__}. "
            "This is a provider gap, not something purge.py can work around - "
            "see core/storage/aws_s3.py for the only current implementation."
        )

    def delete_all_versions(self, key: str) -> int:
        """
        Permanently deletes EVERY version of `key`, not just the
        current one - added for core/fhir/purge.py's disposal
        completeness fix (2026-08-17 audit, C4): a version-specific
        delete_object() call removes exactly one version, so a key that
        was ever overwritten would otherwise leave earlier versions
        recoverable after "disposal," silently - nothing about a single
        delete_object() call signals that older versions still exist.
        Disposal is only actually complete when nothing recoverable
        survives.

        Concrete (not abstract) here, layered on list_object_versions()
        and delete_object() above - both of which stay the two
        extension points a new backend needs to implement, rather than
        requiring a third, backend-specific "delete everything"
        implementation each time.

        Deliberately NOT partial-failure-tolerant: if any individual
        version's delete_object() call raises (now most likely an IAM
        denial or a transient provider error, since no retention lock
        can refuse it - see delete_object()'s own docstring), this
        propagates immediately rather than catching and continuing.
        Versions already deleted before the failure stay deleted -
        there is no transaction spanning multiple object versions in
        any of these storage APIs - so a caller catching an exception
        from this method should assume a PARTIAL deletion may have
        happened and re-check with list_object_versions() before
        deciding what to do next. This method makes no attempt to
        delete "most" of a key's versions and call that success.

        Returns the count of versions that were actually deleted.
        """
        version_ids = self.list_object_versions(key)
        for version_id in version_ids:
            self.delete_object(key, version_id=version_id)
        return len(version_ids)

    def verify_integrity(self, key: str, version_id: Optional[str] = None) -> bool:
        """Default integrity check: recompute SHA-256 of stored ciphertext
        and compare to the recorded digest. Backends may override with a
        cheaper server-side checksum comparison where the provider
        supports it."""
        import hashlib

        meta = self.get_metadata(key, version_id=version_id)
        data = self.get_object(key, version_id=version_id)
        return hashlib.sha256(data).hexdigest() == meta.sha256_hex

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
# Made by Ryan Gomez & Co. Inc.
