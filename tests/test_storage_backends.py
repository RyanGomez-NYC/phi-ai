# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The three object stores - the code every byte of PHI is written through.

WHY THIS FILE EXISTS. core/storage/aws_s3.py, azure_blob.py and gcp_gcs.py
were never imported by any test. All three. The stores that hold every
encrypted record this platform keeps were exercised by nothing, and what
stood in for them was core/storage/memory.py plus a scattering of stubs -
none of which can reproduce a mistake made in these files.

THE INVARIANT THAT MATTERS MOST, and the reason this file leads with it:
THE WRAPPED DEK TRAVELS WITH THE OBJECT. Each record is encrypted with its
own data key, and that key - wrapped by KMS - is stored in the object's
metadata and nowhere else. A backend that writes it under one name and
reads it back under another does not fail. put_object succeeds, the bytes
land, and the object is unrecoverable: get_metadata returns
wrapped_dek_b64="" and the record can never be decrypted again. Nothing
raises, nothing logs, and the loss is discovered whenever somebody next
tries to read that record - which for an archive is years later.

AND THE THREE SPELLINGS ARE NOT THE SAME, deliberately. S3 and GCS use
`wrapped-dek` and `retain-until`; Azure uses `wrapped_dek` and
`retain_until`, because Azure blob metadata keys must be valid C#
identifiers. That difference is correct and it is a trap: it is exactly
the kind of inconsistency somebody tidies up in put_object without
touching get_metadata. So every test below writes through the REAL
backend and reads back through the REAL backend, and asserts the pair
agrees - never that either uses a particular spelling.

The cloud SDK client is the only stub, and it stores what it was handed
and returns it. It reimplements nothing this module owns.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.storage.aws_s3 import S3Storage  # noqa: E402
from core.storage.azure_blob import AzureBlobStorage  # noqa: E402
from core.storage.gcp_gcs import GCSStorage  # noqa: E402

KEY = "records/Patient/eXYZ/Observation/o-1.enc"
CIPHERTEXT = b"\x00\x01not-really-encrypted-but-opaque\xff"
WRAPPED_DEK = "QUFBQUJCQkJDQ0NDRERERA=="   # base64, as KMS returns it
SHA256 = "9f" * 32
RETAIN_UNTIL = datetime(2032, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

class _ClientError(Exception):
    """boto3's ClientError shape: the code lives in .response['Error']['Code']."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    #: S3Storage.object_exists reaches for client.exceptions.ClientError,
    #: which is how botocore hangs its exception classes off the client.
    exceptions = types.SimpleNamespace(ClientError=_ClientError)

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.puts: list[dict] = []

    def put_object(self, Bucket, Key, Body, **extra):   # noqa: N803 - boto3 spelling
        self.puts.append(dict(extra, Bucket=Bucket, Key=Key, Body=Body))
        self.objects[Key] = {"Body": Body, **extra}
        return {"VersionId": "v1"}

    def head_object(self, Bucket, Key, VersionId=None):  # noqa: N803
        if Key not in self.objects:
            raise _ClientError("404")
        o = self.objects[Key]
        return {
            "Metadata": o.get("Metadata", {}),
            "ContentLength": len(o["Body"]),
            "LastModified": datetime.now(timezone.utc),
            "ContentType": o.get("ContentType", "application/octet-stream"),
            "VersionId": "v1",
        }

    def get_object(self, Bucket, Key, VersionId=None):   # noqa: N803
        body = self.objects[Key]["Body"]
        return {"Body": types.SimpleNamespace(read=lambda: body)}


@pytest.fixture
def s3(monkeypatch):
    client = _FakeS3Client()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, region_name=None: client
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    return client


def test_s3_round_trips_the_wrapped_dek(s3):
    """Write then read through the real backend. If these two disagree
    about the metadata name, the record is gone forever."""
    store = S3Storage("phi-bucket", "us-east-1", kms_key_id="arn:aws:kms:k")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=RETAIN_UNTIL)

    read = store.get_metadata(KEY)
    assert read.wrapped_dek_b64 == WRAPPED_DEK, "the data key did not survive the round trip"
    assert read.sha256_hex == SHA256
    assert read.retention_until == RETAIN_UNTIL
    assert store.get_object(KEY) == CIPHERTEXT


def test_s3_asks_for_kms_encryption(s3):
    store = S3Storage("phi-bucket", "us-east-1", kms_key_id="arn:aws:kms:the-key")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256)
    assert s3.puts[0]["ServerSideEncryption"] == "aws:kms"
    assert s3.puts[0]["SSEKMSKeyId"] == "arn:aws:kms:the-key"


def test_s3_omits_the_retention_metadata_when_there_is_no_retention(s3):
    """An absent retention must be absent, not the string 'None'."""
    store = S3Storage("phi-bucket", "us-east-1")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=None)
    assert "retain-until" not in s3.puts[0]["Metadata"]
    assert store.get_metadata(KEY).retention_until is None


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------

class _AzureBlob:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def upload_blob(self, data, overwrite=None, metadata=None, content_settings=None):
        self.store.blobs[self.key] = {
            "data": data, "metadata": metadata or {},
            "content_type": getattr(content_settings, "content_type", None),
        }

    def get_blob_properties(self, version_id=None):
        b = self.store.blobs[self.key]
        return types.SimpleNamespace(
            metadata=b["metadata"], size=len(b["data"]), version_id="v1",
            creation_time=datetime.now(timezone.utc),
            content_settings=types.SimpleNamespace(content_type=b["content_type"]),
        )

    def download_blob(self, version_id=None):
        data = self.store.blobs[self.key]["data"]
        return types.SimpleNamespace(readall=lambda: data)

    def exists(self):
        return self.key in self.store.blobs


class _AzureContainer:
    def __init__(self):
        self.blobs: dict[str, dict] = {}

    def get_blob_client(self, key):
        return _AzureBlob(self, key)


@pytest.fixture
def azure(monkeypatch):
    container = _AzureContainer()
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda *a, **k: object()
    blob = types.ModuleType("azure.storage.blob")
    blob.ContentSettings = lambda content_type=None: types.SimpleNamespace(
        content_type=content_type)
    blob.BlobServiceClient = lambda account_url=None, credential=None: types.SimpleNamespace(
        get_container_client=lambda name: container)
    for name, mod in (("azure", types.ModuleType("azure")),
                      ("azure.identity", identity),
                      ("azure.storage", types.ModuleType("azure.storage")),
                      ("azure.storage.blob", blob)):
        monkeypatch.setitem(sys.modules, name, mod)
    return container


def test_azure_round_trips_the_wrapped_dek(azure):
    """Azure spells the metadata keys with underscores because blob
    metadata keys must be valid C# identifiers. What matters is not which
    spelling it uses but that put and get use the SAME one."""
    store = AzureBlobStorage("https://acct.blob.core.windows.net", "phi")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=RETAIN_UNTIL)

    read = store.get_metadata(KEY)
    assert read.wrapped_dek_b64 == WRAPPED_DEK, "the data key did not survive the round trip"
    assert read.sha256_hex == SHA256
    assert read.retention_until == RETAIN_UNTIL
    assert store.get_object(KEY) == CIPHERTEXT


def test_azure_metadata_keys_are_valid_identifiers(azure):
    """The reason for the underscores, asserted so a later tidy-up to
    hyphens fails here rather than at the Azure API."""
    store = AzureBlobStorage("https://acct.blob.core.windows.net", "phi")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=RETAIN_UNTIL)
    for name in azure.blobs[KEY]["metadata"]:
        assert name.isidentifier(), f"{name!r} is not a valid Azure metadata key"


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------

class _GCSBlob:
    def __init__(self, store, key, kms_key_name=None, generation=None):
        self.store, self.name, self.kms_key_name = store, key, kms_key_name
        self.generation, self.metadata = generation or 1, None
        self.size, self.content_type = None, None
        self.time_created = datetime.now(timezone.utc)

    def upload_from_string(self, data, content_type=None):
        self.store.blobs[self.name] = {
            "data": data, "metadata": self.metadata or {},
            "content_type": content_type, "kms": self.kms_key_name,
        }

    def reload(self):
        b = self.store.blobs[self.name]
        self.metadata, self.size = b["metadata"], len(b["data"])
        self.content_type = b["content_type"]

    def download_as_bytes(self):
        return self.store.blobs[self.name]["data"]

    def exists(self):
        return self.name in self.store.blobs


class _GCSBucket:
    def __init__(self):
        self.blobs: dict[str, dict] = {}

    def blob(self, key, kms_key_name=None, generation=None):
        return _GCSBlob(self, key, kms_key_name, generation)


@pytest.fixture
def gcs(monkeypatch):
    bucket = _GCSBucket()
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = lambda project=None: types.SimpleNamespace(
        bucket=lambda name: bucket)
    google, cloud = types.ModuleType("google"), types.ModuleType("google.cloud")
    cloud.storage, google.cloud = storage, cloud
    for name, mod in (("google", google), ("google.cloud", cloud),
                      ("google.cloud.storage", storage)):
        monkeypatch.setitem(sys.modules, name, mod)
    return bucket


def test_gcs_round_trips_the_wrapped_dek(gcs):
    store = GCSStorage("phi-bucket", "a-project")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=RETAIN_UNTIL)

    read = store.get_metadata(KEY)
    assert read.wrapped_dek_b64 == WRAPPED_DEK, "the data key did not survive the round trip"
    assert read.sha256_hex == SHA256
    assert read.retention_until == RETAIN_UNTIL
    assert store.get_object(KEY) == CIPHERTEXT


def test_gcs_passes_the_cmek_key_through(gcs):
    store = GCSStorage("phi-bucket", "a-project", kms_key_name="projects/p/keys/k")
    store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256)
    assert gcs.blobs[KEY]["kms"] == "projects/p/keys/k"


# ---------------------------------------------------------------------------
# All three
# ---------------------------------------------------------------------------

def test_every_backend_returns_the_same_metadata_for_the_same_write(s3, azure, gcs):
    """Three clouds, one contract. core/fhir/ and core/db/ read
    StoredObjectMetadata without knowing which store produced it, so a
    field one backend fills and another leaves empty is a bug that only
    appears after a cloud migration."""
    stores = [
        S3Storage("b", "us-east-1", kms_key_id="k"),
        AzureBlobStorage("https://acct.blob.core.windows.net", "b"),
        GCSStorage("b", "p"),
    ]
    seen = []
    for store in stores:
        store.put_object(KEY, CIPHERTEXT, WRAPPED_DEK, SHA256, retention_until=RETAIN_UNTIL)
        m = store.get_metadata(KEY)
        seen.append((m.key, m.wrapped_dek_b64, m.sha256_hex, m.size_bytes, m.retention_until))

    assert len(set(seen)) == 1, f"the backends disagree: {seen}"


def test_s3_does_not_report_an_access_failure_as_a_missing_object(s3):
    """A 403 is not a 404, and treating it as one is how a record that is
    merely UNREADABLE registers as "already gone" - which, on a purge or a
    reconciliation path, means deleting the index row for a record that is
    still there. Only the genuine absence codes resolve to False;
    everything else is re-raised so the caller sees the real failure."""
    store = S3Storage("b", "us-east-1")

    def denied(**kw):
        raise _ClientError("403")

    s3.head_object = denied
    with pytest.raises(_ClientError):
        store.object_exists("records/forbidden")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_s3_treats_every_genuine_absence_code_as_absent(s3, code):
    store = S3Storage("b", "us-east-1")

    def missing(**kw):
        raise _ClientError(code)

    s3.head_object = missing
    assert store.object_exists("records/gone") is False


def test_every_backend_reports_a_missing_object_as_missing(s3, azure, gcs):
    for store in (S3Storage("b", "us-east-1"),
                  AzureBlobStorage("https://acct.blob.core.windows.net", "b"),
                  GCSStorage("b", "p")):
        assert store.object_exists("records/never/written") is False
# Made by Ryan Gomez & Co. Inc.
