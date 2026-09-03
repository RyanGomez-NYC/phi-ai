# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
FHIR R4 ingestion client, scoped to Epic.

Pulls resources from a health system's Epic FHIR endpoint using SMART Backend
Services and hands each resource to the storage/crypto/audit layers to be
stored.

AUTH: Epic backend services does NOT use a client secret. It uses
asymmetric JWT client assertion (RFC 7523): the app holds a private key,
signs a short-lived JWT with it, and presents that signed JWT to Epic's
token endpoint. Epic verifies the signature against the public key that
was registered for this client ID on open.epic.com. There is no shared
secret to leak, and Epic's docs are explicit that a client secret does
not work for this flow - see docs/EMR_CONNECTORS.md.

THE SIGNING ALGORITHM IS PER VENDOR, never assumed. SMART App Launch's
client-confidential-asymmetric profile ("Underlying Standards") requires
the CLIENT to support both RS384 and ES384 but lets a server validate
"at least one" of them - so which one a given vendor accepts is that
vendor's documented choice. build_client_assertion() signs with the
algorithm it is given, and authenticate() hands it the profile's
EMRProfile.assertion_algorithm - recorded there, per vendor, from that
vendor's own documentation (Epic documents RS384). The key
family follows from the algorithm, by RFC 7518: §3.3 defines RS384 as
RSASSA-PKCS1-v1_5, so it needs an RSA key; §3.4 defines ES384 as ECDSA on
P-384, so it needs an EC key on secp384r1. check_private_key_signs()
below detects the mounted key's family with the cryptography library and
refuses one that cannot produce the algorithm BEFORE any signing happens;
core/config/settings.py runs the same check at startup, so a mismatch is
reported next to its cause with the vendor and the variable named.

DEBUG LOGGING: authenticate() logs full REQUEST detail (headers, body,
claims) at logging.DEBUG. Two things are never logged at any level: the
private key (only a SHA256 fingerprint of it, sufficient to confirm
which key was used without exposing key material), and the access_token
in the token RESPONSE body - the success response body is redacted down
to its key names, because the token is a live ~1-hour bearer credential
for the entire in-scope patient population and log readers are a
broader audience than anyone granted PHI access. Enable with:
    import logging; logging.basicConfig(level=logging.DEBUG)
or set the 'phi-ai.fhir.client' logger to DEBUG specifically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional

from core.audit.log import AuditLog
from core.crypto.envelope import EnvelopeEncryptor
from core.fhir.emr_profiles import EMRProfile
from core.storage.base import ObjectStore

log = logging.getLogger("phi-ai.fhir.client")


@dataclass
class StoreResult:
    resource_type: str
    resource_id: str
    storage_key: str
    version_id: Optional[str]
    sha256_hex: str
    retention_until: Optional[datetime] = None
    # How many resources the stored object holds: 1 for a per-resource
    # write, N for a bundle. Carried into the index so verification can
    # count a bundled store without opening it.
    resource_count: int = 1


def _retention_until(now: datetime, years: int) -> datetime:
    """
    Retention deadline as `years` FULL CALENDAR YEARS from `now`, not
    `365 * years` days from now.

    FOUND AND FIXED (2026-08-17 audit, MEDIUM): both store_resource()
    and store_psychotherapy_resource() previously computed
    `now + timedelta(days=365 * retention_years)`. Every calendar year
    containing a leap day is 366 days, so a flat 365-day-per-year
    multiplier UNDERSHOOTS the true calendar anniversary by one day per
    leap day inside the retention window - the wrong direction to err
    for a statutory retention period. Proven live: for a resource
    stored 2026-08-18 with a 10-year retention period, the old
    formula computes 2036-08-15 (three days short, one per leap day in
    2028/2032/2036) where the actual 10-year calendar anniversary is
    2036-08-18. Real consequence: core/fhir/purge.py's `expired` mode
    treats retention_until as the authoritative "has this record's
    mandatory retention period actually elapsed" gate (require_expired
    in _dispose_one, the only guard left now that S3 Object Lock has
    been removed) - an undershoot means the tool would treat some
    records as eligible for routine disposal days before the retention
    period a state's medical-records-retention statute actually
    requires has elapsed.

    Fixed by advancing the calendar year field directly rather than
    approximating years as a fixed day count. The one edge case -
    `now` is Feb 29 and `now.year + years` is not a leap year, so
    Feb 29 doesn't exist in the target year - clamps to Feb 28 rather
    than rolling forward to Mar 1, so the deadline stays inside the
    same calendar month rather than silently gaining an extra day.
    """
    try:
        return now.replace(year=now.year + years)
    except ValueError:
        return now.replace(month=2, day=28, year=now.year + years)


def _stored_sha256_hex(nonce: bytes, ciphertext: bytes) -> str:
    """
    The digest that must be recorded alongside a stored object -
    computed over the EXACT bytes actually written to storage
    (nonce-prefixed ciphertext), not over the AES-GCM ciphertext alone.

    FIXED BUG, worth being explicit about: EnvelopeEncryptor.encrypt()
    (core/crypto/envelope.py) returns EncryptedPayload.sha256_hex as a
    digest of `ciphertext` alone - correct and clearly documented for
    what that class does ("digest of the *ciphertext*, for storage-layer
    integrity checks"). But every caller in this module stores
    `nonce + ciphertext` combined (the nonce has to travel with the
    ciphertext for AES-GCM decryption - see store_resource() below),
    and both store_resource() and store_psychotherapy_resource()
    were previously passing payload.sha256_hex - the ciphertext-alone
    digest - as the sha256_hex for the COMBINED bytes actually given to
    put_object(). Those two byte sequences are different, so
    ObjectStore.verify_integrity() (core/storage/base.py) - and any
    restore-time check built the same way - would recompute a hash over
    the combined bytes and compare it against a digest of a shorter,
    different byte sequence. That comparison fails for every single
    object ever stored through this path, including completely
    untampered ones - not a rare edge case, a systemic false positive
    on every integrity check. See tests/test_client_store.py for the
    regression test that catches this if it's ever reintroduced.

    This function is the one place that combination now happens, so
    every caller in this module uses the same, correct definition of
    "the digest of what got stored" rather than each recomputing it
    independently and risking the same mismatch again.

    WHAT THE DIGEST COVERS IS PART OF THE STORAGE CONTRACT. Every object
    records its digest under this rule, and integrity verification
    recomputes it the same way, so changing what the digest covers makes
    verification fail against every object written under the old rule.
    Change it only alongside a deliberate re-verification of everything
    already stored.
    """
    return hashlib.sha256(nonce + ciphertext).hexdigest()


class ClientAssertionKeyError(ValueError):
    """
    The private key handed to build_client_assertion() cannot sign the
    requested algorithm: wrong family (an RSA key for ES384, an EC key for
    RS384), wrong curve (ES384 needs secp384r1 / P-384), a
    password-protected PEM, or bytes that are not a PEM private key at
    all. Raised by check_private_key_signs() before anything is signed.
    core/config/settings.py runs the same check at startup and wraps this
    in a ConfigError that names the vendor and the environment variable.
    """


def describe_private_key(private_key_pem: bytes) -> str:
    """
    What the key IS, for error text and debug logs: "an RSA key
    (2048-bit)", "an EC key on curve secp384r1 (P-384)", "a
    password-protected PEM", ... Detected with the cryptography library -
    the one PyJWT signs with underneath - from the loaded key object's
    type and, for EC keys, its curve. Never includes, hashes, or logs the
    key material: the description is family and size only.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except TypeError:
        # cryptography's signal for an encrypted PEM loaded with no password.
        return "a password-protected PEM (the key must be unencrypted)"
    except ValueError:
        return "bytes that are not a PEM private key"
    if isinstance(key, rsa.RSAPrivateKey):
        return f"an RSA key ({key.key_size}-bit)"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        p384 = " (P-384)" if isinstance(key.curve, ec.SECP384R1) else ""
        return f"an EC key on curve {key.curve.name}{p384}"
    return f"a {type(key).__name__}"


def _algorithm_needs(signer) -> str:
    """
    The key family a PyJWT signing-algorithm object requires, read off
    the object itself - its class, and for ECDSA the expected_curve PyJWT
    registered it with - rather than from a table kept here that could
    drift from PyJWT's own registry.
    """
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    if isinstance(signer, ECAlgorithm):
        curve = getattr(signer, "expected_curve", None)
        return f"an EC key on curve {curve.name}" if curve is not None else "an EC key"
    if isinstance(signer, RSAAlgorithm):
        return "an RSA key"
    return f"a key {type(signer).__name__} can sign with"


def check_private_key_signs(algorithm: str, private_key_pem: bytes) -> None:
    """
    Refuse, with a ClientAssertionKeyError naming both sides, when
    `private_key_pem` cannot sign a JWT with `algorithm`.

    Asked of the signer rather than re-derived here:
    jwt.get_algorithm_by_name(algorithm).prepare_key() is exactly what
    pyjwt.encode() runs before signing - PEM parse, key-family check, and
    for the ES* algorithms the curve check - so this project keeps no
    second algorithm-to-key-family table that could drift from PyJWT's.
    PyJWT would refuse the same key on its own, but only at the first
    token request, and its 2.13 family-mismatch message renders a
    generator object where the expected type should be; this says what
    was found and what was needed.
    """
    import jwt as pyjwt

    try:
        signer = pyjwt.get_algorithm_by_name(algorithm)
    except NotImplementedError as exc:
        raise ClientAssertionKeyError(
            f"{algorithm!r} is not an algorithm PyJWT can sign with - it must be a JWS "
            "algorithm name such as RS384 or ES384 (see EMRProfile.assertion_algorithm)"
        ) from exc
    try:
        signer.prepare_key(private_key_pem)
    except (pyjwt.InvalidKeyError, ValueError, TypeError) as exc:
        # Observed with PyJWT 2.13 / cryptography 50: InvalidKeyError for
        # a wrong family or curve (and for non-PEM bytes under RS*),
        # ValueError for non-PEM bytes under ES*, TypeError for an
        # encrypted PEM. All three mean the same thing to a caller.
        raise ClientAssertionKeyError(
            f"{algorithm} signs with {_algorithm_needs(signer)}, but the key provided is "
            f"{describe_private_key(private_key_pem)}"
        ) from exc


class FHIRIngestionClient:
    def __init__(
        self,
        base_url: str,
        profile: EMRProfile,
        storage: ObjectStore,
        encryptor: EnvelopeEncryptor,
        audit: AuditLog,
        retention_years: int,
        retention_years_overrides: Optional[dict[str, int]] = None,
        actor: str = "phi-ai-ingestion-service",
        index_writer: Optional[Callable[["StoreResult", dict], None]] = None,
        omop_writer: Optional[Callable[["StoreResult", dict], None]] = None,
        psychotherapy_storage: Optional[ObjectStore] = None,
        psychotherapy_encryptor: Optional[EnvelopeEncryptor] = None,
        profile_config=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        # The DEPLOYMENT shape (small/large), distinct from `profile`
        # above which is the EMR vendor profile. Defaults to small so an
        # existing caller that does not pass one behaves exactly as
        # before.
        if profile_config is None:
            from core.config.scale_profile import SMALL

            profile_config = SMALL
        self.profile_config = profile_config
        self.storage = storage
        self.encryptor = encryptor
        self.audit = audit
        self.retention_years = retention_years
        # Per-resource-type retention, in years - keyed by FHIR
        # resourceType (e.g. "DocumentReference"). Falls back to
        # retention_years above for any type not listed here. See
        # core/config/settings.py's _parse_retention_overrides() and
        # docs/COMPLIANCE.md for why a single flat retention period
        # across everything stored isn't always sufficient.
        self.retention_years_overrides = retention_years_overrides or {}
        self.actor = actor
        self._access_token: Optional[str] = None
        # Optional: called (result, resource) after a successful S3 write
        # and audit record. Failures here are logged and swallowed - see
        # store_resource below. S3 and the audit log remain the system
        # of record regardless of whether this succeeds. See
        # core/db/index.py and core/fhir/scheduler.py for how this is
        # wired up to the Postgres queryable index.
        self.index_writer = index_writer

        # Same optional, best-effort, independently-logged shape as
        # index_writer above - called (result, resource) after the same
        # successful S3 write and audit record, never the other way
        # around. A SEPARATE callback rather than composed into
        # index_writer deliberately: the index write and the OMOP
        # analytics write are two independent, independently-
        # fallible operations against two different Postgres roles
        # (phi_ai_ingest vs omop_etl - core/db/bootstrap_aws.sql
        # vs core/db/omop_bootstrap_aws.sql) - a failure in one should
        # never be masked by, or mistaken for, a failure in the other.
        # See core/db/omop_etl.py and core/fhir/scheduler.py for how
        # this is wired up.
        #
        # NEVER called from store_psychotherapy_resource() below, for
        # the same reason index_writer never is there, only more so:
        # that method's own docstring treats even the STRUCTURAL fact
        # that a psychotherapy note exists as sensitive enough to keep
        # out of any index. Putting full structured clinical content
        # from a psychotherapy encounter into a broadly-queryable OMOP
        # table would be a much larger version of exactly the exposure
        # that boundary exists to prevent - store_psychotherapy_resource()
        # simply never references self.omop_writer at all, the same way
        # it already never references self.index_writer.
        self.omop_writer = omop_writer

        # Genuinely separate storage/encryption for psychotherapy notes -
        # see store_psychotherapy_resource() below and
        # runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md. Both optional and
        # independently None-able: a deployment with no behavioral health
        # data simply never sets these, and store_psychotherapy_resource()
        # is never called.
        self.psychotherapy_storage = psychotherapy_storage
        self.psychotherapy_encryptor = psychotherapy_encryptor

    @property
    def access_token(self) -> Optional[str]:
        """The bearer token obtained by the most recent authenticate()
        call, or None if authenticate() hasn't been called yet. Public
        accessor so other modules (e.g. core/fhir/bulk_client.py, which
        reuses this client's auth rather than duplicating it) don't need
        to reach into a private attribute."""
        return self._access_token

    # -- Auth ---------------------------------------------------------

    @staticmethod
    def build_client_assertion(
        client_id: str,
        token_url: str,
        private_key_pem: bytes,
        jwt_kid: Optional[str] = None,
        algorithm: str = "RS384",
    ) -> str:
        """
        Build the signed JWT client assertion the SMART Backend Services
        flow requires in place of a client secret.

        Epic's stated requirements (see docs/EMR_CONNECTORS.md for the
        source): RS384 signature, `iss` and `sub` both set to the client
        ID, `aud` set to the token endpoint, a unique `jti` per request,
        and an expiry no more than 5 minutes out. This assertion is
        single-use and short-lived by design - it authenticates one token
        request, not a session.

        `algorithm` is the JWS "alg" the assertion is signed with AND
        labelled with in its header. It defaults to RS384 - what this
        builder always signed with before it became a parameter, and
        Epic's documented requirement - so every existing caller is
        unchanged; authenticate() passes the vendor profile's
        assertion_algorithm. The key must be of the family the algorithm
        signs with (RFC 7518 §3.3: RSA for RS384; §3.4: EC on P-384 for
        ES384); a mismatch is a ClientAssertionKeyError before anything
        is signed.
        """
        import jwt as pyjwt

        # Load the PEM and detect its family (RSA vs EC, and the curve)
        # first: refuse a key that cannot produce `algorithm` BEFORE
        # signing, naming both sides, rather than letting PyJWT refuse
        # with a message that names neither. Same check settings.py runs
        # at startup; this one also covers callers that never go through
        # Settings.from_env() (hand-built Settings, the delivery entry
        # point).
        key_description = describe_private_key(private_key_pem)
        check_private_key_signs(algorithm, private_key_pem)

        now = int(time.time())
        claims = {
            "iss": client_id,
            "sub": client_id,
            "aud": token_url,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + 240,  # comfortably under Epic's 5-minute ceiling
        }
        # PyJWT signs with headers["alg"] in preference to the algorithm=
        # argument (jwt/api_jws.py: "Prefer headers values if present to
        # function parameters"), so BOTH the header below and the encode()
        # call come from the one `algorithm` value - setting only one of
        # them would silently keep the other.
        headers = {"alg": algorithm, "typ": "JWT"}
        if jwt_kid:
            # Only needed if the public key was registered via a JWK Set
            # URL rather than uploaded directly. Most direct-upload
            # registrations don't require this.
            headers["kid"] = jwt_kid

        # Never log private_key_pem itself. A SHA256 fingerprint lets you
        # confirm which key produced a given assertion without the key
        # material ever touching a log line, log aggregator, or terminal
        # scrollback.
        key_fingerprint = hashlib.sha256(private_key_pem).hexdigest()[:16]
        log.debug(
            "Building client assertion: header=%s claims=%s private_key=%s "
            "private_key_sha256_prefix=%s",
            headers, claims, key_description, key_fingerprint,
        )

        assertion = pyjwt.encode(claims, private_key_pem, algorithm=algorithm, headers=headers)
        log.debug("Assertion built, length=%d chars", len(assertion))
        return assertion

    def authenticate(
        self,
        client_id: str,
        private_key_pem: bytes,
        token_url: str,
        jwt_kid: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> None:
        """
        SMART Backend Services token request using a signed JWT client
        assertion in place of a client secret. Requires the private key
        whose matching public key was registered for this client ID with
        the vendor (Epic: uploaded on open.epic.com - see
        runbooks/RUNBOOK_AWS_SETUP.md and scripts/generate_epic_keypair.sh
        for the one-time setup). The assertion is signed with this
        client's profile's assertion_algorithm, so the key must be of
        that algorithm's family - RSA for RS384, EC P-384 for ES384 - or
        build_client_assertion() refuses it.

        `scope` defaults to None (omitted entirely) rather than a preset
        value: Epic's own "Step 2: POSTing the JWT to Token Endpoint"
        documentation lists exactly three required POST body parameters
        for this flow - grant_type, client_assertion_type,
        client_assertion - and does not document scope as part of the
        backend-services token request at all. Pass an explicit scope
        string only if you have a specific reason to include one.
        """
        import requests

        assertion = self.build_client_assertion(
            client_id, token_url, private_key_pem, jwt_kid,
            algorithm=self.profile.assertion_algorithm,
        )

        data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        }
        if scope:
            data["scope"] = scope

        # Log the fully prepared request - actual bytes and headers that
        # will go on the wire, including whatever requests/urllib3 add by
        # default (User-Agent, Accept, Accept-Encoding, Connection) that
        # calling code never explicitly sets and therefore rarely
        # inspects. The assertion itself is safe to log in full: it's a
        # signed, single-use, ~4-minute-lived credential, not a durable
        # secret - unlike the private key that produced it.
        prepared = requests.Request("POST", token_url, data=data).prepare()
        log.debug("Token request URL: %s", prepared.url)
        log.debug("Token request method: %s", prepared.method)
        log.debug("Token request headers (pre-send, before session-level additions): %s", dict(prepared.headers))
        log.debug("Token request body: %s", prepared.body)

        with requests.Session() as session:
            resp = session.send(prepared, timeout=30)
            log.debug("Token request headers (as sent by session): %s", dict(resp.request.headers))
            log.debug("Token response status: %s", resp.status_code)
            log.debug("Token response headers: %s", dict(resp.headers))
            # REDACTED - never log the success response body: it contains
            # access_token, a live ~1-hour bearer credential for the
            # entire in-scope patient population, and anyone who can read
            # the log aggregator is a far broader audience than anyone
            # granted PHI access. (The client assertion logged above is
            # different in kind: single-use, ~4-minute-lived, and already
            # spent by the time a log could be read.) FOUND AND FIXED -
            # this previously logged resp.text verbatim at DEBUG, while
            # the module docstring actively invited enabling DEBUG
            # (2026-08-17 audit, H3).
            if resp.ok:
                try:
                    body_keys = sorted(resp.json().keys())
                except ValueError:
                    body_keys = []
                log.debug(
                    "Token response body: REDACTED (access_token withheld from logs); keys=%s",
                    body_keys,
                )
            else:
                # Error responses carry no token - safe and genuinely
                # useful to log in full.
                log.debug("Token response body (error, no token issued): %s", resp.text)

        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        log.debug("Access token obtained, length=%d chars", len(self._access_token))

    # -- Ingestion ------------------------------------------------------

    def authenticate_client_secret(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        scope: Optional[str] = None,
    ) -> None:
        """
        Plain OAuth 2.0 client-credentials, for vendors that issue a
        SECRET rather than accepting a signed JWT assertion.

        athenahealth is the one target that works this way (see
        EMRProfile.auth_flow). It is a separate method rather than a
        branch inside authenticate() because the two flows share nothing
        but the URL: no assertion is built, no private key is read, and
        the failure modes are different enough that one function handling
        both would obscure which one actually failed.

        The secret is sent in the POST body rather than an Authorization
        header because that is what athenahealth documents. It is never
        logged - unlike the assertion flow above, which logs the prepared
        request, this deliberately does not, because the body contains a
        long-lived credential rather than a short-lived signed token.
        """
        import requests

        response = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                **({"scope": scope} if scope else {}),
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        log.debug("token response: status=%s", response.status_code)
        response.raise_for_status()

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(
                f"token endpoint returned no access_token: {payload.get('error', payload)}"
            )
        self._access_token = token

    def authenticate_from_settings(self, settings) -> None:
        """
        Authenticate the way this client's vendor profile documents,
        using the deployment's Settings.

        This is the ONE place the profile's auth_flow is turned into a
        choice of token request, and it exists because the choice used to
        be made nowhere: core/fhir/scheduler.py and bulk_scheduler.py
        both called authenticate() - the JWT-assertion flow - regardless
        of vendor, so ingestion against athenahealth (which rejects an
        assertion outright, see tests/test_emulator_integration.py) or
        Oracle Health (which rejects a token request without explicit
        system scopes) failed with the vendor's 400 at startup while the
        emulator suite, exercising the client methods directly, stayed
        green. The seam that made the client testable per-flow was
        exactly the seam the schedulers' hardcoded choice hid behind.

        Scope derivation lives here too: where the profile says explicit
        scopes are mandatory (requires_token_scopes - Oracle Health),
        one system/{Type}.read scope per supported resource type is sent,
        never a wildcard. Where it doesn't, no scope parameter is sent at
        all, matching Epic's documented three-parameter token request.
        """
        profile = self.profile
        scope = None
        if profile.requires_token_scopes:
            scope = " ".join(
                f"system/{rtype}.read" for rtype in profile.supported_resources
            )

        if profile.auth_flow == "oauth2_client_credentials":
            if not settings.fhir_client_secret:
                # Settings.from_env() already refuses this combination at
                # startup; this guard catches hand-built Settings objects
                # (tests, future callers) with the same variable name in
                # the message.
                raise RuntimeError(
                    f"{profile.name} authenticates with a client secret, but "
                    "PHI_AI_FHIR_CLIENT_SECRET is not set"
                )
            self.authenticate_client_secret(
                client_id=settings.fhir_client_id,
                client_secret=settings.fhir_client_secret,
                token_url=settings.fhir_token_url,
                scope=scope,
            )
            return

        self.authenticate(
            client_id=settings.fhir_client_id,
            private_key_pem=settings.fhir_private_key_pem,
            token_url=settings.fhir_token_url,
            jwt_kid=settings.fhir_jwt_kid,
            scope=scope,
        )

    def iter_resources(self, resource_type: str, since: Optional[datetime] = None) -> Iterator[dict]:
        """Page through a FHIR search for the given resource type,
        optionally filtered by _lastUpdated for incremental ingestion."""
        import requests

        if resource_type not in self.profile.supported_resources:
            raise ValueError(
                f"{self.profile.name} profile does not list {resource_type} as supported; "
                "confirm against the instance's CapabilityStatement before proceeding."
            )

        params = {"_count": self.profile.page_size}
        if since:
            params["_lastUpdated"] = f"gt{since.isoformat()}"

        url = f"{self.base_url}/{resource_type}"
        headers = {"Authorization": f"Bearer {self._access_token}", "Accept": "application/fhir+json"}

        while url:
            log.debug("FHIR request: GET %s params=%s", url, params)
            resp = requests.get(url, params=params, headers=headers, timeout=60)
            log.debug("FHIR response: status=%s", resp.status_code)
            resp.raise_for_status()
            bundle = resp.json()

            for entry in bundle.get("entry", []):
                yield entry["resource"]

            next_link = next(
                (link["url"] for link in bundle.get("link", []) if link["relation"] == "next"),
                None,
            )
            url, params = next_link, None  # subsequent requests use the full next URL as-is

    def read_resource(self, resource_type: str, resource_id: str) -> dict:
        """Fetch one current resource from the source EMR by id.

        Added for core/verify/freshness.py, which needs the source's
        CURRENT version of a specific record to tell whether the stored
        copy has been superseded. Deliberately narrow - a single read, no
        paging, no search parameters - so it cannot be mistaken for
        another ingestion path.
        """
        import requests

        response = requests.get(
            f"{self.base_url}/{resource_type}/{resource_id}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/fhir+json",
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def _retention_years_for(self, resource_type: str) -> int:
        """The retention period, in years, that applies to a given FHIR
        resource type: the per-type override if one is configured for
        this exact type, otherwise the deployment-wide default. See
        __init__'s retention_years_overrides docstring."""
        return self.retention_years_overrides.get(resource_type, self.retention_years)

    def store_resource(self, resource: dict) -> StoreResult:
        """Store one resource.

        Under the `large` profile this still writes ONE OBJECT PER CALL -
        it is the single-resource path and cannot bundle what it has not
        been given. Bundling happens in store_all()/store_bundle(),
        which see a whole patient's resources at once. Calling this in a
        loop on a large deployment produces the per-resource object count
        the profile exists to avoid, so store_all() is the entry point
        that respects the profile.
        """
        from core.storage.layout import locate

        resource_type = resource["resourceType"]
        resource_id = resource["id"]
        # Derived through the layout rather than formatted here, so the
        # two profiles cannot disagree about where a resource lives.
        storage_key = locate(resource, self.profile_config).storage_key

        plaintext = json.dumps(resource, sort_keys=True).encode("utf-8")
        payload = self.encryptor.encrypt(plaintext)

        # Nonce must travel with the ciphertext for AES-GCM decryption;
        # store it prefixed onto the ciphertext rather than as a
        # separate object or a metadata field. The digest recorded
        # alongside the object MUST describe these exact combined bytes
        # - see _stored_sha256_hex()'s docstring for the bug this fixes.
        storage_bytes = payload.nonce + payload.ciphertext
        stored_sha256_hex = _stored_sha256_hex(payload.nonce, payload.ciphertext)

        retention_years = self._retention_years_for(resource_type)
        retention_until = _retention_until(datetime.now(timezone.utc), retention_years)

        stored = self.storage.put_object(
            key=storage_key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=payload.wrapped_dek_b64,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
            content_type="application/fhir+json",
        )

        self.audit.record(
            actor=self.actor,
            action="record.write",
            resource_key=storage_key,
            purpose_of_use="scheduled_ingestion",
        )

        result = StoreResult(
            resource_type=resource_type,
            resource_id=resource_id,
            storage_key=storage_key,
            version_id=stored.version_id,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
        )

        # Best-effort secondary index write. S3 + the audit log above are
        # already durable and complete at this point; a failure here must
        # never look like a failed write, and must never raise past
        # this method - the resource IS safely stored either way. The
        # index can always be rebuilt from S3 later if it drifts.
        if self.index_writer is not None:
            try:
                self.index_writer(result, resource)
            except Exception as exc:
                log.error(
                    "Index write failed for %s (resource stored successfully in S3): %s",
                    storage_key,
                    exc,
                )

        # Best-effort OMOP analytics write - same independence from the
        # object store write as index_writer above, and independent of
        # index_writer too: a failure here is logged on its own, never
        # raised past this method, and never mistaken for an index-write
        # failure or vice versa. See __init__'s own comment on
        # omop_writer for why this is a genuinely separate callback
        # rather than folded into index_writer.
        if self.omop_writer is not None:
            try:
                self.omop_writer(result, resource)
            except Exception as exc:
                log.error(
                    "OMOP write failed for %s (resource stored successfully in S3): %s",
                    storage_key,
                    exc,
                )

        return result

    def store_psychotherapy_resource(self, resource: dict) -> StoreResult:
        """
        Stores a psychotherapy note through a completely separate
        storage/encryption/audit path from store_resource() above - see
        the __init__ docstring for psychotherapy_storage/
        psychotherapy_encryptor and runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md
        for the full reasoning.

        Differences from store_resource(), all deliberate:
          - Writes through self.psychotherapy_storage /
            self.psychotherapy_encryptor, never self.storage /
            self.encryptor - raises RuntimeError if either isn't
            configured, rather than falling back to the general object
            store.
          - Storage key uses a "notes/" prefix, not "fhir/" - matching
            deploy/aws/iam.tf's psychotherapy_ingest role, which is
            scoped to that exact prefix. THE PREFIX IS AN ACCESS-CONTROL
            BOUNDARY, not a naming convention, and must stay in step with
            that role.
          - Audit action is "record.write.psychotherapy", not
            "record.write" - so the audit trail itself distinguishes
            this category of write without needing to inspect the
            storage key.
          - NEVER calls self.index_writer OR self.omop_writer,
            regardless of whether either is configured on this client.
            Even structural metadata (that a psychotherapy note exists
            for a given patient) is treated as sensitive here - there is
            deliberately no index, and no OMOP analytics row, for this
            data at all.
        """
        if self.psychotherapy_storage is None or self.psychotherapy_encryptor is None:
            raise RuntimeError(
                "store_psychotherapy_resource() called but psychotherapy_storage / "
                "psychotherapy_encryptor were not configured on this client. There is no "
                "fallback to the general object store - see "
                "runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md."
            )

        resource_type = resource["resourceType"]
        resource_id = resource["id"]
        storage_key = f"notes/{resource_type}/{resource_id}.json"

        plaintext = json.dumps(resource, sort_keys=True).encode("utf-8")
        payload = self.psychotherapy_encryptor.encrypt(plaintext)

        # Same fix as store_resource() above - see _stored_sha256_hex()'s
        # docstring.
        storage_bytes = payload.nonce + payload.ciphertext
        stored_sha256_hex = _stored_sha256_hex(payload.nonce, payload.ciphertext)

        retention_years = self._retention_years_for(resource_type)
        retention_until = _retention_until(datetime.now(timezone.utc), retention_years)

        stored = self.psychotherapy_storage.put_object(
            key=storage_key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=payload.wrapped_dek_b64,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
            content_type="application/fhir+json",
        )

        self.audit.record(
            actor=self.actor,
            action="record.write.psychotherapy",
            resource_key=storage_key,
            purpose_of_use="scheduled_ingestion",
        )

        return StoreResult(
            resource_type=resource_type,
            resource_id=resource_id,
            storage_key=storage_key,
            version_id=stored.version_id,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
        )

    def store_bundle(self, storage_key: str, resources: list[dict]) -> StoreResult:
        """Write several resources as one NDJSON object.

        The `large` profile's write path. Encryption, digest accounting
        and audit are identical to store_resource() - the only
        difference is that the plaintext is NDJSON of many resources
        rather than JSON of one, so a stored bundle verifies,
        restores and disposes through the same code paths.

        The StoreResult reports the BUNDLE, not the resources inside
        it: resource_id is the bundle's group key and sha256 covers the
        whole object. That is the granularity `large` trades away, and
        the result object says so rather than implying per-resource
        precision that no longer exists.
        """
        from core.storage.layout import serialise_bundle

        if not resources:
            raise ValueError("refusing to write an empty bundle")

        resource_type = resources[0]["resourceType"]
        plaintext = serialise_bundle(resources)
        payload = self.encryptor.encrypt(plaintext)
        storage_bytes = payload.nonce + payload.ciphertext
        stored_sha256_hex = _stored_sha256_hex(payload.nonce, payload.ciphertext)

        retention_until = _retention_until(
            datetime.now(timezone.utc), self._retention_years_for(resource_type)
        )

        stored = self.storage.put_object(
            key=storage_key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=payload.wrapped_dek_b64,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
            content_type="application/fhir+ndjson",
        )

        self.audit.record(
            actor=self.actor,
            action="record.write.bundle",
            resource_key=storage_key,
            purpose_of_use="scheduled_ingestion",
        )

        result = StoreResult(
            resource_type=resource_type,
            resource_id=storage_key.rsplit("/", 1)[-1].removesuffix(".ndjson"),
            storage_key=storage_key,
            version_id=stored.version_id,
            sha256_hex=stored_sha256_hex,
            retention_until=retention_until,
            resource_count=len(resources),
        )

        # One index row per bundle, carrying the resource count so a
        # reader knows how many records the object holds without opening
        # it. Best-effort exactly as in store_resource(): storage and
        # the audit log are already durable.
        if self.index_writer is not None:
            try:
                self.index_writer(result, resources[0])
            except Exception as exc:
                log.error("Index write failed for bundle %s (stored in the object store): %s",
                          storage_key, exc)

        return result

    def store_all(self, resource_types: list[str], since: Optional[datetime] = None) -> list[StoreResult]:
        """Store every resource of the given types, respecting the profile.

        Under `small` this is one object per resource, as before. Under
        `large` resources are grouped by (patient, type) and written as
        NDJSON bundles - which is where the ~500x reduction in object
        count actually happens.

        MEMORY IS BOUNDED PER RESOURCE TYPE, not by the whole data set:
        one type's resources are grouped, written and released before the
        next type starts. Grouping everything first would need every
        resource in memory, which is exactly the failure the large
        profile exists to avoid.
        """
        from core.storage.layout import group_for_bundling

        results: list[StoreResult] = []
        for rtype in resource_types:
            if not self.profile_config.bundles:
                for resource in self.iter_resources(rtype, since=since):
                    results.append(self.store_resource(resource))
                continue

            grouped = group_for_bundling(self.iter_resources(rtype, since=since),
                                         self.profile_config)
            for storage_key, resources in grouped.items():
                if storage_key.endswith(".ndjson"):
                    results.append(self.store_bundle(storage_key, resources))
                else:
                    # Unlinked resources have no patient to bundle by and
                    # are written individually - see layout.locate().
                    for resource in resources:
                        results.append(self.store_resource(resource))
        return results
# Made by Ryan Gomez & Co. Inc.
