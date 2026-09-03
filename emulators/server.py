# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
A FHIR EMR emulator: auth, search, bulk export, writes, SMART launch.

ALL DATA SERVED HERE IS SYNTHETIC. The dataset is imported from
scripts/mock_epic_server.py rather than copied, so there is one set of
fake patients in this repository and no chance of the two drifting into
disagreeing about what "eSyn0001Patient" means.

WHAT IT EMULATES, and why each piece is here:

  /metadata                        CapabilityStatement. core/fhir/delivery
                                   refuses to write anything the server
                                   does not advertise, so this is what
                                   makes that check testable at all.
  /.well-known/smart-configuration SMART discovery for in-context launch.
  /.well-known/jwks.json           the emulator's real signing key, so
                                   id_token signature verification is
                                   genuinely exercised rather than stubbed.
  /oauth2/authorize                SMART App Launch, including PKCE.
  /oauth2/token                    all three grants, each accepted only by
                                   the vendors that accept it live. A
                                   client_assertion is verified against
                                   the VENDOR'S assertion_algorithms (the
                                   JWT header never picks the verifier);
                                   its audience, expiry, required claims
                                   and iss==sub are always checked, and
                                   its signature too once the client's
                                   JWK Set is registered on the state
                                   (build_server(client_jwks=...),
                                   `python -m emulators --client-jwks`).
                                   Without a registered JWK Set the server
                                   logs a WARNING at start: signatures are
                                   then NOT verified. A client_secret is
                                   checked against registered credentials
                                   the same way (client_credentials=...).
  /{Type} and /{Type}/{id}         search and read, with real pagination.
  POST /{Type}                     create, honouring If-None-Exist where
                                   the vendor supports it.
  /$export + status + NDJSON       Bulk Data Export, async as specified.
  /emulator/launch                 a page that starts an EHR launch into
                                   the platform, so in-context SSO can be
                                   clicked through end to end.

THE ID_TOKEN IS REALLY SIGNED. An emulator that returned an unsigned or
fixed id_token would let a client that skips signature verification pass
its tests - which is precisely the defect worth catching, since an
unverified id_token is an identity assertion by whoever sent it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emulators.vendors import VENDORS, EmulatorVendor  # noqa: E402

log = logging.getLogger("emulator")

BULK_READY_AFTER_POLLS = 2  # first poll says "in progress", as a real server would


def _load_dataset() -> dict[str, list[dict]]:
    """Import the synthetic dataset from the existing Epic mock."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "mock_epic_server.py"
    spec = importlib.util.spec_from_file_location("mock_epic_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {k: list(v) for k, v in module.RESOURCES_BY_TYPE.items()}


class EmulatorState:
    """Everything one running emulator holds. Per-instance, never global,
    so every vendor in VENDORS can run in one process without sharing
    patients or tokens."""

    def __init__(self, vendor: EmulatorVendor, base_url: str, record_launch_url: str = "",
                 client_jwks: Optional[dict] = None,
                 client_credentials: Optional[dict[str, str]] = None):
        self.vendor = vendor
        self.base_url = base_url.rstrip("/")
        self.record_launch_url = record_launch_url
        self.resources = _load_dataset()

        # The CLIENT'S public JWK Set, when a test hands it over - what a
        # real vendor holds from registration. With it, a client_assertion
        # is signature-verified (an RSA key for RS*, an EC key for ES*);
        # without it the token endpoint still checks the header's alg
        # against the vendor's tuple and the assertion's audience, expiry,
        # required claims and iss==sub, but NOT the signature, because the
        # emulator holds no client key - build_server logs a WARNING.
        self.client_jwks: Optional[dict] = client_jwks

        # The CLIENT'S registered client_id -> client_secret pairs, for
        # the vendors that honour a secret. With them, any other id or
        # secret is invalid_client; without them (the default) any secret
        # mints a token and build_server logs the same WARNING.
        self.client_credentials: Optional[dict[str, str]] = client_credentials

        # Resources written BY a delivery, kept separate from the seeded
        # dataset so a test can tell "was already here" from "we put it
        # here" - which is the whole question delivery verification asks.
        self.written: list[dict] = []

        self.tokens: dict[str, dict] = {}
        self.auth_codes: dict[str, dict] = {}
        self.bulk_jobs: dict[str, dict] = {}

        self._key = None
        self._kid = "emulator-key-1"

    # -- signing key ------------------------------------------------

    @property
    def signing_key(self):
        """RSA key generated once per run.

        Real, not a fixture: the id_token is signed with it and published
        through JWKS, so a client that verifies signatures is exercised
        and one that does not would still pass - which is exactly the
        difference worth being able to test.
        """
        if self._key is None:
            from cryptography.hazmat.primitives.asymmetric import rsa

            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return self._key

    def jwks(self) -> dict:
        from cryptography.hazmat.primitives.asymmetric import rsa

        public = self.signing_key.public_key()
        numbers = public.public_numbers()

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self._kid,
            "n": b64(numbers.n), "e": b64(numbers.e),
        }]}

    def issue_id_token(self, client_id: str, nonce: Optional[str], username: str) -> str:
        import jwt

        from cryptography.hazmat.primitives import serialization

        pem = self.signing_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        now = int(time.time())
        claims = {
            "iss": f"{self.base_url}{self.vendor.fhir_path}",
            "sub": f"emulator|{username}",
            "aud": client_id,
            "iat": now,
            "exp": now + 300,
            "preferred_username": username,
            "fhirUser": f"{self.base_url}{self.vendor.fhir_path}/Practitioner/emulator-prac-1",
        }
        if nonce:
            claims["nonce"] = nonce
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": self._kid})

    # -- data -------------------------------------------------------

    def all_of(self, resource_type: str) -> list[dict]:
        return list(self.resources.get(resource_type, [])) + [
            r for r in self.written if r.get("resourceType") == resource_type
        ]

    def find(self, resource_type: str, resource_id: str) -> Optional[dict]:
        for resource in self.all_of(resource_type):
            if str(resource.get("id")) == resource_id:
                return resource
        return None


# FHIR R4 `id` datatype: [A-Za-z0-9\-\.]{1,64}.
FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def _key_type_for(alg: str) -> Optional[str]:
    """The JWK `kty` a JWS algorithm verifies with - RSA or EC - read off
    PyJWT's own algorithm object (the rule core/fhir/client._algorithm_needs
    applies), not from a prefix table kept here that would drift the day
    a vendor documents an algorithm outside RS*/PS*/ES*. None for anything
    PyJWT does not know or that is not asymmetric (HS*, none)."""
    import jwt as pyjwt
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    try:
        signer = pyjwt.get_algorithm_by_name(alg)
    except NotImplementedError:
        return None
    if isinstance(signer, RSAAlgorithm):
        return "RSA"
    if isinstance(signer, ECAlgorithm):
        return "EC"
    return None


def _outcome(code: str, text: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": text}],
    }


def _bundle(resources: list[dict], total: Optional[int] = None,
            next_url: Optional[str] = None) -> dict:
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total if total is not None else len(resources),
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        bundle["link"] = [{"relation": "next", "url": next_url}]
    return bundle


class EmulatorHandler(BaseHTTPRequestHandler):
    state: EmulatorState = None  # set per server instance

    server_version = "phi-ai-emulator"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------

    def _json(self, status: int, payload: dict, headers: Optional[dict] = None,
              content_type: str = "application/fhir+json") -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _oauth(self, status: int, payload: dict) -> None:
        """A token-endpoint response: the application/json body RFC 6749
        s5.1 and s5.2 specify, not FHIR JSON."""
        self._json(status, payload, content_type="application/json")

    def _text(self, status: int, body: str, content_type: str = "text/html") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    @property
    def fhir_base(self) -> str:
        return f"{self.state.base_url}{self.state.vendor.fhir_path}"

    def _strip_prefix(self, path: str) -> Optional[str]:
        prefix = self.state.vendor.fhir_path
        if path.startswith(prefix):
            return path[len(prefix):] or "/"
        return None

    # -- GET --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        vendor = self.state.vendor

        if path == "/.well-known/smart-configuration" or path.endswith(
            "/.well-known/smart-configuration"
        ):
            return self._smart_configuration()
        if path.endswith("/.well-known/jwks.json"):
            return self._json(200, self.state.jwks())
        if path == "/oauth2/authorize":
            return self._authorize(query)
        if path == "/emulator/launch":
            return self._launch_page(query)
        if path == "/emulator/health":
            # `written` lets a caller that attaches to a running emulator
            # tell a pristine one from one holding another run's records.
            return self._json(200, {
                "vendor": vendor.key, "status": "ok",
                "written": len(self.state.written),
                "verifies_assertion_signatures": bool(self.state.client_jwks),
                "verifies_client_secrets": bool(self.state.client_credentials),
            })

        fhir_path = self._strip_prefix(path)
        if fhir_path is None:
            return self._json(404, _outcome("not-found", f"no route for {path}"))

        if fhir_path == "/metadata":
            return self._capability_statement()

        # Bulk export status polling
        status_match = re.match(r"^/BulkRequest/([^/]+)$", fhir_path)
        if status_match:
            return self._bulk_status(status_match.group(1))

        file_match = re.match(r"^/BulkFile/([^/]+)/([^/]+)$", fhir_path)
        if file_match:
            return self._bulk_file(file_match.group(1), file_match.group(2))

        # $export kickoff at system, Patient or Group level: the shared
        # server answers all three (each vendor's entry says which it
        # documents live), and a vendor without bulk gets an
        # OperationOutcome from _bulk_kickoff, never an empty 2xx.
        if fhir_path.rstrip("/").endswith("$export"):
            return self._bulk_kickoff(query)

        read_match = re.match(r"^/([A-Za-z]+)/([^/$]+)$", fhir_path)
        if read_match:
            return self._read(read_match.group(1), read_match.group(2))

        search_match = re.match(r"^/([A-Za-z]+)$", fhir_path)
        if search_match:
            return self._search(search_match.group(1), query)

        self._json(404, _outcome("not-found", f"no route for {path}"))

    # -- POST -------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/oauth2/token":
            return self._token()

        fhir_path = self._strip_prefix(path)
        if fhir_path is None:
            return self._json(404, _outcome("not-found", f"no route for {path}"))

        if fhir_path.rstrip("/").endswith("$export"):
            return self._bulk_kickoff(parse_qs(parsed.query))

        create_match = re.match(r"^/([A-Za-z]+)$", fhir_path)
        if create_match:
            return self._create(create_match.group(1))

        self._json(404, _outcome("not-found", f"no route for POST {path}"))

    def do_DELETE(self) -> None:  # noqa: N802
        fhir_path = self._strip_prefix(urlparse(self.path).path) or ""
        match = re.match(r"^/BulkRequest/([^/]+)$", fhir_path)
        if match:
            self.state.bulk_jobs.pop(match.group(1), None)
            self.send_response(202)
            self.end_headers()
            return
        self._json(404, _outcome("not-found", "no route"))

    # -- discovery --------------------------------------------------

    def _smart_configuration(self) -> None:
        """SMART discovery, with the client-authentication capabilities
        DERIVED from the vendor entry: client-confidential-asymmetric
        exactly when the token endpoint honours a JWT assertion,
        client-confidential-symmetric exactly when it honours a secret,
        and token_endpoint_auth_signing_alg_values_supported only where
        the entry says the vendor's own discovery document publishes it
        (signing_algs_published) - absent otherwise, as the vendor's is."""
        vendor = self.state.vendor
        capabilities = [
            "launch-ehr", "context-ehr-patient", "context-ehr-encounter",
            "permission-patient", "sso-openid-connect",
        ]
        auth_methods = []
        if vendor.accepts_jwt_assertion:
            capabilities.append("client-confidential-asymmetric")
            auth_methods.append("private_key_jwt")
        if vendor.accepts_client_secret:
            capabilities.append("client-confidential-symmetric")
            auth_methods += ["client_secret_basic", "client_secret_post"]
        document = {
            "issuer": self.fhir_base,
            "jwks_uri": f"{self.fhir_base}/.well-known/jwks.json",
            "authorization_endpoint": f"{self.state.base_url}/oauth2/authorize",
            "token_endpoint": f"{self.state.base_url}/oauth2/token",
            "token_endpoint_auth_methods_supported": auth_methods,
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "capabilities": capabilities,
            "scopes_supported": [
                "launch", "openid", "fhirUser",
                "patient/Patient.read", "patient/Patient.rs",
            ],
        }
        if vendor.accepts_jwt_assertion and vendor.signing_algs_published:
            document["token_endpoint_auth_signing_alg_values_supported"] = list(
                vendor.assertion_algorithms
            )
        self._json(200, document)

    def _capability_statement(self) -> None:
        vendor = self.state.vendor
        resources = []
        for resource_type in sorted(set(self.state.resources) | set(vendor.creatable)):
            interactions = [{"code": "read"}, {"code": "search-type"}]
            if resource_type in vendor.creatable:
                interactions.append({"code": "create"})
            resources.append({"type": resource_type, "interaction": interactions})

        rest: dict[str, Any] = {"mode": "server", "resource": resources}
        if vendor.supports_bulk_export:
            rest["operation"] = [{"name": "export",
                                  "definition": "http://hl7.org/fhir/uv/bulkdata/OperationDefinition/export"}]

        self._json(200, {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "fhirVersion": "4.0.1",
            "format": ["application/fhir+json"],
            "software": {"name": f"{vendor.name} emulator (synthetic data only)"},
            "rest": [rest],
        })

    # -- auth -------------------------------------------------------

    def _authorize(self, query: dict) -> None:
        """SMART App Launch authorize. Auto-approves - there is no human
        to consent here - but validates everything a real server would."""
        redirect_uri = (query.get("redirect_uri") or [""])[0]
        state = (query.get("state") or [""])[0]

        if not redirect_uri:
            return self._json(400, _outcome("invalid", "redirect_uri is required"))

        challenge = (query.get("code_challenge") or [""])[0]
        method = (query.get("code_challenge_method") or [""])[0]
        if not challenge or method != "S256":
            # Refused rather than tolerated: SMART v2 requires PKCE, and a
            # client that omits it should fail here rather than in
            # production.
            return self._redirect(
                f"{redirect_uri}?{urlencode({'error': 'invalid_request', 'error_description': 'PKCE with S256 is required', 'state': state})}"
            )

        code = secrets.token_urlsafe(24)
        self.state.auth_codes[code] = {
            "client_id": (query.get("client_id") or [""])[0],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "nonce": (query.get("nonce") or [""])[0],
            # The launch context a real EHR would resolve from its opaque
            # launch token. Fixed here so tests are deterministic.
            "patient": (query.get("patient") or ["eSyn0001Patient"])[0],
            "encounter": (query.get("encounter") or ["eSynEnc0001"])[0],
            "issued_at": time.time(),
        }
        self._redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")

    MAX_BODY_BYTES = 1024 * 1024  # 1 MiB: larger than any token form or one FHIR resource

    def _read_body(self) -> tuple[Optional[str], Optional[str]]:
        """The request body as UTF-8 text, or (None, why-not). A non-integer
        Content-Length, a body over MAX_BODY_BYTES, or bytes that are not
        UTF-8 each come back as a reason the caller turns into a 400 -
        never a traceback, and never a dropped connection, which a client
        sees as a network error rather than a refusal."""
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            return None, f"Content-Length {raw_length!r} is not an integer"
        if length < 0:
            return None, f"Content-Length {raw_length!r} is negative"
        if length > self.MAX_BODY_BYTES:
            return None, f"body of {length} bytes exceeds the {self.MAX_BODY_BYTES}-byte limit"
        try:
            return self.rfile.read(length).decode("utf-8", errors="strict"), None
        except UnicodeDecodeError as exc:
            return None, f"body is not UTF-8 ({exc.reason} at byte {exc.start})"

    def _token(self) -> None:
        body, problem = self._read_body()
        if body is None:
            return self._oauth(400, {"error": "invalid_request", "error_description": problem})
        form = parse_qs(body)
        grant = (form.get("grant_type") or [""])[0]
        vendor = self.state.vendor

        if grant == "authorization_code":
            return self._token_authorization_code(form)

        if grant == "client_credentials":
            assertion = (form.get("client_assertion") or [""])[0]
            secret = (form.get("client_secret") or [""])[0]

            # Oracle Health documents the Basic authentication scheme
            # (RFC 2617) for system accounts - the secret arrives in the
            # Authorization header, not the form body. Recognise it so a
            # client speaking exactly what those docs describe passes,
            # and one sending a secret to a JWT-only vendor still fails.
            basic = self.headers.get("Authorization") or ""
            if not secret and basic.startswith("Basic "):
                try:
                    decoded = base64.b64decode(basic[6:]).decode("utf-8")
                except Exception:
                    decoded = ""
                if ":" in decoded:
                    basic_id, secret = decoded.split(":", 1)
                    form.setdefault("client_id", [basic_id])

            if assertion and not vendor.accepts_jwt_assertion:
                # What athenahealth actually does. A client that assumes
                # every vendor takes an assertion should fail here.
                return self._oauth(400, {
                    "error": "invalid_client",
                    "error_description": f"{vendor.name} does not accept a JWT client "
                                         "assertion; use client_secret",
                })
            if secret and not vendor.accepts_client_secret:
                return self._oauth(400, {
                    "error": "invalid_client",
                    "error_description": f"{vendor.name} requires a signed JWT client "
                                         "assertion, not a client secret",
                })
            if not assertion and not secret:
                return self._oauth(400, {
                    "error": "invalid_request",
                    "error_description": "no client_assertion or client_secret",
                })

            if assertion:
                # RFC 7523 s2.2: an assertion used for client
                # authentication travels with client_assertion_type set
                # to the jwt-bearer URN. Anything else is a malformed
                # request, refused before the assertion is even parsed.
                assertion_type = (form.get("client_assertion_type") or [""])[0]
                if assertion_type != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer":
                    return self._oauth(400, {
                        "error": "invalid_request",
                        "error_description": "client_assertion requires client_assertion_type="
                                             "urn:ietf:params:oauth:client-assertion-type:"
                                             f"jwt-bearer (got {assertion_type!r})",
                    })
                # The client is authenticated before its request is
                # evaluated (RFC 6749 s4.4.2: the authorization server
                # MUST authenticate the client), so a bad assertion is
                # invalid_client even when the scope would also have
                # failed. The verifier is pinned to the vendor's
                # assertion_algorithms - see _verify_client_assertion.
                reason = self._verify_client_assertion(assertion)
                if reason is not None:
                    # RFC 7523 s3.2: a rejected client JWT is
                    # invalid_client - the same shape as the wrong-grant
                    # refusals above, at the 400 RFC 6749 s5.2 gives an
                    # error response (the assertion travels in the body,
                    # so s5.2's Authorization-header 401 rule is not in
                    # play). The leading phrase is the text Netsmart
                    # documents for a rejected assertion (its Common
                    # Errors page); no other entry in VENDORS cites a
                    # documented text for this case, so the same neutral
                    # phrase serves them all, and the diagnostic after the
                    # colon is the emulator's own - live servers withhold
                    # it, which is exactly why a test wants it.
                    return self._oauth(400, {
                        "error": "invalid_client",
                        "error_description": f"Invalid client assertion JWT: {reason}",
                    })
            else:
                # A client secret authenticates only against the
                # registered client_id:secret pairs, when any are
                # registered (build_server(client_credentials=...)); the
                # client_id comes from the form body or the Basic header.
                reason = self._verify_client_secret(form, secret)
                if reason is not None:
                    return self._oauth(400, {
                        "error": "invalid_client", "error_description": reason,
                    })

            if vendor.requires_token_scope:
                # What Oracle Health actually does: every scope must be
                # requested explicitly. A client tuned on Epic - whose
                # backend token request has no scope parameter at all -
                # should fail here, in a test, not against a real tenant.
                scope = (form.get("scope") or [""])[0]
                if not scope:
                    return self._oauth(400, {
                        "error": "invalid_scope",
                        "error_description": f"{vendor.name} requires explicit "
                                             "system/{Type}.{permission} scopes in the "
                                             "token request",
                    })
            if vendor.refuses_wildcard_scope and "*" in (form.get("scope") or [""])[0]:
                # Only where the vendor documents the refusal (Oracle
                # Health); elsewhere a wildcard passes, as far as the
                # vendor's documentation says.
                return self._oauth(400, {
                    "error": "invalid_scope",
                    "error_description": f"{vendor.name} does not support wildcard scopes",
                })

            token = secrets.token_urlsafe(32)
            self.state.tokens[token] = {"grant": "client_credentials"}
            return self._oauth(200, {
                "access_token": token, "token_type": "Bearer", "expires_in": 3600,
            })

        self._oauth(400, {"error": "unsupported_grant_type", "error_description": grant})

    def _token_authorization_code(self, form: dict) -> None:
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        record = self.state.auth_codes.pop(code, None)

        if record is None:
            return self._oauth(400, {"error": "invalid_grant",
                                    "error_description": "unknown or already-used code"})

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        if expected != record["code_challenge"]:
            return self._oauth(400, {"error": "invalid_grant",
                                    "error_description": "PKCE verifier does not match"})

        token = secrets.token_urlsafe(32)
        self.state.tokens[token] = {"grant": "authorization_code"}
        self._oauth(200, {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "launch openid fhirUser patient/Patient.read",
            # The launch context - what makes it an in-context launch.
            "patient": record["patient"],
            "encounter": record["encounter"],
            "id_token": self.state.issue_id_token(
                record["client_id"], record["nonce"], "emulator.clinician"
            ),
        })

    def _verify_client_assertion(self, assertion: str) -> Optional[str]:
        """Why this vendor refuses the client_assertion, or None if it stands.

        One verifier for every algorithm, pinned to the VENDOR'S
        assertion_algorithms tuple: the JWT header's alg is compared
        against that tuple and never used to pick the verifier, so an
        ES384-only vendor refuses the RS384 assertion every other profile
        signs, an RSA-only vendor refuses ES384, and 'none' or HS* never
        reach a key. That is the SMART client-confidential-asymmetric rule
        (registered keys are matched by kid and by a kty consistent with
        the alg; no match, or more than one, is a failure) with RFC 7523
        s3.2's outcome, invalid_client - both cited in emulators/vendors.py.

        Audience, expiry, the required claims (iss, sub, aud, exp, jti)
        and iss == sub are ALWAYS verified with PyJWT. The signature is
        verified too when the client's JWK Set is registered on the state
        - an RSA public key for RS*, an EC public key for ES*, the same
        decode call for both. Without a registered key (the emulator
        holds no client key of its own) the signature is the one thing
        that cannot be checked, and build_server logs a WARNING saying
        so; an expired, mis-addressed or malformed assertion is still
        refused. A test that needs the signature exercised registers the
        JWKS (tests/test_emulator_integration.py and scripts/e2e_matrix.py
        both do).
        """
        import jwt as pyjwt

        vendor = self.state.vendor
        accepted = vendor.assertion_algorithms
        audience = f"{self.state.base_url}/oauth2/token"
        required = ["iss", "sub", "aud", "exp", "jti"]
        try:
            header = pyjwt.get_unverified_header(assertion)
        except pyjwt.PyJWTError:
            return "client_assertion is not a JWT"

        alg = header.get("alg")
        if alg not in accepted:
            return (f"assertion is signed {alg!r}; {vendor.name} verifies "
                    f"{', '.join(accepted)} only")

        jwks = self.state.client_jwks
        if not jwks:
            # No client key registered: every claim is checked, the
            # signature is not (verify_signature=False also disables
            # PyJWT's exp/aud checks, so they are re-enabled explicitly).
            try:
                claims = pyjwt.decode(
                    assertion, options={
                        "verify_signature": False, "verify_exp": True, "verify_nbf": True,
                        "verify_aud": True, "require": required,
                    },
                    audience=audience, algorithms=list(accepted),
                )
            except (pyjwt.PyJWTError, ValueError) as exc:
                return f"{type(exc).__name__}: {exc}"
            if claims.get("iss") != claims.get("sub"):
                return "iss and sub must both be the client_id"
            return None

        wanted_kty = _key_type_for(alg)
        if wanted_kty is None:
            return f"{alg!r} is not an asymmetric JWS algorithm this emulator can verify"
        keys = [k for k in jwks.get("keys", []) if k.get("kty") == wanted_kty]
        kid = header.get("kid")
        if kid is not None:
            keys = [k for k in keys if k.get("kid") == kid]
        if len(keys) != 1:
            return (f"{len(keys)} registered keys match kid={kid!r} and kty={wanted_kty}; "
                    "exactly one must")

        try:
            claims = pyjwt.decode(
                assertion,
                pyjwt.PyJWK(keys[0]).key,
                algorithms=list(accepted),  # the vendor's tuple, never the header
                audience=audience,
                options={"require": required},
            )
        except (pyjwt.PyJWTError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
        if claims.get("iss") != claims.get("sub"):
            return "iss and sub must both be the client_id"
        return None

    def _verify_client_secret(self, form: dict, secret: str) -> Optional[str]:
        """Why this vendor refuses the client secret, or None if it stands.
        Checked against the registered client_id -> secret pairs; with none
        registered nothing can be checked (build_server warned)."""
        import hmac

        registered = self.state.client_credentials
        if not registered:
            return None
        client_id = (form.get("client_id") or [""])[0]
        expected = registered.get(client_id)
        if expected is None or not hmac.compare_digest(expected, secret):
            return f"client_id {client_id!r} and client_secret do not match a registered client"
        return None

    # -- FHIR read/search -------------------------------------------

    def _read(self, resource_type: str, resource_id: str) -> None:
        resource = self.state.find(resource_type, resource_id)
        if resource is None:
            return self._json(404, _outcome("not-found",
                                            f"{resource_type}/{resource_id} not found"))
        self._json(200, resource)

    def _search(self, resource_type: str, query: dict) -> None:
        vendor = self.state.vendor
        resources = self.state.all_of(resource_type)

        if not resources and resource_type not in self.state.resources:
            return self._json(404, _outcome("not-supported",
                                            f"{resource_type} is not supported here"))

        # Delivery verification searches on _source; supporting it is what
        # lets core/verify/delivery.py be exercised end to end.
        source = (query.get("_source") or [None])[0]
        if source:
            resources = [
                r for r in resources
                if source in str((r.get("meta") or {}).get("source", ""))
            ]

        patient = (query.get("patient") or query.get("subject") or [None])[0]
        if patient:
            wanted = patient.split("/")[-1]
            resources = [
                r for r in resources
                if wanted in json.dumps(r.get("subject", {}) or r.get("patient", {}))
            ]

        if (query.get("_summary") or [""])[0] == "count":
            return self._json(200, {"resourceType": "Bundle", "type": "searchset",
                                    "total": len(resources)})

        try:
            offset = int((query.get("_offset") or ["0"])[0])
        except ValueError:
            return self._json(400, _outcome("invalid", "_offset must be an integer"))
        if offset < 0:
            # Python slicing would quietly serve an empty page with a next
            # link, which a paging client follows as a valid page.
            return self._json(400, _outcome("invalid", "_offset must not be negative"))
        page = resources[offset: offset + vendor.page_size]
        next_offset = offset + vendor.page_size
        next_url = None
        if next_offset < len(resources):
            params = {k: v[0] for k, v in query.items() if k != "_offset"}
            params["_offset"] = str(next_offset)
            next_url = f"{self.fhir_base}/{resource_type}?{urlencode(params)}"

        self._json(200, _bundle(page, total=len(resources), next_url=next_url))

    # -- FHIR create ------------------------------------------------

    def _create(self, resource_type: str) -> None:
        vendor = self.state.vendor
        if resource_type not in vendor.creatable:
            # The OperationOutcome a real server returns for a type its
            # CapabilityStatement did not advertise create for - never a
            # 500 and never an empty 2xx a delivery could mistake for
            # success. Checked before the body is read, so a read-only
            # vendor refuses every POST the same way.
            return self._json(422, _outcome(
                "not-supported",
                f"{vendor.name} does not accept create for {resource_type}",
            ))

        body, problem = self._read_body()
        if body is None:
            return self._json(400, _outcome("structure", problem))
        try:
            resource = json.loads(body)
        except json.JSONDecodeError:
            return self._json(400, _outcome("structure", "body is not valid JSON"))
        if not isinstance(resource, dict):
            # An OperationOutcome, never a traceback: a list or scalar
            # body would otherwise 500 on resource.get() below.
            return self._json(400, _outcome("structure",
                                            "body must be one FHIR resource object"))
        if resource.get("resourceType") != resource_type:
            return self._json(400, _outcome(
                "invalid",
                f"resourceType must be the string {resource_type!r} (the type in the URL); "
                f"got {resource.get('resourceType')!r}",
            ))
        if "id" in resource and not (
            isinstance(resource["id"], str) and FHIR_ID.fullmatch(resource["id"])
        ):
            # FHIR's id grammar: a string of [A-Za-z0-9-.], 1 to 64 long.
            # Anything else would be stored, served back by find(), and
            # break every str(resource["id"]) comparison downstream.
            return self._json(400, _outcome(
                "value", "id must be a string matching [A-Za-z0-9-.]{1,64}",
            ))

        if_none_exist = self.headers.get("If-None-Exist")
        if if_none_exist:
            if not vendor.supports_conditional_create:
                # A vendor that ignores the header would silently
                # duplicate. Returning 412 makes that visible in a test
                # rather than as a second copy in a chart.
                return self._json(412, _outcome(
                    "not-supported",
                    f"{vendor.name} does not support conditional create (If-None-Exist)",
                ))
            existing = [
                r for r in self.state.written
                if r.get("resourceType") == resource_type
                and str((r.get("meta") or {}).get("source", "")) ==
                    str((resource.get("meta") or {}).get("source", ""))
            ]
            if existing:
                # 200, not 201: the record was already there. This is the
                # behaviour that makes a delivery re-runnable.
                return self._json(200, existing[0])

        resource["id"] = resource.get("id") or f"emu-{uuid.uuid4().hex[:12]}"
        self.state.written.append(resource)
        self._json(201, resource, headers={
            "Location": f"{self.fhir_base}/{resource_type}/{resource['id']}"
        })

    # -- bulk export ------------------------------------------------

    def _bulk_kickoff(self, query: dict) -> None:
        vendor = self.state.vendor
        if not vendor.supports_bulk_export:
            # An OperationOutcome, not an empty result: a caller must not
            # be able to mistake "unsupported" for "no data".
            return self._json(400, _outcome(
                "not-supported",
                f"{vendor.name} does not support Bulk Data Export. Ingest by paging the "
                "search API per resource type.",
            ))

        job_id = uuid.uuid4().hex
        requested = (query.get("_type") or [""])[0]
        types = [t for t in requested.split(",") if t] or sorted(self.state.resources)
        self.state.bulk_jobs[job_id] = {"polls": 0, "types": types}

        self.send_response(202)
        self.send_header("Content-Location", f"{self.fhir_base}/BulkRequest/{job_id}")
        self.end_headers()

    def _bulk_status(self, job_id: str) -> None:
        job = self.state.bulk_jobs.get(job_id)
        if job is None:
            return self._json(404, _outcome("not-found", "no such bulk job"))

        job["polls"] += 1
        if job["polls"] < BULK_READY_AFTER_POLLS:
            # Async on purpose: a client that assumes the first poll is
            # ready would break against every real implementation.
            self.send_response(202)
            self.send_header("X-Progress", "in progress")
            self.send_header("Retry-After", "1")
            self.end_headers()
            return

        self._json(200, {
            "transactionTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request": f"{self.fhir_base}/$export",
            "requiresAccessToken": True,
            "output": [
                {"type": t, "url": f"{self.fhir_base}/BulkFile/{job_id}/{t}"}
                for t in job["types"] if self.state.all_of(t)
            ],
            "error": [],
        })

    def _bulk_file(self, job_id: str, resource_type: str) -> None:
        if job_id not in self.state.bulk_jobs:
            return self._json(404, _outcome("not-found", "no such bulk job"))
        lines = "\n".join(json.dumps(r) for r in self.state.all_of(resource_type))
        self._text(200, lines + ("\n" if lines else ""), "application/fhir+ndjson")

    # -- launch page ------------------------------------------------

    def _launch_page(self, query: dict) -> None:
        """A page that starts an EHR launch into the platform.

        This is what makes in-context SSO clickable end to end without a
        real EMR: it is the "open the platform" button a clinician would
        press from inside a patient's chart.
        """
        target = self.state.record_launch_url
        if not target:
            return self._text(400, "<p>No record launch URL configured for this emulator.</p>")

        patient = (query.get("patient") or ["eSyn0001Patient"])[0]
        encounter = (query.get("encounter") or ["eSynEnc0001"])[0]
        launch_token = secrets.token_urlsafe(12)
        url = f"{target}?{urlencode({'iss': self.fhir_base, 'launch': launch_token})}"

        self._text(200, f"""<!doctype html>
<meta charset="utf-8"><title>{self.state.vendor.name} emulator</title>
<style>body{{font:15px -apple-system,system-ui,sans-serif;margin:40px;max-width:640px}}
code{{background:#eef;padding:2px 5px;border-radius:3px}}
a.btn{{display:inline-block;background:#1f5f8b;color:#fff;padding:10px 18px;
border-radius:6px;text-decoration:none;font-weight:600;margin-top:12px}}</style>
<h2>{self.state.vendor.name} — emulator</h2>
<p>Standing in for a clinician viewing a patient's chart. All data is synthetic.</p>
<ul>
  <li>Patient context: <code>{patient}</code></li>
  <li>Encounter context: <code>{encounter}</code></li>
  <li>Issuer: <code>{self.fhir_base}</code></li>
</ul>
<a class="btn" href="{url}">Open the record in context &rarr;</a>
<p style="color:#666;font-size:13px;margin-top:20px">
Sends an SMART EHR launch. The platform should land on this patient's record
without asking you to sign in again.</p>""")


def build_server(vendor_key: str, port: int, record_launch_url: str = "",
                 client_jwks: Optional[dict] = None,
                 client_credentials: Optional[dict[str, str]] = None) -> ThreadingHTTPServer:
    vendor = VENDORS[vendor_key]
    base_url = f"http://127.0.0.1:{port}"
    state = EmulatorState(vendor, base_url, record_launch_url,
                          client_jwks=client_jwks, client_credentials=client_credentials)
    if vendor.accepts_jwt_assertion and not client_jwks:
        log.warning(
            "%s emulator on port %d: client_assertion signatures NOT verified - no client "
            "JWKS registered (register one with build_server(client_jwks=...) or "
            "`python -m emulators --client-jwks PATH`); alg, audience, expiry and claims "
            "are still checked", vendor.name, port,
        )
    if vendor.accepts_client_secret and not client_credentials:
        log.warning(
            "%s emulator on port %d: client_secret NOT verified - no client credentials "
            "registered (build_server(client_credentials={client_id: secret}) or "
            "`python -m emulators --client-secret CLIENT_ID:SECRET`)", vendor.name, port,
        )

    handler = type(
        f"{vendor_key.title()}Handler", (EmulatorHandler,), {"state": state}
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.emulator_state = state
    return server


def serve(vendor_key: str, port: int, record_launch_url: str = "",
          client_jwks: Optional[dict] = None,
          client_credentials: Optional[dict[str, str]] = None) -> threading.Thread:
    server = build_server(vendor_key, port, record_launch_url,
                          client_jwks=client_jwks, client_credentials=client_credentials)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("%s emulator on http://127.0.0.1:%d%s",
             VENDORS[vendor_key].name, port, VENDORS[vendor_key].fhir_path)
    return thread
# Made by Ryan Gomez & Co. Inc.
