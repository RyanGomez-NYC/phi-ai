# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Command line account administration: `python -m core.web.useradmin`.

WHY THIS EXISTS AT ALL, when core/web/admin_routes.py already administers
accounts through the interface: something has to create the FIRST one.
A deployment with local accounts enabled and no accounts created has no
way in - the sign-in page cannot help, and there is no default
credential, because a default credential on a PHI system is a published
credential. So the first administrator is created here, on the host, by
somebody who already holds the database credentials and can already read
the configuration. That is the same trust boundary the bootstrap SQL
assumes, not a new one.

It is also the way back in. An administrator who loses their second
factor, or the only administrator who leaves, or an account locked out at
2am - each has a fix here that does not require another administrator to
exist. `reset-password`, `reset-mfa` and `unlock` are deliberately the
same operations the web interface offers, calling the same functions in
core/db/users.py, so the two cannot drift into disagreeing about what a
reset does.

WHAT IT DOES NOT WRITE: the audit trail. Every operation here IS
recorded, through the same hash-chained audit log the interface uses -
see _audit() below. A command line path that skipped the audit would be
an unaudited way to grant somebody access to PHI, which is precisely the
hole the audit log exists to close. The actor recorded is
`cli:<OS user>`, so an entry from here is distinguishable from the same
action taken in the interface.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

from core.web.local_auth import (
    LocalAuthConfigurationError,
    LocalAuthSettings,
    PasswordPolicyError,
    check_password_policy,
    hash_password,
    normalise_username,
)


def _connect():
    """One database connection, as the same role the web interface uses.

    Deliberately the reader role rather than a dedicated administrative
    one: on GCP a Postgres username IS the service account's email
    (deploy/gcp/database.tf), so a `phi_ai_useradmin` role is not
    portable across the three clouds - the identical constraint that put
    roi_requests on the reader role. See core/db/bootstrap_gcp.sql.
    """
    from core.config.settings import Settings
    from core.db.connection import connect

    settings = Settings.from_env()
    if not settings.db_reader_username:
        raise SystemExit(
            "PHI_AI_DB_READER_USERNAME is not set. Local accounts live in the same "
            "Postgres the web interface reads - see runbooks/RUNBOOK_LOCAL_USERS.md."
        )
    return connect(settings, settings.db_reader_username), settings


def _audit_sink(settings):
    from core.audit.log import AuditLog
    from core.storage.factory import build_audit_sink

    sink = build_audit_sink(settings)
    return AuditLog(sink=sink, last_known_hash=sink.last_hash())


def _audit(audit, action: str, username: str) -> None:
    actor = f"cli:{os.environ.get('USER') or os.environ.get('LOGNAME') or 'unknown'}"
    audit.record(actor=actor, action=action, resource_key=f"user/{username}",
                 purpose_of_use=None)


def _read_password(prompt: str, username: str) -> str:
    """Ask twice, check the policy, never echo.

    Refuses rather than warns: a command that accepts a weak password
    with a printed caution is a command whose caution is scrolled past.
    """
    while True:
        first = getpass.getpass(prompt)
        second = getpass.getpass("Again: ")
        if first != second:
            print("They do not match. Try again.", file=sys.stderr)
            continue
        try:
            check_password_policy(first, username=username)
        except PasswordPolicyError as exc:
            print(f"{exc}", file=sys.stderr)
            continue
        return first


def _issue_password(username: str, generate: bool) -> tuple[str, bool]:
    """Return (password, was_generated)."""
    if generate:
        from core.web.admin_routes import generate_temporary_password

        return generate_temporary_password(), True
    return _read_password("New password: ", username), False


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.web.useradmin",
        description="Administer local accounts for deployments with no identity "
                    "provider. See runbooks/RUNBOOK_LOCAL_USERS.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create an account")
    p_create.add_argument("username")
    p_create.add_argument("--roles", default="",
                          help="comma-separated, e.g. admin,him. See core/web/auth.py.")
    p_create.add_argument("--display-name", default=None)
    p_create.add_argument("--email", default=None)
    p_create.add_argument("--generate-password", action="store_true",
                          help="print a generated temporary password instead of prompting")

    p_reset = sub.add_parser("reset-password", help="issue a new password")
    p_reset.add_argument("username")
    p_reset.add_argument("--generate-password", action="store_true")

    for name, help_text in (
        ("reset-mfa", "clear a second-factor enrolment (lost or replaced device)"),
        ("unlock", "clear a failed-attempt lockout"),
        ("disable", "end all sessions and refuse further sign-in"),
        ("enable", "re-enable a disabled account"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("username")

    p_role = sub.add_parser("grant", help="grant one role")
    p_role.add_argument("username")
    p_role.add_argument("role")
    p_revoke = sub.add_parser("revoke", help="revoke one role")
    p_revoke.add_argument("username")
    p_revoke.add_argument("role")

    sub.add_parser("list", help="list accounts")

    args = parser.parse_args(argv)

    try:
        auth_settings = LocalAuthSettings.from_env()
    except LocalAuthConfigurationError as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1

    from core.db import users as user_store

    conn, settings = _connect()
    audit = _audit_sink(settings)
    try:
        if args.command == "list":
            users = user_store.list_users(conn)
            if not users:
                print("No accounts. Create the first administrator with:\n"
                      "  python -m core.web.useradmin create <username> --roles admin")
                return 0
            width = max(len(u.username) for u in users)
            for user in users:
                mfa = "mfa" if user.mfa_enrolled else "no-mfa"
                print(f"{user.username:<{width}}  {user.status:<8}  {mfa:<6}  "
                      f"{','.join(sorted(user.roles)) or '(no roles)'}")
            return 0

        try:
            username = normalise_username(args.username)
        except PasswordPolicyError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        if args.command == "create":
            roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()]
            password, generated = _issue_password(username, args.generate_password)
            created = user_store.create_user(
                conn, username=username,
                password_hash=hash_password(password, auth_settings.key),
                created_by=f"cli:{os.environ.get('USER', 'unknown')}",
                display_name=args.display_name, email=args.email, roles=roles,
                # True either way. A password an administrator typed at a
                # terminal is a password an administrator knows, which is
                # not a credential belonging to one person - and unique
                # user identification (45 CFR 164.312(a)(2)(i)) is the
                # whole reason these accounts exist.
                must_change_password=True,
            )
            if not created:
                print(f"An account named {username} already exists.", file=sys.stderr)
                return 1
            _audit(audit, "admin.user.created", username)
            for role in roles:
                _audit(audit, "admin.user.role.granted", f"{username}:{role}")
            if generated:
                print(f"Created {username}. Temporary password: {password}")
            else:
                print(f"Created {username}.")
            print("They must change it at first sign-in, and "
                  f"{'will be asked to enrol an authenticator' if auth_settings.mfa == 'required' else 'may enrol an authenticator'}.")
            return 0

        if user_store.get_user(conn, username) is None:
            print(f"No account named {username}.", file=sys.stderr)
            return 1

        actor = f"cli:{os.environ.get('USER', 'unknown')}"

        if args.command == "reset-password":
            password, generated = _issue_password(username, args.generate_password)
            user_store.set_password(
                conn, username, hash_password(password, auth_settings.key), actor,
                must_change=True,
            )
            ended = user_store.revoke_sessions_for_user(conn, username, "password_change")
            _audit(audit, "admin.user.password.reset", username)
            if generated:
                print(f"Temporary password for {username}: {password}")
            print(f"{ended} active session(s) ended. They must choose a new password at "
                  "next sign-in.")
            return 0

        if args.command == "reset-mfa":
            user_store.set_mfa_secret(conn, username, None, actor)
            user_store.revoke_sessions_for_user(conn, username, "admin")
            _audit(audit, "admin.user.mfa.reset", username)
            print(f"Second-factor enrolment cleared for {username}.")
            return 0

        if args.command == "unlock":
            user_store.unlock(conn, username, actor)
            _audit(audit, "admin.user.unlocked", username)
            print(f"Lockout cleared for {username}.")
            return 0

        if args.command in ("disable", "enable"):
            status = "disabled" if args.command == "disable" else "active"
            if status == "disabled":
                user_store.set_status(conn, username, status, actor)
                ended = user_store.revoke_sessions_for_user(conn, username, "disabled")
                _audit(audit, "admin.user.disabled", username)
                print(f"{username} disabled; {ended} active session(s) ended.")
            else:
                user_store.set_status(conn, username, status, actor)
                _audit(audit, "admin.user.enabled", username)
                print(f"{username} re-enabled.")
            return 0

        if args.command in ("grant", "revoke"):
            from core.web.auth import Role

            role = args.role.strip().lower()
            if role not in {r.value for r in Role}:
                print(f"Unknown role {role!r}. Known roles: "
                      f"{', '.join(sorted(r.value for r in Role))}", file=sys.stderr)
                return 1
            if args.command == "grant":
                changed = user_store.grant_role(conn, username, role, actor)
                _audit(audit, "admin.user.role.granted", f"{username}:{role}")
            else:
                changed = user_store.revoke_role(conn, username, role)
                _audit(audit, "admin.user.role.revoked", f"{username}:{role}")
            print(f"{'Applied' if changed else 'No change - already in that state'}.")
            return 0

        parser.error(f"unhandled command {args.command}")  # pragma: no cover
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
