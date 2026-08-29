# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Builds concrete storage + KMS instances from configuration.

Single place where "which cloud am I on" is resolved, so no other module
needs provider-specific branching.
"""

from __future__ import annotations

from typing import Optional

from core.config.settings import Settings


def build_storage(settings: Settings):
    """Return an ObjectStore implementation for the configured cloud."""
    if settings.cloud_provider == "aws":
        from core.storage.aws_s3 import S3Storage

        return S3Storage(
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            kms_key_id=settings.kms_key_id,
        )

    if settings.cloud_provider == "gcp":
        from core.storage.gcp_gcs import GCSStorage

        return GCSStorage(
            bucket=settings.storage_bucket,
            project=settings.gcp_project or "",
            kms_key_name=settings.kms_key_id,
        )

    if settings.cloud_provider == "azure":
        from core.storage.azure_blob import AzureBlobStorage

        return AzureBlobStorage(
            account_url=settings.azure_account_url or "",
            container=settings.storage_bucket,
        )

    raise ValueError(f"Unsupported cloud provider: {settings.cloud_provider}")


def build_kms(settings: Settings, key_id: Optional[str] = None):
    """Return a KeyManagementService implementation for the configured cloud.

    `key_id` overrides the general store's key for callers working
    against a differently-keyed store - the psychotherapy bucket's key
    is the one current case (core/db/retrieval_etl.py's
    --include-psychotherapy pass). Default None keeps every existing
    caller on settings.kms_key_id unchanged.
    """
    key = key_id or settings.kms_key_id
    if settings.cloud_provider == "aws":
        from core.crypto.envelope import AWSKMS

        return AWSKMS(key_id=key, region=settings.storage_region)

    if settings.cloud_provider == "gcp":
        from core.crypto.envelope import GCPKMS

        return GCPKMS(key_name=key)

    if settings.cloud_provider == "azure":
        from core.crypto.envelope import AzureKMS

        return AzureKMS(
            vault_url=settings.azure_vault_url or "",
            key_name=key,
        )

    raise ValueError(f"Unsupported cloud provider: {settings.cloud_provider}")


def build_audit_sink(settings: Settings):
    """
    Return a durable audit sink for the configured cloud - the
    __call__/last_hash/read_all interface core.audit.log.AuditLog and
    core.audit.verify actually need.

    FOUND MISSING during GCP stack work: core/fhir/scheduler.py and
    core/fhir/bulk_scheduler.py - the two primary ingestion entry points
    every deployment actually runs - both hardcoded a direct
    `from core.audit.sink import S3AuditSink` import and construction,
    completely unconditional on settings.cloud_provider. This meant that
    even after core/audit/sink.py gained AzureBlobAuditSink (built
    alongside the Azure deployment stack), neither scheduler ever
    actually used it - an Azure deployment's scheduler would still try
    to construct an S3AuditSink (and thus call boto3) regardless of
    being configured for Azure. The class existing was necessary but not
    sufficient; nothing had ever wired either scheduler to choose the
    right one. Both schedulers now call this function instead of
    importing a sink class directly, so a provider gains real,
    functioning scheduler support in exactly one place - here - rather
    than needing a matching fix hand-applied to every script that starts
    an AuditLog.

    object_lock_mode is no longer passed to S3AuditSink, or to any
    backend built here. The 2026-08-17 audit's H4 fix threaded it through
    every WORM write path in this codebase; that threading has since been
    removed along with Object Lock itself. Retention is now a recorded
    configuration value (core/config/retention_rules.py), not a mode
    selection, so there is nothing left for this factory to propagate -
    and no partial-fix hazard of the kind H4 was chasing, because no code
    path applies a lock at all.

    Distinct from build_audit_storage() below, which predates this
    function and returns a generic ObjectStore instance (put_object /
    get_object / list_keys / etc.) rather than the narrower, hash-chain-
    aware sink interface - the two are not interchangeable, and nothing
    in this codebase actually calls build_audit_storage() as of this
    writing.
    """
    if settings.cloud_provider == "aws":
        from core.audit.sink import S3AuditSink

        return S3AuditSink(
            bucket=settings.audit_bucket,
            region=settings.storage_region,
            kms_key_id=settings.audit_kms_key_id,
        )

    if settings.cloud_provider == "azure":
        from core.audit.sink import AzureBlobAuditSink

        return AzureBlobAuditSink(
            account_url=settings.azure_account_url or "",
            container=settings.audit_bucket,
        )

    if settings.cloud_provider == "gcp":
        from core.audit.sink import GCSAuditSink

        return GCSAuditSink(
            bucket=settings.audit_bucket,
            project=settings.gcp_project or "",
            kms_key_name=settings.audit_kms_key_id,
        )

    raise ValueError(f"Unsupported cloud provider: {settings.cloud_provider}")


def build_audit_storage(settings: Settings):
    """
    Audit records go to a separate bucket with a separate key, so the
    ingestion role can append audit entries without holding decrypt
    permission on stored PHI. See deploy/aws/iam.tf.
    """
    if settings.cloud_provider == "aws":
        from core.storage.aws_s3 import S3Storage

        return S3Storage(
            bucket=settings.audit_bucket,
            region=settings.storage_region,
            kms_key_id=settings.audit_kms_key_id,
        )

    # GCP/Azure audit buckets follow the same pattern; wire them up when
    # those deploy/ stacks land rather than guessing at resource naming now.
    raise NotImplementedError(
        f"Audit storage for {settings.cloud_provider} is not implemented yet. "
        "Only the AWS deployment stack currently provisions a separate audit bucket."
    )


def build_psychotherapy_storage(settings: Settings):
    """
    Storage for psychotherapy notes specifically - a separate bucket and
    key from the general object store. See deploy/aws/s3_psychotherapy.tf,
    deploy/aws/kms.tf, and runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for
    why this boundary exists and why it is a separate bucket rather than
    a prefix within the general object store bucket.

    Raises ValueError if psychotherapy_storage_bucket /
    psychotherapy_kms_key_id aren't configured - there is deliberately no
    fallback to the general object store here. Silently storing a
    psychotherapy note in the general bucket because the separate one
    wasn't configured would be exactly the failure this feature exists
    to prevent, not a reasonable degraded mode.
    """
    if not settings.psychotherapy_storage_bucket or not settings.psychotherapy_kms_key_id:
        # Names the CURRENT spelling. Both are read (see
        # core/config/settings.py's env_var()), but an error message is
        # the most-read documentation this project has and should teach
        # the variable that will still exist next release.
        raise ValueError(
            "PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET and PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID "
            "must both be set to store psychotherapy notes. There is no fallback to the "
            "general object store bucket - see runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for why."
        )

    if settings.cloud_provider == "aws":
        from core.storage.aws_s3 import S3Storage

        return S3Storage(
            bucket=settings.psychotherapy_storage_bucket,
            region=settings.storage_region,
            kms_key_id=settings.psychotherapy_kms_key_id,
        )

    raise NotImplementedError(
        f"Psychotherapy notes storage for {settings.cloud_provider} is not implemented yet. "
        "Only the AWS deployment stack currently provisions this bucket."
    )
# Made by Ryan Gomez & Co. Inc.
