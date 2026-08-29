# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Integration tests: our real client code against the EMR emulators.

Every other test in this suite uses fakes injected at the seam - which
proves the logic and not the wiring. These start actual HTTP servers and
drive the real FHIRIngestionClient, the real SMART launch service and the
real delivery writer against them, so the things fakes cannot catch are
covered: URL construction, header handling, pagination link following,
the async bulk-export handshake, and each vendor's auth quirks.

All data is synthetic. The emulators bind to 127.0.0.1 on ephemeral
ports, so these are hermetic and run anywhere.
"""

import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emulators.server import build_server  # noqa: E402
from emulators.vendors import VENDORS  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def emulators():
    """One emulator per vendor, on ephemeral ports, for the module."""
    running = {}
    for key in VENDORS:
        port = _free_port()
        server = build_server(key, port, record_launch_url="http://127.0.0.1:1/smart/launch")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running[key] = (server, f"http://127.0.0.1:{port}", port)
    yield running
    for server, _, _ in running.values():
        server.shutdown()


def _fhir_base(emulators, key):
    _, base, _ = emulators[key]
    return f"{base}{VENDORS[key].fhir_path}"


def _client(emulators, key):
    """A real FHIRIngestionClient pointed at an emulator."""
    from core.fhir.client import FHIRIngestionClient
    from core.fhir.emr_profiles import profile_for

    client = FHIRIngestionClient(
        base_url=_fhir_base(emulators, key),
        profile=profile_for(key),
        storage=None, encryptor=None, audit=None, retention_years=10,
    )
    _, base, _ = emulators[key]
    if profile_for(key).auth_flow == "oauth2_client_credentials":
        # Chosen by the PROFILE's auth flow, not the emulator's accept
        # flags: Cerner accepts both a Basic-auth secret and a JWT
        # assertion, and the profile picks the assertion (the documented
        # bulk-data mode) - the helper should authenticate the way the
        # real client would.
        client.authenticate_client_secret("cid", "secret", f"{base}/oauth2/token")
    else:
        # The assertion flow signs a JWT; the emulator accepts any
        # well-formed assertion, since verifying it would mean managing
        # the client's key too.
        import requests

        data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": "emulator.accepts.any",
        }
        if VENDORS[key].requires_token_scope:
            data["scope"] = "system/Observation.read system/Patient.read"
        response = requests.post(f"{base}/oauth2/token", data=data, timeout=10)
        response.raise_for_status()
        client._access_token = response.json()["access_token"]
    return client


# ---------------------------------------------------------------------------
# Ingestion against every vendor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor", sorted(VENDORS))
def test_the_real_client_pages_resources_from_every_vendor(emulators, vendor):
    """Exercises the `next` link handling that a fake cannot: the
    emulators page at 2 per response regardless of _count."""
    client = _client(emulators, vendor)
    observations = list(client.iter_resources("Observation"))

    assert len(observations) > 2, "pagination did not follow the next link"
    assert all(o["resourceType"] == "Observation" for o in observations)
    assert len({o["id"] for o in observations}) == len(observations), "duplicate pages"


def test_athenahealth_rejects_a_jwt_assertion_as_it_would_live(emulators):
    """The one target that takes a client secret. A client assuming every
    vendor accepts an assertion should fail here, not in production."""
    import requests

    _, base, _ = emulators["athenahealth"]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_assertion": "some.jwt.assertion",
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client"


def test_the_client_secret_flow_works_against_athenahealth(emulators):
    client = _client(emulators, "athenahealth")
    assert client.access_token


# ---------------------------------------------------------------------------
# authenticate_from_settings: the dispatch the schedulers actually run
# ---------------------------------------------------------------------------
# The tests above exercise each token flow by calling the right client
# method BY HAND - which is exactly how the schedulers' own hardcoded
# authenticate() call stayed green while ingestion against athenahealth
# and Oracle Health failed with the vendor's 400 at startup. These run
# the dispatcher itself, per vendor, against the emulator's real seams.

def _bare_client(emulators, key):
    from core.fhir.client import FHIRIngestionClient
    from core.fhir.emr_profiles import profile_for

    return FHIRIngestionClient(
        base_url=_fhir_base(emulators, key),
        profile=profile_for(key),
        storage=None, encryptor=None, audit=None, retention_years=10,
    )


def _settings_for(emulators, key, tmp_path, secret=None):
    from types import SimpleNamespace

    _, base, _ = emulators[key]
    private_key_pem, _ = _generate_keypair(tmp_path)
    return SimpleNamespace(
        fhir_client_id="cid",
        fhir_client_secret=secret,
        fhir_token_url=f"{base}/oauth2/token",
        fhir_private_key_pem=private_key_pem,
        fhir_jwt_kid=None,
    )


def _generate_keypair(tmp_path):
    import subprocess

    tmp_path.mkdir(parents=True, exist_ok=True)
    priv = tmp_path / "private.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"],
                   capture_output=True, check=True)
    return priv.read_bytes(), None


def test_dispatch_uses_the_secret_flow_for_athenahealth(emulators, tmp_path):
    """The emulator rejects a JWT assertion outright, so this passes only
    if the dispatcher genuinely chose the client-secret flow."""
    client = _bare_client(emulators, "athenahealth")
    client.authenticate_from_settings(
        _settings_for(emulators, "athenahealth", tmp_path, secret="secret")
    )
    assert client.access_token


def test_dispatch_refuses_athenahealth_without_a_secret(emulators, tmp_path):
    client = _bare_client(emulators, "athenahealth")
    with pytest.raises(RuntimeError, match="PHI_AI_FHIR_CLIENT_SECRET"):
        client.authenticate_from_settings(
            _settings_for(emulators, "athenahealth", tmp_path, secret=None)
        )


def test_dispatch_sends_explicit_scopes_to_cerner(emulators, tmp_path):
    """The emulator refuses a token request without explicit system
    scopes (as Oracle Health documents), so this passes only if the
    dispatcher derived and sent them."""
    client = _bare_client(emulators, "cerner")
    client.authenticate_from_settings(_settings_for(emulators, "cerner", tmp_path))
    assert client.access_token


def test_dispatch_still_omits_scope_for_epic(emulators, tmp_path):
    """Epic's documented token request has no scope parameter; the
    dispatcher must not have grown one as a side effect."""
    client = _bare_client(emulators, "epic")
    client.authenticate_from_settings(_settings_for(emulators, "epic", tmp_path))
    assert client.access_token


@pytest.mark.parametrize("vendor", ["epic", "eclinicalworks", "meditech", "nextgen"])
def test_jwt_only_vendors_reject_a_client_secret(emulators, vendor):
    """The inverse of the athenahealth test: sending a secret to a vendor
    that only takes a signed assertion gets invalid_client, live."""
    import requests

    _, base, _ = emulators[vendor]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_secret": "some-secret",
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client"


def test_cerner_accepts_a_basic_auth_client_secret(emulators):
    """Oracle Health's PRIMARY documented system-account mode: the secret
    travels in an RFC 2617 Basic Authorization header, with explicit
    system scopes in the body."""
    import base64 as b64

    import requests

    _, base, _ = emulators["cerner"]
    header = "Basic " + b64.b64encode(b"cid:secret").decode()
    response = requests.post(
        f"{base}/oauth2/token",
        headers={"Authorization": header},
        data={"grant_type": "client_credentials",
              "scope": "system/Observation.read system/Patient.read"},
        timeout=10,
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_cerner_refuses_a_token_request_without_explicit_scopes(emulators):
    """Oracle Health: "Applications must explicitly request each scope."
    A client tuned on Epic - whose backend token request carries no scope
    parameter at all - must fail here, not against a real tenant."""
    import requests

    _, base, _ = emulators["cerner"]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": "emulator.accepts.any",
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


def test_cerner_refuses_wildcard_scopes(emulators):
    """Also verbatim from their docs: "we do not support Wildcard scopes"."""
    import requests

    _, base, _ = emulators["cerner"]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": "emulator.accepts.any",
        "scope": "system/*.read",
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


def test_reading_one_resource_by_id_works(emulators):
    """The path core/verify/freshness.py uses."""
    client = _client(emulators, "epic")
    resource = client.read_resource("Patient", "eSyn0001Patient")
    assert resource["resourceType"] == "Patient"
    assert resource["id"] == "eSyn0001Patient"


# ---------------------------------------------------------------------------
# Bulk export — including the vendors that do not have it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor",
                         ["epic", "cerner", "athenahealth", "eclinicalworks", "meditech"])
def test_bulk_export_completes_its_async_handshake(emulators, vendor):
    """Kickoff returns 202 + Content-Location; the first poll is still in
    progress. A client assuming the first poll is ready breaks against
    every real implementation."""
    import requests

    base = _fhir_base(emulators, vendor)
    token = _client(emulators, vendor).access_token
    headers = {"Authorization": f"Bearer {token}"}

    kickoff = requests.get(f"{base}/$export", headers=headers, timeout=10)
    assert kickoff.status_code == 202
    status_url = kickoff.headers["Content-Location"]

    assert requests.get(status_url, headers=headers, timeout=10).status_code == 202
    ready = requests.get(status_url, headers=headers, timeout=10)
    assert ready.status_code == 200

    output = ready.json()["output"]
    assert output
    ndjson = requests.get(output[0]["url"], headers=headers, timeout=10).text
    assert ndjson.strip(), "bulk file was empty"
    assert all(line.startswith("{") for line in ndjson.strip().splitlines())


@pytest.mark.parametrize("vendor", ["nextgen"])
def test_vendors_without_bulk_export_say_so_rather_than_returning_nothing(emulators, vendor):
    """An empty result could be mistaken for "no data". An
    OperationOutcome cannot."""
    import requests

    response = requests.get(f"{_fhir_base(emulators, vendor)}/$export", timeout=10)
    assert response.status_code == 400
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert "does not support Bulk Data Export" in body["issue"][0]["diagnostics"]


# ---------------------------------------------------------------------------
# Delivery — the real writer against a real server
# ---------------------------------------------------------------------------

def _delivery_rows():
    return [
        ({"storage_key": "fhir/Observation/o1.json",
          "patient_reference": "Patient/eSyn0001Patient"},
         {"resourceType": "Observation", "id": "o1",
          "subject": {"reference": "Patient/eSyn0001Patient"}}),
    ]


def _writer(emulators, vendor, audit=None):
    from core.fhir.delivery.writer import EMRWriter
    from core.fhir.emr_profiles import profile_for

    class _Audit:
        def __init__(self):
            self.events = []

        def record(self, **kwargs):
            self.events.append(kwargs)

    return EMRWriter(
        base_url=_fhir_base(emulators, vendor),
        access_token=_client(emulators, vendor).access_token,
        profile=profile_for(vendor),
        audit=audit or _Audit(),
    )


def test_delivery_reads_the_real_capability_statement(emulators):
    """Cerner's emulator advertises three creatable types; Epic's one."""
    assert "Observation" in _writer(emulators, "cerner").creatable_resource_types()
    assert "Observation" not in _writer(emulators, "epic").creatable_resource_types()


def test_a_real_delivery_writes_and_is_confirmable(emulators):
    """The whole loop: deliver, then verify the destination holds it -
    which only works because the writer tags meta.source and the emulator
    supports searching on it."""
    from core.fhir.delivery.identity import IdentityMap, PatientMapping
    from core.verify.delivery import verify_delivery

    identity = IdentityMap([PatientMapping("eSyn0001Patient", "cerner-1", "tester")])
    writer = _writer(emulators, "cerner")

    result = writer.deliver(_delivery_rows(), identity, "epic-emulator", "treatment",
                            dry_run=False)
    assert result.sent_count == 1, [i.error or i.skipped_reason for i in result.items]

    confirmation = verify_delivery(result, _fhir_base(emulators, "cerner"),
                                   writer.access_token)
    assert confirmation.ok, [f.summary for f in confirmation.findings]


def test_delivering_twice_to_a_conditional_create_vendor_does_not_duplicate(emulators):
    """The property that makes a delivery re-runnable."""
    from core.fhir.delivery.identity import IdentityMap, PatientMapping

    identity = IdentityMap([PatientMapping("eSyn0001Patient", "cerner-2", "tester")])
    writer = _writer(emulators, "cerner")
    rows = [({"storage_key": "fhir/Observation/dedupe.json",
              "patient_reference": "Patient/eSyn0001Patient"},
             {"resourceType": "Observation", "id": "dedupe",
              "subject": {"reference": "Patient/eSyn0001Patient"}})]

    first = writer.deliver(rows, identity, "epic-emulator", "treatment", dry_run=False)
    second = writer.deliver(rows, identity, "epic-emulator", "treatment", dry_run=False)

    assert first.items[0].status == "created"
    assert second.items[0].status == "already present"


def test_delivery_skips_types_the_destination_will_not_accept(emulators):
    """Epic's emulator advertises create for DocumentReference only."""
    from core.fhir.delivery.identity import IdentityMap, PatientMapping

    identity = IdentityMap([PatientMapping("eSyn0001Patient", "epic-1", "tester")])
    result = _writer(emulators, "epic").deliver(
        _delivery_rows(), identity, "src", "treatment", dry_run=True)

    assert result.items[0].skipped_reason
    assert "does not advertise create" in result.items[0].skipped_reason


def test_meditech_advertises_create_for_nothing_and_delivery_skips(emulators):
    """MEDITECH's public docs describe a view-only US Core surface, so the
    emulator's CapabilityStatement advertises no create at all - and the
    real writer must skip rather than attempt the POST."""
    from core.fhir.delivery.identity import IdentityMap, PatientMapping

    writer = _writer(emulators, "meditech")
    assert writer.creatable_resource_types() == set()

    identity = IdentityMap([PatientMapping("eSyn0001Patient", "mt-1", "tester")])
    result = writer.deliver(_delivery_rows(), identity, "src", "treatment", dry_run=True)
    assert result.items[0].skipped_reason
    assert "does not advertise create" in result.items[0].skipped_reason


def test_conditional_create_is_refused_where_the_vendor_does_not_support_it(emulators):
    """NextGen advertises create for DocumentReference but not
    If-None-Exist. A 412 makes the gap visible in a test; a vendor that
    silently ignored the header would duplicate records in a chart."""
    import requests

    base = _fhir_base(emulators, "nextgen")
    token = _client(emulators, "nextgen").access_token
    response = requests.post(
        f"{base}/DocumentReference",
        json={"resourceType": "DocumentReference",
              "meta": {"source": "urn:phi-ai:test"}},
        headers={"Authorization": f"Bearer {token}",
                 "If-None-Exist": "identifier=urn:phi-ai:test"},
        timeout=10,
    )
    assert response.status_code == 412
    assert response.json()["resourceType"] == "OperationOutcome"


# ---------------------------------------------------------------------------
# SMART in-context launch — against a really-signed id_token
# ---------------------------------------------------------------------------

def test_a_full_smart_launch_against_the_emulator(emulators):
    """Drives the real SMARTLaunchService through discovery, PKCE,
    authorize, token exchange and JWKS-verified id_token."""
    import requests

    from core.web.smart.launch import RegisteredIssuer, SMARTLaunchService

    _, base, _ = emulators["epic"]
    issuer = _fhir_base(emulators, "epic")

    service = SMARTLaunchService(
        issuers=[RegisteredIssuer(issuer=issuer, vendor_key="epic", client_id="client-abc")],
        redirect_uri="http://127.0.0.1:1/smart/callback",
        http_get=lambda url: requests.get(url, timeout=10).json(),
        http_post=lambda url, data, auth: requests.post(url, data=data, auth=auth,
                                                        timeout=10).json(),
    )

    authorize_url = service.begin(issuer, "launch-token")
    assert "code_challenge_method=S256" in authorize_url

    # Follow the authorize redirect the way a browser would.
    response = requests.get(authorize_url, allow_redirects=False, timeout=10)
    assert response.status_code == 302

    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(response.headers["Location"]).query)
    context = service.complete(state=query["state"][0], code=query["code"][0])

    assert context.patient_id == "eSyn0001Patient"
    assert context.encounter_id == "eSynEnc0001"
    # Proves the id_token was verified against the emulator's published
    # JWKS - an unverified flow would have left this unnamed.
    assert context.username == "emulator.clinician"


def test_the_launch_fails_if_the_id_token_cannot_be_verified(emulators):
    """An unverifiable identity assertion is not an identity."""
    import requests

    from core.web.smart.launch import RegisteredIssuer, SMARTError, SMARTLaunchService

    issuer = _fhir_base(emulators, "epic")

    def wrong_jwks(uri):
        raise RuntimeError("jwks unreachable")

    service = SMARTLaunchService(
        issuers=[RegisteredIssuer(issuer=issuer, vendor_key="epic", client_id="client-abc")],
        redirect_uri="http://127.0.0.1:1/smart/callback",
        http_get=lambda url: requests.get(url, timeout=10).json(),
        http_post=lambda url, data, auth: requests.post(url, data=data, auth=auth,
                                                        timeout=10).json(),
        jwks_client_factory=wrong_jwks,
    )
    authorize_url = service.begin(issuer, "launch-token")
    response = requests.get(authorize_url, allow_redirects=False, timeout=10)

    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(response.headers["Location"]).query)
    with pytest.raises(SMARTError, match="could not verify the id_token"):
        service.complete(state=query["state"][0], code=query["code"][0])
# Made by Ryan Gomez & Co. Inc.
