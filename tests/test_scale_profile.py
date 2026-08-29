# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for scale profiles and the two storage layouts.

The profile decides the shape of the whole deployment, so these weight
two things: that the layouts produce the object counts they claim, and
that the profile cannot be changed casually on a populated deployment
without that being visible.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config.scale_profile import (  # noqa: E402
    LARGE,
    LARGE_PROFILE_THRESHOLD,
    SMALL,
    IndexPartitioning,
    StorageLayout,
    profile_from_env,
    warn_if_undersized,
)
from core.storage.layout import (  # noqa: E402
    LayoutError,
    group_for_bundling,
    locate,
    parse_bundle,
    serialise_bundle,
)


def _resource(rtype, rid, patient="eAB12cd3"):
    r = {"resourceType": rtype, "id": rid}
    if patient:
        r["subject"] = {"reference": f"Patient/{patient}"}
    return r


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

def test_small_is_the_default(monkeypatch):
    monkeypatch.delenv("PHI_AI_PROFILE", raising=False)
    assert profile_from_env() is SMALL


def test_an_unknown_profile_is_refused_rather_than_defaulted(monkeypatch):
    """Silently falling back to small on a large deployment would produce
    the object explosion the profile exists to prevent."""
    monkeypatch.setenv("PHI_AI_PROFILE", "medium")
    with pytest.raises(ValueError, match="not valid"):
        profile_from_env()


def test_the_profiles_differ_in_both_dimensions():
    assert SMALL.storage_layout is StorageLayout.PER_RESOURCE
    assert SMALL.index_partitioning is IndexPartitioning.NONE
    assert LARGE.storage_layout is StorageLayout.BUNDLED
    assert LARGE.index_partitioning is IndexPartitioning.BY_RESOURCE_TYPE


def test_each_profile_selects_its_own_schema():
    assert SMALL.schema_file.endswith("schema.sql")
    assert LARGE.schema_file.endswith("schema_partitioned.sql")
    for profile in (SMALL, LARGE):
        assert Path(profile.schema_file).is_file(), profile.schema_file


def test_an_oversized_small_deployment_is_warned_about():
    """The failure is gradual - reconciliation simply takes longer until
    it stops completing - so it needs surfacing before then."""
    assert warn_if_undersized(SMALL, LARGE_PROFILE_THRESHOLD + 1)
    assert warn_if_undersized(SMALL, 1_000) is None
    assert warn_if_undersized(LARGE, LARGE_PROFILE_THRESHOLD * 10) is None


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

def test_per_resource_gives_one_object_per_resource():
    location = locate(_resource("Observation", "o1"), SMALL)
    assert location.storage_key == "fhir/Observation/o1.json"
    assert not location.bundled
    assert location.content_type == "application/fhir+json"


def test_bundled_groups_by_patient_and_type():
    location = locate(_resource("Observation", "o1", patient="eAB12cd3"), LARGE)
    assert location.storage_key == "fhir/Observation/eAB12cd3.ndjson"
    assert location.bundled
    assert location.content_type == "application/fhir+ndjson"


def test_bundling_separates_resource_types():
    """Retention is configurable per type. A patient-only bundle would mix
    retention periods inside one object, and disposal could express
    neither."""
    grouped = group_for_bundling([
        _resource("Observation", "o1"),
        _resource("Condition", "c1"),
    ], LARGE)
    assert len(grouped) == 2


def test_bundling_collapses_object_count_within_a_patient_and_type():
    """The whole point: 500 resources become one object."""
    resources = [_resource("Observation", f"o{i}") for i in range(500)]

    per_resource = group_for_bundling(resources, SMALL)
    bundled = group_for_bundling(resources, LARGE)

    assert len(per_resource) == 500
    assert len(bundled) == 1
    assert len(next(iter(bundled.values()))) == 500


def test_resources_without_a_patient_are_stored_individually_not_dropped():
    """Rare, and losing them to make the layout uniform would be the wrong
    trade."""
    location = locate({"resourceType": "Consent", "id": "c1"}, LARGE)
    assert location.storage_key == "fhir/Consent/_unlinked/c1.json"
    assert not location.bundled


def test_both_layouts_present_the_same_interface_to_callers():
    """Under PER_RESOURCE every group holds exactly one, so a write site
    needs one code path rather than a branch on the profile."""
    for profile in (SMALL, LARGE):
        grouped = group_for_bundling([_resource("Observation", "o1")], profile)
        assert sum(len(v) for v in grouped.values()) == 1


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "", "has space", "x" * 65])
def test_ids_that_could_escape_the_key_namespace_are_refused(bad):
    """A resource id comes from an EMR and is untrusted. One containing a
    slash would write outside its prefix - in a bucket, one patient's
    bundle over another's."""
    with pytest.raises(LayoutError):
        locate({"resourceType": "Observation", "id": bad}, SMALL)


def test_a_malicious_patient_id_cannot_redirect_a_bundle():
    with pytest.raises(LayoutError):
        locate({"resourceType": "Observation", "id": "o1",
                "subject": {"reference": "Patient/../../evil"}}, LARGE)


# ---------------------------------------------------------------------------
# NDJSON bundles
# ---------------------------------------------------------------------------

def test_a_bundle_round_trips():
    resources = [_resource("Observation", f"o{i}") for i in range(5)]
    assert list(parse_bundle(serialise_bundle(resources))) == resources


def test_serialisation_is_deterministic():
    """Without stable ordering, re-serialising an unchanged bundle would
    look like a content change to integrity verification."""
    resources = [_resource("Observation", "o1"), _resource("Observation", "o2")]
    assert serialise_bundle(resources) == serialise_bundle(resources)


def test_a_bundle_is_valid_ndjson():
    payload = serialise_bundle([_resource("Observation", f"o{i}") for i in range(3)])
    lines = payload.decode().strip().split("\n")
    assert len(lines) == 3
    import json
    assert all(json.loads(line)["resourceType"] == "Observation" for line in lines)


def test_one_malformed_line_does_not_lose_the_whole_bundle():
    """A bad line must not make a patient's entire record unreadable."""
    good = serialise_bundle([_resource("Observation", "o1")])
    corrupted = good[:-1] + b"\n{not valid json\n" + serialise_bundle(
        [_resource("Observation", "o2")])
    recovered = list(parse_bundle(corrupted))
    assert {r["id"] for r in recovered} == {"o1", "o2"}


def _realistic_observation(i):
    """A FHIR Observation with the structure a real EMR emits.

    An earlier version of this test used a stripped-down 188-byte
    resource, which made the bundle look far smaller than production and
    the cold-tier assertion fail for the wrong reason. Real resources
    carry meta, coding arrays, references and reference ranges."""
    return {
        "resourceType": "Observation", "id": f"obs-{i:08d}",
        "meta": {"versionId": "1", "lastUpdated": "2020-03-15T10:04:31.000Z",
                 "source": "#aBcDeFgH12345678"},
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs", "display": "Vital Signs"}]}],
        "code": {"coding": [
            {"system": "http://loinc.org", "code": "85354-9",
             "display": "Blood pressure panel with all children optional"}],
            "text": "Blood pressure"},
        "subject": {"reference": "Patient/eAB12cd3", "display": "SYNTHETIC, TEST"},
        "encounter": {"reference": "Encounter/eSynEnc0001"},
        "effectiveDateTime": "2020-03-15T10:00:00Z",
        "issued": "2020-03-15T10:04:31.000Z",
        "performer": [{"reference": "Practitioner/ePrac0001",
                       "display": "SYNTHETIC CLINICIAN"}],
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6",
                                  "display": "Systolic blood pressure"}]},
             "valueQuantity": {"value": 126, "unit": "mmHg",
                               "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4",
                                  "display": "Diastolic blood pressure"}]},
             "valueQuantity": {"value": 78, "unit": "mmHg",
                               "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
        ],
    }


def test_a_realistic_bundle_clears_the_cold_tier_minimum():
    """Cold tiers bill a 128 KB MINIMUM PER OBJECT, so a bundle must
    exceed it or lifecycle transitions cost more than they save - which is
    exactly why they are off by default on the small profile.

    ~130 realistic resources clears it. A patient with a real clinical
    history has far more than that per type, so bundles land comfortably
    above the floor - but this is a property of resource SIZE, not of
    bundling itself, and a bundle of very sparse resources would not."""
    assert len(serialise_bundle([_realistic_observation(i) for i in range(130)])) > 128 * 1024


def test_the_cold_tier_benefit_is_not_automatic_for_tiny_bundles():
    """Stated as a test so it is not discovered as a surprise cost: a
    patient with three sparse resources of one type produces an object
    well under the billing floor."""
    assert len(serialise_bundle([_realistic_observation(i) for i in range(3)])) < 128 * 1024


# ---------------------------------------------------------------------------
# End to end: both profiles store and read back through the real client
# ---------------------------------------------------------------------------

def _client_for(profile):
    """A real FHIRIngestionClient with in-memory storage and fake KMS."""
    import base64
    import os

    from core.audit.log import AuditLog
    from core.crypto.envelope import EnvelopeEncryptor, KeyManagementService
    from core.fhir.client import FHIRIngestionClient
    from core.fhir.emr_profiles import EPIC
    from core.storage.base import ObjectStore, StoredObjectMetadata

    class _KMS(KeyManagementService):
        def generate_data_key(self):
            dek = os.urandom(32)
            return dek, base64.b64encode(dek).decode()

        def unwrap_data_key(self, wrapped):
            return base64.b64decode(wrapped)

    class _Storage(ObjectStore):
        def __init__(self):
            self.objects = {}

        def put_object(self, key, ciphertext, wrapped_dek_b64, sha256_hex,
                       retention_until=None, content_type="application/octet-stream"):
            self.objects[key] = (ciphertext, wrapped_dek_b64, sha256_hex, content_type)
            return StoredObjectMetadata(
                key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=sha256_hex,
                stored_at=self._utcnow(), retention_until=retention_until,
                wrapped_dek_b64=wrapped_dek_b64, content_type=content_type)

        def get_object(self, key, version_id=None):
            return self.objects[key][0]

        def get_metadata(self, key, version_id=None):
            ciphertext, wrapped, digest, content_type = self.objects[key]
            return StoredObjectMetadata(
                key=key, version_id="v1", size_bytes=len(ciphertext), sha256_hex=digest,
                stored_at=self._utcnow(), retention_until=None,
                wrapped_dek_b64=wrapped, content_type=content_type)

        def object_exists(self, key):
            return key in self.objects

        def list_keys(self, prefix=""):
            return [k for k in self.objects if k.startswith(prefix)]

    class _Audit(AuditLog):
        def __init__(self):
            self.records = []

        def record(self, actor, action, resource_key, purpose_of_use=None):
            self.records.append((action, resource_key))

    storage = _Storage()
    encryptor = EnvelopeEncryptor(_KMS())
    client = FHIRIngestionClient(
        base_url="https://example.org", profile=EPIC, storage=storage,
        encryptor=encryptor, audit=_Audit(), retention_years=10,
        profile_config=profile,
    )
    return client, storage, encryptor


def _reader_for(storage, encryptor):
    from core.web.data import LiveRecordReader

    return LiveRecordReader(connection_factory=None, storage=storage,
                            encryptor=encryptor, audit_sink=None)


def _fake_source(client, per_type):
    """Make iter_resources yield a fixed synthetic set."""
    def iter_resources(resource_type, since=None):
        for i in range(per_type):
            yield {"resourceType": resource_type, "id": f"{resource_type[:3].lower()}{i}",
                   "subject": {"reference": "Patient/eAB12cd3"}}
    client.iter_resources = iter_resources


def test_small_profile_writes_one_object_per_resource_end_to_end():
    client, storage, encryptor = _client_for(SMALL)
    _fake_source(client, 20)

    results = client.store_all(["Observation"])

    assert len(results) == 20
    assert len(storage.objects) == 20
    assert all(k.endswith(".json") for k in storage.objects)

    reader = _reader_for(storage, encryptor)
    key = next(iter(storage.objects))
    assert reader.read_resource(key)["resourceType"] == "Observation"
    assert len(reader.read_resources(key)) == 1


def test_large_profile_writes_one_bundle_and_reads_every_resource_back():
    """THE property the large profile exists for, proven end to end
    through the real client, encryption and reader."""
    client, storage, encryptor = _client_for(LARGE)
    _fake_source(client, 20)

    results = client.store_all(["Observation"])

    assert len(storage.objects) == 1, "resources were not bundled"
    assert len(results) == 1
    key = next(iter(storage.objects))
    assert key == "fhir/Observation/eAB12cd3.ndjson"

    reader = _reader_for(storage, encryptor)
    recovered = reader.read_resources(key)
    assert len(recovered) == 20, "bundle did not round-trip every resource"
    assert {r["id"] for r in recovered} == {f"obs{i}" for i in range(20)}


def test_a_bundle_read_as_a_single_resource_is_honest_about_being_a_bundle():
    """Returning the first record would be silently wrong."""
    client, storage, encryptor = _client_for(LARGE)
    _fake_source(client, 5)
    client.store_all(["Observation"])

    reader = _reader_for(storage, encryptor)
    wrapper = reader.read_resource(next(iter(storage.objects)))
    assert wrapper["resourceType"] == "Bundle"
    assert wrapper["total"] == 5


def test_the_two_profiles_produce_the_same_resources_from_the_same_source():
    """Different object counts, identical clinical content - which is the
    whole claim."""
    def stored_resources(profile):
        client, storage, encryptor = _client_for(profile)
        _fake_source(client, 12)
        client.store_all(["Observation"])
        reader = _reader_for(storage, encryptor)
        out = []
        for key in sorted(storage.objects):
            out.extend(reader.read_resources(key))
        return sorted(out, key=lambda r: r["id"]), len(storage.objects)

    small_resources, small_objects = stored_resources(SMALL)
    large_resources, large_objects = stored_resources(LARGE)

    assert small_resources == large_resources
    assert small_objects == 12 and large_objects == 1


def test_bundling_keeps_resource_types_in_separate_objects_end_to_end():
    client, storage, encryptor = _client_for(LARGE)
    _fake_source(client, 6)
    client.store_all(["Observation", "Condition"])

    assert sorted(storage.objects) == [
        "fhir/Condition/eAB12cd3.ndjson",
        "fhir/Observation/eAB12cd3.ndjson",
    ]
# Made by Ryan Gomez & Co. Inc.
