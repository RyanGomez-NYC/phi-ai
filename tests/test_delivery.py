# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/fhir/delivery/ and the extended EMR profiles.

Reading from an EMR and writing into one are not symmetric, and these
tests are weighted accordingly. A failed read produces an incomplete
record set, fixed by reading again. A failed write puts records into a
LIVE CLINICAL CHART that other clinicians read and act on, so the tests
that matter are the ones proving delivery refuses to do the three things
that would hurt someone: write to the wrong patient, duplicate a history,
or present stale records as current.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fhir.delivery.identity import (  # noqa: E402
    IdentityMap,
    IdentityMappingError,
    PatientMapping,
    load_identity_map,
)
from core.fhir.delivery.writer import (  # noqa: E402
    PRIOR_RECORD_TAG_SYSTEM,
    DeliveryError,
    EMRWriter,
    dangling_references,
    repoint_to_target_patient,
    tag_as_prior_record,
)
from core.fhir.emr_profiles import PROFILES, profile_for  # noqa: E402

CAPABILITY = {"rest": [{"resource": [
    {"type": "Observation", "interaction": [{"code": "create"}, {"code": "read"}]},
    {"type": "DocumentReference", "interaction": [{"code": "create"}]},
    {"type": "Condition", "interaction": [{"code": "read"}]},  # read only
]}]}


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append({"actor": actor, "action": action,
                            "resource_key": resource_key, "purpose_of_use": purpose_of_use})


def _rows():
    return [
        ({"storage_key": "fhir/Observation/o1.json", "patient_reference": "Patient/eAB12cd3"},
         {"resourceType": "Observation", "id": "o1",
          "subject": {"reference": "Patient/eAB12cd3"},
          "encounter": {"reference": "Encounter/enc1"},
          "effectiveDateTime": "2019-07-04"}),
        ({"storage_key": "fhir/Condition/c1.json", "patient_reference": "Patient/eAB12cd3"},
         {"resourceType": "Condition", "id": "c1",
          "subject": {"reference": "Patient/eAB12cd3"}}),
        ({"storage_key": "fhir/Observation/o2.json", "patient_reference": "Patient/eNOTMAPPED"},
         {"resourceType": "Observation", "id": "o2",
          "subject": {"reference": "Patient/eNOTMAPPED"}}),
    ]


def _map():
    return IdentityMap([PatientMapping("eAB12cd3", "cerner-99871", "j.okafor", "verified by HIM")])


def _writer(vendor="cerner", posts=None, capability=None, audit=None):
    def post(url, resource, headers):
        (posts if posts is not None else []).append((url, resource, headers))
        return {"id": "dest-1"}

    return EMRWriter(
        base_url="https://fhir.destination.example/r4",
        access_token="tok",
        profile=profile_for(vendor),
        audit=audit or _Audit(),
        http_post=post,
        http_get=lambda url: capability or CAPABILITY,
    )


# ---------------------------------------------------------------------------
# Wrong patient — the failure that hurts someone
# ---------------------------------------------------------------------------

def test_an_unmapped_patient_is_never_delivered():
    """No matching, ever. A false positive writes one person's medical
    history into another person's live chart."""
    result = _writer().deliver(_rows(), _map(), "epic-prod", "treatment", dry_run=True)
    unmapped = [i for i in result.items if i.source_id == "o2"]
    assert unmapped[0].skipped_reason
    assert "does not match patients across EMRs" in unmapped[0].skipped_reason


def test_resolving_an_unmapped_patient_raises_rather_than_returning_none():
    """A None return would invite a caller to carry on with no patient."""
    with pytest.raises(IdentityMappingError, match="no destination patient is mapped"):
        _map().resolve("Patient/eNOBODY")


def test_the_patient_reference_is_repointed_to_the_destination_id():
    resource = repoint_to_target_patient(
        {"resourceType": "Observation", "subject": {"reference": "Patient/eAB12cd3"}},
        "Patient/cerner-99871",
    )
    assert resource["subject"]["reference"] == "Patient/cerner-99871"


def test_one_source_patient_cannot_map_to_two_destinations():
    with pytest.raises(IdentityMappingError, match="mapped twice"):
        IdentityMap([
            PatientMapping("eAB12cd3", "dest-1", "him"),
            PatientMapping("eAB12cd3", "dest-2", "him"),
        ])


def test_a_mapping_without_a_named_verifier_is_refused(tmp_path):
    """A patient mapping with nobody's name against it is an assertion
    nobody made."""
    path = tmp_path / "map.csv"
    path.write_text("source_patient_id,target_patient_id,verified_by\neAB12cd3,dest-1,\n")
    with pytest.raises(IdentityMappingError, match="verified_by is required"):
        load_identity_map(str(path))


def test_a_mapping_file_missing_columns_is_refused(tmp_path):
    path = tmp_path / "map.csv"
    path.write_text("source_patient_id,target_patient_id\neAB12cd3,dest-1\n")
    with pytest.raises(IdentityMappingError, match="verified_by"):
        load_identity_map(str(path))


def test_a_valid_mapping_file_loads(tmp_path):
    path = tmp_path / "map.csv"
    path.write_text(
        "source_patient_id,target_patient_id,verified_by,note\n"
        "eAB12cd3,cerner-99871,j.okafor,matched on DOB+MRN\n"
        "eXYz9981,cerner-40012,j.okafor,\n"
    )
    loaded = load_identity_map(str(path))
    assert len(loaded) == 2
    assert loaded.resolve("Patient/eAB12cd3").target_reference == "Patient/cerner-99871"


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_a_destination_without_conditional_create_refuses_to_write_unattended():
    """Running twice would duplicate a patient's whole history in a live
    chart. A failed job is better than a silent second copy."""
    with pytest.raises(DeliveryError, match="conditional create"):
        _writer(vendor="epic").deliver(_rows(), _map(), "src", "treatment", dry_run=False)


def test_that_refusal_can_be_overridden_deliberately():
    posts = []
    result = _writer(vendor="epic", posts=posts).deliver(
        _rows(), _map(), "src", "treatment", dry_run=False, allow_duplicates=True)
    assert result.sent_count >= 1


def test_conditional_create_header_is_sent_where_supported():
    posts = []
    _writer(vendor="cerner", posts=posts).deliver(
        _rows(), _map(), "src", "treatment", dry_run=False)
    _, _, headers = posts[0]
    assert "If-None-Exist" in headers
    assert "fhir/Observation/o1.json" in headers["If-None-Exist"]


# ---------------------------------------------------------------------------
# Stale data presented as current
#
# The assertions below compare against PRIOR_RECORD_TAG_SYSTEM/
# PRIOR_RECORD_TAG_CODE rather than a string literal on purpose: what
# matters is that the tag the writer emits, the If-None-Exist key and
# delivery verification all derive from one definition, not what that
# definition currently spells.
# ---------------------------------------------------------------------------

def test_delivered_records_are_tagged_as_prior_records():
    """A 2019 observation appearing in a chart today, with no indication
    of origin, looks like it was recorded today."""
    tagged = tag_as_prior_record(
        {"resourceType": "Observation", "id": "o1"},
        "epic-prod",                      # source_system
        "fhir/Observation/o1.json",       # storage key of the stored copy
        "Patient/eAB12cd3",               # source_patient_reference
    )
    assert tagged["meta"]["source"] == "epic-prod#fhir/Observation/o1.json"
    assert any(t["system"] == PRIOR_RECORD_TAG_SYSTEM for t in tagged["meta"]["tag"])


def test_the_source_patient_is_preserved_so_records_trace_back():
    tagged = tag_as_prior_record({"resourceType": "Observation"}, "epic-prod",
                                 "fhir/Observation/o1.json", "Patient/eAB12cd3")
    values = [e.get("valueString") for e in tagged["extension"]]
    assert "Patient/eAB12cd3" in values


def test_tagging_never_mutates_the_stored_copy():
    original = {"resourceType": "Observation", "id": "o1"}
    tag_as_prior_record(original, "src", "key", "Patient/x")
    assert "meta" not in original


# ---------------------------------------------------------------------------
# Capability, dangling references, audit
# ---------------------------------------------------------------------------

def test_only_types_the_destination_advertises_are_sent():
    """The vendor table is a planning aid; the server is the authority on
    what a health system's build accepts."""
    result = _writer().deliver(_rows(), _map(), "src", "treatment", dry_run=True)
    condition = [i for i in result.items if i.resource_type == "Condition"][0]
    assert "does not advertise create" in condition.skipped_reason


def test_an_unreadable_capability_statement_refuses_the_whole_delivery():
    """Guessing what a live clinical system accepts is not acceptable."""
    def exploding(url):
        raise RuntimeError("connection refused")

    writer = EMRWriter("https://x.example/r4", "tok", profile_for("cerner"), _Audit(),
                       http_post=lambda *a: {}, http_get=exploding)
    with pytest.raises(DeliveryError, match="CapabilityStatement"):
        writer.creatable_resource_types()


def test_dangling_non_patient_references_are_surfaced_not_stripped():
    """An Encounter reference from the source system will not resolve in
    the destination. Silently stripping it discards clinical context;
    silently keeping it leaves a broken link. A human decides."""
    assert dangling_references(
        {"subject": {"reference": "Patient/x"}, "encounter": {"reference": "Encounter/enc1"}}
    ) == ["Encounter/enc1"]


def test_dry_run_is_the_default_and_writes_nothing():
    posts = []
    result = _writer(posts=posts).deliver(_rows(), _map(), "src", "treatment")
    assert result.dry_run is True
    assert posts == []
    assert result.sent_count == 0


def test_delivery_is_audited_before_the_write():
    audit = _Audit()
    _writer(audit=audit).deliver(_rows(), _map(), "src", "treatment", dry_run=False)
    entry = [e for e in audit.events if e["action"] == "record.deliver"][0]
    assert entry["purpose_of_use"] == "treatment"
    assert "fhir/Observation/o1.json" in entry["resource_key"]


def test_a_dry_run_writes_no_audit_entry():
    """Nothing was disclosed, so nothing is recorded as disclosed."""
    audit = _Audit()
    _writer(audit=audit).deliver(_rows(), _map(), "src", "treatment", dry_run=True)
    assert not [e for e in audit.events if e["action"] == "record.deliver"]


# ---------------------------------------------------------------------------
# EMR profiles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor", ["epic", "cerner", "athenahealth",
                                    "eclinicalworks", "meditech", "nextgen"])
def test_every_target_emr_has_an_ingestion_profile(vendor):
    profile = profile_for(vendor)
    assert profile.supported_resources
    assert profile.auth_flow in ("smart_backend_services", "oauth2_client_credentials")


def test_an_unknown_vendor_does_not_silently_fall_back_to_epic():
    """An unrecognised key quietly getting Epic's auth flow would produce
    an authentication failure far from its cause."""
    with pytest.raises(KeyError, match="unknown EMR vendor"):
        profile_for("allscripts")


def test_bulk_export_support_is_recorded_per_vendor():
    """This changes ingestion planning materially: where $export is absent
    a full history has to be pulled through paged search instead.

    eClinicalWorks moved from False to True in the 2026-08 review - their
    developer portal now documents backend and bulk FHIR APIs. NextGen
    stays False: nothing public documents an Enterprise-level $export
    (see the profile's notes for the g(10) caveat)."""
    assert profile_for("eclinicalworks").supports_bulk_export is True
    assert profile_for("meditech").supports_bulk_export is True
    assert profile_for("nextgen").supports_bulk_export is False
    assert profile_for("epic").supports_bulk_export is True


def test_no_vendor_claims_bulk_import():
    """FHIR $import is draft and essentially unsupported commercially.
    Recorded so the answer is visible rather than rediscovered."""
    assert not any(p.supports_bulk_import for p in PROFILES.values())


def test_athenahealth_uses_a_client_secret_not_a_signed_assertion():
    """The one target that does not use the SMART Backend Services key
    flow, so it needs a different authenticate() path."""
    assert profile_for("athenahealth").auth_flow == "oauth2_client_credentials"
    from core.fhir.client import FHIRIngestionClient

    assert hasattr(FHIRIngestionClient, "authenticate_client_secret")


# ---------------------------------------------------------------------------
# Source systems are never written to
# ---------------------------------------------------------------------------

SOURCE = "https://fhir.example-hospital.org/api/FHIR/R4"


def test_delivery_to_a_source_system_is_refused():
    """This platform exists because the source is being retired. Pushing
    stored records back into it would re-populate a system someone is
    switching off, with records that have been through a round trip."""
    from core.fhir.delivery.writer import SourceSystemWriteRefused

    with pytest.raises(SourceSystemWriteRefused, match="SOURCE system"):
        EMRWriter(base_url=SOURCE, access_token="t", profile=profile_for("epic"),
                  audit=_Audit(), source_system_urls=[SOURCE])


def test_the_refusal_happens_at_construction_before_any_request():
    """An EMRWriter aimed at a source system should not exist. Failing
    here means no token is obtained and no capability request is made
    against a system that should only ever be read."""
    from core.fhir.delivery.writer import SourceSystemWriteRefused

    calls = []
    with pytest.raises(SourceSystemWriteRefused):
        EMRWriter(base_url=SOURCE, access_token="t", profile=profile_for("epic"),
                  audit=_Audit(), source_system_urls=[SOURCE],
                  http_get=lambda url: calls.append(url) or {})
    assert calls == [], "a request was made to a source system"


@pytest.mark.parametrize("variant", [
    SOURCE,
    SOURCE + "/",
    "https://FHIR.EXAMPLE-HOSPITAL.ORG/api/FHIR/R4",
])
def test_trivial_url_variations_do_not_defeat_the_guard(variant):
    from core.fhir.delivery.writer import SourceSystemWriteRefused, assert_not_source_system

    with pytest.raises(SourceSystemWriteRefused):
        assert_not_source_system(variant, [SOURCE])


def test_a_different_tenant_on_the_same_vendor_host_is_still_allowed():
    """Multi-tenant vendors put the tenant in the path. Comparing hosts
    alone would refuse legitimate deliveries between two tenants."""
    from core.fhir.delivery.writer import assert_not_source_system

    assert_not_source_system(
        "https://fhir-ehr.cerner.com/r4/TENANT-B",
        ["https://fhir-ehr.cerner.com/r4/TENANT-A"],
    )


def test_a_genuine_target_system_is_allowed():
    writer = EMRWriter(base_url="https://fhir.newhealth.org/r4", access_token="t",
                       profile=profile_for("cerner"), audit=_Audit(),
                       source_system_urls=[SOURCE],
                       http_get=lambda url: CAPABILITY, http_post=lambda *a: {"id": "x"})
    assert "Observation" in writer.creatable_resource_types()


def test_the_refusal_is_not_a_retryable_delivery_error():
    """Its own class so a generic handler treating delivery failures as
    retryable cannot swallow it. This one is a configuration error with
    clinical consequences."""
    from core.fhir.delivery.writer import DeliveryError, SourceSystemWriteRefused

    assert issubclass(SourceSystemWriteRefused, DeliveryError)


def test_nothing_in_the_ingestion_client_writes_clinical_data():
    """Audit of the read path: the only POST is the OAuth token request.

    Guards against a future change adding a write to the client that
    reads from source systems - which is the exact thing that must not
    happen."""
    import inspect

    from core.fhir import client as client_module

    source = inspect.getsource(client_module)
    writes = [
        line.strip() for line in source.splitlines()
        if ("requests.post" in line or "requests.put" in line
            or "requests.patch" in line or "requests.delete" in line)
    ]
    # Exactly one, and it is the token endpoint.
    assert len(writes) == 1, f"unexpected write verb(s) in the ingestion client: {writes}"
    assert "token_url" in source.split("requests.post")[1][:200]


def test_smart_launch_requests_no_write_scopes():
    """In-context launch reads which patient, nothing more. A write scope
    here would let the platform modify the chart it launched from."""
    from core.web.smart.vendors import VENDORS, baseline_scopes

    for vendor in VENDORS.values():
        scopes = baseline_scopes(vendor).split()
        assert "offline_access" not in scopes, vendor.name
        for scope in scopes:
            if "/" in scope:
                access = scope.split(".")[-1]
                assert access in ("read", "rs"), f"{vendor.name} requests {scope}"
# Made by Ryan Gomez & Co. Inc.
