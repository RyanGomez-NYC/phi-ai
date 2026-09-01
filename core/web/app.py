# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Web interface for the PHI AI Platform.

Serves both an HTML UI and a JSON API from one implementation, so the
two cannot drift - products in this category offer programmatic access
alongside the UI, and maintaining a second code path for it is how the
two come to disagree.

THREE RULES ENFORCED HERE, each mirroring a decision already made
elsewhere in this codebase rather than inventing a new one:

1. Every read of clinical content is audit-logged, with the
   authenticated username as actor and a stated purpose of use. This is
   the application-layer twin of the DenyReadWithoutPurposeOfUse IAM
   condition on the restore role.

2. No PHI in URLs. Search terms are POSTed, never sent as query strings,
   because query strings land in proxy logs, browser history and
   referrer headers. Patient references DO appear in paths - they are the
   EMR's own opaque server-assigned ids, already present in every S3 key
   (see core/db/index.py), and are not real-world identifiers.

3. Authorization is checked per route, and the auditor role is NOT a
   viewer. See core/web/auth.py.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import __version__
from core.assistant import telemetry as assistant_telemetry
from core.assistant.conversations import Turn
from core.assistant.tools import (
    AnalyticsAccess as AssistantAnalyticsAccess,
    ClinicalAccess as AssistantClinicalAccess,
    ResearchAccess as AssistantResearchAccess,
)
from core.config.settings import env_var
from core.fhir.roi import REQUESTER_TYPES
from core.web.assistant_pages import (
    back_label,
    describe as describe_page,
    safe_return_path,
)
from core.web.data import utcnow
from core.web.security import (
    CSRF_EXEMPT_PATHS,
    CSRF_FORM_FIELD,
    PROTECTED_METHODS,
    CSRFError,
    build_csp,
    frame_ancestors,
    issue_csrf_token,
    security_headers,
    verify_csrf_token,
)
from core.web.auth import (
    PURPOSES_OF_USE,
    AuthSettings,
    Identity,
    NotAuthenticated,
    NotAuthorized,
    identity_from_headers,
    purpose_allowed,
    role_allowed_purposes,
    role_default_purpose,
    validate_purpose,
)

log = logging.getLogger("phi-ai.web")

# Resolved from THIS FILE, not the working directory. Relative paths
# worked only when the process happened to start in the repo root, which
# a systemd unit, a container with a different WORKDIR, or a test run
# from tests/ all break - and the failure is at import time with a
# message about a missing directory rather than anything pointing here.
_HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

# The exact shape a source-document key can take, matching what
# core/fhir/documents.py writes: documents/source/doc-<32 hex>.<ext>.
# The /document/source route decrypts and returns raw bytes and echoes
# the key's final segment into a Content-Disposition filename, so it
# validates the WHOLE shape rather than merely the prefix - a prefix check
# still let the rest of the key be anything, including characters that do
# not belong in a header. Anything not matching is refused before the
# object is fetched.
_SOURCE_DOCUMENT_KEY = re.compile(
    r"^documents/source/doc-[0-9a-f]{32}\.(?:pdf|png|jpg|jpeg|tif|tiff|bmp|gif|webp)$"
)

# Session key holding the id of this user's live assistant conversation.
# The conversation itself lives in worker memory, never in the cookie -
# a transcript would not fit in one, and a signed cookie is not where
# text a user typed belongs.
_ASSISTANT_KEY = "assistant_conversation"

# Session keys for the development persona switcher and the ambient
# purpose-of-use shown in the top bar. Both live in the signed session
# cookie; neither ever carries clinical content.
_PERSONA_KEY = "dev_persona"


def _parse_dev_personas() -> list[dict]:
    """Development personas, from PHI_AI_WEB_DEV_PERSONAS.

    `user:role[,role]:label|user:role[,role]:label`. ONLY read in
    dev-identity mode (core/web/auth.py's from_env() refuses to let that
    mode coexist with proxy trust or local accounts), so this can never
    widen a real deployment: it is the same fabricated-identity path
    PHI_AI_WEB_DEV_IDENTITY already is, extended to let an evaluator walk
    the product as each role without running three browsers. Every
    request served under a persona logs the same loud warning the dev
    identity does, and every action is audited against the persona's own
    username.
    """
    raw = (env_var("WEB_DEV_PERSONAS", "") or "").strip()
    if not raw:
        return []
    personas = []
    for entry in raw.split("|"):
        entry = entry.strip()
        if not entry:
            continue
        # user:roles[:display name[:label]] - the display name is the
        # proper-English identity the interface shows; the username stays
        # the audit actor.
        parts = entry.split(":", 3)
        if len(parts) < 2 or not parts[0].strip():
            log.warning("ignoring malformed dev persona entry %r", entry)
            continue
        name = parts[2].strip() if len(parts) > 2 and parts[2].strip() else parts[0].strip()
        personas.append({
            "username": parts[0].strip(),
            "roles": parts[1].strip(),
            "name": name,
            "label": (parts[3].strip() if len(parts) > 3 else parts[1].strip()),
        })
    return personas


def create_app(
    reader,
    auth_settings: Optional[AuthSettings] = None,
    audit=None,
    session_secret_key: Optional[str] = None,
    embedded_issuers=None,
    secure_cookies: bool = True,
    imaging_connection_factory=None,
    local_accounts=None,
    prompt_store=None,
    platform_state=None,
) -> FastAPI:
    """Build the application.

    `reader` and `audit` are injected rather than constructed here so the
    routes can be tested without Postgres, S3 or a KMS - the same reason
    core/fhir/client.py takes its storage and encryptor as arguments.
    """
    settings = auth_settings or AuthSettings.from_env()
    # MUST stay below the session cookie's max-age (PHI_AI_WEB_SESSION_MINUTES,
    # default 30). Past that the cookie's own signature is rejected as
    # stale, the app receives an empty session, and this check never sees
    # a last_seen to compare - the cookie expires instead, which is a
    # blunter outcome than an explicit idle message.
    # env_var(), never os.environ.get(): an installer-produced .env sets
    # PHI_AI_*, and both of these reads are DEFAULTED - a miss does not
    # error, it silently substitutes a timeout the operator did not choose.
    idle_timeout = int(env_var("WEB_IDLE_MINUTES", "15") or "15") * 60
    session_minutes = int(env_var("WEB_SESSION_MINUTES", "30") or "30")
    if idle_timeout >= session_minutes * 60:
        log.warning(
            "PHI_AI_WEB_IDLE_MINUTES (%d) is not below "
            "PHI_AI_WEB_SESSION_MINUTES (%d), so the cookie will expire before the "
            "idle check can fire and users will see a generic re-login rather than an "
            "idle-timeout message.",
            idle_timeout // 60, session_minutes,
        )

    async def session_timeout(request: Request) -> None:
        """Expire an idle session independently of the cookie's own max-age.

        The proxy owns overall session lifetime, but it cannot see idle
        time inside this application - and a clinical workstation left
        unattended with a chart open is the realistic exposure, not a
        session that ran long while someone was actively working. Cookie
        max-age cannot express "idle", only "old", so this is tracked
        here.
        """
        if "session" not in request.scope:
            return
        if request.url.path.startswith("/login"):
            # The sign-in pages are exempt, and have to be. Loading
            # /login stamps last_seen; somebody who then goes to find
            # their authenticator app and comes back sixteen minutes
            # later would otherwise have their POST rejected with "your
            # session expired" before it ever reached the login handler -
            # an idle timeout firing on a session that does not exist
            # yet. There is nothing to protect here: no identity has been
            # established. See core/web/login_routes.py, which enforces
            # its own, much shorter, expiry on the half-completed sign-in
            # between the password step and the second factor.
            return
        session = request.session
        now = int(time.time())
        last_seen = session.get("last_seen")

        if last_seen and now - int(last_seen) > idle_timeout:
            session.clear()
            raise HTTPException(
                status_code=440,
                detail=f"Session expired after {idle_timeout // 60} minutes of "
                       "inactivity. Reload to sign in again.",
            )
        session["last_seen"] = now

    async def csrf_guard(request: Request) -> None:
        """Reject state-changing requests without a valid CSRF token.

        Applied to EVERY route rather than decorated onto each POST: a
        protection you have to remember to add is one that eventually gets
        forgotten on the route that needed it most.
        """
        if request.method not in PROTECTED_METHODS:
            return
        if request.url.path in CSRF_EXEMPT_PATHS:
            return
        try:
            form = await request.form()
            verify_csrf_token(request.session, form.get(CSRF_FORM_FIELD))
        except CSRFError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    app = FastAPI(
        title="PHI AI Platform",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        dependencies=[Depends(session_timeout), Depends(csrf_guard)],
    )
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    # Session cookie, used ONLY to carry an identity a SMART launch
    # already established elsewhere - never to store credentials. Strict
    # settings: https_only so it cannot leak over plain HTTP, and a short
    # max_age so an abandoned browser session on a shared clinical
    # workstation stops being useful quickly. samesite=lax rather than
    # strict because the OAuth callback is a cross-site top-level
    # redirect and strict would drop the cookie on arrival.
    session_secret = session_secret_key or env_var("WEB_SESSION_SECRET")
    if not session_secret:
        # Ephemeral rather than absent: CSRF tokens live in the session,
        # and a protection that exists in one configuration and not
        # another is one nobody can reason about. The cost of generating
        # one here is real and stated loudly - sessions do not survive a
        # restart and do not work across replicas.
        session_secret = secrets.token_urlsafe(48)
        log.warning(
            "PHI_AI_WEB_SESSION_SECRET is not set - generated an ephemeral one. "
            "Sessions will not survive a restart and will not work behind a load balancer "
            "with more than one instance. Set it for any real deployment."
        )

    # ORDER MATTERS. Starlette applies the last-added middleware
    # OUTERMOST, so the security middleware is registered first and
    # SessionMiddleware second - otherwise the CSRF check runs before
    # the session exists and every POST fails with an assertion about
    # missing middleware rather than anything useful.
    # The imaging viewer runs on its own origin (see
    # runbooks/RUNBOOK_DICOM_IMAGING.md), so its requests to /dicomweb are
    # cross-origin and credentialed. CORS is therefore required - and is
    # scoped to /dicomweb by hand rather than by mounting Starlette's
    # CORSMiddleware, which has no path filter and would have granted the
    # viewer origin cross-origin read access to every page in this
    # application, including the patient record. An exact origin, never
    # `*`: the browser refuses `*` alongside credentials anyway, so a
    # wildcard here would not be lax, it would simply not work.
    from core.web.dicomweb_routes import viewer_origin

    _viewer_origin = viewer_origin()

    @app.middleware("http")
    async def _imaging_cors(request: Request, call_next):
        path = request.url.path
        origin = request.headers.get("origin")
        allowed = bool(_viewer_origin and origin == _viewer_origin and path.startswith("/dicomweb"))

        if allowed and request.method == "OPTIONS":
            # Preflight is answered here, before authentication. A browser
            # sends it without credentials, so requiring an identity would
            # fail every cross-origin imaging request with a 401 the
            # viewer cannot explain.
            response = PlainTextResponse("", status_code=204)
        else:
            response = await call_next(request)

        if allowed:
            response.headers["Access-Control-Allow-Origin"] = _viewer_origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type"
            response.headers["Vary"] = "Origin"
        return response

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Security headers only. The CSRF check is a DEPENDENCY, not part
        of this middleware, because reading the form here would consume
        the request body stream and the route handler would then receive
        an empty form. FastAPI caches the parsed form across dependencies
        within one request, so the guard can read it safely there."""
        response = await call_next(request)
        for header, value in security_headers(csp, app.state.embedded_enabled).items():
            response.headers.setdefault(header, value)
        return response

    from starlette.middleware.sessions import SessionMiddleware

    # SameSite=None is required for the cookie to reach a cross-site EHR
    # iframe. It is only used when an issuer opted into embedding,
    # because it materially weakens CSRF defence - which is why CSRF
    # tokens are enforced regardless. See core/web/security.py.
    if not secure_cookies:
        log.warning(
            "session cookies are being issued WITHOUT the Secure flag. This is for local "
            "development over http only - a cookie sent over plain http can be read in "
            "transit. Never run a real deployment this way."
        )

    embed_origins = frame_ancestors(embedded_issuers or [])
    csp = build_csp(embed_origins)
    app.state.embedded_enabled = bool(embed_origins)

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="phi_ai_session",
        max_age=int(env_var("WEB_SESSION_MINUTES", "30") or "30") * 60,
        same_site="none" if embed_origins else "lax",
        # Secure by default. The escape hatch exists because local
        # development over http://127.0.0.1 otherwise cannot hold a
        # session at all - the cookie is never stored, so the CSRF token
        # never round-trips and every form fails - and a developer who
        # cannot run the thing locally will find a worse way around it.
        # SameSite=None REQUIRES Secure, so embedding ignores the flag.
        https_only=secure_cookies or bool(embed_origins),
    )


    app.state.reader = reader
    app.state.auth_settings = settings
    app.state.audit = audit
    # Prompt history / saved prompts (core/web/prompt_store.py). None
    # when unconfigured; the assistant page hides the feature then.
    app.state.prompts = prompt_store
    # Development personas: non-empty ONLY in dev-identity mode, where
    # authentication is already, loudly, fabricated. See
    # _parse_dev_personas().
    app.state.dev_personas = (
        _parse_dev_personas()
        if (settings.dev_identity and not settings.trust_proxy_headers
            and not settings.local_accounts)
        else []
    )
    # None when this deployment stores no DICOM, which is the default.
    # Read below to decide whether the /dicomweb routes exist at all.
    app.state.imaging_connection_factory = imaging_connection_factory
    # None unless this deployment has no identity provider and enabled
    # local accounts (core/web/local_auth.py). Set here rather than
    # after construction so the sign-in routes and the identity
    # resolution above are decided at build time, the same reason the
    # SMART issuers and the imaging factory are.
    app.state.local_accounts = local_accounts
    if settings.local_accounts and local_accounts is None:
        raise RuntimeError(
            "PHI_AI_WEB_LOCAL_ACCOUNTS is set but no account store was passed to "
            "create_app(). Without it there is no way to verify a password, so every "
            "sign-in would fail with a 503 - failing here instead, where the cause is "
            "visible. core/web/__main__.py builds it from the database settings."
        )

    # ---- identity & audit helpers ---------------------------------

    def current_identity(request: Request) -> Identity:
        """Resolve the caller from whichever authentication path applies.

        THREE, in this order:

        1. A completed SMART launch, carried in the session. It wins when
           present: the clinician explicitly launched in a patient's
           context, and that is the more specific statement of who is
           asking.
        2. A local account session, where this deployment has no identity
           provider and enabled local accounts. Resolved against the
           database on every request - see core/web/login_routes.py's
           resolve_local_session for why the roles are not read from the
           cookie.
        3. The proxy headers, which is the recommended deployment.

        None of the three fabricates an identity. The development
        identity, which does, lives inside path 3 and refuses to coexist
        with either of the other two - see core/web/auth.py's from_env().
        """
        # `request.session` ASSERTS when SessionMiddleware is absent
        # rather than returning None, so the guard is on the scope. A
        # deployment with no session secret runs proxy-auth only, and
        # must not fail every request because of it.
        if "session" in request.scope:
            from core.web.auth import identity_from_session

            established = identity_from_session(request.session)
            if established is not None:
                return established

        if settings.local_accounts:
            from core.web.login_routes import resolve_local_session

            # Raises NeedsLogin / NeedsPasswordChange / NeedsMFAEnrolment,
            # each handled by a handler login_routes registers on this
            # app. Deliberately NOT caught here: turning them into a 401
            # would lose the redirect that makes the sign-in flow work,
            # and every one of them means "this browser is not finished
            # authenticating", not "this request is unauthorised".
            return resolve_local_session(request, app.state.local_accounts)

        # Development persona, selected via POST /persona. Same fabricated-
        # identity path as PHI_AI_WEB_DEV_IDENTITY (dev_personas is empty in
        # every other mode), same loud warning per request.
        if app.state.dev_personas and "session" in request.scope:
            selected = request.session.get(_PERSONA_KEY)
            if selected:
                for p in app.state.dev_personas:
                    if p["username"] == selected:
                        from core.web.auth import _parse_roles

                        log.warning(
                            "SERVING REQUEST WITH A FABRICATED DEVELOPMENT "
                            "IDENTITY (%s, persona) - no authentication occurred.",
                            p["username"],
                        )
                        return Identity(
                            username=p["username"], email=None,
                            roles=_parse_roles(p["roles"]),
                        )

        try:
            return identity_from_headers(request.headers, settings)
        except NotAuthenticated as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def record(identity: Identity, action: str, resource_key: str, purpose: Optional[str]) -> None:
        """Write one audit entry. Failure to audit FAILS THE REQUEST.

        Deliberately unlike the index write in core/fhir/client.py, which
        is best-effort because the resource is already safely stored by
        then. Here the audit entry IS the record that a human viewed PHI;
        serving the content without it would produce exactly the
        undetectable access the audit trail exists to prevent.
        """
        record_actor(identity.username, action, resource_key, purpose)

    def record_actor(actor: str, action: str, resource_key: str,
                     purpose: Optional[str] = None) -> None:
        """record() by username rather than by Identity.

        Exists because the sign-in path has events to record BEFORE
        there is an identity to record them against - a failed password,
        a lockout - and those are exactly the events an access-management
        review looks for. Same sink, same fail-the-request rule: an
        authentication system whose evidence is optional is not evidence.
        See core/web/login_routes.py.
        """
        if app.state.audit is None:
            log.error("no audit sink configured - refusing to serve clinical content")
            raise HTTPException(
                status_code=503,
                detail="Audit logging is not configured. Clinical content is not served "
                "without an audit trail.",
            )
        app.state.audit.record(
            actor=actor,
            action=action,
            resource_key=resource_key,
            purpose_of_use=purpose,
        )

    def require(identity: Identity, permission: str) -> None:
        try:
            identity.require(permission)
        except NotAuthorized as exc:
            # Denials are audited too. A pattern of refusals is itself a
            # security signal, and an audit trail that only records
            # successes cannot show one.
            if app.state.audit is not None:
                app.state.audit.record(
                    actor=identity.username,
                    action="access.denied",
                    resource_key=permission,
                    purpose_of_use=None,
                )
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _imaging_studies(identity: Identity, patient_reference: str) -> list:
        """This patient's stored imaging studies, or an empty list.

        Empty rather than an error when imaging is not configured or the
        user's role does not include it - the record page is not the place
        to explain an optional feature's absence, and a viewer with no
        studies is indistinguishable from a patient with no imaging.
        """
        factory = getattr(app.state, "imaging_connection_factory", None)
        if factory is None or not identity.can("imaging:read"):
            return []
        from core.dicom import index as imaging_index

        try:
            conn = factory()
        except Exception as exc:
            log.error("imaging index unavailable: %s", exc)
            return []
        try:
            return imaging_index.search_studies(
                conn, {}, patient_reference=patient_reference, limit=50
            )
        except Exception as exc:
            log.error("imaging lookup failed for %s: %s", patient_reference, exc)
            return []
        finally:
            conn.close()

    def _launch_back(request: Request) -> dict:
        """Deep link back to the EMR chart this session launched from.

        Absent when the session did not originate from a SMART launch, or
        when the issuer configured no chart_url - a launch-back link that
        opens the wrong thing is worse than none.
        """
        session = request.session
        template = session.get("chart_url_template")
        if not template:
            return {"chart_url": None, "chart_label": None}

        from core.web.smart.launch_back import build_chart_url

        return {
            "chart_url": build_chart_url(
                template,
                session.get("launch_patient"),
                session.get("launch_encounter"),
            ),
            "chart_label": session.get("chart_label") or "the EMR",
        }

    def page(request: Request, template: str, identity: Identity,
             status_code: int = 200, **context):
        """Render one page.

        `status_code` is a real parameter rather than another context
        key because the sign-in pages need it: a rejected password must
        answer 401 while still rendering the form, and a page that
        renders an error inside a 200 is one that automated log review
        and any monitoring in front of this will read as a success.
        """
        from core.web import nav as product_nav

        flags = {
            "assistant_enabled": getattr(app.state, "assistant", None) is not None,
            "local_accounts": getattr(app.state, "local_accounts", None) is not None,
            "imaging_enabled": getattr(
                app.state, "imaging_connection_factory", None
            ) is not None,
        }
        meta = product_nav.screen_meta(context.get("active"))
        session = request.session if "session" in request.scope else {}

        # The persona rail: the current identity first, then the other
        # configured development personas as one-click switches. Empty
        # outside dev-persona mode, and the template falls back to the
        # plain signed-in card. `identity` is None on the sign-in pages -
        # no one is anyone yet - so everything identity-derived guards on
        # it rather than assuming a caller.
        personas = []
        if identity is not None and app.state.dev_personas:
            seen_current = False
            for p in app.state.dev_personas:
                current = p["username"] == identity.username
                seen_current = seen_current or current
                personas.append({
                    "username": p["username"], "label": p["label"],
                    "name": p.get("name", p["username"]), "current": current,
                })
            if not seen_current:
                personas.insert(0, {
                    "username": identity.username,
                    "name": identity.username,
                    "label": ", ".join(sorted(r.value for r in identity.roles)),
                    "current": True,
                })

        # Context chip: the launch context when a SMART launch established
        # one, the de-identified plane for population-only roles, and an
        # honest "no launch context" otherwise.
        launch_patient = session.get("launch_patient")
        if launch_patient:
            chip = f"pt {launch_patient}"
            if session.get("launch_encounter"):
                chip += f" · enc {session['launch_encounter']}"
        elif identity is not None and identity.roles and not (
            identity.can("patient:read") or identity.can("patient:search")
        ):
            chip = "de-identified plane"
        else:
            chip = "no launch context"

        # The purpose a record-reading form preselects. Derived from the
        # ROLE, not from ambient session state: a clinician's default is
        # treatment, a records/operations role's is operations, a
        # researcher's is research - and the person can change it on the
        # specific action, where the assertion actually attaches.
        purpose_current = context.get("purpose") or (
            role_default_purpose(identity) if identity is not None else "treatment"
        )

        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            status_code=status_code,
            context={
                "identity": identity,
                # The role dictates what the user can see: purpose
                # selects offer only the purposes this identity may
                # assert.
                "purposes": (role_allowed_purposes(identity)
                             if identity is not None else PURPOSES_OF_USE),
                "nav_groups": (
                    product_nav.nav_for(identity, flags) if identity is not None else []
                ),
                "screen_ref": meta["ref"],
                "screen_title": meta["title"],
                "personas": personas,
                "context_chip": chip,
                "purpose_current": purpose_current,
                "csrf_token": issue_csrf_token(request.session),
                # Shown in the colophon at the foot of every page, so a
                # screenshot in an incident report says which build it
                # came from.
                "release": __version__,
                "embedded": request.query_params.get("embedded") == "1"
                or request.session.get("embedded", False),
                # Drives the ask-the-assistant drawer in base.html. False
                # when the optional assistant is not enabled, which is
                # the default, so the drawer does not exist rather than
                # appearing and failing.
                "assistant_enabled": getattr(app.state, "assistant", None) is not None,
                "imaging_enabled": getattr(
                    app.state, "imaging_connection_factory", None
                ) is not None,
                # Drives the "Your account" link in base.html. False
                # behind an identity provider, where there is no local
                # account for this application to show anybody - their
                # password and their second factor belong to the IdP.
                "local_accounts": getattr(app.state, "local_accounts", None) is not None,
                "assistant_reads_phi": bool(
                    getattr(app.state, "assistant", None)
                    and app.state.assistant.settings.reads_clinical_content
                ),
                **_launch_back(request),
                **context,
            },
        )

    # ---- dashboard -------------------------------------------------

    def _overview(request: Request, identity: Identity):
        stats = reader.stats() if identity.can("patient:search") or identity.can("report:read") else None
        chain = reader.verify_audit_chain() if identity.can("audit:verify") else None
        return page(request, "dashboard.html", identity, stats=stats, chain=chain,
                    active="overview")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, identity: Identity = Depends(current_identity)):
        """The front door.

        The assistant, where a deployment has enabled it and the user's
        role includes it - asking a question in plain language is how
        most people arrive at a clinical record platform, and it is the one page
        that can route someone to the rest. Where it is absent (the
        default) this is the platform overview, exactly as before, so a
        deployment that never turns the assistant on sees no change.
        """
        if getattr(app.state, "assistant", None) is not None and identity.can("assistant:use"):
            return RedirectResponse("/assistant", status_code=303)
        return _overview(request, identity)

    @app.get("/overview", response_class=HTMLResponse)
    def overview(request: Request, identity: Identity = Depends(current_identity)):
        """The platform overview, always reachable by its own address.

        Separate from `/` so that making the assistant the landing page
        does not make this page unreachable - it is linked from the
        navigation on every page.
        """
        return _overview(request, identity)

    # ---- patient search & record view ------------------------------

    @app.get("/patients", response_class=HTMLResponse)
    def patient_search_form(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "patient:search")
        return page(request, "patients.html", identity, results=None, term="")

    @app.post("/patients", response_class=HTMLResponse)
    def patient_search(
        request: Request,
        term: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        # POST, not GET: a search term must not reach a proxy access log
        # or the browser's history.
        require(identity, "patient:search")
        results = reader.search_patients(term)
        record(identity, "record.search", f"patient_reference~{term}", None)
        return page(request, "patients.html", identity, results=results, term=term)

    @app.post("/patients/{patient_id}/open", response_class=HTMLResponse)
    def patient_record(
        request: Request,
        patient_id: str,
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "patient:read")
        try:
            purpose = validate_purpose(purpose_of_use)
            if not purpose_allowed(identity, purpose):
                # The role dictates the purposes it may
                # assert; refusal is audited like any
                # other denial.
                require(identity, f"purpose:{purpose}")
        except NotAuthorized as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        reference = f"Patient/{patient_id}"
        # Audit BEFORE reading - see resource_detail for the reasoning.
        record(identity, "record.read.patient", reference, purpose)
        resources = reader.resources_for_patient(reference)
        return page(
            request,
            "patient.html",
            identity,
            patient_reference=reference,
            resources=resources,
            purpose=purpose,
            imaging_studies=_imaging_studies(identity, reference),
        )

    @app.post("/resource", response_class=HTMLResponse)
    def resource_detail(
        request: Request,
        storage_key: str = Form(...),
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "patient:read")
        purpose = validate_purpose(purpose_of_use)
        if not purpose_allowed(identity, purpose):
            # The role dictates the purposes it may
            # assert; refusal is audited like any
            # other denial.
            require(identity, f"purpose:{purpose}")

        row = reader.resource_index_row(storage_key)
        if row is None:
            raise HTTPException(status_code=404, detail="not in the index")

        # AUDIT BEFORE DECRYPTING, not after. If the audit write fails,
        # the request must end having never decrypted the content - the
        # same ordering core/fhir/purge.py uses, where the disposal entry
        # is written before the delete. Recording afterwards means a
        # failed audit still leaves PHI decrypted in this process, which
        # is precisely the unlogged access the trail exists to prevent.
        record(identity, "record.read", storage_key, purpose)
        resource = reader.read_resource(storage_key)

        ocr_text, source_key = None, None
        if resource.get("resourceType") == "DocumentReference":
            from core.fhir.documents import decode_ocr_text

            ocr_text = decode_ocr_text(resource)
            for entry in resource.get("content", []):
                url = (entry.get("attachment") or {}).get("url", "")
                if url.startswith("documents/source/"):
                    source_key = url
                    break

        return page(
            request,
            "resource.html",
            identity,
            row=row,
            resource=resource,
            ocr_text=ocr_text,
            source_key=source_key,
            purpose=purpose,
        )

    @app.post("/document/source")
    def document_source(
        request: Request,
        storage_key: str = Form(...),
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        """Serve the original scan behind an OCR'd DocumentReference.

        The scan is the record of truth - the OCR text is derived - so a
        clinician checking anything consequential needs the original, not
        a transcription that misreads characters. Audited before the
        object is decrypted, exactly like every other clinical read.
        """
        require(identity, "document:read")
        try:
            purpose = validate_purpose(purpose_of_use)
            if not purpose_allowed(identity, purpose):
                # The role dictates the purposes it may
                # assert; refusal is audited like any
                # other denial.
                require(identity, f"purpose:{purpose}")
        except NotAuthorized as exc:
            # 400, not 500: a bad purpose is a malformed request, and
            # letting NotAuthorized escape produced an opaque server error.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not _SOURCE_DOCUMENT_KEY.match(storage_key):
            # This route decrypts and returns raw bytes AND echoes the
            # key's final segment into a Content-Disposition filename, so
            # it must not become a general object-fetch endpoint and the
            # key must not carry characters that do not belong in a header.
            # The full shape is validated, not merely the prefix.
            raise HTTPException(status_code=400, detail="not a source document key")

        record(identity, "record.read.document.source", storage_key, purpose)

        try:
            payload = reader.read_object_bytes(storage_key)
        except Exception as exc:
            log.error("could not read source document %s: %s", storage_key, exc)
            raise HTTPException(status_code=404, detail="source document unavailable") from exc

        from fastapi.responses import Response

        suffix = storage_key.rsplit(".", 1)[-1].lower()
        media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "tif": "image/tiff", "tiff": "image/tiff",
                 "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp"}.get(
                     suffix, "application/octet-stream")
        return Response(
            content=payload,
            media_type=media,
            headers={
                # inline, unlike the ROI production: this is meant to be
                # looked at next to the extracted text, not filed away.
                "Content-Disposition": f'inline; filename="{storage_key.rsplit("/", 1)[-1]}"',
                "Cache-Control": "no-store",
            },
        )

    # ---- audit -----------------------------------------------------

    @app.get("/audit", response_class=HTMLResponse)
    def audit_browser(
        request: Request,
        actor: Optional[str] = None,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "audit:read")
        events = reader.read_audit_events(actor=actor)
        chain = reader.verify_audit_chain() if identity.can("audit:verify") else None
        return page(request, "audit.html", identity, events=events, chain=chain, actor=actor)

    # ---- retention & disposition -----------------------------------

    @app.get("/retention", response_class=HTMLResponse)
    def retention(
        request: Request,
        within_days: int = 90,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "retention:read")
        rows = reader.expiring_resources(within_days=within_days)

        # Decide elapsed-vs-upcoming HERE, not in the template. The
        # template previously compared a timestamp against an undefined
        # `now`, which raised on every non-empty result - invisible in
        # tests because the fake returned an empty list, so the loop body
        # never ran. Date logic in a template is hard to test for exactly
        # this reason.
        now = utcnow()
        expiring = []
        for row in rows:
            annotated = dict(row)
            due = row.get("retention_until")
            annotated["elapsed"] = bool(due and due <= now)
            expiring.append(annotated)

        return page(request, "retention.html", identity, expiring=expiring,
                    within_days=within_days)

    # ---- document ingestion ----------------------------------------

    @app.get("/documents", response_class=HTMLResponse)
    def document_form(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "document:ingest")
        return page(request, "documents.html", identity, result=None, error=None)

    @app.post("/documents", response_class=HTMLResponse)
    async def document_upload(
        request: Request,
        patient_reference: str = Form(...),
        title: Optional[str] = Form(None),
        upload: UploadFile = None,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "document:ingest")
        ingestor = getattr(app.state, "ingestor", None)
        if ingestor is None:
            raise HTTPException(status_code=503, detail="document ingestion is not configured")

        from core.fhir.documents import DocumentIngestionError

        try:
            payload = await upload.read()
            result = ingestor.ingest(
                source_bytes=payload,
                content_type=upload.content_type or "",
                patient_reference=patient_reference,
                title=title,
            )
        except (DocumentIngestionError, Exception) as exc:  # surfaced, not swallowed
            return page(request, "documents.html", identity, result=None, error=str(exc))

        record(identity, "record.document.ingest", result.source_storage_key, "operations")
        return page(request, "documents.html", identity, result=result, error=None)

    # ---- release of information ------------------------------------

    def roi_service():
        service = getattr(app.state, "roi", None)
        if service is None:
            raise HTTPException(status_code=503, detail="release of information is not configured")
        return service

    @app.get("/roi", response_class=HTMLResponse)
    def roi_list(
        request: Request,
        status: Optional[str] = None,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")
        requests = roi_service().list_requests(status=status)
        return page(request, "roi.html", identity, requests=requests, status=status,
                    requester_types=REQUESTER_TYPES, error=None)

    @app.post("/roi", response_class=HTMLResponse)
    def roi_create(
        request: Request,
        patient_reference: str = Form(...),
        requester_type: str = Form(...),
        requester_detail: str = Form(...),
        purpose_of_use: str = Form(...),
        authorization_reference: Optional[str] = Form(None),
        scope_start: Optional[str] = Form(None),
        scope_end: Optional[str] = Form(None),
        scope_resource_types: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")

        error = None
        try:
            roi_service().create(
                patient_reference=patient_reference,
                requester_type=requester_type,
                requester_detail=requester_detail,
                purpose_of_use=purpose_of_use,
                authorization_reference=authorization_reference,
                created_by=identity.username,
                scope_start=scope_start,
                scope_end=scope_end,
                scope_resource_types=scope_resource_types,
            )
        except Exception as exc:
            error = str(exc)

        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

    @app.get("/roi/{request_id}", response_class=HTMLResponse)
    def roi_detail(
        request: Request,
        request_id: str,
        identity: Identity = Depends(current_identity),
    ):
        """The full review before anything is released.

        The production preview is assembled from the index - resource
        types and counts inside and outside the request's scope - so the
        person deciding sees exactly what fulfilment will assemble and
        what the scope filter will exclude, before the release exists.
        Rendering the preview reads no clinical content (the index holds
        none, by design), so the review itself is not a disclosure; the
        disclosure event is written by fulfil, before any record is
        read.
        """
        require(identity, "roi:create")
        roi_request = roi_service().get(request_id)
        if roi_request is None:
            raise HTTPException(status_code=404, detail="no such request")

        rows = reader.resources_for_patient(roi_request.patient_reference)
        types = (
            frozenset(t.strip() for t in
                      roi_request.scope_resource_types.split(",") if t.strip())
            if roi_request.scope_resource_types else None
        )
        by_type: dict[str, int] = {}
        excluded_types: dict[str, int] = {}
        for row in rows:
            rt_name = row["resource_type"]
            if types is not None and rt_name not in types:
                excluded_types[rt_name] = excluded_types.get(rt_name, 0) + 1
            else:
                by_type[rt_name] = by_type.get(rt_name, 0) + 1
        return page(request, "roi_detail.html", identity, active="roi",
                    r=roi_request, by_type=sorted(by_type.items()),
                    excluded_types=sorted(excluded_types.items()),
                    candidate_total=sum(by_type.values()),
                    excluded_total=sum(excluded_types.values()))

    @app.post("/roi/{request_id}/fulfil", response_class=HTMLResponse)
    def roi_fulfil(
        request: Request,
        request_id: str,
        identity: Identity = Depends(current_identity),
    ):
        # Fulfilling a request discloses PHI, so it needs the export
        # permission, not merely the ability to open a request. Creating
        # and releasing are deliberately separate grants.
        require(identity, "roi:export")
        error = None
        try:
            roi_service().fulfil(request_id, fulfilled_by=identity.username)
        except Exception as exc:
            error = str(exc)
        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

    @app.get("/roi/{request_id}/production")
    def roi_production(request_id: str, identity: Identity = Depends(current_identity)):
        """Download the paginated production document.

        A separate audited event from fulfilment: producing the record set
        and later handing a copy to someone are different disclosures, and
        an accounting that recorded only the first would understate how
        many times the records left the system.
        """
        require(identity, "roi:export")
        service = roi_service()
        roi_request = service.get(request_id)
        if roi_request is None or not roi_request.production_storage_key:
            raise HTTPException(status_code=404, detail="no production document for that request")

        record(identity, "roi.production.download", request_id, roi_request.purpose_of_use)
        pdf = service.read_production(request_id)
        if pdf is None:
            raise HTTPException(status_code=404, detail="production document is unreadable")

        from fastapi.responses import Response

        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                # attachment, not inline: a PHI document should be saved
                # deliberately rather than rendered in a browser tab that
                # may be shared, cached or screen-shared.
                "Content-Disposition": f'attachment; filename="{request_id}-production.pdf"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/roi/{request_id}/deny", response_class=HTMLResponse)
    def roi_deny(
        request: Request,
        request_id: str,
        reason: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")
        error = None
        try:
            roi_service().deny(request_id, denied_by=identity.username, reason=reason)
        except Exception as exc:
            error = str(exc)
        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

    # ---- reports ---------------------------------------------------

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "report:read")
        stats = reader.stats()
        chain = reader.verify_audit_chain()
        expiring = reader.expiring_resources(within_days=365) if identity.can("retention:read") else []
        disclosures = []
        service = getattr(app.state, "roi", None)
        if service is not None and identity.can("roi:create"):
            disclosures = service.list_requests(status="fulfilled", limit=20)
        return page(request, "reports.html", identity, stats=stats, chain=chain,
                    expiring=expiring, disclosures=disclosures)

    # ---- JSON API --------------------------------------------------

    @app.get("/api/stats")
    def api_stats(identity: Identity = Depends(current_identity)):
        require(identity, "report:read")
        stats = reader.stats()
        return JSONResponse(
            {
                "total_resources": stats.total_resources,
                "resource_type_counts": stats.resource_type_counts,
                "distinct_patients": stats.distinct_patients,
            }
        )

    @app.get("/api/audit/verify")
    def api_verify(identity: Identity = Depends(current_identity)):
        require(identity, "audit:verify")
        intact, checked, problem = reader.verify_audit_chain()
        return JSONResponse({"intact": intact, "events_checked": checked, "problem": problem})

    # ---- SMART on FHIR: in-context EHR launch -----------------------

    def smart_service():
        service = getattr(app.state, "smart", None)
        if service is None:
            raise HTTPException(
                status_code=503,
                detail="SMART launch is not configured. Register an EMR in "
                "config/smart_issuers.yaml - see runbooks/RUNBOOK_SMART_LAUNCH.md.",
            )
        return service

    @app.get("/smart/launch")
    def smart_launch(request: Request, iss: str = "", launch: str = ""):
        """Entry point the EMR opens. Unauthenticated BY DEFINITION - the
        whole purpose is to establish who the user is."""
        from fastapi.responses import RedirectResponse

        from core.web.smart.launch import IssuerNotAllowed, SMARTError

        try:
            return RedirectResponse(smart_service().begin(iss, launch), status_code=302)
        except IssuerNotAllowed as exc:
            # 403 rather than 400: this is a refusal to trust, not a
            # malformed request, and the distinction matters when reading
            # logs for a crafted-launch attempt.
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except SMARTError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/smart/callback")
    def smart_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        from fastapi.responses import RedirectResponse

        from core.web.auth import Identity, store_identity_in_session, _parse_roles
        from core.web.smart.launch import SMARTError

        if error:
            raise HTTPException(
                status_code=400,
                detail=f"the EMR refused the launch: {error} {error_description or ''}".strip(),
            )
        if not code or not state:
            raise HTTPException(status_code=400, detail="incomplete callback from the EMR")

        try:
            context = smart_service().complete(state=state, code=code)
        except SMARTError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        identity = Identity(
            username=context.username,
            email=None,
            roles=_parse_roles(",".join(context.roles)),
        )
        if "session" in request.scope:
            store_identity_in_session(
                request.session, identity, issuer=context.issuer, fhir_user=context.fhir_user
            )
        else:
            raise HTTPException(
                status_code=503,
                detail="PHI_AI_WEB_SESSION_SECRET is not set, so a completed SMART "
                "launch cannot be carried across requests. Set it to enable EHR launch.",
            )

        if app.state.audit is not None:
            app.state.audit.record(
                actor=identity.username,
                action="auth.smart.launch",
                resource_key=f"{context.issuer} patient={context.patient_id or 'none'}",
                purpose_of_use="treatment",
            )

        # Remember the presentation choice for the rest of the session:
        # subsequent navigation inside the EHR frame must stay compact,
        # and the redirect below is the only place that knows.
        registered = smart_service().resolve_issuer(context.issuer)
        request.session["embedded"] = bool(registered.embedded)
        # Remember where "back" is. Held in the session rather than
        # recomputed per page so a clinician who navigates deeper into the
        # platform can still return to the chart they came from.
        request.session["chart_url_template"] = registered.chart_url or ""
        request.session["chart_label"] = registered.chart_label or "the EMR"
        request.session["launch_patient"] = context.patient_id or ""
        request.session["launch_encounter"] = context.encounter_id or ""

        # Land in context. Treatment is the correct purpose for a
        # clinician launching from a patient's chart, and it is recorded
        # as such rather than inferred later.
        if context.patient_id and context.record_source:
            target = f"/smart/patient/{context.patient_id}"
            if context.encounter_id:
                # Land on the VISIT they launched from, not the whole
                # record - that is what "in context" means to a clinician
                # who is looking at one encounter.
                target += f"?encounter={context.encounter_id}"
            return RedirectResponse(target, status_code=302)

        reason = None
        if context.patient_id and not context.record_source:
            reason = (
                f"This platform's records did not come from {context.issuer}, so patient "
                f"identifiers from it do not resolve here. Search for the patient by their "
                "identifier in the source system this platform was populated from."
            )
        elif not context.patient_id:
            reason = (
                "The EMR did not send a patient context with this launch, so there is no "
                "record to open. Search for the patient below."
            )
        return TEMPLATES.TemplateResponse(
            request=request, name="smart_no_context.html",
            context={"identity": identity, "purposes": PURPOSES_OF_USE, "reason": reason},
        )

    @app.get("/smart/patient/{patient_id}", response_class=HTMLResponse)
    def smart_patient(
        request: Request,
        patient_id: str,
        encounter: Optional[str] = None,
        identity: Identity = Depends(current_identity),
    ):
        """The in-context landing page.

        A GET, unlike the ordinary patient view which is POSTed with a
        chosen purpose - the EMR redirect cannot POST. Purpose is
        `treatment`, which is what a clinician launching from a patient's
        open chart is doing, and it is recorded in the audit entry exactly
        as an explicitly chosen one would be.
        """
        require(identity, "patient:read")
        reference = f"Patient/{patient_id}"

        record(identity, "record.read.patient", reference, "treatment")
        rows = reader.resources_for_patient(reference)

        encounter_total = None
        if encounter:
            # Encounter membership lives inside the resource, not the
            # index - an encounter id links a patient to a specific
            # episode on a specific date, which schema.sql keeps out. So
            # this reads the resources, like date scoping does.
            from core.fhir.encounter_context import resource_in_encounter

            encounter_total = len(rows)
            filtered = []
            for row in rows:
                try:
                    resource = reader.read_resource(row["storage_key"])
                except Exception as exc:
                    # A resource that cannot be read is SHOWN, not hidden:
                    # dropping it would quietly narrow a clinical view.
                    log.error("could not read %s for encounter filter: %s",
                              row["storage_key"], exc)
                    filtered.append(row)
                    continue
                if resource_in_encounter(resource, encounter):
                    filtered.append(row)
            rows = filtered

        return page(
            request, "patient.html", identity,
            patient_reference=reference, resources=rows, purpose="treatment",
            launched_in_context=True, encounter_id=encounter,
            encounter_total=encounter_total,
            imaging_studies=_imaging_studies(identity, reference),
        )

    # ---- assistant ---------------------------------------------------

    def assistant_runtime():
        rt = getattr(app.state, "assistant", None)
        if rt is None:
            raise HTTPException(
                status_code=503,
                detail="The assistant is not enabled in this deployment. It is an "
                "optional add-on - see runbooks/RUNBOOK_AI_ASSISTANT.md.",
            )
        return rt

    def _assistant_conversation(request: Request, identity: Identity, rt):
        """This user's live conversation, resumed or started.

        The id lives in the signed session cookie and the conversation
        itself in worker memory, so it expires on the same clock as the
        identity that owns it - see core/assistant/conversations.py.
        """
        store = rt.conversations
        conversation = store.get(request.session.get(_ASSISTANT_KEY), identity.username)
        if conversation is None:
            conversation = store.create(identity.username)
            request.session[_ASSISTANT_KEY] = conversation.id
        return conversation

    def _assistant_page(request, identity, conversation, **context):
        rt = assistant_runtime()
        prompts = getattr(app.state, "prompts", None)
        return page(
            request,
            "assistant.html",
            identity,
            conversation=conversation,
            destination=(
                f"{rt.settings.provider}, inside your organisation's own cloud account"
                if rt.settings.stays_in_org_cloud
                else "the Anthropic API, outside this deployment's cloud account"
            ),
            phi_access=rt.settings.phi_access,
            # Prompt history and saved prompts - this user's only, empty
            # when the store is unconfigured so the rail sections do not
            # render at all.
            saved_prompts=prompts.saved(identity.username) if prompts else [],
            recent_prompts=prompts.recent(identity.username) if prompts else [],
            prompts_enabled=prompts is not None,
            **context,
        )

    def _clinical_access(identity: Identity, rt, purpose, patient_reference, storage_key):
        """Resolve clinical tool access for one request, or None.

        Returns None - meaning documentation and aggregates only - unless
        ALL of the following hold. Each is a separate reason, and each is
        reported to the user rather than failing silently:

          - the deployment enabled a PHI tier (config, the org's decision)
          - the caller stated a valid purpose of use
          - at the in-context tier, the page supplied a record to bind to

        The audit callback is core/web/app.py's own record(), so a
        clinical read made through the assistant produces byte-identical
        audit entries to one made by clicking - which is the property that
        keeps an accounting of disclosures correct.
        """
        if not rt.settings.reads_clinical_content:
            return None, None
        if rt.reader is None:
            return None, "the record index is not configured, so records cannot be read"
        try:
            resolved_purpose = validate_purpose(purpose)
        except NotAuthorized:
            return None, (
                "no purpose of use was stated, so the assistant answered without "
                "reading any records. Choose one to let it read."
            )

        if rt.settings.allows_lookup:
            bound_patient, bound_key = None, None
        else:
            bound_patient, bound_key = patient_reference, storage_key
            if not (bound_patient or bound_key):
                return None, (
                    "this deployment lets the assistant read only the record you "
                    "already have open, and this question was not asked from one"
                )

        def record_read(action: str, resource_key: str) -> None:
            # Fails closed: record() raises when no audit sink is
            # configured, and the tool calls this BEFORE decrypting.
            record(identity, action, resource_key, resolved_purpose)

        return (
            AssistantClinicalAccess(
                reader=rt.reader,
                record_read=record_read,
                purpose=resolved_purpose,
                tier=rt.settings.phi_access,
                patient_reference=bound_patient,
                storage_key=bound_key,
            ),
            None,
        )

    def _analytics_access(identity: Identity, rt, purpose):
        """Population-query access for one request, or None.

        Deliberately NOT gated on the PHI tier that governs record
        reading. They are different questions with different answers: an
        analyst counting cohorts has no business opening a chart, and a
        clinician reading one chart has no business running population
        queries. Each is gated by its own permission and its own database
        role, so an organisation can enable either alone.

        A purpose of use is NOT required to reach these tools, and that is
        a deliberate difference from clinical reads. A cohort count
        discloses no individual, so demanding a per-question purpose would
        be ceremony; the query itself is what gets audited, verbatim, and
        that is the record worth keeping. Name search DOES identify people
        and is permissioned separately for exactly that reason.
        """
        if rt.analytics_connection is None and rt.identity_connection is None:
            return None
        if not (identity.can("analytics:query") or identity.can("identity:search")):
            return None

        def record_query(action: str, detail: str) -> None:
            record(identity, action, detail, purpose or "operations")

        return AssistantAnalyticsAccess(
            analytics_connection=rt.analytics_connection,
            identity_connection=rt.identity_connection,
            record_query=record_query,
            purpose=purpose,
        )

    def _research_access(identity: Identity, rt, purpose):
        """Cross-record research access for one request, or None.

        Search snippets ARE clinical text, so unlike the analytics
        gate above this one demands everything a clinical read demands:
        the lookup tier (searching every chart at once has no in-context
        analogue) and a validated purpose of use - the `research` code
        is what a researcher normally states. On top of that, each piece
        follows its own permission: general search appears only for
        `research:search` (the researcher role), the psychotherapy
        pieces only for `psychotherapy:read` AND only where the
        deployment's own psychotherapy gate is on
        (core/assistant/config.py). The audit callback is the same
        record() every other path uses, so a search or note read made
        through the assistant is indistinguishable in the trail from
        one that could have been made by hand.
        """
        if not rt.settings.allows_lookup:
            return None
        wants_search = (
            rt.research_search_connection is not None
            and identity.can("research:search")
        )
        wants_psych = (
            rt.settings.psychotherapy_access
            and identity.can("psychotherapy:read")
            and (
                rt.psychotherapy_search_connection is not None
                or rt.psychotherapy_reader is not None
            )
        )
        if not (wants_search or wants_psych):
            return None
        try:
            resolved_purpose = validate_purpose(purpose)
        except NotAuthorized:
            return None

        def record_research(action: str, detail: str) -> None:
            # Fails closed, before any search runs or any note is
            # decrypted - record() raises when no audit sink exists.
            record(identity, action, detail, resolved_purpose)

        return AssistantResearchAccess(
            search_connection=rt.research_search_connection if wants_search else None,
            psychotherapy_connection=(
                rt.psychotherapy_search_connection if wants_psych else None
            ),
            read_psychotherapy=rt.psychotherapy_reader if wants_psych else None,
            record=record_research,
            purpose=resolved_purpose,
        )

    @app.get("/assistant", response_class=HTMLResponse)
    def assistant_view(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "assistant:use")
        rt = assistant_runtime()
        conversation = _assistant_conversation(request, identity, rt)

        # ?prefill=<row id>: put a history/saved prompt INTO the composer
        # so the person can edit and explicitly press Ask. A click must
        # never fire the model by itself - a model call costs seconds,
        # money, and an audit entry, and none of those belong on a
        # single unconfirmed click. The id is an opaque integer (never
        # the prompt text) so nothing sensitive rides in the URL, and
        # the lookup is scoped to the caller's own rows.
        draft = ""
        prefill = request.query_params.get("prefill")
        prompts = getattr(app.state, "prompts", None)
        if prefill and prompts is not None:
            try:
                wanted = int(prefill)
            except ValueError:
                wanted = None
            if wanted is not None:
                for row in prompts.saved(identity.username) + prompts.recent(identity.username):
                    if row["id"] == wanted:
                        draft = row["prompt"]
                        break

        return _assistant_page(
            request, identity, conversation, error=None, note=None,
            back=None, back_to=None, draft=draft,
        )

    @app.post("/assistant", response_class=HTMLResponse)
    def assistant_ask(
        request: Request,
        question: str = Form(""),
        page_key: Optional[str] = Form(None),
        back: Optional[str] = Form(None),
        action: str = Form("ask"),
        purpose_of_use: Optional[str] = Form(None),
        context_patient: Optional[str] = Form(None),
        context_key: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        """Continue this user's conversation with the assistant.

        POST, like every other form here, and for the same reason: a
        question is free text a user typed, and free text does not belong
        in a URL that reaches proxy logs and browser history. The answer
        is rendered in the response rather than redirected to, so no part
        of it appears in a query string either.

        `page_key` says which page the question was asked FROM. It is a
        key into a server-side table of phrases, never a URL - see
        core/web/assistant_pages.py for why that distinction is the whole
        design of this parameter. `back` is validated as a same-origin
        path before it is ever rendered as a link.

        `context_patient` / `context_key` name the record the user
        already has open, and matter only where the deployment enabled
        the in-context PHI tier. They are a REQUEST for access, not a
        grant of it: core/assistant/tools.py re-derives the permitted
        object keys from the index and refuses anything outside them, so
        a forged value widens nothing.
        """
        require(identity, "assistant:use")
        rt = assistant_runtime()

        return_path = safe_return_path(back)
        conversation = _assistant_conversation(request, identity, rt)

        if action == "clear":
            rt.conversations.discard(conversation.id)
            conversation = rt.conversations.create(identity.username)
            request.session[_ASSISTANT_KEY] = conversation.id
            return _assistant_page(
                request, identity, conversation, error=None, note=None,
                back=return_path, back_to=back_label(return_path),
            )

        clinical, clinical_note = _clinical_access(
            identity, rt, purpose_of_use, context_patient, context_key
        )
        analytics = _analytics_access(identity, rt, purpose_of_use)
        research = _research_access(identity, rt, purpose_of_use)

        # Built per request with the caller's CURRENT permissions, seeded
        # with the conversation so far. A role that changed between
        # questions takes effect on the next one rather than being frozen
        # into a long-lived object - see core/assistant/tools.py.
        session = rt.session_for(
            actor=identity.username,
            capabilities=identity.permissions(),
            audit=app.state.audit,
            require_audit=True,
            history=conversation.messages,
            turn_starts=conversation.turn_starts,
            clinical=clinical,
            analytics=analytics,
            research=research,
        )

        # Prompt history: recorded on the attempt, not the outcome - a
        # prompt that errored is exactly the one worth re-running.
        # Best-effort by construction (see core/web/prompt_store.py);
        # the audit entry the session writes is the record of use.
        if question.strip() and getattr(app.state, "prompts", None):
            app.state.prompts.record(identity.username, question, page_key)

        error = None
        reply = None
        asked_at = time.monotonic()
        # The Control panel's switches, honored before any model call.
        # Both refusals are stated in place; the question was already
        # recorded, and the audit entry the session writes still gates
        # actual reads.
        pstate = getattr(app.state, "platform_state", None)
        if pstate is not None and pstate.config_get("rag_enabled", "on") != "on":
            error = ("Retrieval is DISABLED by the System Administrator "
                     "(Control panel · PHI RAG). The assistant cannot read "
                     "the platform's stores while it is off, and it will not "
                     "answer questions about records without reading them.")
        elif pstate is not None and pstate.config_get("assistant_live", "on") != "on":
            error = ("Live model calls are switched OFF by the System "
                     "Administrator (Control panel · foundation model). No "
                     "request left for the model; switch it back on to ask.")
        try:
            if error is None:
                reply = session.ask(question, page_context=describe_page(page_key))
        except RuntimeError as exc:
            # Audit logging unavailable. The question was not sent; say so
            # in place rather than losing the page to a 503.
            error = str(exc)
        except Exception as exc:
            log.error("assistant request failed: %s", exc)
            error = "The assistant is currently unavailable. The question was not answered."
        else:
            if reply is not None:
                conversation.messages, conversation.turn_starts = session.export_history()
                conversation.record(
                    Turn(
                        question=question.strip(),
                        answer=reply.text,
                        sources=reply.sources,
                        refused=reply.refused,
                    ),
                    max_turns=rt.conversations.max_turns,
                )

        # Telemetry: metrics only, never the question or the answer
        # (core/db/telemetry_schema.sql's header is the contract).
        # Fire-and-forget by construction - record_interaction() cannot
        # raise - and skipped entirely when the ops role is unconfigured.
        assistant_telemetry.record_interaction(
            rt.ops_connection,
            username=identity.username,
            roles=",".join(sorted(r.value for r in identity.roles)),
            page_key=page_key or None,
            provider=rt.settings.provider,
            model=rt.settings.resolved_model,
            latency_ms=int((time.monotonic() - asked_at) * 1000),
            input_tokens=reply.input_tokens if reply else 0,
            output_tokens=reply.output_tokens if reply else 0,
            tool_calls=len(reply.tools_used) if reply else 0,
            tools_used=",".join(reply.tools_used) if reply else "",
            phi_reads=reply.phi_reads if reply else 0,
            refused=bool(reply and reply.refused),
            truncated=bool(reply and reply.truncated),
            error=error is not None,
        )

        return _assistant_page(
            request, identity, conversation, error=error, note=clinical_note,
            back=return_path, back_to=back_label(return_path),
        )

    @app.post("/assistant/prompts", response_class=HTMLResponse)
    def assistant_prompt_action(
        request: Request,
        prompt_id: int = Form(...),
        prompt_action: str = Form(...),
        label: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        """Save, unsave or delete one of the caller's own prompts.

        The store scopes every statement to the caller's username, so a
        forged prompt_id belonging to someone else updates zero rows -
        the same quiet non-result an expired id gets. Nothing here needs
        auditing: these are bookmarks over text the audit trail already
        holds (see core/db/prompts_schema.sql).
        """
        require(identity, "assistant:use")
        prompts = getattr(app.state, "prompts", None)
        if prompts is None:
            raise HTTPException(status_code=404, detail="prompt history is not configured")
        if prompt_action == "save":
            prompts.save(identity.username, prompt_id, label)
        elif prompt_action == "unsave":
            prompts.unsave(identity.username, prompt_id)
        elif prompt_action == "delete":
            prompts.delete(identity.username, prompt_id)
        else:
            raise HTTPException(status_code=400, detail="unknown prompt action")
        return RedirectResponse("/assistant", status_code=303)

    @app.get("/assistant/ops", response_class=HTMLResponse)
    def assistant_ops(
        request: Request,
        days: int = 30,
        identity: Identity = Depends(current_identity),
    ):
        """Usage, performance, compliance and drift metrics for the
        assistant - the operational answer to "how is this AI feature
        behaving", which a deployment that enabled it owes whoever
        signed off on enabling it. Gated by assistant:ops (admin and
        auditor), narrower than report:read because the rows name which
        staff member asked how many questions. Everything rendered here
        is counts and rates; the questions themselves are only in the
        audit trail."""
        require(identity, "assistant:ops")
        rt = assistant_runtime()

        summary = drift = ops_error = None
        if rt.ops_connection is None:
            ops_error = (
                "Assistant telemetry is not configured. Set "
                "PHI_AI_ASSISTANT_OPS_USERNAME to the aiops role "
                "(core/db/telemetry_bootstrap_<cloud>.sql) to record and "
                "report usage."
            )
        else:
            try:
                conn = rt.ops_connection()
                try:
                    summary = assistant_telemetry.usage_summary(conn, days=days)
                    drift = assistant_telemetry.drift_summary(conn)
                finally:
                    conn.close()
            except Exception as exc:
                log.error("assistant ops summary failed: %s", exc)
                ops_error = f"Could not read telemetry: {exc}"

        return page(
            request,
            "assistant_ops.html",
            identity,
            active="assistant",
            summary=summary,
            drift=drift,
            ops_error=ops_error,
            model_description=rt.settings.describe(),
        )

    # ---- imaging -----------------------------------------------------

    @app.post("/imaging/open")
    def imaging_open(
        request: Request,
        study_instance_uid: str = Form(...),
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        """Establish a purpose of use, then hand off to the viewer.

        This exists because DICOMweb has nowhere to carry a purpose of
        use - PS3.18 defines no such parameter and the viewer would not
        send one. So the choice is made HERE, in the platform's own
        interface, by the same person and with the same five codes as any
        other clinical read, and stored in the signed session. Every
        subsequent DICOMweb request inherits it. Without this step the
        DICOMweb API refuses to serve anything, which is what stops
        someone reaching the viewer directly and reading imaging under no
        stated reason at all.
        """
        require(identity, "imaging:read")
        try:
            purpose = validate_purpose(purpose_of_use)
            if not purpose_allowed(identity, purpose):
                # The role dictates the purposes it may
                # assert; refusal is audited like any
                # other denial.
                require(identity, f"purpose:{purpose}")
        except NotAuthorized as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Validate the UID with the SAME check every /dicomweb route
        # applies (dotted digits, <=64 chars), rather than trusting the
        # form value. It is interpolated into the viewer redirect URL
        # below, so an unvalidated value could inject extra query
        # parameters into that URL or reflect unencoded into the Location
        # header. A DICOM UID cannot legitimately contain anything that
        # would.
        from core.web.dicomweb_routes import _valid_uid

        study_instance_uid = _valid_uid(study_instance_uid, "StudyInstanceUID")

        if "session" not in request.scope:
            raise HTTPException(
                status_code=503,
                detail="PHI_AI_WEB_SESSION_SECRET is not set, so a purpose of use "
                "cannot be carried to the viewer. Set it to enable imaging.",
            )

        from core.web.dicomweb_routes import PURPOSE_KEY

        request.session[PURPOSE_KEY] = purpose

        viewer = env_var("IMAGING_VIEWER_URL")
        if not viewer:
            raise HTTPException(
                status_code=503,
                detail="PHI_AI_IMAGING_VIEWER_URL is not set, so there is no viewer "
                "to open. See runbooks/RUNBOOK_DICOM_IMAGING.md.",
            )

        from urllib.parse import quote

        from fastapi.responses import RedirectResponse

        # The study UID is the ONLY thing in this URL. It is a DICOM UID -
        # dotted digits assigned by the modality, opaque, and not a
        # real-world identifier - so it belongs in a path for the same
        # reason this application already accepts an EMR's patient
        # reference in one. Nothing about the patient travels here.
        # URL-encoded even though _valid_uid above already constrains it to
        # dotted digits: the encoding is what keeps this a query VALUE
        # rather than trusting the validator to stay strict forever.
        return RedirectResponse(
            f"{viewer.rstrip('/')}/viewer?StudyInstanceUIDs={quote(study_instance_uid, safe='')}",
            status_code=302,
        )

    # Mounted only when imaging is configured. A deployment that stores
    # no DICOM has no /dicomweb routes at all, rather than routes that
    # return empty results - the difference matters to anyone scanning the
    # surface this application exposes.
    imaging_connection = getattr(app.state, "imaging_connection_factory", None)
    if imaging_connection is not None:
        from core.web.dicomweb_routes import build_router

        app.include_router(
            build_router(
                reader=reader,
                connection_factory=imaging_connection,
                audit=audit,
                require=require,
                current_identity=current_identity,
            )
        )

    # ---- local accounts (only where there is no identity provider) --
    # ---- error rendering: pages for people, JSON for programs ------

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        """Render errors as pages when a browser is on the other end.

        A clinician whose idle session expired was previously shown raw
        JSON - technically correct, humanly indistinguishable from a
        broken product. The JSON API keeps JSON (anything under /api,
        and any client that does not accept text/html); everything else
        gets a page that says what happened and where to go.
        """
        wants_html = (
            "text/html" in (request.headers.get("accept") or "")
            and not request.url.path.startswith("/api/")
        )
        if not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))

        titles = {
            400: "That request didn't make sense",
            403: "Not permitted",
            404: "Not found",
            409: "Refused",
            440: "Session expired",
            503: "Not available",
        }
        retry_path = request.url.path if request.method == "GET" else "/"
        return TEMPLATES.TemplateResponse(
            request=request, name="error.html", status_code=exc.status_code,
            context={
                "status_code": exc.status_code,
                "title": titles.get(exc.status_code, "Something went wrong"),
                "detail": str(exc.detail),
                "retry_path": retry_path,
                "retry_label": "Reload" if exc.status_code == 440 else "Back to the platform",
            },
        )

    # ---- top-bar state: development persona ------------------------
    # (There is deliberately no ambient-purpose route: purpose of use is
    # asserted per action by the workflows that move data, defaulted
    # from the role via role_default_purpose(), never set as session
    # mode - a header dropdown asserting a purpose for no particular
    # action recorded nothing anyone could rely on.)

    @app.post("/persona")
    def switch_persona(
        request: Request,
        persona: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        """Switch development persona. Only exists in dev-persona mode.

        Audited as its own event against the persona being ENTERED, so
        the audit trail shows who the session claimed to be from this
        point on - the same reason a sign-in is recorded.
        """
        if not app.state.dev_personas:
            raise HTTPException(status_code=404, detail="not a development deployment")
        if not any(p["username"] == persona for p in app.state.dev_personas):
            raise HTTPException(status_code=400, detail="unknown persona")
        request.session[_PERSONA_KEY] = persona
        # A fresh persona should not inherit the previous persona's
        # assistant conversation.
        request.session.pop(_ASSISTANT_KEY, None)
        record_actor(persona, "session.persona", f"persona/{persona}")
        return RedirectResponse("/", status_code=303)

    # ---- v1 product screens (core/web/product_routes.py) ----------

    from core.web import product_routes

    product_routes.register(app, page, require, current_identity, record)

    # ---- integration, control panel and documentation --------------
    # (core/web/platform_routes.py). State defaults to the in-memory
    # store so tests and store-less deployments get working screens; the
    # entrypoint passes a SQL-backed instance for persistence.

    from core.web import platform_routes
    from core.web.platform_state import PlatformState

    app.state.platform_state = platform_state or PlatformState()
    platform_routes.register(app, page, require, current_identity, record, reader)

    #
    # Registered LAST, and only when this deployment enabled them, so a
    # proxy-authenticated or SMART-only deployment does not carry a
    # sign-in form, an account administration page, or the exception
    # handlers that redirect to them. See core/web/auth.py's from_env(),
    # which refuses to let local accounts coexist with either proxy trust
    # or the development identity.
    if settings.local_accounts:
        from core.web import admin_routes, login_routes

        login_routes.register(app, page, record_actor)
        admin_routes.register(app, page, require, current_identity)

    # ---- liveness --------------------------------------------------

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        """Unauthenticated ON PURPOSE, and returns no platform information -
        a load balancer needs it before a user session exists."""
        return "ok"

    return app
# Made by Ryan Gomez & Co. Inc.
