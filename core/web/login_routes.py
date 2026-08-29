# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Sign-in, sign-out and self-service, for deployments with local accounts.

MOUNTED ONLY when PHI_AI_WEB_LOCAL_ACCOUNTS is set - see
core/web/auth.py's module docstring for why local accounts are the
exception rather than the recommendation, and core/web/local_auth.py's
for what the credential store does to earn its place. A deployment
behind an identity provider never loads these routes at all, so it is
byte-identical to one built before they existed.

WHAT THIS MODULE IS RESPONSIBLE FOR, and the reason each is here rather
than somewhere more convenient:

  - The ORDER of the sign-in steps. Password, then a second factor, then
    a session - and a user who must change their password or enrol MFA
    gets a session that can reach exactly those two pages and nothing
    else. Doing that as a gate inside identity resolution, rather than as
    a redirect each page remembers to perform, means a page added later
    cannot forget it.

  - Every outcome is AUDITED, including the failures, to the same
    hash-chained log a PHI read goes to. A login failure is a security
    signal and belongs in evidence, not in a application log that rotates.
    45 CFR 164.308(a)(5)(ii)(C) asks for procedures to monitor log-in
    attempts; this is where that becomes a record rather than a policy.

  - Failures are INDISTINGUISHABLE to the person submitting them. No
    such user, wrong password, disabled account and locked account all
    produce the same sentence and the same elapsed time (see
    local_auth.verify_dummy). The information an administrator needs to
    tell them apart is in the audit log and on the account page, where
    only an administrator can read it.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.db import users as user_store
from core.web.auth import (
    LOCAL_PENDING_KEY,
    LOCAL_SESSION_KEY,
    Identity,
    NeedsLogin,
    roles_from_names,
)
from core.web.local_auth import (
    KeyMismatchError,
    LocalAuthSettings,
    PasswordPolicyError,
    check_password_policy,
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    hash_password,
    new_session_id,
    normalise_username,
    totp_uri,
    verify_dummy,
    verify_password,
    verify_totp,
)

log = logging.getLogger("phi-ai.web.login")

# One sentence for every way a sign-in can fail. See this module's
# docstring: the alternative is a page that tells an unauthenticated
# stranger which of an organisation's staff names are real.
SIGNIN_FAILED = "Sign-in failed. Check the username and password and try again."

# How long the half-finished sign-in between the password step and the
# second factor stays usable. Short because it is exactly the window in
# which one factor has been satisfied and the other has not.
PENDING_SECONDS = 300

# Pages a signed-in user may reach while they still owe the system a
# password change or an MFA enrolment.
#
# None of them resolve identity through core/web/app.py's
# current_identity, so none of them can raise the gates below in the
# first place - they use _signed_in_username(), which deliberately does
# not enforce them. This set is the backstop for the day somebody adds
# `Depends(current_identity)` to one of these routes: without it that
# route would redirect to itself forever, and a browser hitting a
# redirect loop reports nothing useful about why. With it, the handler
# re-raises and the operator gets a 500 naming the exception class.
COMPLETION_PATHS = frozenset({
    "/account/password", "/login/enrol", "/logout", "/healthz",
})


class NeedsPasswordChange(Exception):
    """Signed in, but on an administrator-issued password."""


class NeedsMFAEnrolment(Exception):
    """Signed in, but this deployment requires a second factor and there is none."""


class LocalAccounts:
    """Everything the request paths need to reach the account store.

    Holds a connection FACTORY rather than a connection: this is stored
    on app.state for the process lifetime, and a single long-lived
    connection shared by every request would serialise sign-ins behind
    one transaction and die permanently on the first network blip. Each
    operation opens, uses and closes - the same shape
    core/web/app.py's imaging lookup already uses.
    """

    def __init__(self, connection_factory, settings: LocalAuthSettings):
        self.connection_factory = connection_factory
        self.settings = settings

    def connect(self):
        return self.connection_factory()


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def resolve_local_session(request: Request, accounts: LocalAccounts) -> Identity:
    """The signed-in user behind this request's cookie, or raise.

    Roles are read from the database on every request rather than taken
    from the cookie, which is what makes an administrator's role change
    or account disable land on the user's very next page load. The cost
    is one query per request; the alternative is a permission set that is
    correct as of whenever the user last signed in.
    """
    session_id = (request.session or {}).get(LOCAL_SESSION_KEY)
    if not session_id:
        raise NeedsLogin()

    conn = accounts.connect()
    try:
        user = user_store.resolve_session(conn, session_id)
    finally:
        conn.close()

    if user is None:
        # Revoked, expired, or the account was disabled. All three are
        # the same answer to a browser: sign in again.
        request.session.pop(LOCAL_SESSION_KEY, None)
        raise NeedsLogin("session is no longer valid")

    if user.must_change_password or _password_expired(user, accounts.settings):
        raise NeedsPasswordChange()
    if accounts.settings.mfa == "required" and not user.mfa_enrolled:
        raise NeedsMFAEnrolment()

    return Identity(
        username=user.username,
        email=user.email,
        roles=roles_from_names(user.roles),
    )


def _password_expired(user, settings: LocalAuthSettings) -> bool:
    """Whether this deployment's optional password expiry has elapsed.

    OFF unless an operator sets PHI_AI_WEB_LOCAL_AUTH_PASSWORD_MAX_AGE_DAYS,
    because NIST SP 800-63B (5.1.1.2) advises against periodic rotation:
    it produces Summer2026! and a sticky note. The setting exists because
    some organisations' own policy requires expiry regardless, and a
    documented variable that quietly does nothing is worse than not
    offering it - this project has already had to remove one of those.

    An unset or naive password_changed_at is treated as NOT expired
    rather than as expired: locking every account out of a system holding
    PHI on a timestamp this code could not interpret is the wrong
    direction to fail, and the column is NOT NULL with a default in the
    schema, so the case does not arise from the database.
    """
    if settings.password_max_age_days <= 0:
        return False
    changed = getattr(user, "password_changed_at", None)
    if changed is None or changed.tzinfo is None:
        return False
    age = datetime.now(timezone.utc) - changed
    return age > timedelta(days=settings.password_max_age_days)


def _identity_of(user) -> Identity:
    return Identity(
        username=user.username, email=user.email, roles=roles_from_names(user.roles)
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app, page, audit_event) -> None:
    """Mount the sign-in routes on `app`.

    `page` and `audit_event` are passed in from core/web/app.py rather
    than imported, for the same reason the reader and audit sink are
    injected into create_app(): these routes are then testable without
    constructing the whole application, and there is exactly one
    template-rendering path and one audit path in the interface rather
    than two that can drift.
    """

    def accounts() -> LocalAccounts:
        store = getattr(app.state, "local_accounts", None)
        if store is None:  # pragma: no cover - registration implies presence
            raise HTTPException(status_code=503, detail="local accounts are not configured")
        return store

    # ---- the gates -----------------------------------------------------
    #
    # Registered as exception handlers rather than checked in each route:
    # a page added next year gets them without its author knowing they
    # exist, which is the only way a rule like this survives.

    @app.exception_handler(NeedsLogin)
    async def _needs_login(request: Request, exc: NeedsLogin):
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/dicomweb"):
            # A redirect to an HTML form would reach a JSON client as a
            # 200 full of markup, which is a worse failure than a 401.
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(NeedsPasswordChange)
    async def _needs_password(request: Request, exc: NeedsPasswordChange):
        if request.url.path in COMPLETION_PATHS:  # pragma: no cover - backstop
            raise exc  # see COMPLETION_PATHS: a 500 beats a redirect loop
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(NeedsMFAEnrolment)
    async def _needs_mfa(request: Request, exc: NeedsMFAEnrolment):
        if request.url.path in COMPLETION_PATHS:  # pragma: no cover - backstop
            raise exc  # see COMPLETION_PATHS: a 500 beats a redirect loop
        return RedirectResponse("/login/enrol", status_code=303)

    # ---- helpers -------------------------------------------------------

    def _audit(actor: str, action: str, username: str) -> None:
        """Record one authentication event.

        Goes through core/web/app.py's own audit path, which FAILS THE
        REQUEST if there is no audit sink. That is the right answer here
        for the same reason it is for a PHI read: an authentication
        system whose evidence is optional is not evidence. `resource_key`
        is the account acted upon, never a credential and never a
        password-shaped thing - see core/audit/ for what a record looks
        like.
        """
        audit_event(actor=actor, action=action, resource_key=f"user/{username}",
                    purpose=None)

    def _finish_signin(request: Request, store: LocalAccounts, conn, user):
        """Establish the session row and the cookie. One place, three callers."""
        user_store.record_success(conn, user.username)
        session_id = new_session_id()
        user_store.create_session(
            conn, session_id, user.username, store.settings.session_minutes
        )
        # Everything from before sign-in goes, including the pending
        # half-authentication and any CSRF token minted for the login
        # form. A session that carries state across the authentication
        # boundary is how session fixation works.
        request.session.clear()
        request.session[LOCAL_SESSION_KEY] = session_id
        _audit(user.username, "auth.login", user.username)
        return RedirectResponse("/", status_code=303)

    def _pending(request: Request) -> Optional[str]:
        raw = (request.session or {}).get(LOCAL_PENDING_KEY) or {}
        username, at = raw.get("username"), raw.get("at")
        if not username or not at or (time.time() - float(at)) > PENDING_SECONDS:
            request.session.pop(LOCAL_PENDING_KEY, None)
            return None
        return username

    # ---- sign in -------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if (request.session or {}).get(LOCAL_SESSION_KEY):
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html", None, active="login", error=None)

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
        store = accounts()
        settings = store.settings

        def refuse():
            """The only failure response this route has. See SIGNIN_FAILED."""
            return page(request, "login.html", None, active="login",
                        error=SIGNIN_FAILED, status_code=401)

        try:
            name = normalise_username(username)
        except PasswordPolicyError:
            # Not a real username, so nothing to attribute the failure
            # to in the audit log. Still pay the hashing cost, so a
            # malformed submission is not measurably faster than a wrong
            # password.
            verify_dummy(settings.key)
            return refuse()

        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            if user is None:
                verify_dummy(settings.key)
                # Not audited against a username: there is no account to
                # attribute it to, and writing the submitted string into
                # the audit trail would let an unauthenticated stranger
                # choose what appears in it.
                log.info("sign-in attempt for an account that does not exist")
                return refuse()

            if not user.is_active:
                verify_dummy(settings.key)
                _audit(name, "auth.login.disabled", name)
                return refuse()

            if user_store.is_locked(conn, name):
                # Checked BEFORE the password is verified, so a locked
                # account cannot be used to make this endpoint do
                # unbounded scrypt work. verify_dummy keeps the timing
                # indistinguishable anyway.
                verify_dummy(settings.key)
                _audit(name, "auth.login.locked", name)
                return refuse()

            try:
                ok = verify_password(password, user.password_hash, settings.key)
            except KeyMismatchError as exc:
                # NOT a failed login, and it must not be reported as one.
                # Every account in the deployment is in this state, and
                # the operator is the only person who can fix it.
                log.error("local account key mismatch: %s", exc)
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            if not ok:
                locked = user_store.record_failure(
                    conn, name, settings.max_failures, settings.lockout_minutes
                )
                _audit(name, "auth.login.failed", name)
                if locked:
                    _audit(name, "auth.login.lockout", name)
                return refuse()

            # Password is right. What happens next depends on what else
            # this account owes.
            if user.mfa_enrolled:
                request.session[LOCAL_PENDING_KEY] = {"username": name, "at": time.time()}
                return RedirectResponse("/login/mfa", status_code=303)
            if settings.mfa == "required":
                request.session[LOCAL_PENDING_KEY] = {"username": name, "at": time.time()}
                return RedirectResponse("/login/enrol", status_code=303)
            return _finish_signin(request, store, conn, user)
        finally:
            conn.close()

    # ---- second factor -------------------------------------------------

    @app.get("/login/mfa", response_class=HTMLResponse)
    def mfa_form(request: Request):
        if not _pending(request):
            return RedirectResponse("/login", status_code=303)
        return page(request, "login_mfa.html", None, active="login", error=None)

    @app.post("/login/mfa", response_class=HTMLResponse)
    def mfa_submit(request: Request, code: str = Form("")):
        store = accounts()
        name = _pending(request)
        if not name:
            return RedirectResponse("/login", status_code=303)

        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            if user is None or not user.is_active or not user.mfa_enrolled:
                request.session.pop(LOCAL_PENDING_KEY, None)
                return RedirectResponse("/login", status_code=303)

            try:
                secret = decrypt_secret(user.mfa_secret, store.settings.key)
            except KeyMismatchError as exc:
                log.error("local account key mismatch on MFA secret: %s", exc)
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            step = verify_totp(secret, code, last_used_step=user.mfa_last_step)
            # record_mfa_step is the actual replay guard: it only
            # succeeds if the stored step is still below this one, so two
            # requests presenting the same code cannot both win a race.
            if step is None or not user_store.record_mfa_step(conn, name, step):
                user_store.record_failure(
                    conn, name, store.settings.max_failures,
                    store.settings.lockout_minutes,
                )
                _audit(name, "auth.mfa.failed", name)
                return page(request, "login_mfa.html", None, active="login",
                            error="That code was not accepted. Codes expire after "
                                  "30 seconds and each one can be used only once.",
                            status_code=401)

            return _finish_signin(request, store, conn, user)
        finally:
            conn.close()

    # ---- enrolment ------------------------------------------------------

    _ENROL_KEY = "local_enrol_secret"

    @app.get("/login/enrol", response_class=HTMLResponse)
    def enrol_form(request: Request):
        store = accounts()
        name = _pending(request) or _signed_in_username(request, store)
        if not name:
            return RedirectResponse("/login", status_code=303)

        secret = (request.session or {}).get(_ENROL_KEY)
        if not secret:
            secret = generate_totp_secret()
            request.session[_ENROL_KEY] = secret
        return page(
            request, "login_enrol.html", None, active="login", error=None,
            mfa_secret=secret,
            mfa_uri=totp_uri(secret, name, store.settings.issuer),
            mfa_username=name,
        )

    @app.post("/login/enrol", response_class=HTMLResponse)
    def enrol_submit(request: Request, code: str = Form("")):
        store = accounts()
        name = _pending(request) or _signed_in_username(request, store)
        secret = (request.session or {}).get(_ENROL_KEY)
        if not name or not secret:
            return RedirectResponse("/login", status_code=303)

        # No last_used_step: this secret has never authenticated
        # anything, so there is nothing to replay yet.
        step = verify_totp(secret, code)
        if step is None:
            return page(
                request, "login_enrol.html", None, active="login",
                error="That code did not match. Check the authenticator's clock is "
                      "correct, then enter the current code.",
                mfa_secret=secret,
                mfa_uri=totp_uri(secret, name, store.settings.issuer),
                mfa_username=name, status_code=401,
            )

        conn = store.connect()
        try:
            user_store.set_mfa_secret(conn, name, encrypt_secret(secret, store.settings.key), name)
            user_store.record_mfa_step(conn, name, step)
            request.session.pop(_ENROL_KEY, None)
            _audit(name, "auth.mfa.enrolled", name)

            user = user_store.get_user(conn, name)
            if (request.session or {}).get(LOCAL_SESSION_KEY):
                # Already signed in and re-enrolling; keep the session.
                return RedirectResponse("/account", status_code=303)
            return _finish_signin(request, store, conn, user)
        finally:
            conn.close()

    # ---- sign out -------------------------------------------------------

    @app.post("/logout")
    def logout(request: Request):
        """POST, never GET.

        A GET sign-out can be triggered by any page that can make this
        browser load a URL - an <img> tag on another site is enough - and
        logging somebody out mid-task is a denial of service, however
        minor. It is state-changing, so it goes through the CSRF check
        like every other state-changing route.
        """
        store = accounts()
        session_id = (request.session or {}).get(LOCAL_SESSION_KEY)
        if session_id:
            conn = store.connect()
            try:
                user = user_store.resolve_session(conn, session_id)
                user_store.revoke_session(conn, session_id, "signout")
                if user is not None:
                    _audit(user.username, "auth.logout", user.username)
            finally:
                conn.close()
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---- self service ---------------------------------------------------

    def _signed_in_username(request: Request, store: LocalAccounts) -> Optional[str]:
        """Who this cookie belongs to, WITHOUT the completion gates.

        Distinct from resolve_local_session() on purpose: the pages that
        exist to satisfy those gates cannot be reached through a function
        that enforces them, and inlining a "unless the path is one of
        these" exception into the main resolver would be a rule with a
        hole in it.
        """
        session_id = (request.session or {}).get(LOCAL_SESSION_KEY)
        if not session_id:
            return None
        conn = store.connect()
        try:
            user = user_store.resolve_session(conn, session_id)
        finally:
            conn.close()
        return user.username if user else None

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        store = accounts()
        name = _signed_in_username(request, store)
        if not name:
            raise NeedsLogin()
        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            sessions = user_store.active_session_count(conn, name)
        finally:
            conn.close()
        return page(request, "account.html", _identity_of(user), active="account",
                    account=user, active_sessions=sessions,
                    mfa_policy=store.settings.mfa, error=None, notice=None)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_form(request: Request):
        store = accounts()
        name = _signed_in_username(request, store)
        if not name:
            raise NeedsLogin()
        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
        finally:
            conn.close()
        return page(request, "account_password.html", None, active="account",
                    error=None, forced=user.must_change_password, username=name)

    @app.post("/account/password", response_class=HTMLResponse)
    def password_submit(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        confirm_password: str = Form(""),
    ):
        store = accounts()
        name = _signed_in_username(request, store)
        if not name:
            raise NeedsLogin()

        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)

            def refuse(message: str):
                return page(request, "account_password.html", None, active="account",
                            error=message, forced=user.must_change_password,
                            username=name, status_code=400)

            try:
                if not verify_password(current_password, user.password_hash, store.settings.key):
                    _audit(name, "auth.password.failed", name)
                    return refuse("Current password is not correct.")
            except KeyMismatchError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            if new_password != confirm_password:
                return refuse("The two new passwords do not match.")
            if new_password == current_password:
                return refuse("The new password must be different from the current one.")
            try:
                check_password_policy(new_password, username=name)
            except PasswordPolicyError as exc:
                return refuse(str(exc))

            user_store.set_password(
                conn, name, hash_password(new_password, store.settings.key), name,
                must_change=False,
            )
            # Every other session for this account ends here. A password
            # change is what somebody does when they think a credential
            # leaked, and leaving the sessions that credential opened
            # running would defeat the point. This browser gets a new
            # session id rather than an exemption - same rule, no
            # special case.
            user_store.revoke_sessions_for_user(conn, name, "password_change")
            _audit(name, "auth.password.changed", name)

            session_id = new_session_id()
            user_store.create_session(conn, session_id, name, store.settings.session_minutes)
            request.session.clear()
            request.session[LOCAL_SESSION_KEY] = session_id
            return RedirectResponse("/account", status_code=303)
        finally:
            conn.close()
# Made by Ryan Gomez & Co. Inc.
