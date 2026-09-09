# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The audit sinks themselves - the code that writes every audit record.

WHY THIS FILE EXISTS. core/audit/sink.py was never imported by a single
test. Not "thinly covered" - never loaded, across the whole suite. What
the suite tested instead was a series of stand-ins: tests/test_web.py
passes `audit_sink=_Sink(events)`, tests/test_entrypoints.py
monkeypatches `build_audit_sink` to return a `_StubAuditSink`, and
tests/test_audit_log.py checks "the shape core/audit/sink.py's read_all()
WOULD hand to this check". Every one of those is a hand-written imitation
of this module, and an imitation cannot be wrong in the same way the
original is. The compliance story of this platform rests on the file
nothing ran.

So these tests construct the REAL sink classes. Only the cloud SDK client
is a stub, because the alternative is a live bucket - and the stub is
deliberately dumb: it records calls and returns them, it does not
reimplement any behaviour this module owns. Everything asserted below -
the key layout, the encryption parameters, the retention metadata, the
overwrite refusal, chain resumption - is this module's own logic.

WHAT IS NOT TESTED HERE, and cannot be: that AWS honours SSEKMSKeyId,
that Azure honours overwrite=False, that GCS honours if_generation_match.
Those are the providers' promises. What is testable is that this code
ASKS for them on every write, which is the part that has ever been the
bug.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import GENESIS_HASH  # noqa: E402
from core.audit.sink import (  # noqa: E402
    AzureBlobAuditSink,
    GCSAuditSink,
    S3AuditSink,
    StdoutAuditSink,
)

# A real serialized event, shaped as core/audit/log.py emits one. The
# timestamp and hash are what the key layout is derived from, so they are
# the only fields these tests care about.
EVENT = {
    "timestamp": "2026-09-08T14:23:05.123456+00:00",
    "event_hash": "abcdef0123456789" + "0" * 48,
    "prev_hash": GENESIS_HASH,
    "actor": "dr.chen",
    "action": "record.read",
    "resource_ref": "Patient/eXYZ",
    "purpose_of_use": "TREAT",
}
EXPECTED_KEY = "audit/2026/09/08/2026-09-08T14:23:05.123456+00:00-abcdef012345.json"


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

class _FakeS3:
    """Records calls. Reimplements nothing."""

    def __init__(self, objects=None):
        self.puts: list[dict] = []
        self.objects = objects or {}       # key -> body bytes

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):     # noqa: N803 - boto3's own spelling
        return {"Body": types.SimpleNamespace(read=lambda: self.objects[Key])}

    def get_paginator(self, _name):
        contents = [{"Key": k} for k in sorted(self.objects)]
        return types.SimpleNamespace(
            paginate=lambda **kw: [{"Contents": contents}] if contents else [{}]
        )


@pytest.fixture
def s3(monkeypatch):
    fake = _FakeS3()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, region_name=None: fake
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    return fake


def test_s3_writes_one_object_per_event_under_a_dated_key(s3):
    """The layout the module docstring promises: audit/YYYY/MM/DD/<ts>-<hash>.

    Single-event granularity is the whole tamper-detection design - drop
    one event and the chain breaks at exactly that point - so the key
    being per-event and chronologically sortable is load-bearing, not
    cosmetic.
    """
    S3AuditSink("audit-bucket", "us-east-1", "arn:aws:kms:key")(EVENT)

    assert len(s3.puts) == 1
    assert s3.puts[0]["Key"] == EXPECTED_KEY
    assert json.loads(s3.puts[0]["Body"]) == EVENT


def test_s3_asks_for_kms_encryption_on_every_write(s3):
    """Audit records are PHI-adjacent disclosure records. A write that
    forgets this succeeds, and is unencrypted."""
    S3AuditSink("audit-bucket", "us-east-1", "arn:aws:kms:the-key")(EVENT)

    put = s3.puts[0]
    assert put["ServerSideEncryption"] == "aws:kms"
    assert put["SSEKMSKeyId"] == "arn:aws:kms:the-key"
    assert put["ContentType"] == "application/json"


def test_s3_records_the_declared_retention_on_the_object(s3):
    """Declared, not enforced - the module docstring is explicit that
    nothing provisions Object Lock. The metadata is the only record that
    a retention was ever intended, so it has to be written."""
    S3AuditSink("audit-bucket", "us-east-1", "k", retention_days=10)(EVENT)
    assert "retain-until" in s3.puts[0]["Metadata"]


def test_s3_starts_from_genesis_when_the_bucket_is_empty(s3):
    assert S3AuditSink("audit-bucket", "us-east-1", "k").last_hash() == GENESIS_HASH


def test_s3_resumes_the_chain_from_the_newest_record(s3):
    """THE POINT OF last_hash(). If a restarting process started a fresh
    chain from GENESIS, deleting a whole run's worth of records would
    leave no evidence - every remaining chain would still verify."""
    sink = S3AuditSink("audit-bucket", "us-east-1", "k")
    sink(EVENT)
    later = dict(EVENT, timestamp="2026-09-08T19:00:00.000000+00:00",
                 event_hash="f" * 64, prev_hash=EVENT["event_hash"])
    sink(later)

    assert sink.last_hash() == later["event_hash"], (
        "last_hash must return the newest event, and audit keys sort "
        "chronologically because the timestamp is ISO-8601"
    )


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------

class _FakeBlobClient:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def upload_blob(self, body, overwrite=None, content_settings=None):
        self.store.uploads.append(
            {"key": self.key, "body": body, "overwrite": overwrite}
        )


class _FakeContainer:
    def __init__(self):
        self.uploads: list[dict] = []

    def get_blob_client(self, key):
        return _FakeBlobClient(self, key)


@pytest.fixture
def azure(monkeypatch):
    container = _FakeContainer()
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda *a, **k: object()
    blob = types.ModuleType("azure.storage.blob")
    blob.ContentSettings = lambda content_type=None: {"content_type": content_type}
    blob.BlobServiceClient = lambda account_url=None, credential=None: types.SimpleNamespace(
        get_container_client=lambda name: container
    )
    for name, mod in (("azure", types.ModuleType("azure")),
                      ("azure.identity", identity),
                      ("azure.storage", types.ModuleType("azure.storage")),
                      ("azure.storage.blob", blob)):
        monkeypatch.setitem(sys.modules, name, mod)
    return container


def test_azure_uses_the_same_key_layout_as_every_other_sink(azure):
    """One layout across three clouds, or the verifier needs three."""
    AzureBlobAuditSink("https://acct.blob.core.windows.net", "audit")(EVENT)
    assert azure.uploads[0]["key"] == EXPECTED_KEY


def test_azure_refuses_to_overwrite_an_existing_audit_record(azure):
    """overwrite=False. Each audit key is unique by construction, so a
    collision is a real bug - a duplicate timestamp and hash prefix -
    and papering over it with overwrite=True would silently destroy the
    record already there."""
    AzureBlobAuditSink("https://acct.blob.core.windows.net", "audit")(EVENT)
    assert azure.uploads[0]["overwrite"] is False


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------

class _FakeBlob:
    def __init__(self, store, key, kms_key_name):
        self.store, self.key, self.kms_key_name = store, key, kms_key_name

    def upload_from_string(self, body, content_type=None, if_generation_match=None):
        self.store.uploads.append({
            "key": self.key, "body": body, "kms": self.kms_key_name,
            "if_generation_match": if_generation_match,
        })


class _FakeBucket:
    def __init__(self):
        self.uploads: list[dict] = []

    def blob(self, key, kms_key_name=None):
        return _FakeBlob(self, key, kms_key_name)


@pytest.fixture
def gcs(monkeypatch):
    bucket = _FakeBucket()
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = lambda project=None: types.SimpleNamespace(
        bucket=lambda name: bucket
    )
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    google.cloud = cloud
    for name, mod in (("google", google), ("google.cloud", cloud),
                      ("google.cloud.storage", storage)):
        monkeypatch.setitem(sys.modules, name, mod)
    return bucket


def test_gcs_uses_the_same_key_layout(gcs):
    GCSAuditSink("audit-bucket", "a-project")(EVENT)
    assert gcs.uploads[0]["key"] == EXPECTED_KEY


def test_gcs_refuses_to_overwrite_an_existing_audit_record(gcs):
    """if_generation_match=0 is GCS's "only if no generation exists" -
    the same refusal Azure gets from overwrite=False."""
    GCSAuditSink("audit-bucket", "a-project")(EVENT)
    assert gcs.uploads[0]["if_generation_match"] == 0


def test_gcs_passes_the_cmek_key_through_when_one_is_configured(gcs):
    GCSAuditSink("audit-bucket", "a-project", kms_key_name="projects/p/keys/k")(EVENT)
    assert gcs.uploads[0]["kms"] == "projects/p/keys/k"


# ---------------------------------------------------------------------------
# The development sink
# ---------------------------------------------------------------------------

def test_the_stdout_sink_persists_nothing_and_says_so_by_returning_genesis(capsys):
    """It is a development sink and its last_hash is honest about it:
    GENESIS every time, because there is nothing to resume from."""
    sink = StdoutAuditSink()
    sink(EVENT)
    assert json.loads(capsys.readouterr().out) == EVENT
    assert sink.last_hash() == GENESIS_HASH


# ---------------------------------------------------------------------------
# Across all three
# ---------------------------------------------------------------------------

def test_all_three_cloud_sinks_agree_on_the_key(s3, azure, gcs):
    """core/audit/verify.py walks one layout. Three sinks that disagree
    about where a record goes is three audit logs, one of which the
    verifier can read."""
    S3AuditSink("b", "us-east-1", "k")(EVENT)
    AzureBlobAuditSink("https://acct.blob.core.windows.net", "audit")(EVENT)
    GCSAuditSink("b", "p")(EVENT)

    assert s3.puts[0]["Key"] == azure.uploads[0]["key"] == gcs.uploads[0]["key"]
# Made by Ryan Gomez & Co. Inc.
