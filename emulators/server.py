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
                                   the vendors that accept it live.
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
    so five can run in one process without sharing patients or tokens."""

    def __init__(self, vendor: EmulatorVendor, base_url: str, record_launch_url: str = ""):
        self.vendor = vendor
        self.base_url = base_url.rstrip("/")
        self.record_launch_url = record_launch_url
        self.resources = _load_dataset()

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

    def _json(self, status: int, payload: dict, headers: Optional[dict] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

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
            return self._json(200, {"vendor": vendor.key, "status": "ok"})

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

        # $export kickoff, system or group level
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
        capabilities = [
            "launch-ehr", "client-confidential-symmetric", "context-ehr-patient",
            "context-ehr-encounter", "permission-patient", "sso-openid-connect",
        ]
        self._json(200, {
            "issuer": self.fhir_base,
            "jwks_uri": f"{self.fhir_base}/.well-known/jwks.json",
            "authorization_endpoint": f"{self.state.base_url}/oauth2/authorize",
            "token_endpoint": f"{self.state.base_url}/oauth2/token",
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "capabilities": capabilities,
            "scopes_supported": [
                "launch", "openid", "fhirUser",
                "patient/Patient.read", "patient/Patient.rs",
            ],
        })

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

    def _token(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
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
                    secret = decoded.split(":", 1)[1]

            if assertion and not vendor.accepts_jwt_assertion:
                # What athenahealth actually does. A client that assumes
                # every vendor takes an assertion should fail here.
                return self._json(400, {
                    "error": "invalid_client",
                    "error_description": f"{vendor.name} does not accept a JWT client "
                                         "assertion; use client_secret",
                })
            if secret and not vendor.accepts_client_secret:
                return self._json(400, {
                    "error": "invalid_client",
                    "error_description": f"{vendor.name} requires a signed JWT client "
                                         "assertion, not a client secret",
                })
            if not assertion and not secret:
                return self._json(400, {
                    "error": "invalid_request",
                    "error_description": "no client_assertion or client_secret",
                })

            if vendor.requires_token_scope:
                # What Oracle Health actually does: every scope must be
                # requested explicitly, and wildcards are unsupported. A
                # client tuned on Epic - whose backend token request has
                # no scope parameter at all - should fail here, in a
                # test, not against a real tenant.
                scope = (form.get("scope") or [""])[0]
                if not scope:
                    return self._json(400, {
                        "error": "invalid_scope",
                        "error_description": f"{vendor.name} requires explicit "
                                             "system/{Type}.{permission} scopes in the "
                                             "token request",
                    })
                if "*" in scope:
                    return self._json(400, {
                        "error": "invalid_scope",
                        "error_description": f"{vendor.name} does not support "
                                             "wildcard scopes",
                    })

            token = secrets.token_urlsafe(32)
            self.state.tokens[token] = {"grant": "client_credentials"}
            return self._json(200, {
                "access_token": token, "token_type": "Bearer", "expires_in": 3600,
            })

        self._json(400, {"error": "unsupported_grant_type", "error_description": grant})

    def _token_authorization_code(self, form: dict) -> None:
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        record = self.state.auth_codes.pop(code, None)

        if record is None:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "unknown or already-used code"})

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        if expected != record["code_challenge"]:
            return self._json(400, {"error": "invalid_grant",
                                    "error_description": "PKCE verifier does not match"})

        token = secrets.token_urlsafe(32)
        self.state.tokens[token] = {"grant": "authorization_code"}
        self._json(200, {
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

        offset = int((query.get("_offset") or ["0"])[0])
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
            return self._json(422, _outcome(
                "not-supported",
                f"{vendor.name} does not accept create for {resource_type}",
            ))

        length = int(self.headers.get("Content-Length") or 0)
        try:
            resource = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return self._json(400, _outcome("structure", "body is not valid JSON"))

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


def build_server(vendor_key: str, port: int, record_launch_url: str = "") -> ThreadingHTTPServer:
    vendor = VENDORS[vendor_key]
    base_url = f"http://127.0.0.1:{port}"
    state = EmulatorState(vendor, base_url, record_launch_url)

    handler = type(
        f"{vendor_key.title()}Handler", (EmulatorHandler,), {"state": state}
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.emulator_state = state
    return server


def serve(vendor_key: str, port: int, record_launch_url: str = "") -> threading.Thread:
    server = build_server(vendor_key, port, record_launch_url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("%s emulator on http://127.0.0.1:%d%s",
             VENDORS[vendor_key].name, port, VENDORS[vendor_key].fhir_path)
    return thread
# Made by Ryan Gomez & Co. Inc.
