# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The client assertion's signing algorithm is a per-vendor fact.

tests/test_epic_auth.py pins the assertion to RS384 because Epic
documents RS384. Other vendors document ES384, which needs an EC P-384
key rather than an RSA one - the two are not interchangeable, and pyjwt
refuses the wrong pairing before anything reaches the wire. So the
algorithm lives on the vendor profile (EMRProfile.assertion_algorithm,
sourced from each vendor's own documentation - see its chapter in
docs/EMR_CONNECTORS.md), the client signs with whatever the profile
says, and Settings.from_env() refuses a private key of the wrong type
for the selected vendor at startup, next to its cause, rather than as
the vendor's invalid_client mid-run.

These build real keys of both types and check the header the way an
authorization server does: by reading `alg` and verifying the signature
with the matching public key.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt as pyjwt  # noqa: E402

from core.config.settings import ConfigError, Settings  # noqa: E402
from core.fhir.client import ClientAssertionKeyError, FHIRIngestionClient  # noqa: E402
from core.fhir.emr_profiles import profile_for  # noqa: E402

CLIENT_ID = "test-client-id-0001"
TOKEN_URL = "https://ehr.example/oauth2/token"

# One vendor per algorithm, by name: Epic documents RS384; ModMed
# documents ES384 (citation in its docs/EMR_CONNECTORS.md chapter). Named
# rather than searched for, so a profile that lost its algorithm fails
# here loudly instead of being skipped.
RS384_VENDOR = "epic"
ES384_VENDOR = "modmed"


def _rsa_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _pem_pair(key, serialization)


def _ec_p384_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP384R1())
    return _pem_pair(key, serialization)


def _pem_pair(key, serialization) -> tuple[bytes, bytes]:
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


# ---------------------------------------------------------------------------
# The builder signs with the profile's algorithm
# ---------------------------------------------------------------------------

def test_the_profiles_record_one_vendor_per_algorithm():
    """The premise of everything below."""
    assert profile_for(RS384_VENDOR).assertion_algorithm == "RS384"
    assert profile_for(ES384_VENDOR).assertion_algorithm == "ES384"


def test_an_rsa_key_on_an_rs384_profile_signs_rs384():
    private_key, public_key = _rsa_keypair()
    profile = profile_for(RS384_VENDOR)

    assertion = FHIRIngestionClient.build_client_assertion(
        CLIENT_ID, TOKEN_URL, private_key, algorithm=profile.assertion_algorithm,
    )

    assert pyjwt.get_unverified_header(assertion)["alg"] == "RS384"
    # What the vendor's authorization server does: verify with the public
    # key on file, allowing only the documented algorithm.
    claims = pyjwt.decode(assertion, public_key, algorithms=["RS384"], audience=TOKEN_URL)
    assert claims["iss"] == CLIENT_ID
    assert claims["sub"] == CLIENT_ID


def test_an_ec_p384_key_on_an_es384_profile_signs_es384():
    private_key, public_key = _ec_p384_keypair()
    profile = profile_for(ES384_VENDOR)

    assertion = FHIRIngestionClient.build_client_assertion(
        CLIENT_ID, TOKEN_URL, private_key, algorithm=profile.assertion_algorithm,
    )

    assert pyjwt.get_unverified_header(assertion)["alg"] == "ES384"
    claims = pyjwt.decode(assertion, public_key, algorithms=["ES384"], audience=TOKEN_URL)
    assert claims["iss"] == CLIENT_ID
    assert claims["sub"] == CLIENT_ID
    # An ES384 assertion must not verify as RS384 - the vendor's server
    # allows one algorithm, not a family.
    with pytest.raises(pyjwt.exceptions.InvalidAlgorithmError):
        pyjwt.decode(assertion, public_key, algorithms=["RS384"], audience=TOKEN_URL)


def test_the_builder_still_defaults_to_rs384():
    """Every existing caller that never named an algorithm keeps signing
    the way Epic documents."""
    private_key, _ = _rsa_keypair()
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    assert pyjwt.get_unverified_header(assertion)["alg"] == "RS384"


def test_the_builder_refuses_a_key_of_the_wrong_family_before_signing():
    """An RSA key cannot sign ES384 and an EC key cannot sign RS384. The
    builder says so itself - naming both sides - rather than letting
    pyjwt's InvalidKeyError surface with a generator object where the
    expected type should be."""
    rsa_private, _ = _rsa_keypair()
    ec_private, _ = _ec_p384_keypair()

    with pytest.raises(ClientAssertionKeyError, match="ES384") as raised:
        FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, rsa_private,
                                                   algorithm="ES384")
    assert "RSA" in str(raised.value)

    with pytest.raises(ClientAssertionKeyError, match="RS384") as raised:
        FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, ec_private,
                                                   algorithm="RS384")
    assert "EC" in str(raised.value)


def test_the_es384_claims_match_the_rs384_ones():
    """Only the signature differs. iss/sub/aud/jti/exp are RFC 7523, not
    per-vendor - a vendor-specific claim set would be a second bug."""
    rsa_private, _ = _rsa_keypair()
    ec_private, _ = _ec_p384_keypair()

    rs = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, rsa_private,
                                                    algorithm="RS384")
    es = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, ec_private,
                                                    algorithm="ES384")
    rs_claims = pyjwt.decode(rs, options={"verify_signature": False})
    es_claims = pyjwt.decode(es, options={"verify_signature": False})

    assert set(rs_claims) == set(es_claims)
    for claim in ("iss", "sub", "aud"):
        assert rs_claims[claim] == es_claims[claim]
    assert es_claims["exp"] - es_claims["iat"] <= 300


# ---------------------------------------------------------------------------
# The dispatcher signs with the profile's algorithm - the real path
# ---------------------------------------------------------------------------

class _FakeTokenResponse:
    """The minimum authenticate() reads off a requests.Response."""
    ok = True
    status_code = 200
    headers: dict = {}

    def __init__(self, prepared):
        self.request = prepared

    def json(self):
        return {"access_token": "emulated-token", "token_type": "Bearer", "expires_in": 3600}

    def raise_for_status(self):
        return None


def _capture_token_request(monkeypatch):
    """Intercept the one HTTP send authenticate() makes and hand back the
    form it posted. No server: this is about what the client SIGNED."""
    import requests

    posted = {}

    def send(self, prepared, **kwargs):
        posted.update({k: v[0] for k, v in parse_qs(prepared.body).items()})
        return _FakeTokenResponse(prepared)

    monkeypatch.setattr(requests.Session, "send", send)
    return posted


def _settings(private_key_pem: bytes):
    from types import SimpleNamespace

    return SimpleNamespace(
        fhir_client_id=CLIENT_ID,
        fhir_client_secret=None,
        fhir_token_url=TOKEN_URL,
        fhir_private_key_pem=private_key_pem,
        fhir_jwt_kid=None,
    )


@pytest.mark.parametrize("vendor, keypair, algorithm", [
    (RS384_VENDOR, _rsa_keypair, "RS384"),
    (ES384_VENDOR, _ec_p384_keypair, "ES384"),
])
def test_authenticate_from_settings_signs_with_the_profiles_algorithm(
        monkeypatch, vendor, keypair, algorithm):
    """authenticate_from_settings() is the one place the profile becomes
    a token request (see its docstring); a builder that can sign ES384
    but a dispatcher that never asks it to would fail every ES384 vendor
    at startup. This does not depend on how the builder is parameterised
    - only on what actually went on the wire."""
    private_key, public_key = keypair()
    posted = _capture_token_request(monkeypatch)

    client = FHIRIngestionClient(
        base_url="https://ehr.example/fhir", profile=profile_for(vendor),
        storage=None, encryptor=None, audit=None, retention_years=10,
    )
    client.authenticate_from_settings(_settings(private_key))

    assert client.access_token == "emulated-token"
    assert posted["grant_type"] == "client_credentials"
    assertion = posted["client_assertion"]
    assert pyjwt.get_unverified_header(assertion)["alg"] == algorithm
    assert pyjwt.decode(assertion, public_key, algorithms=[algorithm],
                        audience=TOKEN_URL)["iss"] == CLIENT_ID


# ---------------------------------------------------------------------------
# Settings refuse a key of the wrong type for the vendor, at startup
# ---------------------------------------------------------------------------

def _deployment_env(monkeypatch, tmp_path, vendor: str, private_key_pem: bytes) -> None:
    """The variables Settings.from_env() requires, with a REAL private
    key on disk (unlike the placeholder other suites use) - the key's
    type is the thing under test."""
    key = tmp_path / "fhir-private-key.pem"
    key.write_bytes(private_key_pem)

    for name, value in {
        "PHI_AI_CLOUD_PROVIDER": "aws",
        "PHI_AI_STORAGE_BUCKET": "records-bucket",
        "PHI_AI_STORAGE_REGION": "us-east-1",
        "PHI_AI_KMS_KEY_ID": "alias/records",
        "PHI_AI_AUDIT_BUCKET": "audit-bucket",
        "PHI_AI_AUDIT_KMS_KEY_ID": "alias/audit",
        "PHI_AI_FHIR_BASE_URL": "https://ehr.example/fhir",
        "PHI_AI_FHIR_TOKEN_URL": TOKEN_URL,
        "PHI_AI_FHIR_CLIENT_ID": CLIENT_ID,
        "PHI_AI_FHIR_PRIVATE_KEY_PATH": str(key),
        "PHI_AI_EMR_VENDOR": vendor,
    }.items():
        monkeypatch.setenv(name, value)
    # Nothing from the developer's own shell may steer the outcome.
    for name in ("PHI_AI_FHIR_CLIENT_SECRET", "PHI_AI_RETENTION_RULESET_PATH",
                 "PHI_AI_RETENTION_YEARS", "PHI_AI_RETENTION_YEARS_OVERRIDES"):
        monkeypatch.delenv(name, raising=False)


def test_an_rsa_key_for_an_es384_vendor_is_refused_at_startup(monkeypatch, tmp_path):
    """The mismatch this whole file exists for. The error names the
    algorithm the vendor requires and the vendor, so the operator can act
    on it without reading the vendor's docs first."""
    rsa_private, _ = _rsa_keypair()
    _deployment_env(monkeypatch, tmp_path, ES384_VENDOR, rsa_private)

    with pytest.raises(ConfigError, match="ES384") as raised:
        Settings.from_env()
    assert ES384_VENDOR in str(raised.value).lower()


def test_an_ec_key_for_an_rs384_vendor_is_refused_at_startup(monkeypatch, tmp_path):
    """The mirror image: an EC key cannot sign RS384 either, and pyjwt's
    InvalidKeyError mid-run is a worse place to learn that than here."""
    ec_private, _ = _ec_p384_keypair()
    _deployment_env(monkeypatch, tmp_path, RS384_VENDOR, ec_private)

    with pytest.raises(ConfigError, match="RS384"):
        Settings.from_env()


def test_a_matching_key_type_loads_for_either_algorithm(monkeypatch, tmp_path):
    """The check must not refuse the RIGHT pairing - in either direction."""
    rsa_private, _ = _rsa_keypair()
    _deployment_env(monkeypatch, tmp_path, RS384_VENDOR, rsa_private)
    settings = Settings.from_env()
    assert settings.emr_vendor == RS384_VENDOR
    assert settings.fhir_private_key_pem == rsa_private

    ec_private, _ = _ec_p384_keypair()
    _deployment_env(monkeypatch, tmp_path, ES384_VENDOR, ec_private)
    settings = Settings.from_env()
    assert settings.emr_vendor == ES384_VENDOR
    assert settings.fhir_private_key_pem == ec_private
# Made by Ryan Gomez & Co. Inc.
