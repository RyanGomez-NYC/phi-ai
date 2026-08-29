# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Google Cloud Storage backend.

Requires (provisioned via deploy/gcp/, not by this code):
  - Bucket with Object Versioning enabled
  - CMEK (organization-managed encryption key) via Cloud KMS
  - Uniform bucket-level access + org policy restricting public access

Deliberately does NOT use Bucket Lock or per-object retention. Bucket
Lock in particular is permanent: once a retention policy is locked on a
bucket it can be lengthened but never shortened or removed, for the life
of the bucket. `retention_until` is written as the `retain-until`
metadata key - the intended disposition date, enforced by nothing.

Requires `google-cloud-storage` at runtime (see requirements.txt).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.storage.base import (
    ObjectStore,
    StoredObjectMetadata,
    decode_retention,
    encode_retention,
)


class GCSStorage(ObjectStore):
    def __init__(
        self,
        bucket: str,
        project: str,
        kms_key_name: Optional[str] = None,
    ):
        from google.cloud import storage as gcs  # deferred import

        self.bucket_name = bucket
        self.project = project
        self.kms_key_name = kms_key_name
        self._client = gcs.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    def put_object(
        self,
        key: str,
        ciphertext: bytes,
        wrapped_dek_b64: str,
        sha256_hex: str,
        retention_until: Optional[datetime] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObjectMetadata:
        metadata = {"wrapped-dek": wrapped_dek_b64, "sha256": sha256_hex}
        # Recorded, not enforced - see the module docstring.
        encoded_retention = encode_retention(retention_until)
        if encoded_retention:
            metadata["retain-until"] = encoded_retention

        blob = self._bucket.blob(key, kms_key_name=self.kms_key_name)
        blob.metadata = metadata
        blob.upload_from_string(ciphertext, content_type=content_type)

        blob.reload()
        return StoredObjectMetadata(
            key=key,
            version_id=str(blob.generation),
            size_bytes=blob.size or len(ciphertext),
            sha256_hex=sha256_hex,
            stored_at=self._utcnow(),
            retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64,
            content_type=content_type,
        )

    def get_object(self, key: str, version_id: Optional[str] = None) -> bytes:
        blob = self._bucket.blob(key, generation=int(version_id) if version_id else None)
        return blob.download_as_bytes()

    def get_metadata(self, key: str, version_id: Optional[str] = None) -> StoredObjectMetadata:
        blob = self._bucket.blob(key, generation=int(version_id) if version_id else None)
        blob.reload()
        meta = blob.metadata or {}
        return StoredObjectMetadata(
            key=key,
            version_id=str(blob.generation),
            size_bytes=blob.size,
            sha256_hex=meta.get("sha256", ""),
            stored_at=blob.time_created,
            retention_until=decode_retention(meta.get("retain-until")),
            wrapped_dek_b64=meta.get("wrapped-dek", ""),
            content_type=blob.content_type or "application/octet-stream",
        )

    def object_exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists()

    def list_keys(self, prefix: str = "") -> list[str]:
        # Client.list_blobs pages internally already - no manual
        # pagination needed, unlike the S3 backend.
        return [blob.name for blob in self._client.list_blobs(self._bucket, prefix=prefix)]
# Made by Ryan Gomez & Co. Inc.
