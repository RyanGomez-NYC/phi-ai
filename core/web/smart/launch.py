# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
SMART on FHIR EHR launch: in-context single sign-on.

WHAT THIS DELIVERS. A clinician viewing a patient in their EMR clicks a
link to the platform and arrives already signed in, already on that
patient's stored record. No second login, no re-searching for the
patient they were already looking at. That is the whole point of an
"in-context" launch, and it is the difference between a records system
people use and one they avoid.

THE SEQUENCE (SMART App Launch Framework):

  1. The EMR opens our /smart/launch with `iss` (its FHIR base URL) and
     `launch` (an opaque context token).
  2. We validate `iss` against an allowlist, then fetch that server's
     /.well-known/smart-configuration to find its authorize and token
     endpoints.
  3. We redirect to the authorization endpoint with the launch token,
     PKCE challenge, state and nonce.
  4. The EMR authenticates the user - usually silently, since they are
     already logged in. THIS is the single sign-on.
  5. It redirects back with a code, which we exchange for an access
     token. The token response carries `patient`: the context.
  6. We establish a session and send the user to that patient's record.

SECURITY DECISIONS THAT ARE NOT OPTIONAL HERE:

  - `iss` IS ALLOW-LISTED. This is the one that matters most. `iss`
    arrives as a query parameter from an untrusted redirect, and it tells
    us which server to trust for authentication. An attacker who can send
    a clinician a crafted launch URL with their own `iss` would otherwise
    have us fetch THEIR discovery document, redirect to THEIR
    authorization server, and accept THEIR token as proof of identity.
    An allowlist is the documented mitigation and there is no safe
    alternative to it.
  - PKCE (S256) on every launch, public or confidential client. Required
    by SMART v2 and harmless where it is not.
  - `state` is bound to a server-side pending record, not just echoed.
  - `nonce` is checked in the id_token.
  - The id_token signature is verified against the issuer's JWKS. An
    unverified id_token is an assertion by whoever sent it.
  - `aud` on the token request is the FHIR server, per spec.

WHAT THIS DOES NOT DO: it does not read clinical data from the EMR. The
only thing it wants is WHICH patient, and this platform supplies the
records. Requesting broad clinical scopes would expand what a compromise
of this application yields, for nothing.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

log = logging.getLogger("phi-ai.web.smart")

# A pending launch is short-lived by design: it exists only between our
# redirect to the authorization server and the user's return. Ten minutes
# is generous for a human completing a login and short enough that stale
# entries are not an accumulating store of launch context.
PENDING_TTL_SECONDS = 600
DISCOVERY_CACHE_TTL_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 15


class SMARTError(RuntimeError):
    pass


class IssuerNotAllowed(SMARTError):
    """The `iss` is not in the configured allowlist.

    Its own class because it is the one failure that is most likely an
    attack rather than a misconfiguration, and it should be logged and
    surfaced differently.
    """


@dataclass(frozen=True)
class RegisteredIssuer:
    """One EMR instance permitted to launch this application.

    Per-instance rather than per-vendor, because every one of these
    vendors is federated: `epic.example-hospital.org` and
    `epic.other-hospital.org` are different trust decisions and issue
    different client ids.
    """

    issuer: str                  # exact FHIR base URL, matched normalised
    vendor_key: str              # into core/web/smart/vendors.VENDORS
    client_id: str
    client_secret: Optional[str] = None
    label: str = ""

    # Roles granted to anyone launching from this issuer. Explicit per
    # issuer, never a global default: "authenticated by an EMR" is not
    # the same claim as "authorised to read this platform's records", and
    # which roles a launching clinician gets is the deploying
    # organisation's decision.
    roles: tuple[str, ...] = ("viewer",)

    # Whether THIS deployment's records came from THIS EMR instance. When
    # false, patient ids from a launch will not resolve here and the user
    # is landed on search with an explanation instead of an empty record.
    #
    # Set from the `record_source` key in the issuer file - the same
    # spelling in the YAML, in core/web/smart/config.py's loader and
    # here. There is no alternative spelling.
    record_source: bool = True

    # Permit this EMR to render this application inside its own iframe.
    #
    # Off by default. Enabling it adds this issuer's origin to
    # Content-Security-Policy frame-ancestors, and - because a session
    # cookie only reaches a cross-site frame with SameSite=None - changes
    # the cookie policy for the WHOLE deployment. That is why CSRF tokens
    # are unconditional; see core/web/security.py.
    embedded: bool = False

    # Deep link back into this EMR's own chart UI, with {patient} and
    # optionally {encounter} placeholders. Site-specific: the URL shape
    # differs per vendor AND per install, so there is nothing to infer
    # from the issuer. Empty = no launch-back link is offered, which is
    # better than a guessed one that opens the wrong thing.
    chart_url: Optional[str] = None
    chart_label: str = ""

    def normalised(self) -> str:
        return normalise_issuer(self.issuer)


def normalise_issuer(issuer: str) -> str:
    """Normalise for comparison: scheme and host lowercased, no trailing
    slash. Deliberately conservative - it does NOT ignore path, port or
    query, since those distinguish genuinely different servers."""
    parsed = urlparse((issuer or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise SMARTError(f"issuer {issuer!r} is not an absolute URL")
    host = parsed.hostname or ""
    is_loopback = host in ("127.0.0.1", "::1", "localhost")

    if parsed.scheme.lower() != "https":
        # An http issuer would let a network attacker rewrite the
        # discovery document, which is the whole trust anchor here.
        #
        # LOOPBACK IS THE ONE EXCEPTION, and it is narrow on purpose:
        # traffic to 127.0.0.1 never crosses a network, so there is no
        # position from which to rewrite it. This is the same carve-out
        # RFC 8252 makes for native apps, and it is what lets the EMR
        # emulators in emulators/ be driven over plain http without
        # weakening the rule for any real issuer. A remote http issuer is
        # still refused outright.
        if not is_loopback:
            raise SMARTError(
                f"issuer {issuer!r} must be https. Only loopback (127.0.0.1, ::1, "
                "localhost) may use http, for local emulators."
            )
        log.warning(
            "accepting http issuer %s because it is loopback - this must never be a "
            "real EMR", issuer,
        )
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


@dataclass
class PendingLaunch:
    state: str
    code_verifier: str
    nonce: str
    issuer: str
    vendor_key: str
    token_endpoint: str
    client_id: str = ""
    created_at: float = field(default_factory=time.time)

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) - self.created_at > PENDING_TTL_SECONDS


@dataclass(frozen=True)
class LaunchContext:
    """What a completed launch establishes."""

    username: str
    roles: frozenset[str]
    issuer: str
    patient_id: Optional[str]
    encounter_id: Optional[str]
    fhir_user: Optional[str]
    record_source: bool

    @property
    def patient_reference(self) -> Optional[str]:
        return f"Patient/{self.patient_id}" if self.patient_id else None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class SMARTLaunchService:
    """
    Drives the launch flow.

    `http_get`/`http_post` are injected so the flow is testable without a
    live EMR - the same reason core/fhir/client.py takes its collaborators
    rather than constructing them.
    """

    def __init__(
        self,
        issuers: list[RegisteredIssuer],
        redirect_uri: str,
        http_get=None,
        http_post=None,
        jwks_client_factory=None,
    ):
        # Keyed by (issuer, client_id) so ONE issuer can carry SEVERAL
        # registrations. An organisation running two apps against the same
        # FHIR server - a pilot alongside production, say - previously had
        # the second silently overwrite the first, and the id_token
        # audience check would then fail against a client_id nobody
        # configured. The launch carries the client_id forward so the
        # right registration is used on the way back.
        self._issuers: dict[str, RegisteredIssuer] = {}
        self._by_issuer_client: dict[tuple[str, str], RegisteredIssuer] = {}
        for issuer in issuers:
            key = issuer.normalised()
            self._by_issuer_client[(key, issuer.client_id)] = issuer
            # First registration wins as the default for a bare launch,
            # which is what an EMR sends when it knows only the issuer.
            self._issuers.setdefault(key, issuer)
        self.redirect_uri = redirect_uri
        self._http_get = http_get or _default_get
        self._http_post = http_post or _default_post
        self._jwks_client_factory = jwks_client_factory or _default_jwks_client
        self._pending: dict[str, PendingLaunch] = {}
        self._discovery: dict[str, tuple[float, dict]] = {}

    # -- issuer allowlist -------------------------------------------

    def resolve_issuer(self, issuer: str) -> RegisteredIssuer:
        try:
            key = normalise_issuer(issuer)
        except SMARTError as exc:
            raise IssuerNotAllowed(str(exc)) from exc

        registered = self._issuers.get(key)
        if registered is None:
            # Logged at warning with the offending value: a launch from an
            # unregistered issuer is either a misconfigured EMR or someone
            # trying to point this application at an authorization server
            # they control.
            log.warning("SMART launch REFUSED for unregistered issuer %r", issuer)
            raise IssuerNotAllowed(
                f"{issuer} is not a registered EMR for this platform. An EMR must be "
                "explicitly registered before it can launch this application - this "
                "prevents a crafted launch link from pointing it at an attacker's "
                "authorization server."
            )
        return registered

    # -- discovery ---------------------------------------------------

    def discover(self, issuer: str) -> dict:
        key = normalise_issuer(issuer)
        cached = self._discovery.get(key)
        if cached and time.time() - cached[0] < DISCOVERY_CACHE_TTL_SECONDS:
            return cached[1]

        url = f"{key}/.well-known/smart-configuration"
        document = self._http_get(url)
        for required in ("authorization_endpoint", "token_endpoint"):
            if not document.get(required):
                raise SMARTError(
                    f"{issuer} returned a SMART configuration with no {required}. "
                    "It may not support SMART App Launch."
                )
        self._discovery[key] = (time.time(), document)
        return document

    # -- step 1: begin -----------------------------------------------

    def begin(self, issuer: str, launch_token: str) -> str:
        """Validate, discover, and return the authorization URL."""
        registered = self.resolve_issuer(issuer)
        if not launch_token:
            raise SMARTError(
                "no launch token. This endpoint handles EHR launch only - it is opened by "
                "the EMR, not visited directly."
            )

        from core.web.smart.vendors import VENDORS, baseline_scopes

        profile = VENDORS.get(registered.vendor_key, VENDORS["generic"])
        document = self.discover(issuer)

        verifier, challenge = make_pkce()
        state = _b64url(secrets.token_bytes(24))
        nonce = _b64url(secrets.token_bytes(16))

        self._prune_pending()
        self._pending[state] = PendingLaunch(
            state=state,
            code_verifier=verifier,
            nonce=nonce,
            issuer=registered.normalised(),
            vendor_key=registered.vendor_key,
            token_endpoint=document["token_endpoint"],
            client_id=registered.client_id,
        )

        params = {
            "response_type": "code",
            "client_id": registered.client_id,
            "redirect_uri": self.redirect_uri,
            "launch": launch_token,
            "scope": baseline_scopes(profile),
            "state": state,
            # aud MUST be the FHIR server, per the SMART spec. Omitting it
            # lets a token issued for one audience be replayed at another.
            "aud": registered.normalised(),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        return f"{document['authorization_endpoint']}?{urlencode(params)}"

    # -- step 2: complete --------------------------------------------

    def complete(self, state: str, code: str) -> LaunchContext:
        """Exchange the code and establish launch context."""
        pending = self._pending.pop(state, None)
        if pending is None:
            raise SMARTError(
                "unknown or already-used launch state. Launches are single-use; start "
                "again from the EMR."
            )
        if pending.expired():
            raise SMARTError("this launch expired before it completed; start again from the EMR")

        # Resolve the exact registration this launch began with, not
        # merely the issuer's default - they differ when one issuer has
        # several client ids.
        registered = self._by_issuer_client.get(
            (pending.issuer, pending.client_id)
        ) or self._issuers[pending.issuer]
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": registered.client_id,
            "code_verifier": pending.code_verifier,
        }
        auth = None
        if registered.client_secret:
            auth = (registered.client_id, registered.client_secret)

        token = self._http_post(pending.token_endpoint, payload, auth)
        if "access_token" not in token:
            raise SMARTError(f"token endpoint returned no access token: {token.get('error')}")

        fhir_user, username = self._identify(token, registered, pending.nonce)

        patient_id = token.get("patient")
        if not patient_id:
            # Not fatal - the user is authenticated and can search - but
            # the whole point was to land on a patient, so say so rather
            # than silently dropping them somewhere generic.
            log.warning(
                "launch from %s returned no patient context; user will land on search",
                registered.normalised(),
            )

        return LaunchContext(
            username=username,
            roles=frozenset(registered.roles),
            issuer=registered.normalised(),
            patient_id=patient_id,
            encounter_id=token.get("encounter"),
            fhir_user=fhir_user,
            record_source=registered.record_source,
        )

    # -- identity ----------------------------------------------------

    def _identify(self, token: dict, registered: RegisteredIssuer, nonce: str):
        """Resolve the clinician from the id_token.

        VERIFIED, not decoded. An id_token read without signature
        verification is an assertion by whoever handed it over, which in a
        flow whose entire purpose is establishing identity is worth
        nothing. Falls back to an explicit anonymous-but-authenticated
        principal only when the server issued no id_token at all, so the
        audit trail never silently attributes a PHI view to a name that
        was not proven.
        """
        raw = token.get("id_token")
        if not raw:
            log.warning("no id_token from %s - user recorded as unnamed", registered.normalised())
            return (None, f"smart:{registered.vendor_key}:unidentified")

        import jwt

        try:
            document = self.discover(registered.issuer)
            jwks_uri = document.get("jwks_uri") or f"{registered.normalised()}/.well-known/jwks.json"
            signing_key = self._jwks_client_factory(jwks_uri).get_signing_key_from_jwt(raw)
            claims = jwt.decode(
                raw,
                signing_key.key,
                algorithms=["RS256", "RS384", "ES384"],
                audience=registered.client_id,
                options={"require": ["exp", "iat"]},
            )
        except Exception as exc:
            raise SMARTError(
                f"could not verify the id_token from {registered.normalised()}: {exc}. "
                "Refusing the launch - an unverified identity assertion is not an identity."
            ) from exc

        # We sent a nonce on the authorization request, so per OIDC Core
        # §3.1.3.7 the id_token MUST carry it back and we MUST verify it.
        # A missing nonce is a verification FAILURE, not something to skip:
        # the previous `if claims.get("nonce")` guard passed any id_token
        # that simply omitted the claim, which is exactly what an id_token
        # replayed from a different launch would do. Compared in constant
        # time - a nonce check that leaks position through timing is one an
        # attacker can grind down.
        import hmac

        if not hmac.compare_digest(str(claims.get("nonce") or ""), str(nonce)):
            raise SMARTError(
                "id_token nonce is missing or does not match this launch. A launch is "
                "bound to the nonce sent on its authorization request; an id_token that "
                "does not carry that exact nonce back cannot be accepted as proof of "
                "this launch."
            )

        fhir_user = claims.get("fhirUser")
        username = (
            claims.get("preferred_username")
            or claims.get("email")
            or fhir_user
            or claims.get("sub")
            or f"smart:{registered.vendor_key}:unidentified"
        )
        return (fhir_user, str(username))

    # -- housekeeping -------------------------------------------------

    def _prune_pending(self) -> None:
        now = time.time()
        for state in [s for s, p in self._pending.items() if p.expired(now)]:
            self._pending.pop(state, None)


def _default_get(url: str) -> dict:
    import requests

    response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS,
                            headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


def _default_post(url: str, data: dict, auth) -> dict:
    import requests

    response = requests.post(url, data=data, auth=auth, timeout=HTTP_TIMEOUT_SECONDS,
                             headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


def _default_jwks_client(jwks_uri: str):
    from jwt import PyJWKClient

    return PyJWKClient(jwks_uri)
# Made by Ryan Gomez & Co. Inc.
