# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
CSRF protection and frame-embedding policy.

THESE TWO ARE ONE SUBJECT, which is why they share a module.

Rendering inside an EHR's iframe is a cross-site context. The session
cookie only reaches a cross-site frame with `SameSite=None`, and
`SameSite=Lax` - which this application relied on until embedding was
added - was doing real CSRF work: it is what stopped another site
POSTing to /roi or /documents using a logged-in clinician's cookie.
Turning it off without replacing it would have traded a clickjacking
mitigation for a CSRF hole.

So embedding requires CSRF tokens, and CSRF tokens are enabled
unconditionally rather than only in embedded deployments - a protection
that exists in one configuration and not another is one nobody can reason
about.

PROXY-AUTHENTICATED DEPLOYMENTS NEED THIS TOO, and arguably needed it
before. When identity comes from a header an authenticating proxy adds to
every request from that browser, a cross-site form POST carries the
header just as a legitimate request does. Header authentication is not
CSRF-resistant; only the SameSite cookie was, and only for session users.

FRAME-ANCESTORS IS AN ALLOWLIST, never `*`. It is derived from the
registered SMART issuers that opted into embedding, so the set of origins
permitted to frame this application is exactly the set of EMRs a
deployment already decided to trust for authentication. An open frame
policy on a PHI application invites clickjacking: an attacker overlays
their own UI on a real, authenticated session.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlparse

log = logging.getLogger("phi-ai.web.security")

CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"

# Methods that change state. GET/HEAD/OPTIONS are exempt because they are
# required to be safe; a GET that changes state is a bug this check would
# only paper over.
PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths exempt from CSRF, deliberately enumerated rather than pattern
# matched. The SMART callback is a GET, so nothing here today - the list
# exists so a future exemption has to be written down and justified.
CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()


class CSRFError(Exception):
    pass


def issue_csrf_token(session) -> str:
    """Return this session's CSRF token, minting one on first use."""
    token = (session or {}).get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def verify_csrf_token(session, submitted: str | None) -> None:
    expected = (session or {}).get(CSRF_SESSION_KEY)
    if not expected:
        raise CSRFError(
            "no CSRF token in this session. The session may have expired - reload the page "
            "and try again."
        )
    # Constant-time: a token check that leaks position through timing is a
    # token check an attacker can grind down.
    if not submitted or not hmac.compare_digest(str(submitted), str(expected)):
        raise CSRFError(
            "CSRF token missing or invalid. This request did not originate from a page "
            "this application served."
        )


def frame_ancestors(issuers) -> list[str]:
    """Origins permitted to frame this application.

    Derived from registered issuers with `embedded: true`, reduced to
    scheme://host[:port] - a frame-ancestors source is an origin, and
    including the FHIR base path would silently match nothing.
    """
    origins: list[str] = []
    for issuer in issuers or []:
        if not getattr(issuer, "embedded", False):
            continue
        parsed = urlparse(issuer.normalised())
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
    return origins


def build_csp(origins: list[str]) -> str:
    """Content-Security-Policy for every response.

    frame-ancestors 'none' when nothing opted into embedding, which is
    both the default and the safe answer.

    The rest is tight because it can be: this interface serves its own
    CSS and no JavaScript at all, so it has no reason to permit inline
    script, remote script, or any third-party origin. A PHI interface
    that does not need those should not allow them.
    """
    ancestors = " ".join(origins) if origins else "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # small inline styles in templates
            "script-src 'none'",
            "img-src 'self' data:",
            "form-action 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            f"frame-ancestors {ancestors}",
        ]
    )


def security_headers(csp: str, embedded_enabled: bool) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if not embedded_enabled:
        # X-Frame-Options is obsolete where CSP frame-ancestors is
        # supported, but it is still honoured by older browsers and costs
        # nothing. Omitted when embedding IS enabled, because DENY there
        # would override frame-ancestors in exactly the browsers that
        # still read it.
        headers["X-Frame-Options"] = "DENY"
    return headers
# Made by Ryan Gomez & Co. Inc.
