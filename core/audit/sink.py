# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Durable audit sinks.

The hash chain in `core.audit.log` is only meaningful if it survives
process restarts: if each run starts a fresh chain from GENESIS, an
attacker who deletes a whole run's worth of records leaves no evidence.
All three sinks below therefore resume the chain from the last persisted
record on startup.

Layout in the audit container/bucket:

    audit/YYYY/MM/DD/<iso8601-timestamp>-<short-hash>.json

One object per event. Verbose, but it keeps tamper detection at
single-event granularity: dropping one event breaks the hash chain at
exactly that point rather than corrupting an opaque batched file. For
high-volume deployments this is the first thing to revisit (batching
trades tamper-granularity for cost).

NO IMMUTABILITY PROTECTION, on any cloud. These objects were previously
described as independently WORM-protected (Object Lock on AWS,
container-level policy on Azure, retention lock on GCP); none of those
are provisioned anymore. Deleting an audit record is possible for any
principal holding delete permission. It remains DETECTABLE - the chain
breaks, versioning keeps the superseded object, and CloudTrail (or the
provider equivalent) records the call - but it is not prevented. That
makes verifying the chain on a schedule, rather than only during an
incident, the control that actually does the work here; see
core/audit/verify.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.audit.log import GENESIS_HASH


class S3AuditSink:
    def __init__(
        self,
        bucket: str,
        region: str,
        kms_key_id: str,
        retention_days: int = 2192,
    ):
        import boto3

        self.bucket = bucket
        self.kms_key_id = kms_key_id
        # Declared retention only. Recorded as object metadata; nothing
        # enforces it. See the module docstring.
        self.retention_days = retention_days
        self._client = boto3.client("s3", region_name=region)

    def __call__(self, event: dict) -> None:
        """AuditLog calls this with each serialized event."""
        ts = event["timestamp"]
        day_prefix = ts[:10].replace("-", "/")
        key = f"audit/{day_prefix}/{ts}-{event['event_hash'][:12]}.json"

        retain_until = datetime.now(timezone.utc) + timedelta(days=self.retention_days)

        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(event, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=self.kms_key_id,
            Metadata={"retain-until": retain_until.isoformat()},
        )

    def last_hash(self) -> str:
        """
        Return the hash of the most recent persisted event, so a
        restarting process continues the existing chain instead of
        starting a new one.

        Ordering note: keys are ISO-8601 timestamped, which sorts
        lexicographically in chronological order, so the last key from a
        sorted listing is the newest event.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        newest_key: Optional[str] = None

        for page in paginator.paginate(Bucket=self.bucket, Prefix="audit/"):
            for obj in page.get("Contents", []):
                if newest_key is None or obj["Key"] > newest_key:
                    newest_key = obj["Key"]

        if newest_key is None:
            return GENESIS_HASH

        body = self._client.get_object(Bucket=self.bucket, Key=newest_key)["Body"].read()
        return json.loads(body)["event_hash"]

    def iter_events(self, prefix: str = "audit/", after_key: Optional[str] = None):
        """Yield (key, event) in chronological order, without loading the log.

        THE SCALING FIX. read_all() below materialises every event into a
        list, which is fine for a small deployment and impossible for a
        large one: at ~100 million events that is tens of gigabytes of RAM
        before verification even starts. Object keys are
        `audit/YYYY/MM/DD/<iso8601>-<hash>.json`, so listing order IS
        chronological order and a generator can stream them.

        `after_key` resumes from a checkpoint, so routine verification
        reads only what has been written since the last run rather than
        the whole history - which is what makes daily verification stay
        constant-time as the log grows.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        if after_key:
            # S3 lists in lexicographic order and StartAfter is exclusive,
            # so this skips everything already verified without reading it.
            kwargs["StartAfter"] = after_key

        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                yield key, json.loads(body)

    def read_all(self, prefix: str = "audit/") -> list[dict]:
        """Read every audit event under a prefix, in chronological order.

        Loads the entire log into memory. Fine for a small deployment; use
        iter_events() for anything large - see its docstring.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []

        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))

        events = []
        for key in sorted(keys):
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            events.append(json.loads(body))
        return events

    def recent_events(self, limit: int = 200, prefix: str = "audit/") -> list[dict]:
        """The newest `limit` events, in chronological order.

        LISTING IS CHEAP, READING IS NOT: a key listing costs one request
        per thousand keys, while iter_events() performs one GetObject per
        event just to discard everything but the tail. Keys are
        ISO-8601-timestamped (see __call__), so lexicographic order is
        chronological order and the tail of the listing IS the newest
        slice - only those objects are fetched. This is what keeps the
        audit page a page rather than a batch job as the log grows.
        """
        from concurrent.futures import ThreadPoolExecutor

        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        keys.sort()
        tail = keys[-max(limit, 0):]

        def _fetch(key: str) -> dict:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            return json.loads(body)

        # Parallel GETs, ordered results. boto3 clients are thread-safe
        # for use like this; sixteen lanes turn a multi-second sequential
        # tail read into roughly one round trip. Chain verification is
        # order-independent (diagnose_chain resolves by hash, not list
        # position), and the returned list preserves key order anyway.
        with ThreadPoolExecutor(max_workers=16) as pool:
            return list(pool.map(_fetch, tail))


class AzureBlobAuditSink:
    """
    Azure equivalent of S3AuditSink - same key layout, same resume-the-
    chain-on-startup behavior, same three-method interface AuditLog and
    core.audit.verify expect (__call__, last_hash, read_all), so both
    sinks are interchangeable from AuditLog's point of view.

    Takes no retention_days argument, and unlike S3AuditSink records no
    retain-until metadata: there is no container-level WORM policy behind
    it anymore (deploy/azure/storage.tf no longer creates an immutability
    policy), and no per-write parameter that would mean anything. Audit
    retention on Azure is currently a documented operational period only,
    with nothing written per blob to record it - a real gap relative to
    the S3 sink, which at least stamps the intended date. Worth closing
    if Azure moves past being a secondary target; see
    runbooks/RUNBOOK_AZURE_SETUP.md's Known Gaps.

    Similarly, no server-side-encryption parameters are passed per write
    - unlike S3, where ServerSideEncryption/SSEKMSKeyId must be specified
    on every PutObject to guarantee the object uses the intended key
    rather than silently falling back to unencrypted or a different key
    (see docs/COMPLIANCE.md's account of the DenyMissingEncryptionHeader
    fix on the AWS side for exactly this failure mode). Azure Storage
    accounts apply their configured default encryption (Microsoft-
    managed or organization-managed via Key Vault) to every write
    unconditionally - there is no unencrypted-write path to guard
    against at the application layer the way there was on S3, since
    Azure Storage has no equivalent "request explicitly opts out of the
    account default" behavior for encryption.
    """

    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.account_url = account_url
        self.container_name = container
        credential = DefaultAzureCredential()
        self._service = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = self._service.get_container_client(container)

    def __call__(self, event: dict) -> None:
        """AuditLog calls this with each serialized event."""
        from azure.storage.blob import ContentSettings

        ts = event["timestamp"]
        day_prefix = ts[:10].replace("-", "/")
        key = f"audit/{day_prefix}/{ts}-{event['event_hash'][:12]}.json"

        blob_client = self._container.get_blob_client(key)
        blob_client.upload_blob(
            json.dumps(event, sort_keys=True).encode("utf-8"),
            overwrite=False,  # each audit key is unique by construction; a collision indicates a real bug, not something to paper over with overwrite=True
            content_settings=ContentSettings(content_type="application/json"),
        )

    def last_hash(self) -> str:
        """
        Return the hash of the most recent persisted event, so a
        restarting process continues the existing chain instead of
        starting a new one. Same lexicographic-sort-of-ISO8601-keys
        reasoning as S3AuditSink.last_hash() - see that method's own
        docstring.
        """
        newest_key: Optional[str] = None

        for blob in self._container.list_blobs(name_starts_with="audit/"):
            if newest_key is None or blob.name > newest_key:
                newest_key = blob.name

        if newest_key is None:
            return GENESIS_HASH

        body = self._container.get_blob_client(newest_key).download_blob().readall()
        return json.loads(body)["event_hash"]

    def read_all(self, prefix: str = "audit/") -> list[dict]:
        """Read every audit event under a prefix, in chronological order.
        Used by the chain verifier and by incident response scoping."""
        keys = [blob.name for blob in self._container.list_blobs(name_starts_with=prefix)]

        events = []
        for key in sorted(keys):
            body = self._container.get_blob_client(key).download_blob().readall()
            events.append(json.loads(body))
        return events


class GCSAuditSink:
    """
    GCP equivalent of S3AuditSink/AzureBlobAuditSink - same key layout,
    same resume-the-chain-on-startup behavior, same three-method
    interface (__call__, last_hash, read_all).

    Explicitly passes kms_key_name on every upload, matching
    core/storage/gcp_gcs.py's GCSStorage - which does the same for the
    object store bucket - rather than relying on the bucket's own
    configured default_kms_key_name to apply implicitly. GCS would in
    fact apply a bucket's default CMEK automatically to an upload that
    doesn't specify a key, so this explicit pass is not strictly
    load-bearing the way S3AuditSink's SSEKMSKeyId is (S3 has no
    bucket-level default CMEK concept - omitting it there really does
    mean unencrypted-or-wrong-key, which is exactly the gap
    docs/COMPLIANCE.md's DenyMissingEncryptionHeader account describes).
    Passed explicitly anyway, for the same reason GCSStorage already
    does: never rely on a default matching what's intended, even a
    default that would currently be correct - a future bucket
    reconfiguration should not be able to silently change which key this
    class's writes end up under.

    Retention is enforced independently of anything this class does -
    see deploy/gcp/storage.tf's Object Retention Lock configuration on
    the audit bucket. Like AzureBlobAuditSink, this constructor takes no
    retention_days argument: the retention floor is a property of the
    bucket/object configuration Terraform establishes at provisioning
    time, not something a per-write application parameter drives. NOT
    part of the 2026-08-17 audit's H4 WORM-mode-hardcode finding for the
    same reason AzureBlobAuditSink isn't - see that class's own NOTE.
    core/storage/gcp_gcs.py's GCSStorage.put_object() IS part of that
    finding, because it makes its own separate, genuinely per-object
    blob.retention calls that this class does not.
    """

    def __init__(self, bucket: str, project: str, kms_key_name: Optional[str] = None):
        from google.cloud import storage as gcs

        self.bucket_name = bucket
        self.project = project
        self.kms_key_name = kms_key_name
        self._client = gcs.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    def __call__(self, event: dict) -> None:
        """AuditLog calls this with each serialized event."""
        ts = event["timestamp"]
        day_prefix = ts[:10].replace("-", "/")
        key = f"audit/{day_prefix}/{ts}-{event['event_hash'][:12]}.json"

        blob = self._bucket.blob(key, kms_key_name=self.kms_key_name)
        # if_generation_match=0 is GCS's precondition for "only succeed if
        # no generation of this object currently exists" - the GCS
        # equivalent of AzureBlobAuditSink's overwrite=False. Each audit
        # key is unique by construction; a collision indicates a real
        # bug (e.g. clock skew producing a duplicate timestamp+hash
        # prefix), not something to paper over by allowing an overwrite.
        blob.upload_from_string(
            json.dumps(event, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            if_generation_match=0,
        )

    def last_hash(self) -> str:
        """
        Return the hash of the most recent persisted event, so a
        restarting process continues the existing chain instead of
        starting a new one. Same lexicographic-sort-of-ISO8601-keys
        reasoning as S3AuditSink.last_hash() - see that method's own
        docstring.
        """
        newest_key: Optional[str] = None

        for blob in self._client.list_blobs(self._bucket, prefix="audit/"):
            if newest_key is None or blob.name > newest_key:
                newest_key = blob.name

        if newest_key is None:
            return GENESIS_HASH

        body = self._bucket.blob(newest_key).download_as_bytes()
        return json.loads(body)["event_hash"]

    def read_all(self, prefix: str = "audit/") -> list[dict]:
        """Read every audit event under a prefix, in chronological order.
        Used by the chain verifier and by incident response scoping."""
        keys = [blob.name for blob in self._client.list_blobs(self._bucket, prefix=prefix)]

        events = []
        for key in sorted(keys):
            body = self._bucket.blob(key).download_as_bytes()
            events.append(json.loads(body))
        return events


class StdoutAuditSink:
    """Development sink. Prints events instead of persisting them.

    Not suitable for anything touching real PHI: nothing durable is
    written, so there is no audit trail to verify or investigate against.
    """

    def __call__(self, event: dict) -> None:
        print(json.dumps(event, sort_keys=True))

    def last_hash(self) -> str:
        return GENESIS_HASH
# Made by Ryan Gomez & Co. Inc.
