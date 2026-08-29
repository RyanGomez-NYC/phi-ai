# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Account administration, for deployments with local accounts.

MOUNTED ONLY when PHI_AI_WEB_LOCAL_ACCOUNTS is set, and reachable
only with `admin:users` - see core/web/auth.py, where that permission is
granted to the admin role and to nothing else, and deliberately alongside
no clinical permission at all. An account administrator can create the
person who reads a chart; they cannot read one.

WHAT THIS IS FOR, in the words the regulation uses: 45 CFR 164.308(a)(3)
and (a)(4) require an organisation to authorise access, to review it, and
to terminate it when someone leaves. Behind an identity provider all
three happen in the IdP. Without one they have to happen somewhere, and
until now this system had no answer - which meant a deployment with no
IdP either shared one credential (defeating 164.312(a)(2)(i)'s unique
user identification outright) or did not exist.

WHAT IS DELIBERATELY NOT HERE:

  - No account deletion. Disable, always. An audit entry from 2029 names
    an actor; if that account row can vanish, the entry names nobody.
    See core/db/users_schema.sql.
  - No "reveal password". A reset issues a new temporary one, shown once
    to the administrator who issued it, with must_change_password set so
    it stops working as soon as the user chooses their own. There is no
    state in which this application can show an existing password,
    because it does not have one - only a scrypt hash of an HMAC of it.
  - No email. There is no mail path in this system, so recovery is
    administrator-mediated by design rather than by omission: a reset
    link in an inbox is a second credential channel with its own failure
    modes, and a small deployment without an IdP is unlikely to have a
    hardened one.
  - No route that can leave the platform unadministerable. Removing the
    last active administrator's admin role, or disabling the last
    administrator, is refused - see LAST_ADMIN below.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.db import users as user_store
from core.web.auth import Role
from core.web.local_auth import (
    PasswordPolicyError,
    check_password_policy,
    hash_password,
    normalise_username,
)

log = logging.getLogger("phi-ai.web.admin")

LAST_ADMIN = (
    "This is the only active administrator. Grant the admin role to somebody else "
    "first - a deployment with no administrator can only be recovered from the command "
    "line, on the host, with the database credentials."
)

# Every role an administrator may grant, in the order they appear on the
# page: read-only first, then the ones that can disclose or destroy.
# Sourced from the Role enum rather than hardcoded, so a role added there
# appears here without anyone remembering to add it.
GRANTABLE_ROLES = (
    Role.VIEWER, Role.HIM, Role.ANALYST, Role.AUDITOR, Role.DISPOSITION, Role.ADMIN,
)


def generate_temporary_password() -> str:
    """A password the administrator reads out once and never stores.

    High-entropy and random rather than pronounceable: it exists for the
    minutes between an administrator creating an account and the user
    choosing their own, and every account created this way carries
    must_change_password. Re-generated in the unlikely event the policy
    check refuses it, rather than returned unchecked - a temporary
    password the login page would then reject is a support call.
    """
    for _ in range(8):
        candidate = secrets.token_urlsafe(15)
        try:
            check_password_policy(candidate)
        except PasswordPolicyError:  # pragma: no cover - vanishingly unlikely
            continue
        return candidate
    raise RuntimeError(  # pragma: no cover
        "could not generate a temporary password that satisfies the password policy"
    )


def register(app, page, require, current_identity_dep) -> None:
    """Mount the administration routes on `app`.

    `page`, `require` and the identity dependency come from
    core/web/app.py rather than being rebuilt here, so these routes
    render through the same template path and authorise through the same
    checker - including the part where a denial is itself audited - as
    every other page in the interface.
    """
    from fastapi import Depends

    def accounts():
        store = getattr(app.state, "local_accounts", None)
        if store is None:  # pragma: no cover - registration implies presence
            raise HTTPException(status_code=503, detail="local accounts are not configured")
        return store

    def _audit(identity, action: str, username: str) -> None:
        app_audit = getattr(app.state, "audit", None)
        if app_audit is None:
            # Same rule as a PHI read: without a durable record of who
            # granted what to whom, the access-management control is a
            # claim rather than evidence.
            raise HTTPException(
                status_code=503,
                detail="Audit logging is not configured. Account changes are not made "
                       "without an audit trail.",
            )
        app_audit.record(
            actor=identity.username, action=action,
            resource_key=f"user/{username}", purpose_of_use=None,
        )

    def _render_list(request: Request, identity, error=None, notice=None,
                     issued: Optional[tuple] = None, status_code: int = 200):
        store = accounts()
        conn = store.connect()
        try:
            users = user_store.list_users(conn)
            admins = user_store.count_active_admins(conn)
        finally:
            conn.close()
        return page(
            request, "admin_users.html", identity, active="admin", users=users,
            grantable=GRANTABLE_ROLES, error=error, notice=notice,
            issued=issued, admin_count=admins, mfa_policy=store.settings.mfa,
            status_code=status_code,
        )

    # ---- list and create -------------------------------------------------

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, identity=Depends(current_identity_dep)):
        require(identity, "admin:users")
        return _render_list(request, identity)

    @app.post("/admin/users", response_class=HTMLResponse)
    def create_user(
        request: Request,
        identity=Depends(current_identity_dep),
        username: str = Form(""),
        display_name: str = Form(""),
        email: str = Form(""),
        roles: list[str] = Form(default=[]),
    ):
        require(identity, "admin:users")
        store = accounts()

        try:
            name = normalise_username(username)
        except PasswordPolicyError as exc:
            return _render_list(request, identity, error=str(exc), status_code=400)

        valid = {role.value for role in GRANTABLE_ROLES}
        requested = sorted({r for r in roles if r in valid})
        unknown = sorted(set(roles) - valid)
        if unknown:
            # Refused rather than dropped: a grant that silently does
            # nothing looks identical, on this page, to one that worked.
            return _render_list(
                request, identity, status_code=400,
                error=f"Unknown role(s): {', '.join(unknown)}.",
            )

        temporary = generate_temporary_password()
        conn = store.connect()
        try:
            created = user_store.create_user(
                conn, username=name,
                password_hash=hash_password(temporary, store.settings.key),
                created_by=identity.username,
                display_name=display_name.strip() or None,
                email=email.strip() or None,
                roles=requested,
                must_change_password=True,
            )
        finally:
            conn.close()

        if not created:
            return _render_list(
                request, identity, status_code=409,
                error=f"An account named {name} already exists. Accounts are disabled "
                      "rather than deleted, so a former colleague's account may still "
                      "hold that name - re-enable it instead of creating a second one.",
            )

        _audit(identity, "admin.user.created", name)
        for role in requested:
            _audit(identity, "admin.user.role.granted", f"{name}:{role}")
        return _render_list(
            request, identity, issued=(name, temporary),
            notice=f"Account {name} created.",
        )

    # ---- one account -----------------------------------------------------

    def _load(name: str):
        store = accounts()
        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            if user is None:
                raise HTTPException(status_code=404, detail="no such account")
            return user, user_store.active_session_count(conn, name), \
                user_store.count_active_admins(conn), user_store.is_locked(conn, name)
        finally:
            conn.close()

    def _render_one(request: Request, identity, name: str, error=None, notice=None,
                    issued=None, status_code: int = 200):
        user, sessions, admins, locked = _load(name)
        events = []
        reader = getattr(app.state, "reader", None)
        if reader is not None:
            try:
                # The account's own history, from the hash-chained audit
                # log rather than from a table an administrator could
                # edit. Best-effort: an audit sink that cannot be read
                # must not take out the page that lets an administrator
                # disable a compromised account.
                events = reader.read_audit_events(limit=25, actor=name)
            except Exception as exc:
                log.error("could not read audit history for an account: %s", exc)
        return page(
            request, "admin_user.html", identity, active="admin", account=user,
            grantable=GRANTABLE_ROLES, active_sessions=sessions, locked=locked,
            admin_count=admins, events=events, error=error, notice=notice,
            issued=issued, mfa_policy=accounts().settings.mfa, status_code=status_code,
        )

    @app.get("/admin/users/{name}", response_class=HTMLResponse)
    def user_detail(request: Request, name: str, identity=Depends(current_identity_dep)):
        require(identity, "admin:users")
        return _render_one(request, identity, name)

    @app.post("/admin/users/{name}/roles", response_class=HTMLResponse)
    def set_roles(request: Request, name: str, identity=Depends(current_identity_dep),
                  roles: list[str] = Form(default=[])):
        """Reconcile this account's grants to exactly the boxes ticked.

        A whole-set submission rather than one grant-or-revoke button per
        role: an administrator reasons about "what should this person be
        able to do", and a page that makes them apply six separate
        changes to express one decision is a page where the sixth gets
        forgotten.
        """
        require(identity, "admin:users")
        store = accounts()
        valid = {role.value for role in GRANTABLE_ROLES}
        wanted = {r for r in roles if r in valid}
        unknown = sorted(set(roles) - valid)
        if unknown:
            return _render_one(request, identity, name, status_code=400,
                               error=f"Unknown role(s): {', '.join(unknown)}.")

        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            if user is None:
                raise HTTPException(status_code=404, detail="no such account")
            held = set(user.roles)

            if (Role.ADMIN.value in held and Role.ADMIN.value not in wanted
                    and user.is_active
                    and user_store.count_active_admins(conn) <= 1):
                return _render_one(request, identity, name, error=LAST_ADMIN,
                                   status_code=400)

            for role in sorted(wanted - held):
                user_store.grant_role(conn, name, role, identity.username)
            for role in sorted(held - wanted):
                user_store.revoke_role(conn, name, role)
        finally:
            conn.close()

        for role in sorted(wanted - held):
            _audit(identity, "admin.user.role.granted", f"{name}:{role}")
        for role in sorted(held - wanted):
            _audit(identity, "admin.user.role.revoked", f"{name}:{role}")
        return _render_one(request, identity, name, notice="Roles updated.")

    @app.post("/admin/users/{name}/status", response_class=HTMLResponse)
    def set_status(request: Request, name: str, identity=Depends(current_identity_dep),
                   status: str = Form("")):
        require(identity, "admin:users")
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="status must be active or disabled")
        store = accounts()

        if status == "disabled" and name == identity.username:
            return _render_one(
                request, identity, name, status_code=400,
                error="You cannot disable your own account. Ask another administrator, "
                      "so that the platform is never left with nobody able to sign in.",
            )

        conn = store.connect()
        try:
            user = user_store.get_user(conn, name)
            if user is None:
                raise HTTPException(status_code=404, detail="no such account")
            if (status == "disabled" and Role.ADMIN.value in user.roles
                    and user_store.count_active_admins(conn) <= 1):
                return _render_one(request, identity, name, error=LAST_ADMIN,
                                   status_code=400)

            user_store.set_status(conn, name, status, identity.username)
            revoked = 0
            if status == "disabled":
                # THE POINT OF SERVER-SIDE SESSIONS. Without this,
                # "disabled" would mean "cannot sign in again", and
                # somebody dismissed at 09:00 would keep reading charts
                # until their cookie aged out.
                revoked = user_store.revoke_sessions_for_user(conn, name, "disabled")
        finally:
            conn.close()

        _audit(identity, "admin.user.disabled" if status == "disabled"
               else "admin.user.enabled", name)
        notice = ("Account disabled" + (f"; {revoked} active session(s) ended."
                                        if revoked else "."))
        if status == "active":
            notice = "Account re-enabled."
        return _render_one(request, identity, name, notice=notice)

    @app.post("/admin/users/{name}/password", response_class=HTMLResponse)
    def reset_password(request: Request, name: str,
                       identity=Depends(current_identity_dep)):
        require(identity, "admin:users")
        store = accounts()
        temporary = generate_temporary_password()
        conn = store.connect()
        try:
            if user_store.get_user(conn, name) is None:
                raise HTTPException(status_code=404, detail="no such account")
            user_store.set_password(
                conn, name, hash_password(temporary, store.settings.key),
                identity.username, must_change=True,
            )
            # Sessions opened with the old password end. A reset happens
            # because a credential is suspect or lost; leaving its
            # sessions running would make the reset cosmetic.
            user_store.revoke_sessions_for_user(conn, name, "password_change")
        finally:
            conn.close()

        _audit(identity, "admin.user.password.reset", name)
        return _render_one(
            request, identity, name, issued=(name, temporary),
            notice="Temporary password issued. It is shown once, here, and the user "
                   "must choose their own before they can do anything else.",
        )

    @app.post("/admin/users/{name}/mfa", response_class=HTMLResponse)
    def reset_mfa(request: Request, name: str, identity=Depends(current_identity_dep)):
        """Clear an enrolment, for the lost or replaced phone.

        There are no printed recovery codes in this system, deliberately:
        a sheet of one-time codes is a second credential the user has to
        store safely, and in a deployment small enough to have no
        identity provider it will be stored in the same drawer as the
        password. Administrator-mediated recovery instead - one person,
        identifiable in the audit log, who can be asked how they
        confirmed who they were talking to.
        """
        require(identity, "admin:users")
        store = accounts()
        conn = store.connect()
        try:
            if user_store.get_user(conn, name) is None:
                raise HTTPException(status_code=404, detail="no such account")
            user_store.set_mfa_secret(conn, name, None, identity.username)
            user_store.revoke_sessions_for_user(conn, name, "admin")
        finally:
            conn.close()

        _audit(identity, "admin.user.mfa.reset", name)
        return _render_one(
            request, identity, name,
            notice="Second-factor enrolment cleared. The user will be asked to enrol "
                   "a new authenticator the next time they sign in.",
        )

    @app.post("/admin/users/{name}/unlock", response_class=HTMLResponse)
    def unlock(request: Request, name: str, identity=Depends(current_identity_dep)):
        require(identity, "admin:users")
        store = accounts()
        conn = store.connect()
        try:
            if user_store.get_user(conn, name) is None:
                raise HTTPException(status_code=404, detail="no such account")
            user_store.unlock(conn, name, identity.username)
        finally:
            conn.close()
        _audit(identity, "admin.user.unlocked", name)
        return _render_one(request, identity, name, notice="Lockout cleared.")

    @app.post("/admin/users/{name}/sessions", response_class=HTMLResponse)
    def revoke_sessions(request: Request, name: str,
                        identity=Depends(current_identity_dep)):
        require(identity, "admin:users")
        store = accounts()
        conn = store.connect()
        try:
            if user_store.get_user(conn, name) is None:
                raise HTTPException(status_code=404, detail="no such account")
            revoked = user_store.revoke_sessions_for_user(conn, name, "admin")
        finally:
            conn.close()
        _audit(identity, "admin.sessions.revoked", name)
        return _render_one(request, identity, name,
                           notice=f"{revoked} active session(s) ended.")
# Made by Ryan Gomez & Co. Inc.
