# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Storage for local user accounts, roles and sessions (core/db/users_schema.sql).

Only reachable when a deployment enabled local accounts - see
core/web/local_auth.py for why that is an opt-in exception rather than
the recommended shape, and core/db/users_schema.sql for what these tables
hold and deliberately do not.

CONNECTIONS ARE NOT ALWAYS psycopg, exactly as core/db/index.py's own
docstring states: psycopg on AWS/Azure, pg8000 on GCP. This module
therefore uses only the DB-API 2.0 surface both drivers guarantee -
cursor()/execute()/commit()/rollback()/fetchone()/fetchall()/description/
close() - closes cursors explicitly in `finally` rather than relying on
`with conn.cursor()` (DB-API does not promise cursors are context
managers), and classifies errors through core/db/pg_errors.py instead of
any driver's exception classes. That is not stylistic: catching
psycopg's UniqueViolation by class is precisely the bug that broke the
resource index on GCP.

TIME COMES FROM THE DATABASE, never from this process. Every timestamp
written here is now(), and every expiry comparison happens in SQL. A
lockout that expires according to the web container's clock is a lockout
that two containers disagree about, and "your session expired" is not a
thing to decide with a clock that can drift.

NO DELETE ON authn.local_users, in this module or in any bootstrap
file's grants. Accounts are disabled. See users_schema.sql's header for
why an account row that can disappear breaks the attributability of
every audit entry naming it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from core.db.pg_errors import is_unique_violation

# Every column of authn.local_users that anything outside this module
# reads, in one place, so a SELECT cannot drift from the LocalUser
# dataclass below. The qualified form is not cosmetic: resolve_session()
# joins local_sessions, which has its own `username`, and an unqualified
# list there fails outright with "column reference is ambiguous" -
# proven against live PostgreSQL 16.
_USER_FIELDS = (
    "username", "display_name", "email", "password_hash", "password_changed_at",
    "must_change_password", "mfa_secret", "mfa_enrolled_at", "mfa_last_step",
    "status", "failed_attempts", "locked_until", "last_login_at", "created_at",
    "created_by", "updated_at", "updated_by",
)
_USER_COLUMNS = ", ".join(_USER_FIELDS)
_USER_COLUMNS_Q = ", ".join(f"u.{name}" for name in _USER_FIELDS)


@dataclass(frozen=True)
class LocalUser:
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    password_hash: str = ""
    password_changed_at: Optional[datetime] = None
    must_change_password: bool = True
    mfa_secret: Optional[str] = None
    mfa_enrolled_at: Optional[datetime] = None
    mfa_last_step: Optional[int] = None
    status: str = "active"
    failed_attempts: int = 0
    locked_until: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    created_by: str = ""
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def mfa_enrolled(self) -> bool:
        return bool(self.mfa_secret)

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def _rows(cur) -> list[dict]:
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_user(conn: Any, username: str) -> Optional[LocalUser]:
    """One account with its role grants, or None.

    `locked_until` is returned as stored; whether the lock is still in
    force is answered by is_locked() below, in SQL, against the
    database's clock.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {_USER_COLUMNS} FROM authn.local_users WHERE username = %s",
            (username,),
        )
        rows = _rows(cur)
        if not rows:
            return None
        cur.execute(
            "SELECT role FROM authn.local_user_roles WHERE username = %s ORDER BY role",
            (username,),
        )
        roles = frozenset(row[0] for row in cur.fetchall())
    finally:
        cur.close()
    return LocalUser(**rows[0], roles=roles)


def list_users(conn: Any) -> list[LocalUser]:
    """Every account, with roles, for the administration page.

    Two queries and a join in Python rather than one query with an
    aggregate: string_agg over a LEFT JOIN would work on Postgres and
    would also be the only place in this project that depended on it,
    for a table whose realistic size is tens of rows.
    """
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {_USER_COLUMNS} FROM authn.local_users ORDER BY username")
        rows = _rows(cur)
        cur.execute("SELECT username, role FROM authn.local_user_roles")
        grants: dict[str, set[str]] = {}
        for username, role in cur.fetchall():
            grants.setdefault(username, set()).add(role)
    finally:
        cur.close()
    return [LocalUser(**row, roles=frozenset(grants.get(row["username"], ()))) for row in rows]


def is_locked(conn: Any, username: str) -> bool:
    """Whether a failed-attempt lockout is still in force, per the database clock."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT locked_until IS NOT NULL AND locked_until > now() "
            "FROM authn.local_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        return bool(row and row[0])
    finally:
        cur.close()


def count_active_admins(conn: Any) -> int:
    """Active accounts holding the admin role.

    Used to refuse the change that locks everybody out: removing the last
    administrator's admin role, or disabling the last administrator,
    leaves a deployment nobody can administer and no way back in except
    the command line. Refusing is kinder than the recovery.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT count(*) FROM authn.local_users u "
            "JOIN authn.local_user_roles r ON r.username = u.username "
            "WHERE r.role = 'admin' AND u.status = 'active'"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------

def create_user(
    conn: Any,
    *,
    username: str,
    password_hash: str,
    created_by: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    roles=(),
    must_change_password: bool = True,
) -> bool:
    """Create an account and its initial role grants in ONE transaction.

    Returns False if the username already exists - a duplicate is a
    normal outcome of an administrator submitting a form twice, not an
    error to raise at them. Classified through pg_errors.is_unique_violation()
    for the same cross-driver reason core/db/index.py documents.

    Atomic on purpose: a user created without their roles is an account
    that exists, can sign in, and can do nothing, which looks to
    everyone involved like the system is broken rather than like the
    second half of the form failed.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO authn.local_users
                    (username, display_name, email, password_hash, created_by,
                     updated_by, must_change_password)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (username, display_name, email, password_hash, created_by,
                 created_by, must_change_password),
            )
            for role in sorted(set(roles)):
                cur.execute(
                    "INSERT INTO authn.local_user_roles (username, role, granted_by) "
                    "VALUES (%s, %s, %s)",
                    (username, role, created_by),
                )
        finally:
            cur.close()
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        if is_unique_violation(exc):
            return False
        raise


def set_password(conn: Any, username: str, password_hash: str, actor: str,
                 must_change: bool = False) -> None:
    """Replace an account's password hash.

    Does NOT revoke that user's sessions - the caller decides, because
    the two cases differ: a user changing their own password from inside
    a session should keep it, and an administrator resetting somebody
    else's should not. See core/web/admin_routes.py and
    core/web/login_routes.py, each of which calls
    revoke_sessions_for_user() explicitly or deliberately does not.
    """
    _update(
        conn,
        "SET password_hash = %s, password_changed_at = now(), "
        "must_change_password = %s, failed_attempts = 0, locked_until = NULL",
        (password_hash, must_change),
        username, actor,
    )


def set_status(conn: Any, username: str, status: str, actor: str) -> None:
    if status not in ("active", "disabled"):
        raise ValueError(f"unknown account status {status!r}")
    # Re-enabling clears the lockout: an administrator turning an account
    # back on means it should work, not that it should work in fourteen
    # more minutes.
    _update(
        conn,
        "SET status = %s, failed_attempts = 0, locked_until = NULL",
        (status,), username, actor,
    )


def _update(conn: Any, set_clause: str, params: tuple, username: str,
            actor: Optional[str]) -> None:
    """Every mutation of authn.local_users goes through here.

    One place that always stamps updated_at/updated_by and always rolls
    back on failure - the same reason core/db/index.py's write functions
    each roll back explicitly: a failed statement leaves the transaction
    aborted, and this connection is reused for the rest of the request.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE authn.local_users {set_clause}, updated_at = now(), "
                "updated_by = %s WHERE username = %s",
                (*params, actor, username),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def grant_role(conn: Any, username: str, role: str, actor: str) -> bool:
    """Grant one role. False if the user already held it.

    The role name is checked by ck_local_user_roles_role in the schema
    against the same list as core/web/auth.py's Role enum - a typo here
    is a constraint violation rather than a grant that silently confers
    nothing.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO authn.local_user_roles (username, role, granted_by) "
                "VALUES (%s, %s, %s)",
                (username, role, actor),
            )
        finally:
            cur.close()
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        if is_unique_violation(exc):
            return False
        raise


def revoke_role(conn: Any, username: str, role: str) -> bool:
    """Remove one role grant. False if it was not held."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM authn.local_user_roles WHERE username = %s AND role = %s",
                (username, role),
            )
            removed = cur.rowcount > 0
        finally:
            cur.close()
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Sign-in throttling
# ---------------------------------------------------------------------------

def record_failure(conn: Any, username: str, max_failures: int,
                   lockout_minutes: int) -> bool:
    """Count one failed attempt; lock the account if it hit the ceiling.

    Returns True if this attempt caused a lock, so the caller can audit
    the lockout as its own event rather than as another failure.

    The count and the comparison both happen inside one UPDATE, against
    the database's own value - read-then-write from the application
    would let two simultaneous guesses each read 4, each write 5, and
    the ceiling would be worth one extra attempt per concurrent request.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                UPDATE authn.local_users
                   SET failed_attempts = failed_attempts + 1,
                       locked_until = CASE
                           WHEN failed_attempts + 1 >= %s
                           THEN now() + (%s::int * INTERVAL '1 minute')
                           ELSE locked_until
                       END,
                       updated_at = now()
                 WHERE username = %s
             RETURNING failed_attempts >= %s
                """,
                (max_failures, lockout_minutes, username, max_failures),
            )
            row = cur.fetchone()
            locked = bool(row and row[0])
        finally:
            cur.close()
        conn.commit()
        return locked
    except Exception:
        conn.rollback()
        raise


def record_success(conn: Any, username: str) -> None:
    """Clear the failure counter and stamp the sign-in."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE authn.local_users SET failed_attempts = 0, locked_until = NULL, "
                "last_login_at = now() WHERE username = %s",
                (username,),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def unlock(conn: Any, username: str, actor: str) -> None:
    _update(conn, "SET failed_attempts = 0, locked_until = NULL", (), username, actor)


# ---------------------------------------------------------------------------
# MFA enrolment
# ---------------------------------------------------------------------------

def set_mfa_secret(conn: Any, username: str, encrypted_secret: Optional[str],
                   actor: str) -> None:
    """Store or clear a TOTP enrolment.

    Clearing also clears mfa_last_step: the replay guard is a property of
    one shared secret, and carrying a step count across a re-enrolment
    would reject the new authenticator's first several codes for no
    reason anybody could diagnose.
    """
    _update(
        conn,
        # %s::text, not a bare %s: Postgres cannot infer a type for a
        # parameter whose only use is `IS NULL`, and rejects the
        # statement with "could not determine data type of parameter"
        # rather than running it. Proven against live PostgreSQL 16.
        "SET mfa_secret = %s, mfa_enrolled_at = CASE WHEN %s::text IS NULL THEN NULL "
        "ELSE now() END, mfa_last_step = NULL",
        (encrypted_secret, encrypted_secret), username, actor,
    )


def record_mfa_step(conn: Any, username: str, step: int) -> bool:
    """Consume a TOTP time step, refusing a replay.

    The guard is the WHERE clause, not a prior read: two requests
    presenting the same code race, both read a lower step, and both would
    be accepted if this were checked in Python. Returns False when the
    step was already used, which the caller must treat as a failed
    authentication.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE authn.local_users SET mfa_last_step = %s WHERE username = %s "
                "AND (mfa_last_step IS NULL OR mfa_last_step < %s)",
                (step, username, step),
            )
            accepted = cur.rowcount > 0
        finally:
            cur.close()
        conn.commit()
        return accepted
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(conn: Any, session_id: str, username: str,
                   lifetime_minutes: int) -> None:
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO authn.local_sessions (session_id, username, expires_at) "
                "VALUES (%s, %s, now() + (%s::int * INTERVAL '1 minute'))",
                (session_id, username, lifetime_minutes),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def resolve_session(conn: Any, session_id: str) -> Optional[LocalUser]:
    """The live account behind a session cookie, or None.

    ONE query answers every reason a session can be dead - revoked,
    expired, or belonging to an account an administrator has since
    disabled - because they are all the same answer to the caller and
    checking them separately invites checking only some of them. Roles
    come from the grant table on every call, never from the cookie, so a
    role change lands on the user's next request.

    Deliberately does NOT update last_seen_at: that would be a write on
    every request, on every page, for a column nothing enforces. See
    touch_session(), which the sign-in path calls.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT {_USER_COLUMNS_Q}
              FROM authn.local_sessions s
              JOIN authn.local_users u ON u.username = s.username
             WHERE s.session_id = %s
               AND s.revoked_at IS NULL
               AND s.expires_at > now()
               AND u.status = 'active'
            """,
            (session_id,),
        )
        rows = _rows(cur)
        if not rows:
            return None
        cur.execute(
            "SELECT role FROM authn.local_user_roles WHERE username = %s ORDER BY role",
            (rows[0]["username"],),
        )
        roles = frozenset(row[0] for row in cur.fetchall())
    finally:
        cur.close()
    return LocalUser(**rows[0], roles=roles)


def touch_session(conn: Any, session_id: str) -> None:
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE authn.local_sessions SET last_seen_at = now() WHERE session_id = %s",
                (session_id,),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def revoke_session(conn: Any, session_id: str, reason: str) -> None:
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE authn.local_sessions SET revoked_at = now(), revoked_reason = %s "
                "WHERE session_id = %s AND revoked_at IS NULL",
                (reason, session_id),
            )
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def revoke_sessions_for_user(conn: Any, username: str, reason: str) -> int:
    """End every live session for one account. Returns how many.

    This is what makes disabling an account mean something immediately,
    and it is why sessions are rows rather than signed cookies at all.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE authn.local_sessions SET revoked_at = now(), revoked_reason = %s "
                "WHERE username = %s AND revoked_at IS NULL AND expires_at > now()",
                (reason, username),
            )
            count = cur.rowcount
        finally:
            cur.close()
        conn.commit()
        return max(count, 0)
    except Exception:
        conn.rollback()
        raise


def active_session_count(conn: Any, username: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT count(*) FROM authn.local_sessions WHERE username = %s "
            "AND revoked_at IS NULL AND expires_at > now()",
            (username,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        cur.close()


def purge_expired_sessions(conn: Any, older_than_days: int = 7) -> int:
    """Delete session rows that can no longer answer any question.

    The one DELETE in this module, and it is on the one table here that
    is not a record of anything: who signed in and when is in the audit
    log, hash-chained. A session row past its expiry is a cookie that
    cannot work, kept only so nothing is deleted out from under a
    request in flight - hence the grace period rather than `now()`.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM authn.local_sessions "
                "WHERE expires_at < now() - (%s::int * INTERVAL '1 day')",
                (older_than_days,),
            )
            count = cur.rowcount
        finally:
            cur.close()
        conn.commit()
        return max(count, 0)
    except Exception:
        conn.rollback()
        raise
# Made by Ryan Gomez & Co. Inc.
