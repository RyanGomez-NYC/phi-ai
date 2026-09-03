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
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fhir.emr_profiles import PROFILES  # noqa: E402
from emulators.server import build_server  # noqa: E402
from emulators.vendors import VENDORS  # noqa: E402

# The asymmetric JWS algorithms a vendor's token endpoint might accept
# for a client assertion. Each emulator accepts the subset its vendor
# documents (EmulatorVendor.assertion_algorithms); the tests below sign
# with each accepted one to get in, and with every other one - plus an
# unsigned `none` - to prove the door is shut.
CANDIDATE_ALGORITHMS = ("RS256", "RS384", "ES256", "ES384")

# Vendor selections DERIVED from the emulator flags, so a vendor added to
# emulators/vendors.py is covered by the right tests without anyone
# remembering to extend a list here. Each is the set of vendors whose
# emulator has the property the test exercises - not a copy of the
# registry that can rot in either direction.
JWT_ONLY_VENDORS = [k for k in sorted(VENDORS) if not VENDORS[k].accepts_client_secret]
CLIENT_SECRET_VENDORS = [k for k in sorted(VENDORS) if VENDORS[k].accepts_client_secret]
ASSERTION_VENDORS = [k for k in sorted(VENDORS) if VENDORS[k].accepts_jwt_assertion]
BULK_EXPORT_VENDORS = [k for k in sorted(VENDORS) if VENDORS[k].supports_bulk_export]
NO_BULK_EXPORT_VENDORS = [k for k in sorted(VENDORS) if not VENDORS[k].supports_bulk_export]

# The vendors whose PROFILE says the token request is a signed assertion
# - the dispatcher's own branch, which is what authenticate_from_settings
# is tested against below.
ASSERTION_PROFILES = [k for k in sorted(PROFILES)
                      if PROFILES[k].auth_flow == "smart_backend_services"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The client_id:secret the client-secret tests present. Registered with
# every emulator below, so a wrong id or secret is refused as a vendor
# holding the registration would refuse it.
CLIENT_ID, CLIENT_SECRET = "cid", "secret"


@pytest.fixture(scope="module")
def emulators():
    """One emulator per vendor, on ephemeral ports, for the module - with
    this module's PUBLIC JWK Set and its client credentials registered, so
    every token endpoint verifies assertion signatures and secrets the way
    a vendor holding the registration does."""
    running = {}
    for key in VENDORS:
        port = _free_port()
        server = build_server(key, port, record_launch_url="http://127.0.0.1:1/smart/launch",
                              client_jwks=_client_jwks(),
                              client_credentials={CLIENT_ID: CLIENT_SECRET})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running[key] = (server, f"http://127.0.0.1:{port}", port)
    yield running
    for server, _, _ in running.values():
        server.shutdown()


def _fhir_base(emulators, key):
    _, base, _ = emulators[key]
    return f"{base}{VENDORS[key].fhir_path}"


# ---------------------------------------------------------------------------
# Signing keys - RSA and EC, generated IN MEMORY with the cryptography
# library (the one PyJWT signs with), of the family each JWT algorithm
# needs (RFC 7518: RS* signs with an RSA key, ES256/ES384 with an EC key
# on P-256/P-384). pyjwt refuses the wrong pairing outright, so a test
# that signs ES384 really does hold a P-384 key. Nothing is written to
# disk: the private halves live in this process and the PUBLIC halves are
# registered with the emulators as a JWK Set, so the signature is checked.
# ---------------------------------------------------------------------------

_EC_CURVES = {"ES256": "prime256v1", "ES384": "secp384r1", "ES512": "secp521r1"}


def _generate_keypair(algorithm="RS384"):
    """(private PEM bytes, public JWK dict) for `algorithm`'s key family."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    if algorithm.startswith(("RS", "PS")):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        to_jwk = RSAAlgorithm.to_jwk
    elif algorithm in _EC_CURVES:
        curve = {"ES256": ec.SECP256R1, "ES384": ec.SECP384R1, "ES512": ec.SECP521R1}[algorithm]
        key = ec.generate_private_key(curve())
        to_jwk = ECAlgorithm.to_jwk
    else:
        raise ValueError(f"no key type for JWT algorithm {algorithm!r}")
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, to_jwk(key.public_key(), as_dict=True)


# One key per FAMILY the candidate algorithms need (RS256 and RS384 share
# the RSA key; each EC curve has its own), each with a stable kid the
# assertion header names so the emulator's JWKS lookup finds exactly one.
_KEY_FAMILY = {"RS256": "RS384", "PS256": "RS384", "PS384": "RS384"}
_KEY_CACHE: dict[str, tuple[bytes, dict]] = {}


def _key_for(algorithm):
    family = _KEY_FAMILY.get(algorithm, algorithm)
    if family not in _KEY_CACHE:
        private_pem, jwk = _generate_keypair(family)
        _KEY_CACHE[family] = (private_pem, {**jwk, "kid": f"test-{family.lower()}", "use": "sig"})
    return _KEY_CACHE[family]


def _signing_key(algorithm):
    """The private PEM that signs `algorithm`, shared across the module -
    key generation is the slow part of every token request below."""
    return _key_for(algorithm)[0]


def _kid_for(algorithm):
    return _key_for(algorithm)[1]["kid"]


def _client_jwks():
    """The PUBLIC JWK Set registered with every emulator: every family a
    candidate algorithm can need, generated up front."""
    for algorithm in CANDIDATE_ALGORITHMS:
        _key_for(algorithm)
    return {"keys": [jwk for _, jwk in _KEY_CACHE.values()]}


def _signed_assertion(token_url, algorithm):
    """A real RFC 7523 client assertion, signed with `algorithm` by a key
    of the matching family - or, for `none`, not signed at all. Built with
    pyjwt directly rather than the client's own builder so the emulator's
    algorithm check is tested on its own, not through whatever the client
    chose."""
    import jwt as pyjwt

    now = int(time.time())
    claims = {"iss": "cid", "sub": "cid", "aud": token_url,
              "jti": str(uuid.uuid4()), "iat": now, "exp": now + 240}
    if algorithm == "none":
        return pyjwt.encode(claims, None, algorithm="none")
    return pyjwt.encode(claims, _signing_key(algorithm), algorithm=algorithm,
                        headers={"kid": _kid_for(algorithm)})


def _token_request(emulators, key, **fields):
    """POST a client_credentials token request to a vendor's emulator,
    adding the explicit system scopes where the vendor demands them."""
    import requests

    _, base, _ = emulators[key]
    data = {"grant_type": "client_credentials", **fields}
    if VENDORS[key].requires_token_scope:
        data["scope"] = "system/Observation.read system/Patient.read"
    return requests.post(f"{base}/oauth2/token", data=data, timeout=10)


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
        client.authenticate_client_secret(CLIENT_ID, CLIENT_SECRET, f"{base}/oauth2/token")
    else:
        # The assertion flow signs a JWT. The emulator verifies the
        # signature against the JWK Set the fixture registered and holds
        # the assertion's `alg` header to what the vendor documents - so
        # sign a real one, with the algorithm the PROFILE records (again:
        # the way the real client would, not whatever the emulator
        # happens to accept).
        algorithm = profile_for(key).assertion_algorithm
        response = _token_request(
            emulators, key,
            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            client_assertion=_signed_assertion(f"{base}/oauth2/token", algorithm),
        )
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


def _settings_for(emulators, key, secret=None):
    """Settings the way Settings.from_env() would load them for this
    vendor: the private key is of the type the profile's assertion
    algorithm needs (from_env refuses any other pairing at startup), and
    its public half is in the JWK Set the emulator holds, under the kid
    the settings name - as a registered client's would be."""
    from types import SimpleNamespace

    from core.fhir.emr_profiles import profile_for

    _, base, _ = emulators[key]
    algorithm = profile_for(key).assertion_algorithm
    return SimpleNamespace(
        fhir_client_id=CLIENT_ID,
        fhir_client_secret=secret,
        fhir_token_url=f"{base}/oauth2/token",
        fhir_private_key_pem=_signing_key(algorithm),
        fhir_jwt_kid=_kid_for(algorithm),
    )


def test_dispatch_uses_the_secret_flow_for_athenahealth(emulators):
    """The emulator rejects a JWT assertion outright, so this passes only
    if the dispatcher genuinely chose the client-secret flow."""
    client = _bare_client(emulators, "athenahealth")
    client.authenticate_from_settings(
        _settings_for(emulators, "athenahealth", secret="secret")
    )
    assert client.access_token


def test_dispatch_refuses_athenahealth_without_a_secret(emulators):
    client = _bare_client(emulators, "athenahealth")
    with pytest.raises(RuntimeError, match="PHI_AI_FHIR_CLIENT_SECRET"):
        client.authenticate_from_settings(
            _settings_for(emulators, "athenahealth", secret=None)
        )


def test_dispatch_sends_explicit_scopes_to_cerner(emulators):
    """The emulator refuses a token request without explicit system
    scopes (as Oracle Health documents), so this passes only if the
    dispatcher derived and sent them."""
    client = _bare_client(emulators, "cerner")
    client.authenticate_from_settings(_settings_for(emulators, "cerner"))
    assert client.access_token


def test_dispatch_still_omits_scope_for_epic(emulators):
    """Epic's documented token request has no scope parameter; the
    dispatcher must not have grown one as a side effect."""
    client = _bare_client(emulators, "epic")
    client.authenticate_from_settings(_settings_for(emulators, "epic"))
    assert client.access_token


@pytest.mark.parametrize("vendor", ASSERTION_PROFILES)
def test_dispatch_authenticates_every_assertion_vendor_with_its_documented_key_type(
        emulators, vendor):
    """The dispatcher, per vendor, end to end: it must pick the assertion
    flow, sign with the algorithm the PROFILE records (the emulator
    refuses any other `alg`), and send explicit scopes exactly where the
    profile says they are mandatory (the emulator refuses a request
    without them, or with them where wildcards are the only option). A
    vendor whose profile and emulator disagree on any of these fails
    here, against a live token endpoint."""
    client = _bare_client(emulators, vendor)
    client.authenticate_from_settings(_settings_for(emulators, vendor))
    assert client.access_token


@pytest.mark.parametrize("vendor", JWT_ONLY_VENDORS)
def test_jwt_only_vendors_reject_a_client_secret(emulators, vendor):
    """The inverse of the athenahealth test: sending a secret to a vendor
    that only takes a signed assertion gets invalid_client, live."""
    response = _token_request(emulators, vendor, client_secret="some-secret")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client"


@pytest.mark.parametrize("vendor", CLIENT_SECRET_VENDORS)
def test_vendors_that_take_a_client_secret_issue_a_token_for_one(emulators, vendor):
    """Every vendor whose emulator honours a client secret must actually
    issue a token for one - including those that also take an assertion,
    where the secret is a documented alternative and not a fallback."""
    response = _token_request(emulators, vendor, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


@pytest.mark.parametrize("vendor", CLIENT_SECRET_VENDORS)
def test_a_secret_that_is_not_the_registered_one_is_invalid_client(emulators, vendor):
    """The secret authenticates the client: with credentials registered on
    the emulator (as they are on a vendor), a wrong secret, a wrong
    client_id, or no client_id at all gets invalid_client, never a
    token."""
    for fields in (
        {"client_id": CLIENT_ID, "client_secret": "not-the-secret"},
        {"client_id": "someone-else", "client_secret": CLIENT_SECRET},
        {"client_secret": CLIENT_SECRET},
    ):
        response = _token_request(emulators, vendor, **fields)
        assert response.status_code == 400, (fields, response.text)
        assert response.json()["error"] == "invalid_client", (fields, response.text)


# ---------------------------------------------------------------------------
# Assertion signing algorithm - RS384 for most vendors, ES384 for some
# ---------------------------------------------------------------------------
# A vendor that documents ES384 rejects an RS384-signed assertion, and vice
# versa, before it ever looks at the signature: the `alg` header is the
# first thing an authorization server checks. A client that signs RS384
# everywhere (as this one used to) fails against the ES384 vendors at
# startup, so each emulator holds the header to its vendor's algorithm.

@pytest.mark.parametrize("vendor", ASSERTION_VENDORS)
def test_an_assertion_signed_with_the_documented_algorithm_is_accepted(emulators, vendor):
    _, base, _ = emulators[vendor]
    accepted = VENDORS[vendor].assertion_algorithms
    assert accepted, f"{vendor} takes an assertion but accepts no algorithm"
    for algorithm in accepted:
        response = _token_request(
            emulators, vendor,
            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            client_assertion=_signed_assertion(f"{base}/oauth2/token", algorithm),
        )
        assert response.status_code == 200, (algorithm, response.text)
        assert response.json()["access_token"]


@pytest.mark.parametrize("vendor", ASSERTION_VENDORS)
def test_an_assertion_signed_with_any_other_algorithm_is_invalid_client(emulators, vendor):
    """Every candidate algorithm the vendor does NOT document, and an
    unsigned `alg: none` assertion, which no vendor accepts. A vendor
    that documents all four candidates is still held to `none`."""
    _, base, _ = emulators[vendor]
    accepted = VENDORS[vendor].assertion_algorithms
    others = [a for a in CANDIDATE_ALGORITHMS if a not in accepted] + ["none"]
    for algorithm in others:
        response = _token_request(
            emulators, vendor,
            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            client_assertion=_signed_assertion(f"{base}/oauth2/token", algorithm),
        )
        assert response.status_code == 400, (algorithm, response.text)
        assert response.json()["error"] == "invalid_client", (algorithm, response.text)


def test_cerner_accepts_a_basic_auth_client_secret(emulators):
    """Oracle Health's PRIMARY documented system-account mode: the secret
    travels in an RFC 2617 Basic Authorization header, with explicit
    system scopes in the body."""
    import base64 as b64

    import requests

    _, base, _ = emulators["cerner"]
    header = "Basic " + b64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
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
    parameter at all - must fail here, not against a real tenant. The
    assertion is a properly signed one: the client is authenticated
    before its scope is looked at, so a bad assertion would surface as
    invalid_client and never reach the check this test is about."""
    import requests

    _, base, _ = emulators["cerner"]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": _signed_assertion(f"{base}/oauth2/token",
                                              PROFILES["cerner"].assertion_algorithm),
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope", response.text


def test_cerner_refuses_wildcard_scopes(emulators):
    """Also verbatim from their docs: "we do not support Wildcard scopes"."""
    import requests

    _, base, _ = emulators["cerner"]
    response = requests.post(f"{base}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": _signed_assertion(f"{base}/oauth2/token",
                                              PROFILES["cerner"].assertion_algorithm),
        "scope": "system/*.read",
    }, timeout=10)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope", response.text


WILDCARD_UNDOCUMENTED_VENDORS = [k for k in sorted(VENDORS)
                                 if VENDORS[k].requires_token_scope
                                 and not VENDORS[k].refuses_wildcard_scope]


@pytest.mark.parametrize("vendor", WILDCARD_UNDOCUMENTED_VENDORS)
def test_a_wildcard_scope_is_refused_only_where_the_vendor_documents_the_refusal(emulators, vendor):
    """Oracle Health documents the refusal; the other scope-requiring
    vendors either document a wildcard form (ModMed system/*.rs, TruBridge
    system/*.*, Netsmart system/*.rs, Nextech system/*.read) or document
    nothing - and an emulator must not borrow Oracle Health's rule for
    them. Every wildcard refusal in VENDORS is an explicit per-vendor
    flag; with the flag off, a wildcard passes."""
    _, base, _ = emulators[vendor]
    profile = PROFILES[vendor]
    fields = {"scope": "system/*.read"}
    if VENDORS[vendor].accepts_jwt_assertion:
        fields.update(
            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            client_assertion=_signed_assertion(f"{base}/oauth2/token", profile.assertion_algorithm),
        )
    else:
        fields.update(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    import requests

    response = requests.post(f"{base}/oauth2/token",
                             data={"grant_type": "client_credentials", **fields}, timeout=10)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# What the token endpoint verifies on an assertion - always, and with keys
# ---------------------------------------------------------------------------

def _no_jwks_emulator(key):
    """A second emulator for `key` with NO client JWKS registered - the
    only mode a bare `python -m emulators` runs in - so the claims it
    still verifies can be proven without a key."""
    port = _free_port()
    server = build_server(key, port, record_launch_url="http://127.0.0.1:1/smart/launch")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _assertion_with(token_url, algorithm, **overrides):
    """A signed assertion whose claims can be bent one at a time."""
    import jwt as pyjwt

    now = int(time.time())
    claims = {"iss": "cid", "sub": "cid", "aud": token_url,
              "jti": str(uuid.uuid4()), "iat": now, "exp": now + 240}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, _signing_key(algorithm), algorithm=algorithm,
                        headers={"kid": _kid_for(algorithm)})


def _post_assertion(base, assertion, key):
    import requests

    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    }
    if VENDORS[key].requires_token_scope:
        data["scope"] = "system/Observation.read system/Patient.read"
    return requests.post(f"{base}/oauth2/token", data=data, timeout=10)


@pytest.mark.parametrize("defect", ["expired", "wrong audience", "iss != sub", "no jti", "not yet valid"])
def test_a_bad_assertion_is_refused_even_with_no_jwks_registered(defect, caplog):
    """Without a registered client key the emulator cannot check the
    signature - and it says so, with a WARNING at start - but it still
    checks expiry, audience, the required claims, nbf and iss == sub, so
    a bare `python -m emulators` never mints a token for an assertion
    whose header merely says the right alg."""
    import logging

    key = "modmed"  # the vendor whose 'ES384-only' guarantee this protects
    with caplog.at_level(logging.WARNING, logger="emulator"):
        server, base = _no_jwks_emulator(key)
    try:
        assert any("NOT verified" in r.getMessage() for r in caplog.records), (
            "build_server did not warn that signatures are unverified")
        token_url = f"{base}/oauth2/token"
        algorithm = PROFILES[key].assertion_algorithm
        bent = {
            "expired": dict(exp=1),
            "wrong audience": dict(aud="x"),
            "iss != sub": dict(sub="someone-else"),
            "no jti": dict(jti=None),
            "not yet valid": dict(nbf=int(time.time()) + 3600),
        }[defect]
        good = _post_assertion(base, _assertion_with(token_url, algorithm), key)
        assert good.status_code == 200, ("a well-formed assertion must still pass", good.text)
        bad = _post_assertion(base, _assertion_with(token_url, algorithm, **bent), key)
        assert bad.status_code == 400, (defect, bad.text)
        assert bad.json()["error"] == "invalid_client", (defect, bad.text)
    finally:
        server.shutdown()


def test_a_forged_signature_with_the_right_alg_header_is_refused_without_a_jwks():
    """The exact forgery the header-only check let through: a header
    saying alg=ES384, a body with exp in 1970 and aud 'x', and a signature
    of arbitrary bytes. Refused on the claims, without any key."""
    import base64
    import json as _json

    server, base = _no_jwks_emulator("modmed")
    try:
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
        forged = ".".join([
            b64(_json.dumps({"alg": "ES384", "typ": "JWT"}).encode()),
            b64(_json.dumps({"iss": "cid", "sub": "cid", "aud": "x", "exp": 1, "jti": "j"}).encode()),
            b64(b"AAAA"),
        ])
        response = _post_assertion(base, forged, "modmed")
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client", response.text
    finally:
        server.shutdown()


@pytest.mark.parametrize("vendor", ASSERTION_VENDORS)
def test_an_assertion_signed_by_an_unregistered_key_is_invalid_client(emulators, vendor):
    """The registered JWK Set is what authenticates the client: a fresh key
    of the right family, signing the right algorithm and naming the
    registered kid, is still refused - the signature does not verify."""
    import jwt as pyjwt

    _, base, _ = emulators[vendor]
    token_url = f"{base}/oauth2/token"
    algorithm = VENDORS[vendor].assertion_algorithms[0]
    stranger_pem, _ = _generate_keypair(algorithm)
    now = int(time.time())
    assertion = pyjwt.encode(
        {"iss": "cid", "sub": "cid", "aud": token_url, "jti": str(uuid.uuid4()),
         "iat": now, "exp": now + 240},
        stranger_pem, algorithm=algorithm, headers={"kid": _kid_for(algorithm)},
    )
    response = _post_assertion(base, assertion, vendor)
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_client", response.text
    assert "InvalidSignatureError" in response.json()["error_description"], response.text


@pytest.mark.parametrize("vendor", ASSERTION_VENDORS)
def test_an_assertion_needs_the_jwt_bearer_client_assertion_type(emulators, vendor):
    """RFC 7523 s2.2: client_assertion travels with client_assertion_type
    set to the jwt-bearer URN. Missing or wrong -> invalid_request."""
    import requests

    _, base, _ = emulators[vendor]
    token_url = f"{base}/oauth2/token"
    algorithm = VENDORS[vendor].assertion_algorithms[0]
    for assertion_type in (None, "urn:ietf:params:oauth:grant-type:jwt-bearer"):
        data = {"grant_type": "client_credentials",
                "client_assertion": _signed_assertion(token_url, algorithm)}
        if assertion_type:
            data["client_assertion_type"] = assertion_type
        if VENDORS[vendor].requires_token_scope:
            data["scope"] = "system/Patient.read"
        response = requests.post(token_url, data=data, timeout=10)
        assert response.status_code == 400, (assertion_type, response.text)
        assert response.json()["error"] == "invalid_request", (assertion_type, response.text)


# ---------------------------------------------------------------------------
# Malformed input: a 400 with a body, never a dropped connection
# ---------------------------------------------------------------------------

def _raw_post(base, path, body: bytes, content_length: str, content_type: str,
              extra_headers: str = "") -> tuple[int, bytes]:
    """A hand-built HTTP/1.1 POST over a raw socket, so Content-Length and
    the body bytes can be exactly what a library would refuse to send."""
    from urllib.parse import urlparse

    parsed = urlparse(base)
    request = (
        f"POST {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {content_length}\r\n"
        f"{extra_headers}Connection: close\r\n\r\n"
    ).encode("ascii") + body
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    assert raw.startswith(b"HTTP/1."), f"no HTTP response at all (connection dropped): {raw!r}"
    status = int(raw.split(b" ", 2)[1])
    return status, raw.split(b"\r\n\r\n", 1)[1]


MALFORMED_BODIES = {
    "Content-Length not an integer": (b"grant_type=client_credentials", "abc"),
    "body not UTF-8": (b"\xff\xfe", "2"),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_BODIES))
def test_a_malformed_token_request_is_a_400_not_a_dropped_connection(emulators, case):
    body, length = MALFORMED_BODIES[case]
    _, base, _ = emulators["netsmart"]
    status, payload = _raw_post(base, "/oauth2/token", body, length,
                                "application/x-www-form-urlencoded")
    assert status == 400, (case, payload)
    import json as _json

    assert _json.loads(payload)["error"] == "invalid_request", (case, payload)


@pytest.mark.parametrize("case", sorted(MALFORMED_BODIES))
def test_a_malformed_create_is_a_400_outcome_not_a_dropped_connection(emulators, case):
    """On a creatable type (Netsmart's DiagnosticReport) the body IS read,
    so this is the path that used to crash the handler."""
    import json as _json

    body, length = MALFORMED_BODIES[case]
    _, origin, _ = emulators["netsmart"]
    token = _client(emulators, "netsmart").access_token
    status, payload = _raw_post(
        origin, f"{VENDORS['netsmart'].fhir_path}/DiagnosticReport", body, length,
        "application/fhir+json", f"Authorization: Bearer {token}\r\n",
    )
    assert status == 400, (case, payload)
    outcome = _json.loads(payload)
    assert outcome["resourceType"] == "OperationOutcome", (case, payload)
    assert outcome["issue"][0]["code"] == "structure", (case, payload)


def test_a_create_with_a_non_string_id_is_refused(emulators):
    """`id` must satisfy FHIR's id grammar; an object would be stored,
    served back, and break every str(id) comparison downstream."""
    import requests

    base = _fhir_base(emulators, "netsmart")
    headers = {"Authorization": f"Bearer {_client(emulators, 'netsmart').access_token}"}
    for bad_id in ({"x": 1}, 7, "has space", "x" * 65, ""):
        response = requests.post(f"{base}/DiagnosticReport",
                                 json={"resourceType": "DiagnosticReport", "id": bad_id},
                                 headers=headers, timeout=10)
        assert response.status_code == 400, (bad_id, response.text)
        assert response.json()["issue"][0]["code"] == "value", (bad_id, response.text)


def test_a_create_whose_resource_type_differs_from_the_url_is_refused(emulators):
    import requests

    base = _fhir_base(emulators, "netsmart")
    headers = {"Authorization": f"Bearer {_client(emulators, 'netsmart').access_token}"}
    for body in ({"resourceType": "Observation"}, {"resourceType": 5}, {}):
        response = requests.post(f"{base}/DiagnosticReport", json=body, headers=headers, timeout=10)
        assert response.status_code == 400, (body, response.text)
        assert response.json()["issue"][0]["code"] == "invalid", (body, response.text)


def test_a_negative_search_offset_is_refused(emulators):
    """Python slicing would serve an empty page with a next link, which a
    paging client follows as a valid page."""
    import requests

    base = _fhir_base(emulators, "epic")
    headers = {"Authorization": f"Bearer {_client(emulators, 'epic').access_token}"}
    response = requests.get(f"{base}/Patient?_offset=-5", headers=headers, timeout=10)
    assert response.status_code == 400, response.text
    assert response.json()["resourceType"] == "OperationOutcome"


# ---------------------------------------------------------------------------
# Discovery says what the token endpoint honours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor", sorted(VENDORS))
def test_discovery_advertises_exactly_the_grants_the_vendor_honours(emulators, vendor):
    """client-confidential-asymmetric iff a JWT assertion is honoured,
    client-confidential-symmetric iff a secret is, and the signing-alg
    list only where the vendor's own discovery document publishes one
    (EmulatorVendor.signing_algs_published) - absent otherwise."""
    import requests

    entry = VENDORS[vendor]
    document = requests.get(f"{_fhir_base(emulators, vendor)}/.well-known/smart-configuration",
                            timeout=10).json()
    capabilities = set(document["capabilities"])
    assert ("client-confidential-asymmetric" in capabilities) is entry.accepts_jwt_assertion
    assert ("client-confidential-symmetric" in capabilities) is entry.accepts_client_secret
    methods = set(document["token_endpoint_auth_methods_supported"])
    assert ("private_key_jwt" in methods) is entry.accepts_jwt_assertion
    assert ("client_secret_basic" in methods) is entry.accepts_client_secret
    published = document.get("token_endpoint_auth_signing_alg_values_supported")
    if entry.accepts_jwt_assertion and entry.signing_algs_published:
        assert published == list(entry.assertion_algorithms)
    else:
        assert published is None, f"{vendor} publishes an alg list its vendor does not"


def test_reading_one_resource_by_id_works(emulators):
    """The path core/verify/freshness.py uses."""
    client = _client(emulators, "epic")
    resource = client.read_resource("Patient", "eSyn0001Patient")
    assert resource["resourceType"] == "Patient"
    assert resource["id"] == "eSyn0001Patient"


# ---------------------------------------------------------------------------
# Bulk export — including the vendors that do not have it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor", BULK_EXPORT_VENDORS)
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


@pytest.mark.parametrize("vendor", NO_BULK_EXPORT_VENDORS)
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
