# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Azure Blob Storage backend.

Requires (provisioned via deploy/azure/, not by this code):
  - Storage account with blob versioning enabled
  - Organization-managed key (CMK) encryption via Azure Key Vault
  - Storage account network rules restricting to the deploying org's VNet

Deliberately does NOT set a version-level immutability policy. This
backend previously called set_immutability_policy() on every write, and
in Locked mode that is irreversible - a Locked policy cannot be shortened
or removed before expiry by anyone, including the subscription owner.
`retention_until` is now written as the `retain_until` metadata key: the
intended disposition date, enforced by nothing.

Requires `azure-storage-blob` and `azure-identity` at runtime (see
requirements.txt).
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


class AzureBlobStorage(ObjectStore):
    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential  # deferred import
        from azure.storage.blob import BlobServiceClient

        self.account_url = account_url
        self.container_name = container
        credential = DefaultAzureCredential()
        self._service = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = self._service.get_container_client(container)

    def put_object(
        self,
        key: str,
        ciphertext: bytes,
        wrapped_dek_b64: str,
        sha256_hex: str,
        retention_until: Optional[datetime] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObjectMetadata:
        from azure.storage.blob import ContentSettings

        metadata = {"wrapped_dek": wrapped_dek_b64, "sha256": sha256_hex}
        # Recorded, not enforced. Azure metadata keys must be valid C#
        # identifiers, hence the underscore rather than a hyphen.
        encoded_retention = encode_retention(retention_until)
        if encoded_retention:
            metadata["retain_until"] = encoded_retention

        blob_client = self._container.get_blob_client(key)
        blob_client.upload_blob(
            ciphertext,
            overwrite=True,
            metadata=metadata,
            content_settings=ContentSettings(content_type=content_type),
        )

        props = blob_client.get_blob_properties()
        return StoredObjectMetadata(
            key=key,
            version_id=props.version_id,
            size_bytes=props.size,
            sha256_hex=sha256_hex,
            stored_at=self._utcnow(),
            retention_until=retention_until,
            wrapped_dek_b64=wrapped_dek_b64,
            content_type=content_type,
        )

    def get_object(self, key: str, version_id: Optional[str] = None) -> bytes:
        blob_client = self._container.get_blob_client(key)
        stream = blob_client.download_blob(version_id=version_id)
        return stream.readall()

    def get_metadata(self, key: str, version_id: Optional[str] = None) -> StoredObjectMetadata:
        blob_client = self._container.get_blob_client(key)
        props = blob_client.get_blob_properties(version_id=version_id)
        meta = props.metadata or {}
        return StoredObjectMetadata(
            key=key,
            version_id=props.version_id,
            size_bytes=props.size,
            sha256_hex=meta.get("sha256", ""),
            stored_at=props.creation_time,
            retention_until=decode_retention(meta.get("retain_until")),
            wrapped_dek_b64=meta.get("wrapped_dek", ""),
            content_type=props.content_settings.content_type if props.content_settings else "application/octet-stream",
        )

    def object_exists(self, key: str) -> bool:
        return self._container.get_blob_client(key).exists()

    def list_keys(self, prefix: str = "") -> list[str]:
        # ContainerClient.list_blobs pages internally already - no manual
        # pagination needed, unlike the S3 backend.
        return [blob.name for blob in self._container.list_blobs(name_starts_with=prefix)]
# Made by Ryan Gomez & Co. Inc.
