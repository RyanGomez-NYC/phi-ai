# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/web/smart/ — SMART on FHIR in-context EHR launch.

Weighted heavily toward the security boundary. An EHR launch establishes
WHO a user is from parameters that arrive on an untrusted redirect, so
the tests that matter are the ones proving the flow refuses to trust the
wrong things: an unregistered issuer, an unverifiable id_token, a replayed
or expired state.

No live EMR: HTTP and JWKS are injected, exactly as core/fhir/client.py
takes its collaborators.
"""

import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.web.smart.launch import (  # noqa: E402
    IssuerNotAllowed,
    RegisteredIssuer,
    SMARTError,
    SMARTLaunchService,
    make_pkce,
    normalise_issuer,
)
from core.web.smart.vendors import VENDORS as SMART_VENDORS  # noqa: E402
from core.web.smart.vendors import VENDORS, baseline_scopes  # noqa: E402

ISSUER = "https://fhir.example-hospital.org/api/FHIR/R4"
REDIRECT = "https://records.example-hospital.org/smart/callback"

DISCOVERY = {
    "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
    "token_endpoint": f"{ISSUER}/oauth2/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
}


def _registered(**overrides):
    # record_source is the field name on core/web/smart/launch.py's
    # RegisteredIssuer AND the key an operator writes in the issuer file
    # (core/web/smart/config.py reads entry["record_source"], and
    # config/smart_issuers.example.yaml spells it that way) - one name
    # end to end, with no second spelling accepted anywhere.
    kwargs = dict(issuer=ISSUER, vendor_key="epic", client_id="client-123",
                  client_secret="shh", roles=("viewer",), record_source=True)
    kwargs.update(overrides)
    return RegisteredIssuer(**kwargs)


def _service(issuers=None, token=None, get=None, jwks=None):
    token_response = token if token is not None else {
        "access_token": "at", "patient": "eAB12cd3", "encounter": "enc1",
    }
    return SMARTLaunchService(
        issuers=issuers if issuers is not None else [_registered()],
        redirect_uri=REDIRECT,
        http_get=get or (lambda url: DISCOVERY),
        http_post=lambda url, data, auth: token_response,
        jwks_client_factory=jwks or (lambda uri: None),
    )


# ---------------------------------------------------------------------------
# The issuer allowlist — the control everything else depends on
# ---------------------------------------------------------------------------

def test_an_unregistered_issuer_is_refused():
    """THE critical test. `iss` arrives on an untrusted redirect and names
    the server to trust for authentication. Without an allowlist, a
    crafted launch link would have this app fetch an attacker's discovery
    document and accept their token as proof of identity."""
    with pytest.raises(IssuerNotAllowed):
        _service().begin("https://attacker.example.com/fhir", "launch-token")


def test_a_plain_http_issuer_is_refused():
    """An http issuer lets a network attacker rewrite the discovery
    document, which is the trust anchor for the whole flow."""
    with pytest.raises(SMARTError, match="must be https"):
        normalise_issuer("http://fhir.example-hospital.org/api/FHIR/R4")


def test_issuer_matching_ignores_only_case_and_trailing_slash():
    """Conservative on purpose: path and port distinguish genuinely
    different servers and must not be normalised away."""
    assert normalise_issuer(ISSUER + "/") == normalise_issuer(ISSUER.upper().replace(
        "HTTPS", "https").replace("/API/FHIR/R4", "/api/FHIR/R4"))
    assert normalise_issuer(ISSUER) != normalise_issuer(ISSUER + "/other")
    assert normalise_issuer(ISSUER) != normalise_issuer(
        ISSUER.replace("fhir.example-hospital.org", "fhir.example-hospital.org:8443"))


def test_a_launch_with_no_token_is_refused():
    """This endpoint is opened by an EMR, not browsed to directly."""
    with pytest.raises(SMARTError, match="launch token"):
        _service().begin(ISSUER, "")


# ---------------------------------------------------------------------------
# The authorization request
# ---------------------------------------------------------------------------

def _authorize_params(service=None):
    url = (service or _service()).begin(ISSUER, "launch-token-abc")
    return parse_qs(urlparse(url).query)


def test_authorization_request_carries_pkce_state_nonce_and_aud():
    params = _authorize_params()
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] and params["state"] and params["nonce"]
    # aud must be the FHIR server: without it a token issued for one
    # audience can be replayed at another.
    assert params["aud"] == [ISSUER]
    assert params["launch"] == ["launch-token-abc"]
    assert params["redirect_uri"] == [REDIRECT]


def test_pkce_challenge_is_the_s256_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = make_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge == expected


def test_launch_scope_is_requested_so_the_emr_resolves_patient_context():
    """Without `launch`, the user would be asked to pick a patient - which
    defeats the entire purpose of an in-context launch."""
    assert "launch" in _authorize_params()["scope"][0].split()


def test_only_patient_read_is_requested_and_no_offline_access():
    """This platform never writes to the EMR and reads no clinical data
    from it - it only needs to know WHICH patient. Broad scopes would
    expand what a compromise of this app yields, for nothing."""
    scopes = set(_authorize_params()["scope"][0].split())
    assert "offline_access" not in scopes
    assert not any(s.startswith("user/") or s.startswith("system/") for s in scopes)
    assert not any("write" in s or ".w" in s.split(".")[-1] for s in scopes)


@pytest.mark.parametrize("vendor_key", sorted(k for k in SMART_VENDORS if k != "generic"))
def test_every_named_emr_produces_a_usable_authorization_request(vendor_key):
    """Every vendor with its own dialect entry in core/web/smart/vendors.py
    (derived from that registry, never hand-listed) implements SMART App
    Launch - which is why this is one implementation rather than one per
    vendor; everything else launches through `generic`."""
    service = _service(issuers=[_registered(vendor_key=vendor_key)])
    params = _authorize_params(service)
    scopes = params["scope"][0].split()
    assert "launch" in scopes and "fhirUser" in scopes
    assert any(s.startswith("patient/Patient.") for s in scopes)


def test_smart_v2_and_v1_scope_dialects_differ():
    """Asking a v1-only server for a v2 scope is rejected outright by some
    servers rather than downgraded."""
    assert "patient/Patient.rs" in baseline_scopes(VENDORS["epic"])
    assert "patient/Patient.read" in baseline_scopes(VENDORS["nextgen"])


# ---------------------------------------------------------------------------
# Completing the launch
# ---------------------------------------------------------------------------

def test_completed_launch_returns_the_patient_context():
    service = _service()
    state = _authorize_params(service)["state"][0]
    context = service.complete(state=state, code="auth-code")
    assert context.patient_id == "eAB12cd3"
    assert context.patient_reference == "Patient/eAB12cd3"
    assert context.roles == frozenset({"viewer"})


def test_a_state_cannot_be_replayed():
    """Launches are single-use; a replayed state would let a captured
    callback URL be redeemed twice."""
    service = _service()
    state = _authorize_params(service)["state"][0]
    service.complete(state=state, code="auth-code")
    with pytest.raises(SMARTError, match="unknown or already-used"):
        service.complete(state=state, code="auth-code")


def test_an_unknown_state_is_refused():
    with pytest.raises(SMARTError, match="unknown or already-used"):
        _service().complete(state="never-issued", code="auth-code")


def test_an_expired_launch_is_refused():
    service = _service()
    state = _authorize_params(service)["state"][0]
    service._pending[state].created_at = time.time() - 3600
    with pytest.raises(SMARTError, match="expired"):
        service.complete(state=state, code="auth-code")


def test_a_token_response_without_an_access_token_is_refused():
    service = _service(token={"error": "invalid_grant"})
    state = _authorize_params(service)["state"][0]
    with pytest.raises(SMARTError, match="no access token"):
        service.complete(state=state, code="auth-code")


def test_an_unverifiable_id_token_fails_the_launch():
    """An id_token read without signature verification is an assertion by
    whoever handed it over - worthless in a flow whose entire purpose is
    establishing identity. It must fail the launch, not degrade to an
    unnamed user."""
    def exploding_jwks(uri):
        raise RuntimeError("jwks unreachable")

    service = _service(
        token={"access_token": "at", "patient": "eAB12cd3", "id_token": "not.a.real.jwt"},
        jwks=exploding_jwks,
    )
    state = _authorize_params(service)["state"][0]
    with pytest.raises(SMARTError, match="could not verify the id_token"):
        service.complete(state=state, code="auth-code")


def _signed_id_token(private_key, *, nonce, aud="client-123"):
    """A real RS256-signed id_token, so the nonce path is exercised end to
    end rather than mocked. `nonce=None` omits the claim entirely."""
    import time as _time

    import jwt

    now = int(_time.time())
    claims = {"iss": ISSUER, "sub": "user-1", "aud": aud, "iat": now,
              "exp": now + 300, "preferred_username": "dr.who"}
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, private_key, algorithm="RS256")


def _launch_ready_for_id_token():
    """Return (service, state, sent_nonce, token_response). The token
    response is a mutable dict so a test can drop in an id_token signed
    with whatever nonce it wants AFTER learning the nonce this launch
    actually sent."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    class _SigningKey:
        key = public_key

    class _JWKSClient:
        def get_signing_key_from_jwt(self, raw):
            return _SigningKey()

    token_response = {"access_token": "at", "patient": "eAB12cd3"}
    service = _service(token=token_response, jwks=lambda uri: _JWKSClient())
    state = _authorize_params(service)["state"][0]
    sent_nonce = service._pending[state].nonce
    return service, state, sent_nonce, token_response, private_key


def test_a_matching_id_token_nonce_identifies_the_clinician():
    service, state, sent_nonce, token_response, key = _launch_ready_for_id_token()
    token_response["id_token"] = _signed_id_token(key, nonce=sent_nonce)
    context = service.complete(state=state, code="auth-code")
    assert context.username == "dr.who"


def test_an_id_token_with_a_mismatched_nonce_is_refused():
    """The nonce binds the id_token to THIS launch. A token minted for a
    different launch - or replayed - carries a different nonce and must be
    refused, not accepted."""
    service, state, sent_nonce, token_response, key = _launch_ready_for_id_token()
    token_response["id_token"] = _signed_id_token(key, nonce="a-different-launch")
    with pytest.raises(SMARTError, match="nonce"):
        service.complete(state=state, code="auth-code")


def test_an_id_token_with_no_nonce_is_refused():
    """We sent a nonce, so per OIDC the id_token must carry it back. A
    token that simply omits the claim used to pass (the check was skipped
    when absent); it is now a verification failure - that omission is
    exactly how a replayed token would look."""
    service, state, sent_nonce, token_response, key = _launch_ready_for_id_token()
    token_response["id_token"] = _signed_id_token(key, nonce=None)
    with pytest.raises(SMARTError, match="nonce"):
        service.complete(state=state, code="auth-code")


def test_a_launch_with_no_patient_context_still_authenticates():
    """Not fatal - the user is signed in and can search - but they must
    not be landed on a confidently-wrong record."""
    service = _service(token={"access_token": "at"})
    state = _authorize_params(service)["state"][0]
    context = service.complete(state=state, code="auth-code")
    assert context.patient_id is None
    assert context.patient_reference is None


def test_record_source_false_is_carried_into_the_context():
    """Patient ids are opaque and instance-specific. An id from an EMR
    this platform's records did not come from resolves to nothing, and
    the user must be told rather than shown an empty record."""
    service = _service(issuers=[_registered(record_source=False)])
    state = _authorize_params(service)["state"][0]
    context = service.complete(state=state, code="auth-code")
    assert context.patient_id == "eAB12cd3"
    assert context.record_source is False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_a_server_without_smart_endpoints_is_reported_clearly():
    service = _service(get=lambda url: {"authorization_endpoint": ""})
    with pytest.raises(SMARTError, match="may not support SMART"):
        service.begin(ISSUER, "launch-token")


def test_discovery_is_cached_so_every_launch_is_not_a_round_trip():
    calls = []

    def counting_get(url):
        calls.append(url)
        return DISCOVERY

    service = _service(get=counting_get)
    service.begin(ISSUER, "t1")
    service.begin(ISSUER, "t2")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_missing_issuer_file_disables_launch_rather_than_allowing_everything():
    from core.web.smart.config import load_issuers

    assert load_issuers("config/does-not-exist.yaml") == []


def test_a_dangling_secret_reference_is_refused(monkeypatch, tmp_path):
    """Silently proceeding with no secret would produce confusing token
    endpoint failures much later."""
    from core.web.smart.config import load_issuers

    path = tmp_path / "issuers.yaml"
    path.write_text(
        "issuers:\n"
        f"  - issuer: {ISSUER}\n"
        "    vendor: epic\n"
        "    client_id: abc\n"
        "    client_secret: env:NOT_SET_ANYWHERE\n"
    )
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(SMARTError, match="NOT_SET_ANYWHERE"):
        load_issuers(str(path))


def test_an_issuer_missing_required_fields_is_refused(tmp_path):
    from core.web.smart.config import load_issuers

    path = tmp_path / "issuers.yaml"
    path.write_text(f"issuers:\n  - issuer: {ISSUER}\n    vendor: epic\n")
    with pytest.raises(SMARTError, match="client_id"):
        load_issuers(str(path))


# ---------------------------------------------------------------------------
# End to end through the HTTP routes, against a fake EMR
# ---------------------------------------------------------------------------

def _app_with_smart(record_source=True, token=None):
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings
    from test_web import _FakeReader, _RecordingAudit

    audit = _RecordingAudit()
    app = create_app(
        reader=_FakeReader(),
        auth_settings=AuthSettings(trust_proxy_headers=True),
        audit=audit,
        session_secret_key="test-secret-not-for-production",
    )
    app.state.smart = _service(
        issuers=[_registered(record_source=record_source)], token=token
    )
    # base_url https so the https_only session cookie is actually set
    return TestClient(app, base_url="https://records.example-hospital.org"), app, audit


def test_full_launch_lands_the_clinician_on_the_patient_record():
    """The whole point: a clinician in a patient's chart clicks through
    and arrives signed in, on that same patient."""
    client, app, audit = _app_with_smart()

    launch = client.get("/smart/launch", params={"iss": ISSUER, "launch": "tok"},
                        follow_redirects=False)
    assert launch.status_code == 302
    state = parse_qs(urlparse(launch.headers["location"]).query)["state"][0]

    callback = client.get("/smart/callback", params={"code": "c", "state": state},
                          follow_redirects=False)
    assert callback.status_code == 302
    # Encounter context is carried through: the clinician launched from a
    # specific visit and lands on it.
    assert callback.headers["location"] == "/smart/patient/eAB12cd3?encounter=enc1"

    # The session now carries the identity, with no proxy header present.
    landed = client.get("/smart/patient/eAB12cd3")
    assert landed.status_code == 200
    assert "Patient/eAB12cd3" in landed.text
    assert "Opened in context" in landed.text

    actions = [e["action"] for e in audit.events]
    assert "auth.smart.launch" in actions
    # record.read.patient, matching core/web/app.py verbatim.
    read = [e for e in audit.events if e["action"] == "record.read.patient"][0]
    assert read["purpose_of_use"] == "treatment"


def test_an_unregistered_issuer_gets_403_not_a_redirect():
    """403, not 400: a refusal to TRUST, and the distinction matters when
    reading logs for a crafted-launch attempt."""
    client, _, _ = _app_with_smart()
    response = client.get("/smart/launch",
                          params={"iss": "https://attacker.example.com/fhir", "launch": "t"},
                          follow_redirects=False)
    assert response.status_code == 403


def test_a_launch_from_a_non_source_emr_explains_instead_of_showing_nothing():
    """A patient id from an EMR this platform's records did not come from
    resolves to nothing. Showing an empty record would read as 'this
    patient has no history', which is false and clinically misleading."""
    client, _, _ = _app_with_smart(record_source=False)

    launch = client.get("/smart/launch", params={"iss": ISSUER, "launch": "tok"},
                        follow_redirects=False)
    state = parse_qs(urlparse(launch.headers["location"]).query)["state"][0]
    callback = client.get("/smart/callback", params={"code": "c", "state": state})

    assert callback.status_code == 200
    assert "did not come from" in callback.text
    assert "no patient context" in callback.text.lower()


def test_an_emr_refusal_is_surfaced_not_swallowed():
    client, _, _ = _app_with_smart()
    response = client.get("/smart/callback",
                          params={"error": "access_denied",
                                  "error_description": "user cancelled"})
    assert response.status_code == 400
    assert "access_denied" in response.text


def test_smart_routes_report_clearly_when_launch_is_not_configured():
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings
    from test_web import _FakeReader, _RecordingAudit

    app = create_app(reader=_FakeReader(),
                     auth_settings=AuthSettings(trust_proxy_headers=True),
                     audit=_RecordingAudit())
    response = TestClient(app).get("/smart/launch",
                                   params={"iss": ISSUER, "launch": "t"},
                                   follow_redirects=False)
    assert response.status_code == 503
    assert "smart_issuers" in response.text


# ---------------------------------------------------------------------------
# Landing in the encounter
# ---------------------------------------------------------------------------

def _encounter_app(token=None, embedded=False):
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings
    from test_web import _RecordingAudit

    RESOURCES = {
        "fhir/Encounter/enc1.json": {"resourceType": "Encounter", "id": "enc1"},
        "fhir/Observation/in.json": {"resourceType": "Observation", "id": "in",
                                     "encounter": {"reference": "Encounter/enc1"}},
        "fhir/Observation/out.json": {"resourceType": "Observation", "id": "out",
                                      "encounter": {"reference": "Encounter/other"}},
        "fhir/Observation/none.json": {"resourceType": "Observation", "id": "none"},
    }

    class _Reader:
        def stats(self):
            from core.web.data import PlatformStats
            return PlatformStats(0, {}, 0, None, None)

        def search_patients(self, term, limit=50):
            return []

        def resources_for_patient(self, patient_reference):
            # stored_at, matching what LiveRecordReader.resources_for_patient
            # actually selects from stored_resources now.
            return [{"resource_type": r["resourceType"], "resource_id": r["id"],
                     "storage_key": k, "sha256_hex": "a" * 64,
                     "stored_at": None, "retention_until": None}
                    for k, r in RESOURCES.items()]

        def resource_index_row(self, storage_key):
            return None

        def read_resource(self, storage_key):
            return RESOURCES[storage_key]

        def verify_object_integrity(self, storage_key):
            return True

        def read_audit_events(self, limit=200, actor=None):
            return []

        def verify_audit_chain(self):
            return (True, 0, None)

        def expiring_resources(self, within_days=90):
            return []

    registered = _registered(embedded=embedded)
    app = create_app(
        reader=_Reader(),
        auth_settings=AuthSettings(trust_proxy_headers=True),
        audit=_RecordingAudit(),
        session_secret_key="test-secret",
        embedded_issuers=[registered] if embedded else None,
    )
    app.state.smart = _service(issuers=[registered], token=token or {
        "access_token": "at", "patient": "eAB12cd3", "encounter": "enc1"})
    return TestClient(app, base_url="https://records.example-hospital.org")


def _complete_launch(client):
    launch = client.get("/smart/launch", params={"iss": ISSUER, "launch": "t"},
                        follow_redirects=False)
    state = parse_qs(urlparse(launch.headers["location"]).query)["state"][0]
    return client.get("/smart/callback", params={"code": "c", "state": state},
                      follow_redirects=False)


def test_encounter_launch_filters_to_that_visit():
    client = _encounter_app()
    _complete_launch(client)
    body = client.get("/smart/patient/eAB12cd3", params={"encounter": "enc1"}).text

    assert "Showing one encounter" in body
    assert ">in<" in body or "obs" in body  # the in-encounter observation
    assert "Encounter/other" not in body


def test_the_encounter_view_says_it_may_be_incomplete():
    """A clinician who believes they are seeing a complete visit when they
    are not is worse off than one who knows the view is filtered."""
    client = _encounter_app()
    _complete_launch(client)
    body = client.get("/smart/patient/eAB12cd3", params={"encounter": "enc1"}).text

    assert "not necessarily every record" in body
    assert "Show the complete record" in body


def test_without_an_encounter_the_full_record_is_shown():
    client = _encounter_app(token={"access_token": "at", "patient": "eAB12cd3"})
    callback = _complete_launch(client)
    assert callback.headers["location"] == "/smart/patient/eAB12cd3"


# ---------------------------------------------------------------------------
# Embedding in the EHR frame
# ---------------------------------------------------------------------------

def test_framing_is_denied_by_default():
    """An open frame policy on a PHI application invites clickjacking."""
    client = _encounter_app(embedded=False)
    response = client.get("/smart/launch", params={"iss": ISSUER, "launch": "t"},
                          follow_redirects=False)
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers.get("x-frame-options") == "DENY"


def test_an_opted_in_emr_origin_is_allowed_to_frame():
    client = _encounter_app(embedded=True)
    response = client.get("/smart/launch", params={"iss": ISSUER, "launch": "t"},
                          follow_redirects=False)
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors https://fhir.example-hospital.org" in csp
    # X-Frame-Options DENY would override frame-ancestors in the older
    # browsers that still read it, so it must be absent here.
    assert "x-frame-options" not in {k.lower() for k in response.headers}


def test_frame_ancestors_is_an_origin_not_the_full_fhir_url():
    """A frame-ancestors source is an origin; including the FHIR base path
    would silently match nothing."""
    from core.web.security import frame_ancestors

    origins = frame_ancestors([_registered(embedded=True)])
    assert origins == ["https://fhir.example-hospital.org"]


def test_embedding_switches_the_session_cookie_to_samesite_none():
    """A cookie only reaches a cross-site frame with SameSite=None - and
    that is exactly why CSRF tokens are unconditional."""
    embedded = _encounter_app(embedded=True)
    cookie = _complete_launch(embedded).headers["set-cookie"].lower()
    assert "samesite=none" in cookie and "secure" in cookie

    standalone = _encounter_app(embedded=False)
    cookie = _complete_launch(standalone).headers["set-cookie"].lower()
    assert "samesite=lax" in cookie


def test_the_embedded_layout_is_applied_after_an_embedded_launch():
    client = _encounter_app(embedded=True)
    _complete_launch(client)
    body = client.get("/smart/patient/eAB12cd3").text
    assert 'class="embedded"' in body


def test_scripts_are_forbidden_outright():
    """This interface serves no JavaScript, so it has no reason to permit
    any - which removes XSS as a delivery route entirely."""
    client = _encounter_app()
    csp = client.get("/smart/launch", params={"iss": ISSUER, "launch": "t"},
                     follow_redirects=False).headers["content-security-policy"]
    assert "script-src 'none'" in csp
    assert "object-src 'none'" in csp


# ---------------------------------------------------------------------------
# Launch back into the EMR
# ---------------------------------------------------------------------------

CHART = "https://epic.example-hospital.org/Chart?pat={patient}&csn={encounter}"


def _launchback_app(chart_url=CHART, token=None, embedded=False):
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings
    from test_web import _FakeReader, _RecordingAudit

    registered = _registered(chart_url=chart_url, chart_label="Epic", embedded=embedded)
    app = create_app(
        reader=_FakeReader(),
        auth_settings=AuthSettings(trust_proxy_headers=True),
        audit=_RecordingAudit(),
        session_secret_key="test-secret",
        embedded_issuers=[registered] if embedded else None,
    )
    app.state.smart = _service(issuers=[registered], token=token or {
        "access_token": "at", "patient": "eAB12cd3", "encounter": "enc1"})
    client = TestClient(app, base_url="https://records.example-hospital.org")
    launch = client.get("/smart/launch", params={"iss": ISSUER, "launch": "t"},
                        follow_redirects=False)
    state = parse_qs(urlparse(launch.headers["location"]).query)["state"][0]
    client.get("/smart/callback", params={"code": "c", "state": state},
               follow_redirects=False)
    return client


def test_a_back_link_is_offered_after_an_in_context_launch():
    body = _launchback_app().get("/smart/patient/eAB12cd3").text
    assert "Back to Epic" in body
    assert "pat=eAB12cd3" in body and "csn=enc1" in body


def test_the_back_link_targets_top_so_it_escapes_the_ehr_frame():
    """Loading the EHR inside our iframe - itself inside the EHR - would
    be nonsense, and the EHR's own frame-ancestors would block it."""
    body = _launchback_app(embedded=True).get("/smart/patient/eAB12cd3").text
    assert 'target="_top"' in body


def test_no_link_is_offered_when_the_issuer_configured_none():
    """A launch-back link that opens the wrong thing is worse than none."""
    body = _launchback_app(chart_url=None).get("/smart/patient/eAB12cd3").text
    assert "Back to" not in body


def test_no_link_when_the_template_needs_an_encounter_the_session_lacks():
    """A partially substituted URL either 404s or opens the wrong record."""
    client = _launchback_app(token={"access_token": "at", "patient": "eAB12cd3"})
    assert "Back to Epic" not in client.get("/smart/patient/eAB12cd3").text


def test_a_patient_only_template_still_works_without_encounter_context():
    client = _launchback_app(
        chart_url="https://epic.example-hospital.org/Chart?pat={patient}",
        token={"access_token": "at", "patient": "eAB12cd3"},
    )
    body = client.get("/smart/patient/eAB12cd3").text
    assert "Back to Epic" in body and "pat=eAB12cd3" in body


def test_proxy_authenticated_sessions_get_no_back_link():
    """Someone who never launched from an EMR has nowhere to go back to."""
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings
    from test_web import _FakeReader, _RecordingAudit

    app = create_app(reader=_FakeReader(),
                     auth_settings=AuthSettings(trust_proxy_headers=False,
                                                dev_identity="staff:him"),
                     audit=_RecordingAudit(), session_secret_key="s")
    client = TestClient(app, base_url="https://records.example.org")
    assert "Back to" not in client.get("/patients").text


def test_ids_are_percent_encoded_into_the_link():
    """An id is untrusted input the moment it leaves the EMR, and building
    a URL by concatenation is how injection happens."""
    from core.web.smart.launch_back import build_chart_url

    url = build_chart_url("https://e.org/c?p={patient}", "a b&admin=1")
    assert "a%20b%26admin%3D1" in url


@pytest.mark.parametrize("bad,reason", [
    ("http://e.org/c?p={patient}", "https"),
    ("https://user:pw@e.org/c", "credentials"),
    ("https://e.org/c?x={unknown}", "placeholder"),
])
def test_bad_templates_are_refused_at_configuration_time(bad, reason):
    """Failing at load rather than at click time: a clinician mid-shift
    trying to get back to a chart is the worst moment to find out."""
    from core.web.smart.launch_back import LaunchBackError, validate_template

    with pytest.raises(LaunchBackError, match=reason):
        validate_template(bad)


def test_a_bad_template_fails_the_whole_issuer_file(tmp_path):
    from core.web.smart.config import load_issuers

    path = tmp_path / "issuers.yaml"
    path.write_text(
        "issuers:\n"
        f"  - issuer: {ISSUER}\n"
        "    vendor: epic\n"
        "    client_id: abc\n"
        "    chart_url: \"http://insecure.example.org/chart?p={patient}\"\n"
    )
    with pytest.raises(SMARTError, match="chart_url"):
        load_issuers(str(path))


def test_one_issuer_can_carry_several_client_registrations():
    """An organisation running a pilot alongside production against the
    same FHIR server. Previously the second silently overwrote the first,
    and the id_token audience check then failed against a client_id
    nobody configured."""
    prod = _registered(client_id="prod-client")
    pilot = _registered(client_id="pilot-client")
    service = _service(issuers=[prod, pilot])

    url = service.begin(ISSUER, "tok")
    state = parse_qs(urlparse(url).query)["state"][0]
    # The launch carries its client_id forward, so completing resolves the
    # registration it started with rather than the issuer default.
    assert service._pending[state].client_id == "prod-client"
    assert len(service._by_issuer_client) == 2


def test_loopback_may_use_http_but_nothing_else_may():
    """Emulators run on 127.0.0.1 over plain http. Traffic that never
    crosses a network cannot be rewritten in transit - the same carve-out
    RFC 8252 makes. A remote http issuer is still refused."""
    assert normalise_issuer("http://127.0.0.1:9101/r4")
    assert normalise_issuer("http://localhost:9101/r4")
    for remote in ("http://fhir.hospital.example/r4", "http://10.0.0.5/r4"):
        with pytest.raises(SMARTError, match="must be https"):
            normalise_issuer(remote)
# Made by Ryan Gomez & Co. Inc.
