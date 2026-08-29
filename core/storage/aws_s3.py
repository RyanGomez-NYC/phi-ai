# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
AWS S3 backend.

Requires (provisioned via deploy/aws/, not by this code):
  - Bucket with versioning enabled
  - Default SSE-KMS encryption with a organization-managed CMK
  - Bucket policy denying any non-TLS (aws:SecureTransport=false) request

Deliberately does NOT require or use S3 Object Lock. This backend never
sends ObjectLockMode or ObjectLockRetainUntilDate; the buckets in
deploy/aws/ are created without Object Lock, and sending those headers to
a non-Object-Lock bucket is an InvalidRequest error anyway.
`retention_until` is written as the `retain-until` user metadata key: a
record of the intended disposition date, readable by the disposition
tooling, enforced by nothing.

Requires `boto3` at runtime (see requirements.txt). Import is deferred so
this module can be imported for type-checking / docs generation without
boto3 installed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from core.storage.base import (
    ObjectStore,
    StoredObjectMetadata,
    decode_retention,
    encode_retention,
)


class S3Storage(ObjectStore):
    def __init__(
        self,
        bucket: str,
        region: str,
        kms_key_id: Optional[str] = None,
    ):
        import boto3  # deferred import

        self.bucket = bucket
        self.region = region
        self.kms_key_id = kms_key_id
        self._client = boto3.client("s3", region_name=region)

    def put_object(
        self,
        key: str,
        ciphertext: bytes,
        wrapped_dek_b64: str,
        sha256_hex: str,
        retention_until: Optional[datetime] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObjectMetadata:
        metadata = {
            "wrapped-dek": wrapped_dek_b64,
            "sha256": sha256_hex,
        }
        # Recorded, not enforced - see the module docstring.
        encoded_retention = encode_retention(retention_until)
        if encoded_retention:
            metadata["retain-until"] = encoded_retention

        extra = {
            "ServerSideEncryption": "aws:kms",
            "Metadata": metadata,
            "ContentType": content_type,
        }
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id

        resp = self._client.put_object(Bucket=self.bucket, Key=key, Body=ciphertext, **extra)

        return StoredObjectMetadata(
            key=key,
            version_id=resp.get("VersionId"),
            size_bytes=len(ciphertext),
            sha256_hex=sha256_hex,
            stored_at=self._utcnow(),
            retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64,
            content_type=content_type,
        )

    def get_object(self, key: str, version_id: Optional[str] = None) -> bytes:
        kwargs = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kwargs["VersionId"] = version_id
        return self._client.get_object(**kwargs)["Body"].read()

    def get_metadata(self, key: str, version_id: Optional[str] = None) -> StoredObjectMetadata:
        kwargs = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kwargs["VersionId"] = version_id
        head = self._client.head_object(**kwargs)
        meta = head.get("Metadata", {})
        return StoredObjectMetadata(
            key=key,
            version_id=head.get("VersionId"),
            size_bytes=head["ContentLength"],
            sha256_hex=meta.get("sha256", ""),
            stored_at=head["LastModified"],
            retention_until=decode_retention(meta.get("retain-until")),
            wrapped_dek_b64=meta.get("wrapped-dek", ""),
            content_type=head.get("ContentType", "application/octet-stream"),
        )

    def object_exists(self, key: str) -> bool:
        # FOUND AND FIXED (2026-08-17 audit, MEDIUM): this previously
        # caught the bare `ClientError` and returned False for ANY
        # failure - a genuine 404 and a 403 (IAM regression / access
        # denial) were indistinguishable, both read as "does not
        # exist". Proven live against a stub client matching botocore's
        # real exception shape: head_object failing with code "403"
        # returned False under the old code, identical to a real "404".
        # That's a real blast radius, not theoretical - core/fhir/purge.py
        # and core/fhir/psychotherapy_purge.py both call this to decide
        # whether a disposal target is "missing" (skipped) vs. present
        # and eligible for deletion; under the old behavior, a broken
        # IAM grant on the object store bucket would make every in-scope
        # object silently register as "already gone" instead of
        # surfacing the access failure. Only the genuine "does not
        # exist" codes now resolve to False; everything else
        # (403, 500, KMS grant failures, etc.) is re-raised so the
        # caller sees the real failure rather than a masked absence -
        # consistent with this project's "no silent fallbacks for
        # security- or compliance-relevant misconfiguration" invariant.
        # Known gap: GCS's and Azure's `.exists()` calls
        # (core/storage/gcp_gcs.py, core/storage/azure_blob.py) have
        # their own, different per-SDK error-swallowing semantics and
        # are not addressed by this fix - tracked separately, since
        # confirming their real behavior needs the same prove-first
        # process against each SDK's actual exception shape rather than
        # assuming this AWS fix transfers directly.
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list_keys(self, prefix: str = "") -> list[str]:
        # list_objects_v2 caps at 1000 keys per call - paginate internally
        # so callers always get the complete list, not a silently
        # truncated one. A real deployment will exceed 1000 objects
        # quickly.
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def iter_keys(self, prefix: str = ""):
        """Stream keys straight off the paginator.

        S3 returns keys in lexicographic order within a listing, so this
        yields sorted keys while holding only one page - which is what
        makes reconciliation over a large deployment bounded by the number
        of DISCREPANCIES rather than by the number of objects.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def delete_object(
        self,
        key: str,
        version_id: Optional[str] = None,
    ) -> None:
        """
        See ObjectStore.delete_object's docstring for the full
        contract this implements.

        No BypassGovernanceRetention, because there is no lock to bypass:
        these buckets carry no Object Lock and this backend applies no
        per-object retention. S3 will refuse this call only on an IAM
        denial, which is now the ONLY thing standing between a caller and
        permanent deletion of stored PHI. That refusal is passed
        through uncaught and untranslated, since a caller needs the real
        reason a delete failed rather than a generic wrapper exception.

        version_id still matters, for a different reason than it used to.
        On a versioned bucket, DeleteObject without a version_id creates
        a delete marker and leaves every prior version recoverable, so a
        caller intending actual disposal must pass the version_id from a
        prior get_metadata() call, or use
        list_object_versions()/delete_all_versions() to cover every
        version of a key at once.
        """
        kwargs = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kwargs["VersionId"] = version_id

        self._client.delete_object(**kwargs)

    def list_object_versions(self, key: str) -> list[str]:
        """
        See ObjectStore.list_object_versions's docstring for the
        full contract. Every version_id S3 currently holds for this
        exact key - real object versions only, not delete markers (a
        delete marker carries no content, so there is nothing under it
        for delete_all_versions() to remove; if a prior unversioned-style
        delete created one for this key, it is left in place here,
        since removing a delete marker un-deletes the version beneath
        it, which purge.py's callers do not want).

        list_object_versions is a prefix-based listing API, not a
        direct key lookup - paginated the same way list_keys() already
        is, and filtered to exact-key matches so a delete_all_versions()
        call for "fhir/DocumentReference/eSyn0001Note.json" doesn't also
        match "fhir/DocumentReference/eSyn0001Note2.json".
        """
        paginator = self._client.get_paginator("list_object_versions")
        version_ids: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            for v in page.get("Versions", []) or []:
                if v["Key"] == key:
                    version_ids.append(v["VersionId"])
        return version_ids
# Made by Ryan Gomez & Co. Inc.
