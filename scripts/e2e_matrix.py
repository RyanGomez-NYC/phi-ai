#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
End-to-end matrix proof: every vendor as a SOURCE, every vendor as a TARGET.

    python scripts/e2e_matrix.py --out /path/to/e2e-proof.md
    python scripts/e2e_matrix.py --vendors epic,cerner --keep-running

ONE TO ONE, ONE TO MANY, MANY TO MANY. One pair (source -> target) is one
migration: pull from the source with the REAL ingestion client, push into
the target with the REAL delivery writer. One row of the matrix is one
source feeding every target; one column is every source feeding one
target; the whole product is many to many. Every cell is asserted, the
diagonal included: a delivery pointed back at its own source is REFUSED
by the writer, and that refusal is what the diagonal proves.

WHAT IS EXERCISED, per source, with the real code under core/fhir/:
  - the token request the vendor documents, sent by
    FHIRIngestionClient.authenticate_from_settings(): a JWT client
    assertion signed with the profile's algorithm by a key of the family
    that algorithm needs (RS384 -> the RSA-2048 pair, ES384 -> the EC
    P-384 pair), or a client secret where the vendor issues one, with
    explicit system scopes exactly where the profile says they are
    mandatory. The emulator refuses the wrong grant, the wrong `alg` and
    a missing scope the way the vendor does.
  - GET /metadata, read through core.fhir.conformance_probe.
  - paged search per resource type through iter_resources(), with the
    number of pages OBSERVED (the emulators page at 2 per response), so
    a `next` link that was not followed fails here.
  - Bulk Data $export through core.fhir.bulk_client where the vendor has
    it - kickoff, async status, NDJSON files - and the OperationOutcome
    refusal where it does not. Both are pass conditions; neither is a
    skip.
And per pair, with core/fhir/delivery/writer.py:
  - the source-system guard (the diagonal),
  - the conditional-create refusal where the target has none,
  - a real create of each type the target's CapabilityStatement
    advertises, confirmed present afterwards by searching the target,
  - the writer's structured refusal (skipped_reason) for each type it
    does not advertise.

ALL DATA IS SYNTHETIC. The emulators serve the eSyn* fixtures from
scripts/mock_epic_server.py - fabricated patients whose every id is
prefixed "eSyn" and whose every note labels itself synthetic. Nothing
here touches a real EMR, a real patient, or the network beyond
127.0.0.1. This is a non-PHI setup by construction, and the proof
asserts the id prefix on everything it ingests rather than trusting the
label.

WHAT A GREEN MATRIX PROVES, AND DOES NOT. It proves the client and the
writer handle the seams the emulators reproduce - which is where
integration defects live. It is not certification against any vendor's
live system: emulators/vendors.py says so, every profile says to confirm
against the instance's own CapabilityStatement, and that still applies.

The helpers in this module are shared with tests/test_e2e_matrix.py,
where every pair is its own pytest case. Nothing here is duplicated
there; the test imports this module.
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest import mock
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.fhir.emr_profiles import profile_for  # noqa: E402
from emulators.server import build_server  # noqa: E402
from emulators.vendors import DEFAULT_PORTS, VENDORS  # noqa: E402

# The resource types pushed through the writer for every pair, DERIVED
# from the registries rather than listed: every type any emulator's
# CapabilityStatement advertises create for (so each advertised write
# path is exercised somewhere), plus Observation and Condition as the
# guaranteed refusal probes on the targets that advertise neither. Every
# type is pushed at EVERY target: the target's CapabilityStatement
# decides, per type, whether the outcome asserted is "created" or the
# writer's refusal. Order (alphabetical) is the order of the letters in
# a matrix cell. ingest() asserts each is ingestible from every source.
DELIVERY_TYPES = tuple(sorted(
    frozenset().union(*(v.creatable for v in VENDORS.values())) | {"Observation", "Condition"}
))

# The one id prefix the synthetic dataset promises (scripts/
# mock_epic_server.py: "Resource IDs are deliberately prefixed eSyn").
# Asserted on every ingested resource, so a dataset that stopped being
# the synthetic one would fail the proof rather than pass it quietly.
SYNTHETIC_ID_PREFIX = "eSyn"

# Every delivery in one run carries this in meta.source, so a run against
# emulators that already hold an earlier run's records still sees its own
# creates (conditional create matches on meta.source) and confirms
# exactly its own records afterwards.
RUN_ID = uuid.uuid4().hex[:8]

CLIENT_ID = "e2e-matrix"
CLIENT_SECRET = "e2e-matrix-secret"      # only ever sent to an emulator on 127.0.0.1
EXPORT_GROUP_ID = "synthetic-e2e"
RECORD_LAUNCH_URL = "http://127.0.0.1:1/smart/launch"
HTTP_TIMEOUT = 30

# Outcomes a matrix cell can hold, per delivered type, and the letter
# each is rendered as.
CREATED = "created"
REFUSED_NOT_ADVERTISED = "refused (target does not advertise create)"
REFUSED_SOURCE_SYSTEM = "refused (target is the source system)"
OUTCOME_CODES = {CREATED: "C", REFUSED_NOT_ADVERTISED: "R", REFUSED_SOURCE_SYSTEM: "S"}
FAILED_CODE = "F"


# ---------------------------------------------------------------------------
# Signing keys - one RSA-2048 pair and one EC P-384 pair per run
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyPair:
    private_pem: bytes
    public_pem: bytes
    description: str  # family and size only, never material (core.fhir.client.describe_private_key)
    kid: str
    jwk: dict         # the PUBLIC half as a JWK, what a vendor holds from registration


@dataclass(frozen=True)
class SigningKeys:
    rsa: KeyPair
    ec_p384: KeyPair

    @property
    def jwks(self) -> dict:
        """The client's JWK Set, registered with every emulator this run
        starts (build_server(client_jwks=...)) exactly as the public keys
        would be registered with a vendor. With it the emulator's token
        endpoint verifies each assertion's signature, audience, expiry
        and required claims against the key its kid names."""
        return {"keys": [self.rsa.jwk, self.ec_p384.jwk]}


def generate_signing_keys() -> SigningKeys:
    """Generated in memory with the cryptography library - the one PyJWT
    signs with - and never written to disk. The public half is kept so
    each assertion the real builder produces can be VERIFIED here with
    the public key, and registered with the emulators so they verify it
    too."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    from core.fhir.client import describe_private_key

    def pair(key, kid: str, to_jwk) -> KeyPair:
        private = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        jwk = {**to_jwk(key.public_key(), as_dict=True), "kid": kid, "use": "sig"}
        return KeyPair(private, public, describe_private_key(private), kid, jwk)

    return SigningKeys(
        rsa=pair(rsa.generate_private_key(public_exponent=65537, key_size=2048),
                 f"e2e-rsa-{RUN_ID}", RSAAlgorithm.to_jwk),
        ec_p384=pair(ec.generate_private_key(ec.SECP384R1()),
                     f"e2e-ec-p384-{RUN_ID}", ECAlgorithm.to_jwk),
    )


def key_pair_for(keys: SigningKeys, algorithm: str) -> KeyPair:
    """The pair that signs `algorithm`, decided by asking PyJWT's own
    algorithm object what family it needs - the rule
    core.fhir.client.check_private_key_signs() applies - rather than by
    a second algorithm-to-key table kept here."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    signer = pyjwt.get_algorithm_by_name(algorithm)  # NotImplementedError for an unknown name
    if isinstance(signer, RSAAlgorithm):
        return keys.rsa
    if isinstance(signer, ECAlgorithm) and getattr(signer, "expected_curve", None) is ec.SECP384R1:
        return keys.ec_p384
    raise AssertionError(
        f"{algorithm}: neither generated key pair signs this algorithm (this proof holds "
        "one RSA-2048 pair for RS384 and one EC P-384 pair for ES384)"
    )


# ---------------------------------------------------------------------------
# Emulators on their DEFAULT_PORTS ports, in-process
# ---------------------------------------------------------------------------

@dataclass
class Emulator:
    key: str
    port: int
    base_url: str
    server: object = None  # ThreadingHTTPServer when started here; None when attached
    # True when this run registered its JWK Set with the emulator, so its
    # token endpoint verifies assertion SIGNATURES (not only the alg
    # header). Never true for an attached emulator: its state is not ours.
    verifies_signatures: bool = False

    @property
    def started_here(self) -> bool:
        return self.server is not None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def start_emulators(keys, reuse_running: bool = False,
                    client_jwks: Optional[dict] = None,
                    port_offset: int = 0) -> dict[str, Emulator]:
    """One emulator per vendor key, each on its DEFAULT_PORTS port.

    `client_jwks` is the client's public JWK Set (SigningKeys.jwks),
    registered with every emulator started here so its token endpoint
    verifies assertion signatures the way a vendor holding the registered
    public key would.

    `port_offset` is added to every DEFAULT_PORTS port. Zero by default
    and never chosen silently: it exists for a machine where the
    DEFAULT_PORTS ports are held by emulators that belong to something
    else (a long-running `python -m emulators`), and the proof document
    states the ports actually used.

    reuse_running=False (the tests): every port must be free; a port
    already in use fails loudly rather than moving to another port,
    because the proof states which port each vendor was served on.
    reuse_running=True (the CLI): a port that is already open must be
    answering as THAT vendor's emulator (/emulator/health names it), in
    which case it is attached to and left running afterwards - with
    whatever state and key registration it already has, which the proof
    reports rather than assumes.
    """
    import requests

    running: dict[str, Emulator] = {}
    for key in keys:
        assert key in VENDORS, f"{key}: not in emulators.vendors.VENDORS ({', '.join(sorted(VENDORS))})"
        assert key in DEFAULT_PORTS, f"{key}: no DEFAULT_PORTS entry in emulators/vendors.py"
        port = DEFAULT_PORTS[key] + port_offset
        base_url = f"http://127.0.0.1:{port}"

        if reuse_running and _port_open(port):
            try:
                health = requests.get(f"{base_url}/emulator/health", timeout=HTTP_TIMEOUT)
                body = health.json() if health.status_code == 200 else {}
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError(
                    f"{key}: port {port} is open but does not answer /emulator/health as an "
                    f"emulator ({exc}); stop whatever holds it, or use --port-offset"
                ) from exc
            assert health.status_code == 200 and body.get("vendor") == key, (
                f"{key}: port {port} is open but is not the {VENDORS[key].name} emulator "
                f"(/emulator/health answered {health.status_code}: {health.text[:200]})"
            )
            # Never deliver into a foreign emulator's state: one that
            # already holds written records, or one verifying signatures
            # against a JWK Set that is not this run's, is refused before
            # a single token request, not discovered after 84 failures.
            assert not body.get("written"), (
                f"{key}: the emulator on port {port} already holds {body.get('written')} "
                "written record(s) from an earlier run; stop it and re-run so the proof is "
                "on the pristine dataset"
            )
            assert not body.get("verifies_assertion_signatures"), (
                f"{key}: the emulator on port {port} verifies assertion signatures against a "
                "JWK Set that is not this run's; stop it and let this run start its own"
            )
            running[key] = Emulator(key=key, port=port, base_url=base_url)
            continue

        try:
            server = build_server(key, port, record_launch_url=RECORD_LAUNCH_URL,
                                  client_jwks=client_jwks,
                                  client_credentials={CLIENT_ID: CLIENT_SECRET})
        except OSError as exc:
            raise RuntimeError(
                f"{key}: cannot bind 127.0.0.1:{port} for the {VENDORS[key].name} emulator "
                f"({exc}); is `python -m emulators` or another matrix run already using it? "
                "Stop it, or run on shifted ports (scripts/e2e_matrix.py --port-offset N; "
                "tests: E2E_MATRIX_PORT_OFFSET=N)"
            ) from exc
        threading.Thread(target=server.serve_forever, daemon=True).start()
        running[key] = Emulator(key=key, port=port, base_url=base_url, server=server,
                                verifies_signatures=bool(client_jwks))
    return running


def stop_emulators(handles: dict[str, Emulator]) -> None:
    for handle in handles.values():
        if handle.started_here:
            handle.server.shutdown()
            handle.server.server_close()


def fhir_base(handles: dict[str, Emulator], key: str) -> str:
    return f"{handles[key].base_url}{VENDORS[key].fhir_path}"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Auth:
    vendor: str
    grant: str                # "JWT RS384" | "JWT ES384" | "client_secret"
    key_description: str      # "-" for a client secret
    explicit_scopes: bool     # what the profile records; the dispatcher derives and sends them
    access_token: str
    # Whether the emulator's token endpoint verified the assertion's
    # SIGNATURE against the registered JWK Set (it always holds the alg
    # header to the vendor's tuple). False for a client secret, and for
    # an attached emulator this run did not register its keys with.
    signature_verified_by_emulator: bool = False
    # The vendor-specific token requests the emulator REFUSED before the
    # real grant was sent - observed 400s with their error codes, so the
    # proof states what was refused rather than what was configured.
    refusals: tuple[str, ...] = ()


@dataclass
class Ingest:
    source: str
    auth: Auth
    fhir_version: str
    types: tuple[str, ...]
    pages: dict[str, int]
    resources: dict[str, list[dict]]
    export: str          # "ok" | "refused"
    export_files: int
    export_rows: int

    @property
    def total_pages(self) -> int:
        return sum(self.pages.values())

    @property
    def total_resources(self) -> int:
        return sum(len(v) for v in self.resources.values())


@dataclass
class Cell:
    source: str
    target: str
    outcomes: dict[str, str]          # DELIVERY_TYPES -> CREATED | REFUSED_*
    creatable: frozenset[str]         # what the target advertised (empty on the diagonal)
    conditional_create: bool

    @property
    def code(self) -> str:
        return "".join(OUTCOME_CODES[self.outcomes[t]] for t in DELIVERY_TYPES)


@dataclass
class MatrixReport:
    vendors: list[str]
    ingests: dict[str, Ingest]
    cells: dict[tuple[str, str], Cell]
    failures: dict[str, str] = field(default_factory=dict)  # "source" or "source->target" -> reason
    # Emulators this run attached to rather than started: their state
    # predates the run, and the proof says so rather than presenting them
    # as pristine.
    attached: dict[str, int] = field(default_factory=dict)  # vendor -> port
    ports: dict[str, int] = field(default_factory=dict)     # vendor -> port actually used

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return list(itertools.product(self.vendors, self.vendors))

    @property
    def passed(self) -> int:
        return sum(1 for pair in self.pairs if pair in self.cells)

    @property
    def ok(self) -> bool:
        return not self.failures and self.passed == len(self.pairs)


class _RecordingAudit:
    """What the writer audits before each write, kept so the proof can
    assert one record.deliver per record actually sent."""

    def __init__(self):
        self.events: list[dict] = []

    def record(self, **kwargs) -> None:
        self.events.append(kwargs)


# ---------------------------------------------------------------------------
# The session: one ingest per source, one delivery per pair
# ---------------------------------------------------------------------------

class MatrixSession:
    """Holds the emulators, the keys, and the caches.

    Ingestion is cached per vendor and a target is always ingested
    BEFORE anything is delivered into it: the emulators serve delivered
    records alongside the seeded dataset (that is what makes delivery
    confirmable), so a vendor ingested only after it had been written
    into would page duplicate ids. Pulling from each vendor before
    pushing into it keeps every cached ingest the pristine dataset.
    """

    def __init__(self, handles: dict[str, Emulator], keys: SigningKeys):
        self.handles = handles
        self.keys = keys
        self.auths: dict[str, Auth] = {}
        self.ingests: dict[str, object] = {}       # Ingest, or the exception it failed with
        self.cells: dict[tuple[str, str], Cell] = {}
        self.failures: dict[str, str] = {}

    # -- auth ---------------------------------------------------------

    def auth(self, key: str) -> Auth:
        if key not in self.auths:
            self.auths[key] = authenticate(self.handles, key, self.keys)
        return self.auths[key]

    # -- ingest -------------------------------------------------------

    def ingest(self, key: str) -> Ingest:
        if key not in self.ingests:
            try:
                self.ingests[key] = ingest(self.handles, key, self.auth(key))
            except Exception as exc:
                self.ingests[key] = exc
                self.failures[key] = f"{type(exc).__name__}: {exc}"
                raise
        cached = self.ingests[key]
        if isinstance(cached, BaseException):
            raise cached
        return cached

    # -- deliver ------------------------------------------------------

    def deliver(self, source: str, target: str) -> Cell:
        ingested = self.ingest(source)
        self.ingest(target)  # see the class docstring: pull before push
        cell = deliver(self.handles, ingested, target, self.auth(target))
        self.cells[(source, target)] = cell
        return cell

    def run_pair(self, source: str, target: str) -> Cell:
        """One pair, recorded either way and re-raised on failure - the
        tests let the exception through, the CLI catches it per pair."""
        try:
            return self.deliver(source, target)
        except Exception as exc:
            self.failures[f"{source}->{target}"] = f"{type(exc).__name__}: {exc}"
            raise

    def report(self, vendors) -> MatrixReport:
        vendors = list(vendors)
        return MatrixReport(
            vendors=vendors,
            ingests={k: v for k, v in self.ingests.items() if isinstance(v, Ingest)},
            cells={pair: cell for pair, cell in self.cells.items()
                   if pair[0] in vendors and pair[1] in vendors},
            failures=dict(self.failures),
            attached={k: self.handles[k].port for k in vendors
                      if k in self.handles and not self.handles[k].started_here},
            ports={k: self.handles[k].port for k in vendors if k in self.handles},
        )


# ---------------------------------------------------------------------------
# authenticate: the vendor's real seam, through the dispatcher
# ---------------------------------------------------------------------------

def _expect_refusal(token_url: str, data: dict, expected_error: str, label: str,
                    must_mention: Optional[str] = None) -> str:
    """POST one deliberately wrong token request; assert the emulator
    answers 400 with `expected_error`, and return the label recorded in
    the proof. A 200 here is a failed proof, not a passing grant."""
    import requests

    response = requests.post(token_url, data=data, timeout=HTTP_TIMEOUT)
    assert response.status_code == 400, (
        f"{label}: expected HTTP 400 {expected_error}, got HTTP {response.status_code} "
        f"{response.text[:200]}"
    )
    body = response.json()
    assert body.get("error") == expected_error, (
        f"{label}: expected error={expected_error!r}, got {body}"
    )
    if must_mention:
        assert must_mention in body.get("error_description", ""), (
            f"{label}: error_description does not mention {must_mention!r}: {body}"
        )
    return f"{label} -> {expected_error}"


def probe_refusals(handles: dict[str, Emulator], key: str, keys: SigningKeys) -> tuple[str, ...]:
    """The vendor-specific NEGATIVES, sent before the real grant: each is
    a token request the vendor's documentation says it refuses, and each
    must come back as the documented 400. Recorded on Auth.refusals and
    rendered in the proof, so 'not one generic grant for all' is shown
    by what was refused, not inferred from configuration.

      - a JWT vendor: an assertion signed in an algorithm the vendor
        does not accept (ES384 to an RS384-only vendor and vice versa),
        and an unsigned alg=none assertion, each -> invalid_client;
      - a JWT vendor whose emulator this run started (keys registered):
        the right algorithm signed by a freshly generated UNREGISTERED
        key -> invalid_client naming the signature failure;
      - a vendor that requires explicit scopes: the right assertion (or
        secret) with no scope -> invalid_scope;
      - the wrong grant: a client secret to a JWT-only vendor, a JWT to a
        secret-only vendor, each -> invalid_client; and for a vendor that
        honours a secret, when this run registered credentials, a wrong
        secret -> invalid_client.
    """
    import jwt as pyjwt

    profile = profile_for(key)
    vendor = VENDORS[key]
    handle = handles[key]
    token_url = f"{handle.base_url}/oauth2/token"
    jwt_type = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    scope = " ".join(f"system/{t}.read" for t in profile.supported_resources)
    refusals: list[str] = []

    def signed(algorithm: str, private_pem: Optional[bytes] = None, kid: Optional[str] = None) -> str:
        pair = key_pair_for(keys, algorithm)
        return FHIRIngestionClient_build(
            CLIENT_ID, token_url, private_pem or pair.private_pem, kid or pair.kid, algorithm,
        )

    from core.fhir.client import FHIRIngestionClient

    def FHIRIngestionClient_build(client_id, url, pem, kid, algorithm):
        return FHIRIngestionClient.build_client_assertion(client_id, url, pem, kid, algorithm=algorithm)

    if vendor.accepts_jwt_assertion:
        for algorithm in ("RS384", "ES384"):
            if algorithm in vendor.assertion_algorithms:
                continue
            refusals.append(_expect_refusal(
                token_url,
                {"grant_type": "client_credentials", "client_assertion_type": jwt_type,
                 "client_assertion": signed(algorithm), "scope": scope},
                "invalid_client", f"wrong alg {algorithm}",
            ))
        now = int(time.time())
        unsigned = pyjwt.encode(
            {"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": token_url, "jti": uuid.uuid4().hex,
             "iat": now, "exp": now + 240},
            None, algorithm="none",
        )
        refusals.append(_expect_refusal(
            token_url,
            {"grant_type": "client_credentials", "client_assertion_type": jwt_type,
             "client_assertion": unsigned, "scope": scope},
            "invalid_client", "unsigned alg=none",
        ))
        algorithm = profile.assertion_algorithm
        if handle.verifies_signatures:
            stranger = _fresh_private_pem(algorithm)
            refusals.append(_expect_refusal(
                token_url,
                {"grant_type": "client_credentials", "client_assertion_type": jwt_type,
                 "client_assertion": signed(algorithm, stranger, key_pair_for(keys, algorithm).kid),
                 "scope": scope},
                "invalid_client", "unregistered key", must_mention="InvalidSignatureError",
            ))
        else:
            refusals.append("unregistered key: NOT probed (attached emulator, keys not registered)")
        if vendor.requires_token_scope:
            refusals.append(_expect_refusal(
                token_url,
                {"grant_type": "client_credentials", "client_assertion_type": jwt_type,
                 "client_assertion": signed(algorithm)},
                "invalid_scope", "no scope",
            ))
    if not vendor.accepts_client_secret:
        refusals.append(_expect_refusal(
            token_url,
            {"grant_type": "client_credentials", "client_id": CLIENT_ID,
             "client_secret": CLIENT_SECRET, "scope": scope},
            "invalid_client", "client_secret to a JWT-only vendor",
        ))
    elif handle.started_here:
        refusals.append(_expect_refusal(
            token_url,
            {"grant_type": "client_credentials", "client_id": CLIENT_ID,
             "client_secret": "not-" + CLIENT_SECRET, "scope": scope},
            "invalid_client", "wrong client_secret",
        ))
        if vendor.requires_token_scope:
            refusals.append(_expect_refusal(
                token_url,
                {"grant_type": "client_credentials", "client_id": CLIENT_ID,
                 "client_secret": CLIENT_SECRET},
                "invalid_scope", "secret with no scope",
            ))
    else:
        refusals.append("wrong client_secret: NOT probed (attached emulator, credentials not registered)")
    if not vendor.accepts_jwt_assertion:
        refusals.append(_expect_refusal(
            token_url,
            {"grant_type": "client_credentials", "client_assertion_type": jwt_type,
             "client_assertion": signed("RS384"), "scope": scope},
            "invalid_client", "JWT to a secret-only vendor",
        ))
    assert refusals, f"{key}: no refusal was probed - every vendor has at least one documented refusal"
    return tuple(refusals)


def _fresh_private_pem(algorithm: str) -> bytes:
    """A brand-new private key of the family `algorithm` signs with - one
    NO emulator holds the public half of - for the unregistered-key probe."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    import jwt as pyjwt

    signer = pyjwt.get_algorithm_by_name(algorithm)
    if isinstance(signer, RSAAlgorithm):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif isinstance(signer, ECAlgorithm):
        key = ec.generate_private_key(signer.expected_curve())
    else:
        raise AssertionError(f"{algorithm}: not an asymmetric algorithm")
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def authenticate(handles: dict[str, Emulator], key: str, keys: SigningKeys) -> Auth:
    """A real FHIRIngestionClient, authenticated the way its PROFILE
    documents by authenticate_from_settings() - the dispatcher the
    schedulers run - against the vendor's emulator.

    Before the real grant, probe_refusals() sends the vendor-specific
    negatives and asserts each documented 400 (recorded on Auth.refusals).

    For a signed-assertion vendor the assertion the real builder
    produces is also verified here with the public key of the pair that
    signed it, and its header checked for the profile's algorithm. The
    emulator's own verification depends on how it was started: an
    emulator THIS RUN started holds the run's JWK Set
    (Emulator.verifies_signatures) and verifies the wire assertion's
    signature, audience, expiry and claims; an emulator the CLI attached
    to verifies everything but the signature (its keys are not ours).
    The proof's Sources table says which applied, per source.
    """
    import jwt as pyjwt
    import requests

    from core.fhir.client import FHIRIngestionClient

    profile = profile_for(key)
    vendor = VENDORS[key]
    token_url = f"{handles[key].base_url}/oauth2/token"

    # The scope seam is asserted on BOTH registries, the way the bulk
    # flags are cross-checked in ingest(): an emulator that stopped
    # demanding scopes would otherwise leave the proof's column reading
    # from the profile alone.
    assert vendor.requires_token_scope == profile.requires_token_scopes, (
        f"{key}: the emulator {'requires' if vendor.requires_token_scope else 'does not require'} "
        f"explicit scopes but the profile records requires_token_scopes="
        f"{profile.requires_token_scopes}"
    )
    refusals = probe_refusals(handles, key, keys)

    client = FHIRIngestionClient(
        base_url=fhir_base(handles, key), profile=profile,
        storage=None, encryptor=None, audit=None, retention_years=0,
    )

    if profile.auth_flow == "oauth2_client_credentials":
        assert vendor.accepts_client_secret, (
            f"{key}: the profile authenticates with a client secret but the "
            f"{vendor.name} emulator does not accept one"
        )
        settings = SimpleNamespace(
            fhir_client_id=CLIENT_ID, fhir_client_secret=CLIENT_SECRET,
            fhir_token_url=token_url, fhir_private_key_pem=None, fhir_jwt_kid=None,
        )
        grant, key_description = "client_secret", "-"
    elif profile.auth_flow == "smart_backend_services":
        assert vendor.accepts_jwt_assertion, (
            f"{key}: the profile authenticates with a JWT assertion but the "
            f"{vendor.name} emulator does not accept one"
        )
        algorithm = profile.assertion_algorithm
        assert algorithm in vendor.assertion_algorithms, (
            f"{key}: the profile signs its assertion with {algorithm} but the {vendor.name} "
            f"emulator accepts only {', '.join(vendor.assertion_algorithms)}"
        )
        pair = key_pair_for(keys, algorithm)

        assertion = FHIRIngestionClient.build_client_assertion(
            CLIENT_ID, token_url, pair.private_pem, pair.kid, algorithm=algorithm,
        )
        header = pyjwt.get_unverified_header(assertion)
        assert header.get("alg") == algorithm, (
            f"{key}: the assertion header says alg={header.get('alg')!r}, the profile says "
            f"{algorithm}"
        )
        assert header.get("kid") == pair.kid, (
            f"{key}: the assertion header carries kid={header.get('kid')!r}, not the registered "
            f"key's {pair.kid!r}"
        )
        claims = pyjwt.decode(assertion, pair.public_pem, algorithms=[algorithm], audience=token_url)
        assert claims["iss"] == CLIENT_ID and claims["sub"] == CLIENT_ID, (
            f"{key}: assertion iss/sub are not the client id: {claims}"
        )

        settings = SimpleNamespace(
            fhir_client_id=CLIENT_ID, fhir_client_secret=None,
            fhir_token_url=token_url, fhir_private_key_pem=pair.private_pem, fhir_jwt_kid=pair.kid,
        )
        grant, key_description = f"JWT {algorithm}", pair.description
    else:
        raise AssertionError(f"{key}: profile records an unknown auth_flow {profile.auth_flow!r}")

    try:
        client.authenticate_from_settings(settings)
    except requests.HTTPError as exc:
        response = exc.response
        raise AssertionError(
            f"{key}: the {vendor.name} emulator refused the {grant} token request: "
            f"HTTP {response.status_code} {response.text}"
        ) from exc

    assert client.access_token, f"{key}: authenticate_from_settings() returned no access token"
    return Auth(
        vendor=key, grant=grant, key_description=key_description,
        explicit_scopes=profile.requires_token_scopes, access_token=client.access_token,
        signature_verified_by_emulator=(
            grant != "client_secret" and handles[key].verifies_signatures
        ),
        refusals=refusals,
    )


# ---------------------------------------------------------------------------
# ingest: metadata, paged search per type, $export or its refusal
# ---------------------------------------------------------------------------

def ingest(handles: dict[str, Emulator], key: str, auth: Auth) -> Ingest:
    import requests

    from core.fhir.bulk_client import (
        delete_export,
        iter_ndjson_resources,
        kickoff_export,
        wait_for_export,
    )
    from core.fhir.client import FHIRIngestionClient
    from core.fhir.conformance_probe import probe

    profile = profile_for(key)
    vendor = VENDORS[key]
    base = fhir_base(handles, key)
    token = auth.access_token
    bearer = {"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"}
    # An emulator this run attached to (reuse_running=True, port already
    # open) holds whatever was written to it before - records another
    # process delivered, ids it minted for bodies posted without one.
    # The assertions on what is served stay exactly as strict; this only
    # names the cause when they fail.
    provenance = (
        "" if handles[key].started_here else
        f" [attached to an emulator already running on port {handles[key].port}; its state "
        "predates this run - stop it and re-run to serve the pristine dataset]"
    )

    # -- /metadata ----------------------------------------------------
    response = requests.get(f"{base}/metadata", headers=bearer, timeout=HTTP_TIMEOUT)
    assert response.status_code == 200, f"{key}: GET /metadata answered {response.status_code}"
    statement = response.json()
    assert statement.get("resourceType") == "CapabilityStatement", (
        f"{key}: GET /metadata returned a {statement.get('resourceType')!r}, not a CapabilityStatement"
    )
    matrix = probe(statement)

    # Types to ingest: what the PROFILE lists as supported AND the server
    # declares searchable - derived from both, hand-listed by neither.
    types = tuple(t for t in profile.supported_resources if matrix.supports(t, "search-type"))
    assert "Patient" in types, (
        f"{key}: Patient is not both in the profile's supported_resources and declared "
        f"searchable by the emulator's CapabilityStatement (got {types})"
    )
    assert len(types) >= 3, f"{key}: only {len(types)} ingestible type(s) ({types}); Patient plus two more are required"
    for rtype in DELIVERY_TYPES:
        assert rtype in types, (
            f"{key}: {rtype} is not ingestible here (profile lists it: "
            f"{rtype in profile.supported_resources}; server declares search-type: "
            f"{matrix.supports(rtype, 'search-type')}) - the matrix delivers it from every source"
        )

    # -- paged search through the real client ------------------------
    client = FHIRIngestionClient(
        base_url=base, profile=profile, storage=None, encryptor=None, audit=None, retention_years=0,
    )
    client._access_token = token  # the token authenticate() obtained above, on a fresh client

    pages: dict[str, int] = {}
    resources: dict[str, list[dict]] = {}
    for rtype in types:
        # Observe the page count: every GET the client makes for this
        # type is one page. The wrapper calls straight through to
        # requests.get - nothing is faked, only counted.
        with mock.patch.object(requests, "get", wraps=requests.get) as counted:
            rows = list(client.iter_resources(rtype))
        fetched = counted.call_count
        expected = max(1, math.ceil(len(rows) / vendor.page_size))
        assert fetched == expected, (
            f"{key}: {rtype}: {len(rows)} resources at {vendor.page_size} per page should take "
            f"{expected} page(s); the client made {fetched} request(s){provenance}"
        )
        assert rows, f"{key}: {rtype}: the emulator declares it searchable but served nothing{provenance}"
        assert all(r.get("resourceType") == rtype for r in rows), (
            f"{key}: {rtype}: a page carried a resource of another type{provenance}"
        )
        ids = [r.get("id") for r in rows]
        assert len(set(ids)) == len(ids), f"{key}: {rtype}: duplicate ids across pages: {ids}{provenance}"
        assert all(str(i).startswith(SYNTHETIC_ID_PREFIX) for i in ids), (
            f"{key}: {rtype}: an id without the {SYNTHETIC_ID_PREFIX!r} synthetic prefix was "
            f"served: {ids} - this proof runs on the synthetic dataset only{provenance}"
        )
        pages[rtype] = fetched
        resources[rtype] = rows

    assert max(pages.values()) >= 2, (
        f"{key}: no resource type needed more than one page ({pages}); the next-link "
        "pagination was never exercised"
    )

    # -- Bulk Data $export, or the refusal ---------------------------
    if vendor.supports_bulk_export:
        assert profile.supports_bulk_export is True, (
            f"{key}: the emulator serves $export but the profile records "
            "supports_bulk_export=False - core/fhir/bulk_scheduler.py would refuse this vendor"
        )
        job = kickoff_export(base, EXPORT_GROUP_ID, token, resource_types=list(types))
        manifest = wait_for_export(job, token, poll_interval_seconds=0, max_wait_seconds=30)
        output = manifest.get("output") or []
        assert output, f"{key}: the $export manifest lists no output files: {manifest}"
        export_rows = 0
        for entry in output:
            rows = list(iter_ndjson_resources(entry["url"], token))
            assert rows, f"{key}: bulk file for {entry.get('type')} at {entry['url']} was empty"
            assert all(r.get("resourceType") == entry.get("type") for r in rows), (
                f"{key}: bulk file for {entry.get('type')} carried other types"
            )
            export_rows += len(rows)
        delete_export(job.status_url, token)
        export, export_files = "ok", len(output)
    else:
        assert profile.supports_bulk_export is False, (
            f"{key}: the profile records supports_bulk_export=True but the emulator refuses $export"
        )
        try:
            kickoff_export(base, EXPORT_GROUP_ID, token, resource_types=list(types))
        except requests.HTTPError as exc:
            refusal = exc.response
        else:
            raise AssertionError(
                f"{key}: $export kickoff was accepted although the {vendor.name} emulator "
                "records no Bulk Data Export support"
            )
        assert refusal.status_code == 400, (
            f"{key}: $export refusal was HTTP {refusal.status_code}, expected 400 with an OperationOutcome"
        )
        body = refusal.json()
        assert body.get("resourceType") == "OperationOutcome", (
            f"{key}: $export refusal body is not an OperationOutcome: {body}"
        )
        diagnostics = (body.get("issue") or [{}])[0].get("diagnostics", "")
        assert "does not support Bulk Data Export" in diagnostics, (
            f"{key}: $export refusal does not say so: {diagnostics!r}"
        )
        export, export_files, export_rows = "refused", 0, 0

    return Ingest(
        source=key, auth=auth, fhir_version=matrix.fhir_version, types=types,
        pages=pages, resources=resources,
        export=export, export_files=export_files, export_rows=export_rows,
    )


# ---------------------------------------------------------------------------
# deliver: the real writer against the target emulator
# ---------------------------------------------------------------------------

def deliver(handles: dict[str, Emulator], ingested: Ingest, target: str, auth: Auth) -> Cell:
    import requests

    from core.config.scale_profile import SMALL
    from core.db.index import extract_patient_reference
    from core.fhir.delivery.identity import IdentityMap, PatientMapping
    from core.fhir.delivery.writer import DeliveryError, EMRWriter, SourceSystemWriteRefused
    from core.storage.layout import locate

    source = ingested.source
    pair = f"{source}->{target}"
    profile = profile_for(target)
    vendor = VENDORS[target]
    source_base = fhir_base(handles, source)
    target_base = fhir_base(handles, target)
    audit = _RecordingAudit()

    # The source-system guard, at construction. On the diagonal this is
    # the outcome; anywhere else the writer must exist.
    try:
        writer = EMRWriter(
            base_url=target_base, access_token=auth.access_token, profile=profile,
            audit=audit, source_system_urls=[source_base],
        )
    except SourceSystemWriteRefused as exc:
        assert source == target, (
            f"{pair}: the writer refused {target_base} as a source system, but the source "
            f"of this delivery is {source_base}"
        )
        assert "SOURCE system" in str(exc), f"{pair}: the refusal does not name the source system: {exc}"
        return Cell(
            source=source, target=target,
            outcomes={t: REFUSED_SOURCE_SYSTEM for t in DELIVERY_TYPES},
            creatable=frozenset(), conditional_create=profile.supports_conditional_create,
        )
    assert source != target, f"{pair}: the writer accepted a delivery back into its own source system"

    # What the target itself advertises - the writer's authority.
    creatable = writer.creatable_resource_types()
    assert creatable == frozenset(vendor.creatable), (
        f"{pair}: the writer read create for {sorted(creatable)} from the target's "
        f"CapabilityStatement; the {vendor.name} emulator is configured for {sorted(vendor.creatable)}"
    )

    # One ingested resource per delivered type, keyed and linked the way
    # the ingestion path would have stored and indexed it.
    rows: list[tuple[dict, dict]] = []
    for rtype in DELIVERY_TYPES:
        resource = ingested.resources[rtype][0]
        patient = extract_patient_reference(resource)
        assert patient, f"{pair}: ingested {rtype} {resource.get('id')} links to no patient"
        rows.append((
            {"storage_key": locate(resource, SMALL).storage_key, "patient_reference": patient},
            resource,
        ))
    identity = IdentityMap([
        PatientMapping(p["id"], f"{target}-{p['id']}", verified_by="e2e-matrix")
        for p in ingested.resources["Patient"]
    ])
    source_system = f"{source}-emulator/{RUN_ID}"

    # A target without conditional create must refuse to run unattended.
    if not profile.supports_conditional_create:
        try:
            writer.deliver(rows, identity, source_system, "e2e-matrix", dry_run=False)
        except DeliveryError as exc:
            assert "conditional create" in str(exc), f"{pair}: unexpected refusal: {exc}"
        else:
            raise AssertionError(
                f"{pair}: {profile.name} records no conditional create, yet the writer ran "
                "unattended without allow_duplicates"
            )
        assert audit.events == [], f"{pair}: the refused delivery still audited: {audit.events}"

    result = writer.deliver(
        rows, identity, source_system, "e2e-matrix",
        dry_run=False, allow_duplicates=not profile.supports_conditional_create,
    )
    assert not result.dry_run, f"{pair}: the delivery ran as a dry run"
    assert [i.resource_type for i in result.items] == list(DELIVERY_TYPES), (
        f"{pair}: the writer reported on {[i.resource_type for i in result.items]}, "
        f"not {list(DELIVERY_TYPES)}"
    )

    outcomes: dict[str, str] = {}
    for item in result.items:
        rtype = item.resource_type
        assert not item.error, f"{pair}: {rtype}: the delivery errored: {item.error}"
        if rtype in creatable:
            assert item.sent and item.status == "created" and item.destination_id, (
                f"{pair}: {rtype}: the target advertises create but the writer reported "
                f"sent={item.sent} status={item.status!r} destination_id={item.destination_id!r} "
                f"skipped_reason={item.skipped_reason!r}"
            )
            _confirm_present(
                target_base, auth.access_token, rtype,
                expected_source=f"{source_system}#{item.storage_key}", pair=pair,
            )
            outcomes[rtype] = CREATED
        else:
            assert not item.sent and item.destination_id is None, (
                f"{pair}: {rtype}: the target does not advertise create but the writer sent it "
                f"(status={item.status!r}, destination_id={item.destination_id!r})"
            )
            assert item.skipped_reason and "does not advertise create" in item.skipped_reason, (
                f"{pair}: {rtype}: expected the writer's structured refusal, got "
                f"skipped_reason={item.skipped_reason!r}"
            )
            outcomes[rtype] = REFUSED_NOT_ADVERTISED

    delivered = [e for e in audit.events if e.get("action") == "record.deliver"]
    assert len(delivered) == result.sent_count, (
        f"{pair}: {result.sent_count} record(s) sent but {len(delivered)} record.deliver audit "
        "entries - the writer audits before every write"
    )

    return Cell(
        source=source, target=target, outcomes=outcomes,
        creatable=creatable, conditional_create=profile.supports_conditional_create,
    )


def _confirm_present(target_base: str, token: str, rtype: str, expected_source: str, pair: str) -> None:
    """The target holds exactly one record carrying THIS delivery's
    meta.source. Searched on _source the way core/verify/delivery.py
    does, then matched exactly: every source delivers the same synthetic
    ids into a target, so a substring match on the storage key alone
    would count other pairs' records as this one."""
    import requests

    response = requests.get(
        f"{target_base}/{rtype}?_source={quote(expected_source, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"},
        timeout=HTTP_TIMEOUT,
    )
    assert response.status_code == 200, f"{pair}: {rtype}: confirmation search answered {response.status_code}"
    bundle = response.json()
    exact = [
        e["resource"] for e in bundle.get("entry", [])
        if (e.get("resource", {}).get("meta") or {}).get("source") == expected_source
    ]
    assert bundle.get("total") == 1 and len(exact) == 1, (
        f"{pair}: {rtype}: the target reports {bundle.get('total')} record(s) with "
        f"meta.source={expected_source!r} ({len(exact)} exact); expected exactly one"
    )


# ---------------------------------------------------------------------------
# The proof document
# ---------------------------------------------------------------------------

def git_commit() -> str:
    def run(*args) -> str:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()

    try:
        short = run("rev-parse", "--short", "HEAD")
        dirty = run("status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"unknown ({exc})"
    return f"`{short}`" + (" (uncommitted changes in the working tree)" if dirty else "")


def dataset_description() -> str:
    """Named from the emulator's own loader, with the counts it serves."""
    from emulators.server import _load_dataset

    dataset = _load_dataset()
    total = sum(len(v) for v in dataset.values())
    prefixed = all(str(r.get("id", "")).startswith(SYNTHETIC_ID_PREFIX)
                   for rows in dataset.values() for r in rows)
    return (
        f"the synthetic `{SYNTHETIC_ID_PREFIX}*` fixtures from `scripts/mock_epic_server.py` "
        f"(RESOURCES_BY_TYPE), served by `emulators/server.py`: {total} resources across "
        f"{len(dataset)} types, {len(dataset.get('Patient', []))} fabricated patients. "
        f"SYNTHETIC, NOT PHI - every id carries the `{SYNTHETIC_ID_PREFIX}` prefix "
        f"({'verified' if prefixed else 'NOT verified'} on load), every note labels itself "
        "synthetic, and nothing leaves 127.0.0.1."
    )


def render_markdown(report: MatrixReport, *, commit: str, dataset: str,
                    started_at: datetime, duration_seconds: float, command: str) -> str:
    vendors = report.vendors
    n = len(vendors)
    lines: list[str] = []
    add = lines.append

    grants = sorted({a.grant for i in report.ingests.values() for a in [i.auth]})
    key_descriptions = sorted({i.auth.key_description for i in report.ingests.values()
                               if i.auth.key_description != "-"})

    add("# PHI AI end-to-end matrix proof")
    add("")
    add(f"Generated {started_at.isoformat(timespec='seconds')} by `{command}` in {duration_seconds:.1f}s.")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Commit | {commit} |")
    add(f"| Vendors | {n}: {', '.join(vendors)} |")
    add(f"| Pairs | {n * n} ({n} x {n}: every vendor as source and as target, the diagonal included) |")
    offsets = {report.ports[k] - DEFAULT_PORTS[k] for k in report.ports}
    if offsets == {0}:
        where = "on their DEFAULT_PORTS ports (emulators/vendors.py)"
    else:
        where = ("on DEFAULT_PORTS + " + "/".join(str(o) for o in sorted(offsets))
                 + " (--port-offset; the DEFAULT_PORTS ports were in use by something else)")
    ports = ", ".join(f"{k} {p}" for k, p in report.ports.items())
    if report.attached:
        attached = ", ".join(f"{k} (port {p})" for k, p in report.attached.items())
        add(f"| Emulators | {n - len(report.attached)} started by this run in-process {where}; "
            f"{len(report.attached)} ATTACHED to already-running processes whose state predates "
            f"this run: {attached}. Ports: {ports} |")
    else:
        add(f"| Emulators | all {n} started by this run, in-process {where}, serving the "
            f"pristine dataset. Ports: {ports} |")
    add(f"| Dataset | {dataset} |")
    add(f"| Grants used | {', '.join(grants) if grants else '(no source authenticated)'} |")
    add(f"| Signing keys | {'; '.join(key_descriptions) if key_descriptions else '(none used)'} - generated in memory for this run, never written to disk; the public halves were registered as a JWK Set with every emulator this run started, so those token endpoints verified each assertion's signature, audience, expiry and claims against the key its kid names |")
    add(f"| Run id | `{RUN_ID}` (carried in every delivered record's meta.source) |")
    add(f"| Result | **{report.passed}/{n * n} pairs passed**{'' if report.ok else f', {len(report.failures)} failure(s) - see below'} |")
    add("")

    add("## Sources")
    add("")
    add("One row per source: how the real client authenticated, which wrong token requests the emulator refused first (each an observed HTTP 400 with the documented error code - see `probe_refusals()`), what `/metadata` said, how many pages the paged search took, what was ingested, and what `$export` did.")
    add("")
    add("| source | grant / alg | key | signature verified by emulator | explicit scopes | refused (observed 400s before the real grant) | /metadata | pages | ingested | $export |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for key in vendors:
        ingested = report.ingests.get(key)
        if ingested is None:
            add(f"| {key} | FAILED | | | | | | | | {report.failures.get(key, 'not reached')} |")
            continue
        export = (f"ok ({ingested.export_files} NDJSON files, {ingested.export_rows} rows)"
                  if ingested.export == "ok" else "refused (OperationOutcome)")
        if ingested.auth.grant == "client_secret":
            verified = "n/a (no assertion)"
        elif ingested.auth.signature_verified_by_emulator:
            verified = "yes"
        else:
            verified = "NO - alg header only (attached emulator, keys not registered)"
        refused = "; ".join(ingested.auth.refusals) or "(none probed)"
        add(f"| {key} | {ingested.auth.grant} | {ingested.auth.key_description} | {verified} | "
            f"{'yes' if ingested.auth.explicit_scopes else 'no'} | {refused} | "
            f"CapabilityStatement, FHIR {ingested.fhir_version} | {ingested.total_pages} | "
            f"{ingested.total_resources} resources / {len(ingested.types)} types | {export} |")
    add("")

    add("## Targets")
    add("")
    add("What each target's own CapabilityStatement advertised create for (read by the writer), and whether it supports conditional create.")
    add("")
    add("| target | advertises create for | conditional create |")
    add("|---|---|---|")
    for key in vendors:
        cells = [c for (s, t), c in report.cells.items() if t == key and s != t]
        if cells:
            creatable = ", ".join(sorted(cells[0].creatable)) or "nothing"
            conditional = "yes" if cells[0].conditional_create else "no (delivery refused unattended; re-run with allow_duplicates)"
        else:
            creatable, conditional = "?", "?"
        add(f"| {key} | {creatable} | {conditional} |")
    add("")

    add("## Matrix: source (rows) -> target (columns)")
    add("")
    add(f"Each cell is one migration through `core/fhir/delivery/writer.py`; its letters are the outcome for "
        + ", ".join(DELIVERY_TYPES) + " in that order. "
        + "; ".join(f"**{code}** = {outcome}" for outcome, code in OUTCOME_CODES.items())
        + f"; **{FAILED_CODE}** = failed (listed below). A created record was confirmed present in the target by search.")
    add("")
    add("| source \\ target | " + " | ".join(vendors) + " |")
    add("|---|" + "---|" * n)
    for source in vendors:
        row = []
        for target in vendors:
            cell = report.cells.get((source, target))
            row.append(cell.code if cell else FAILED_CODE)
        add(f"| {source} | " + " | ".join(row) + " |")
    add("")

    if report.failures:
        add("## Failures")
        add("")
        for name, reason in report.failures.items():
            add(f"- `{name}`: {reason}")
        add("")

    add("## What this proves, and does not")
    add("")
    add("Every row above ran the real ingestion client (`core/fhir/client.py`, `core/fhir/bulk_client.py`) "
        "and every cell the real delivery writer against per-vendor emulators (`emulators/`) that reproduce "
        "each vendor's documented seams: the grant it takes, the assertion algorithm it accepts, whether it "
        "demands explicit scopes, whether it has `$export`, what it advertises create for, and whether it "
        "honours conditional create. It is not certification against any vendor's live system - every "
        "profile in `core/fhir/emr_profiles.py` still says to confirm against the instance's own "
        "CapabilityStatement, and that stands.")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_matrix(session: MatrixSession, vendors) -> MatrixReport:
    """Ingest every vendor, then deliver every pair. Failures are
    recorded per source / per pair and the run continues, so one proof
    document reports the whole matrix; main() exits non-zero on any."""
    vendors = list(vendors)
    for source in vendors:
        try:
            ingested = session.ingest(source)
        except Exception as exc:
            _progress(f"ingest {source:16} FAILED  {type(exc).__name__}: {exc}")
            continue
        _progress(f"ingest {source:16} ok      {ingested.auth.grant}, {ingested.total_pages} pages, "
                  f"{ingested.total_resources} resources, $export {ingested.export}")

    for source, target in itertools.product(vendors, vendors):
        pair = f"{source}->{target}"
        try:
            cell = session.run_pair(source, target)
        except Exception as exc:
            _progress(f"deliver {pair:34} FAILED  {type(exc).__name__}: {exc}")
            continue
        _progress(f"deliver {pair:34} {cell.code}")

    return session.report(vendors)


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _parse_vendors(value: str) -> list[str]:
    if value.strip().lower() in ("", "all"):
        return sorted(VENDORS)
    chosen = [v.strip().lower() for v in value.split(",") if v.strip()]
    unknown = [v for v in chosen if v not in VENDORS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown vendor(s) {', '.join(unknown)}; known: {', '.join(sorted(VENDORS))}"
        )
    return sorted(dict.fromkeys(chosen))


def main(argv: Optional[list] = None) -> int:
    import logging

    parser = argparse.ArgumentParser(
        prog="python scripts/e2e_matrix.py",
        description="Run the source x target end-to-end matrix against the synthetic EMR "
                    "emulators and write a Markdown proof. Exits 1 if any pair fails.",
    )
    parser.add_argument("--out", type=Path, default=None,
                        help="write the Markdown proof here (it is always printed to stdout)")
    parser.add_argument("--vendors", type=_parse_vendors, default="all",
                        help="comma-separated vendor keys from emulators.vendors.VENDORS (default: all)")
    parser.add_argument("--keep-running", action="store_true",
                        help="leave the emulators this run started serving until Ctrl-C")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="add N to every DEFAULT_PORTS port (default 0). For a machine where "
                             "those ports are held by emulators that belong to something else; "
                             "the proof states the ports actually used")
    args = parser.parse_args(argv)
    vendors = _parse_vendors(args.vendors) if isinstance(args.vendors, str) else args.vendors

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    # Paths in the recorded command are rendered relative to the
    # checkout's parent, so the proof never carries an operator's home
    # directory; the full path stays in the stderr 'wrote ...' line.
    def _portable(arg: str) -> str:
        if os.path.isabs(arg):
            try:
                relative = os.path.relpath(arg, REPO.parent)
            except ValueError:
                relative = ".."
            # Beside the checkout: say so. Anywhere else: the file name only.
            return relative if not relative.startswith("..") else os.path.basename(arg)
        return arg

    command = "scripts/e2e_matrix.py " + " ".join(
        _portable(a) for a in (argv if argv is not None else sys.argv[1:])
    )
    started_at = datetime.now(timezone.utc)
    clock = time.monotonic()

    keys = generate_signing_keys()
    handles = start_emulators(vendors, reuse_running=True, client_jwks=keys.jwks,
                              port_offset=args.port_offset)
    for key in vendors:
        handle = handles[key]
        _progress(f"emulator {key:16} {handle.base_url}{VENDORS[key].fhir_path}"
                  f"{'' if handle.started_here else '  (already running, attached)'}")

    session = MatrixSession(handles, keys)
    report = run_matrix(session, vendors)
    text = render_markdown(
        report, commit=git_commit(), dataset=dataset_description(),
        started_at=started_at, duration_seconds=time.monotonic() - clock, command=command,
    )

    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        _progress(f"wrote {args.out}")

    n = len(vendors)
    _progress(f"{report.passed}/{n * n} pairs passed; {len(report.failures)} failure(s)")

    if args.keep_running:
        _progress("emulators left running (synthetic data only). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    stop_emulators(handles)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
