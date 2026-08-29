# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for the Epic backend services JWT client assertion.

Epic's backend services flow does not use a client secret - it uses an
RS384-signed JWT client assertion, verified against a public key Epic
holds on file. These tests build a real keypair and check the assertion
against Epic's stated requirements the same way Epic's own authorization
server would: by verifying the signature with the public key alone and
inspecting the claims it produces.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt as pyjwt  # noqa: E402

from core.fhir.client import FHIRIngestionClient  # noqa: E402

CLIENT_ID = "test-client-id-0001"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"


def _generate_keypair(tmp_path: Path) -> tuple[bytes, bytes]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    priv = tmp_path / "private.pem"
    pub = tmp_path / "public.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"], capture_output=True, check=True)
    subprocess.run(
        ["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], capture_output=True, check=True
    )
    return priv.read_bytes(), pub.read_bytes()


def test_assertion_verifies_against_matching_public_key(tmp_path):
    private_key, public_key = _generate_keypair(tmp_path)
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)

    # This is exactly what Epic's authorization server does: verify the
    # signature using only the public key it has on file.
    decoded = pyjwt.decode(assertion, public_key, algorithms=["RS384"], audience=TOKEN_URL)
    assert decoded["iss"] == CLIENT_ID
    assert decoded["sub"] == CLIENT_ID
    assert decoded["aud"] == TOKEN_URL


def test_assertion_rejected_by_wrong_public_key(tmp_path):
    private_key, _ = _generate_keypair(tmp_path)
    _, wrong_public_key = _generate_keypair(tmp_path / "other")
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)

    try:
        pyjwt.decode(assertion, wrong_public_key, algorithms=["RS384"], audience=TOKEN_URL)
        raise AssertionError("assertion must not verify against an unrelated public key")
    except pyjwt.InvalidSignatureError:
        pass


def test_algorithm_is_rs384():
    """Epic's docs specify RS384. A different algorithm is rejected by
    Epic's authorization server regardless of whether the signature is
    otherwise valid, so this is worth pinning explicitly."""
    private_key, _ = _generate_keypair(Path("/tmp"))
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    header = pyjwt.get_unverified_header(assertion)
    assert header["alg"] == "RS384"


def test_expiry_within_five_minutes():
    """Epic requires exp no more than 5 minutes from issuance. A longer-
    lived assertion is rejected outright."""
    private_key, _ = _generate_keypair(Path("/tmp"))
    before = int(time.time())
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    claims = pyjwt.decode(assertion, options={"verify_signature": False})
    assert claims["exp"] - before <= 300
    assert claims["exp"] > before


def test_jti_is_unique_per_assertion():
    """Each token request must carry a unique jti - reusing one is a
    replay-attack surface Epic's server is expected to reject."""
    private_key, _ = _generate_keypair(Path("/tmp"))
    a = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    b = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    claims_a = pyjwt.decode(a, options={"verify_signature": False})
    claims_b = pyjwt.decode(b, options={"verify_signature": False})
    assert claims_a["jti"] != claims_b["jti"]


def test_kid_header_included_when_provided():
    """kid is only needed for JWK-Set-URL registrations, but when a caller
    supplies one it must land in the JWT header, since that's how Epic
    knows which key in the set to check against."""
    private_key, _ = _generate_keypair(Path("/tmp"))
    assertion = FHIRIngestionClient.build_client_assertion(
        CLIENT_ID, TOKEN_URL, private_key, jwt_kid="key-2026-01"
    )
    header = pyjwt.get_unverified_header(assertion)
    assert header["kid"] == "key-2026-01"


def test_kid_omitted_by_default():
    private_key, _ = _generate_keypair(Path("/tmp"))
    assertion = FHIRIngestionClient.build_client_assertion(CLIENT_ID, TOKEN_URL, private_key)
    header = pyjwt.get_unverified_header(assertion)
    assert "kid" not in header


def _run_all():
    import tempfile

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        if "tmp_path" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        else:
            fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
