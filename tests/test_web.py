# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/web/.

Weighted toward the security boundary rather than rendering. The three
properties worth guarding are that an unauthenticated request cannot read
PHI, that an authorized read cannot happen without an audit entry, and
that the auditor role cannot read clinical content. Everything else is
presentation.

Uses a fake RecordReader so no Postgres, S3 or KMS is needed.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.web.app import create_app  # noqa: E402
from core.web.auth import (  # noqa: E402
    AuthConfigurationError,
    AuthSettings,
    Identity,
    Role,
    validate_purpose,
)
from core.web.data import PlatformStats  # noqa: E402


class _FakeReader:
    def __init__(self):
        self.reads = []

    def stats(self):
        return PlatformStats(
            total_resources=3, resource_type_counts={"Patient": 1, "Observation": 2},
            distinct_patients=1,
            earliest_stored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            latest_stored_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

    def search_patients(self, term, limit=50):
        if "eAB" not in term:
            return []
        # "last_stored" is the search query's own result alias, and it is
        # what core/web/data.py's LiveRecordReader.search_patients emits
        # today: "... MAX(stored_at) AS last_stored FROM stored_resources".
        # core/assistant/tools.py's find_patients tool already reads that
        # key. A fake must mirror the thing it stands in for, so this key
        # tracks the reader, not the template. patients.html renders
        # r.last_stored and patient.html renders r.stored_at, so the
        # template, the reader and this fake all agree.
        return [{"patient_reference": "Patient/eAB12cd3", "resource_count": 3,
                 "last_stored": datetime(2026, 8, 1, tzinfo=timezone.utc)}]

    def resources_for_patient(self, patient_reference):
        return [{"resource_type": "Observation", "resource_id": "obs1",
                 "storage_key": "fhir/Observation/obs1.json", "sha256_hex": "a" * 64,
                 "stored_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                 "retention_until": datetime(2036, 8, 1, tzinfo=timezone.utc)}]

    def resource_index_row(self, storage_key):
        if storage_key != "fhir/Observation/obs1.json":
            return None
        return {"resource_type": "Observation", "resource_id": "obs1",
                "patient_reference": "Patient/eAB12cd3", "storage_key": storage_key,
                "storage_version_id": "v1", "sha256_hex": "a" * 64,
                "stored_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "retention_until": datetime(2036, 8, 1, tzinfo=timezone.utc)}

    def read_resource(self, storage_key):
        self.reads.append(storage_key)
        return {"resourceType": "Observation", "id": "obs1",
                "subject": {"reference": "Patient/eAB12cd3"}}

    def read_audit_events(self, limit=200, actor=None):
        return [{"timestamp": "2026-08-18T10:00:00+00:00", "actor": "alice",
                 "action": "record.read", "resource_key": "fhir/Observation/obs1.json",
                 "purpose_of_use": "treatment"}]

    def verify_audit_chain(self):
        return (True, 12, None)

    def expiring_resources(self, within_days=90):
        # NON-EMPTY on purpose. An earlier version of this fake returned
        # [], so the retention template's row loop never executed under
        # test and a crash on every real result went unnoticed until the
        # page was opened in a browser.
        return [
            {"resource_type": "Observation", "resource_id": "obs-past",
             "patient_reference": "Patient/eAB12cd3",
             "storage_key": "fhir/Observation/obs-past.json",
             "retention_until": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"resource_type": "Encounter", "resource_id": "enc-future",
             "patient_reference": "Patient/eXYz9981",
             "storage_key": "fhir/Encounter/enc-future.json",
             "retention_until": datetime(2036, 1, 1, tzinfo=timezone.utc)},
        ]


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append(
            {"actor": actor, "action": action, "resource_key": resource_key,
             "purpose_of_use": purpose_of_use}
        )


def _client(roles="him", audit=None, **app_kwargs):
    settings = AuthSettings(trust_proxy_headers=False, dev_identity=f"tester:{roles}")
    reader = _FakeReader()
    audit = audit if audit is not None else _RecordingAudit()
    app = create_app(reader=reader, auth_settings=settings, audit=audit, **app_kwargs)
    # https base_url so the Secure session cookie is actually stored -
    # without it the CSRF token cannot round-trip and every POST 403s.
    return TestClient(app, base_url="https://records.example.org"), reader, audit


def _csrf(client, path="/patients"):
    """Fetch a page to establish a session and read its CSRF token.

    The realistic flow: a browser loads a form before submitting it. Tests
    that POST without doing this get 403, which is the protection working.
    """
    body = client.get(path).text
    match = re.search(r'name="csrf-token" content="([^"]+)"', body) or re.search(
        r'name="csrf_token" value="([^"]+)"', body
    )
    assert match, f"no CSRF token rendered on {path}"
    return match.group(1)


def _post(client, path, data=None, form_path="/patients"):
    payload = dict(data or {})
    payload["csrf_token"] = _csrf(client, form_path)
    return client.post(path, data=payload)


# ---------------------------------------------------------------------------
# Configuration refuses to guess
# ---------------------------------------------------------------------------

def test_startup_refuses_without_an_explicit_deployment_shape(monkeypatch):
    """No default for header trust. Defaulting to trusting them makes an
    accidental direct exposure catastrophic; defaulting to not trusting
    them breaks correctly-proxied deployments, which invites turning the
    check off entirely."""
    monkeypatch.delenv("PHI_AI_WEB_TRUST_PROXY_AUTH", raising=False)
    monkeypatch.delenv("PHI_AI_WEB_DEV_IDENTITY", raising=False)
    with pytest.raises(AuthConfigurationError, match="TRUST_PROXY_AUTH"):
        AuthSettings.from_env()


def test_dev_identity_cannot_coexist_with_proxy_trust(monkeypatch):
    """The dev identity bypasses authentication; leaving it available in
    a proxied deployment would silently defeat the proxy."""
    monkeypatch.setenv("PHI_AI_WEB_TRUST_PROXY_AUTH", "true")
    monkeypatch.setenv("PHI_AI_WEB_DEV_IDENTITY", "someone:admin")
    with pytest.raises(AuthConfigurationError, match="both set"):
        AuthSettings.from_env()


def test_unauthenticated_request_is_refused_when_proxy_is_trusted():
    """With no proxy header present, there is no identity - and PHI is
    not served to nobody in particular."""
    settings = AuthSettings(trust_proxy_headers=True)
    app = create_app(reader=_FakeReader(), auth_settings=settings, audit=_RecordingAudit())
    assert TestClient(app).get("/patients").status_code == 401


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def test_auditor_cannot_read_clinical_content():
    """An auditor is NOT a viewer with extras. They verify the trail; they
    have no business reading the records it describes."""
    client, _, _ = _client(roles="auditor")
    assert _post(client, "/patients", {"term": "eAB"}, form_path="/audit").status_code == 403
    assert client.get("/audit").status_code == 200


def test_viewer_cannot_ingest_documents_or_read_the_audit_trail():
    client, _, _ = _client(roles="viewer")
    assert client.get("/documents").status_code == 403
    assert client.get("/audit").status_code == 403


def test_user_with_no_mapped_role_gets_nothing():
    """An authenticated user whose IdP groups map to no role must not
    fall back to a default. Defaulting to viewer would mean any
    successful SSO login could read PHI."""
    client, _, _ = _client(roles="")
    # No page renders a token for this user, so post raw - the 403 under
    # test is the authorization one either way.
    assert client.post("/patients", data={"term": "eAB"}).status_code == 403


def test_denied_requests_are_audited():
    """A pattern of refusals is a security signal; a trail recording only
    successes cannot show one."""
    audit = _RecordingAudit()
    client, _, _ = _client(roles="viewer", audit=audit)
    client.get("/audit")
    assert any(e["action"] == "access.denied" for e in audit.events)


# ---------------------------------------------------------------------------
# Audit coverage of PHI access - the property that makes the rest defensible
# ---------------------------------------------------------------------------

def test_reading_a_resource_writes_an_audit_entry_with_actor_and_purpose():
    audit = _RecordingAudit()
    client, _, _ = _client(audit=audit)
    r = _post(client, "/resource", {"storage_key": "fhir/Observation/obs1.json",
                                    "purpose_of_use": "operations"})
    assert r.status_code == 200
    entry = [e for e in audit.events if e["action"] == "record.read"][0]
    assert entry["actor"] == "tester"
    assert entry["purpose_of_use"] == "operations"
    assert entry["resource_key"] == "fhir/Observation/obs1.json"


def test_clinical_content_is_not_served_when_auditing_is_unavailable():
    """Serving PHI without recording the access would produce exactly the
    undetectable read the audit trail exists to prevent. The request fails
    instead."""
    settings = AuthSettings(trust_proxy_headers=False, dev_identity="tester:him")
    reader = _FakeReader()
    app = create_app(reader=reader, auth_settings=settings, audit=None)
    client = TestClient(app, base_url="https://records.example.org")
    token = _csrf(client)
    r = client.post("/resource", data={"storage_key": "fhir/Observation/obs1.json",
                                       "purpose_of_use": "operations",
                                       "csrf_token": token})
    assert r.status_code == 503
    assert reader.reads == [], "resource was decrypted despite the audit failure"


@pytest.mark.parametrize("bad", ["", "curiosity", "OTHER", "treatment; drop table"])
def test_reads_require_a_declared_purpose_of_use(bad):
    """Free text is refused. A purpose that is only ever written and never
    compared is not auditable, and 'other' collects everything."""
    from core.web.auth import NotAuthorized

    with pytest.raises(NotAuthorized):
        validate_purpose(bad)


def test_patient_search_uses_post_so_identifiers_stay_out_of_urls():
    """GET would put the identifier in proxy access logs, browser history
    and referrer headers."""
    client, _, _ = _client()
    assert client.get("/patients", params={"term": "eAB"}).status_code == 200  # form only
    assert _post(client, "/patients", {"term": "eAB"}).status_code == 200


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_dashboard_renders_and_shows_chain_status():
    client, _, _ = _client(roles="him,auditor")
    body = client.get("/").text
    assert "Platform holdings" in body
    assert "verified intact" in body


def test_search_results_render():
    client, _, _ = _client()
    body = _post(client, "/patients", {"term": "eAB"}).text
    assert "Patient/eAB12cd3" in body


def test_resource_view_marks_preliminary_ocr_for_review():
    """Low-confidence OCR must be visibly distinguished from verified
    text, not rendered identically."""
    settings = AuthSettings(trust_proxy_headers=False, dev_identity="tester:him")

    class _Preliminary(_FakeReader):
        def read_resource(self, storage_key):
            return {"resourceType": "DocumentReference", "id": "doc1",
                    "docStatus": "preliminary",
                    "subject": {"reference": "Patient/eAB12cd3"}, "content": []}

    app = create_app(reader=_Preliminary(), auth_settings=settings, audit=_RecordingAudit())
    client = TestClient(app, base_url="https://records.example.org")
    body = client.post("/resource", data={"storage_key": "fhir/Observation/obs1.json",
                                          "purpose_of_use": "operations",
                                          "csrf_token": _csrf(client)}).text
    assert "Preliminary" in body and "not been verified" in body


def test_healthz_is_open_and_leaks_nothing():
    settings = AuthSettings(trust_proxy_headers=True)
    app = create_app(reader=_FakeReader(), auth_settings=settings, audit=_RecordingAudit())
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_api_and_ui_share_the_same_authorization():
    client, _, _ = _client(roles="viewer")
    assert client.get("/api/audit/verify").status_code == 403
    client2, _, _ = _client(roles="auditor")
    assert client2.get("/api/audit/verify").json()["intact"] is True


# ---------------------------------------------------------------------------
# Chain verification must not reimplement the walk
# ---------------------------------------------------------------------------

def _event(actor, prev_hash, timestamp):
    from core.audit.log import AuditEvent

    return AuditEvent(
        actor=actor, action="record.read", resource_key="k",
        purpose_of_use="treatment", timestamp=timestamp, prev_hash=prev_hash,
    ).to_dict()


class _Sink:
    def __init__(self, events):
        self._events = events

    def read_all(self, prefix="audit/"):
        return self._events


def _reader_over(events):
    from core.web.data import LiveRecordReader

    return LiveRecordReader(None, None, None, audit_sink=_Sink(events))


def test_concurrent_writer_fork_is_not_reported_as_tampering():
    """Two writers extending the same parent is legitimate, not a breach.

    The first version of verify_audit_chain() walked the chain linearly
    and would have called this tampering - showing INTEGRITY FAILURE on a
    healthy platform and sending an operator to the incident response
    runbook for nothing. It now delegates to AuditLog.diagnose_chain(),
    which distinguishes a fork from corruption. Do not reimplement that
    walk in the web layer."""
    from core.audit.log import GENESIS_HASH

    root = _event("alice", GENESIS_HASH, "2026-08-18T10:00:00+00:00")
    events = [
        root,
        _event("alice", root["event_hash"], "2026-08-18T10:00:01+00:00"),
        _event("bob", root["event_hash"], "2026-08-18T10:00:02+00:00"),
    ]
    intact, checked, problem = _reader_over(events).verify_audit_chain()
    assert intact is True, f"healthy fork reported as tampering: {problem}"
    assert checked == 3


def test_a_modified_event_is_still_reported_as_tampering():
    """Tolerating forks must not have cost real detection."""
    from core.audit.log import GENESIS_HASH

    root = _event("alice", GENESIS_HASH, "2026-08-18T10:00:00+00:00")
    child = _event("alice", root["event_hash"], "2026-08-18T10:00:01+00:00")
    tampered = dict(child, actor="mallory")  # hash no longer recomputes

    intact, _, problem = _reader_over([root, tampered]).verify_audit_chain()
    assert intact is False
    assert "modified" in problem


# ---------------------------------------------------------------------------
# CSRF — a prerequisite for EHR embedding, not an optional extra
# ---------------------------------------------------------------------------

def test_a_post_without_a_csrf_token_is_refused():
    """Embedding requires SameSite=None, which is what SameSite=Lax was
    doing for CSRF. Tokens replace it."""
    client, _, _ = _client()
    client.get("/patients")  # establish the session
    assert client.post("/patients", data={"term": "eAB"}).status_code == 403


def test_a_post_with_another_sessions_token_is_refused():
    client_a, _, _ = _client()
    client_b, _, _ = _client()
    stolen = _csrf(client_a)
    client_b.get("/patients")
    assert client_b.post("/patients", data={"term": "eAB", "csrf_token": stolen}
                         ).status_code == 403


def test_state_changing_roi_routes_are_csrf_protected():
    """The routes that matter: ingestion and release both write."""
    client, _, _ = _client()
    client.get("/patients")
    for path in ("/documents", "/roi"):
        assert client.post(path, data={}).status_code == 403


def test_safe_methods_are_not_blocked():
    """A GET that changes state is a bug this check would only paper
    over, so GET is exempt by design."""
    client, _, _ = _client(roles="him,auditor")
    assert client.get("/").status_code == 200
    assert client.get("/audit").status_code == 200


def test_every_page_carries_a_token_for_its_forms():
    client, _, _ = _client(roles="him,auditor,disposition")
    for path in ("/", "/patients", "/documents", "/audit", "/retention"):
        assert 'name="csrf-token"' in client.get(path).text, path


def test_retention_page_renders_rows_and_distinguishes_elapsed_from_upcoming():
    """Regression: the template compared a timestamp against an undefined
    `now`, raising on every non-empty result. It went unnoticed because
    the fake returned an empty list, so the row loop never ran."""
    client, _, _ = _client(roles="disposition")
    response = client.get("/retention")

    assert response.status_code == 200
    assert "obs-past" in response.text and "enc-future" in response.text
    assert "eligible for disposal" in response.text  # retention elapsed
    assert "expiring" in response.text               # still in future


# ---------------------------------------------------------------------------
# Closed gaps
# ---------------------------------------------------------------------------

def test_an_idle_session_expires_independently_of_the_cookie():
    """Cookie max-age expresses "old", not "idle". A clinical workstation
    left unattended with a chart open is the realistic exposure."""
    import time as _time

    import core.web.app as app_module

    client, _, _ = _client()
    client.get("/patients")  # establishes last_seen

    # 20 minutes: longer than the 15-minute idle limit, but SHORTER than
    # the session cookie's own 30-minute max-age. Jump past that and
    # itsdangerous rejects the signature as stale, the app receives an
    # EMPTY session, last_seen is gone, and the idle check never runs -
    # the cookie expired instead. The idle check only operates inside the
    # cookie's lifetime, which is why idle < max-age has to hold.
    real_time = app_module.time.time
    try:
        app_module.time.time = lambda: real_time() + 20 * 60
        assert client.get("/patients").status_code == 440
    finally:
        app_module.time.time = real_time


def test_the_source_document_route_refuses_arbitrary_storage_keys():
    """It decrypts and returns raw bytes, so it must not become a general
    object-fetch endpoint."""
    client, _, _ = _client()
    response = _post(client, "/document/source",
                     {"storage_key": "fhir/Patient/eAB12cd3.json",
                      "purpose_of_use": "operations"})
    assert response.status_code == 400


def test_the_source_document_route_requires_a_purpose_of_use():
    client, _, _ = _client()
    response = _post(client, "/document/source",
                     {"storage_key": "documents/source/doc-1.pdf",
                      "purpose_of_use": "curiosity"})
    assert response.status_code in (400, 403)


@pytest.mark.parametrize("bad_key", [
    # Right prefix, but the rest is not a real doc-<32 hex>.<ext> key. The
    # final segment is echoed into a Content-Disposition filename, so a
    # prefix-only check let these through into a response header.
    'documents/source/doc-0011223344556677889900aabbccddee.pdf"; x=1',
    "documents/source/doc-0011223344556677889900aabbccddee.pdf\r\nX-Evil: 1",
    "documents/source/../../etc/passwd",
    "documents/source/doc-not-hex.pdf",
    "documents/source/doc-0011223344556677889900aabbccddee.exe",
])
def test_the_source_document_route_validates_the_whole_key_shape(bad_key):
    """Only documents/source/doc-<32 hex>.<ext> is accepted. A key that
    merely starts with the prefix but carries header-injection characters
    or an unexpected shape is refused before anything is decrypted."""
    client, _, _ = _client()
    response = _post(client, "/document/source",
                     {"storage_key": bad_key, "purpose_of_use": "operations"})
    assert response.status_code == 400


def test_imaging_open_validates_the_study_uid():
    """The study UID is interpolated into the viewer redirect URL, so a
    value that is not a DICOM UID (dotted digits) is refused before it can
    inject query parameters or reflect unencoded into the Location
    header - the same _valid_uid check every /dicomweb route applies."""
    client, _, _ = _client(roles="him")
    response = _post(
        client, "/imaging/open",
        {"study_instance_uid": "1.2.3&StudyInstanceUIDs=9.9.9", "purpose_of_use": "operations"},
    )
    assert response.status_code == 400


def test_a_purpose_outside_the_role_is_refused_before_any_read():
    """The role dictates what a user can assert: HIM does records custody,
    not treatment, so a posted treatment purpose refuses - and nothing is
    decrypted on the refused request."""
    audit = _RecordingAudit()
    client, reader, _ = _client(audit=audit)
    r = _post(client, "/resource", {"storage_key": "fhir/Observation/obs1.json",
                                    "purpose_of_use": "treatment"})
    assert r.status_code in (400, 403)
    assert reader.reads == [], "resource was decrypted despite the refused purpose"
# Made by Ryan Gomez & Co. Inc.
