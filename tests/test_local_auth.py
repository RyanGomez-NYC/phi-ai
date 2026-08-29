# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for local accounts (core/web/local_auth.py, core/db/users.py,
core/web/login_routes.py, core/web/admin_routes.py).

WEIGHTED TOWARD THE SECURITY BOUNDARY, like tests/test_web.py, because
this is the one place in the project that verifies a credential. The
properties worth guarding, each with a test below:

  - An unauthenticated browser reaches the login page and nothing else.
  - A wrong password, an unknown user, a disabled account and a locked
    account are indistinguishable in the response.
  - Failures count toward a lockout, and every outcome is audited.
  - A TOTP code works once. The same code, replayed inside its own
    window, does not.
  - Disabling an account, revoking a session, or changing a role takes
    effect on the user's NEXT REQUEST - not their next sign-in. This is
    the property that server-side session rows exist for, and the reason
    a signed-cookie session would not have been good enough.
  - An administrator cannot leave the platform with no administrator.

ENVIRONMENT VARIABLES ARE SET UNDER THE PHI_AI_ PREFIX, which is the
only one there is: core/config/settings.py's env_var() resolves
ENV_PREFIX + suffix and reads nothing else, with no fallback spelling.
That is what lets a delenv() below genuinely establish that a setting is
off, which is what two of the mutual-exclusion tests depend on.

WHAT THE FAKE STORE IS, AND WHAT IT IS NOT. `_MemoryAccounts` below
implements the same functions as core/db/users.py and enforces the same
constraints the schema does - unique usernames, role names from the
enum's own list, sessions that stop resolving when revoked or expired or
when their account is disabled, a monotonic MFA step. It is a realistic
stub, not a database: the SQL in core/db/users.py is verified separately
against live PostgreSQL 16 running the exact grants in
core/db/users_bootstrap_aws.sql (see runbooks/RUNBOOK_LOCAL_USERS.md, "How
this was verified"), because CI has no Postgres. That split is a KNOWN
GAP and is stated there rather than implied by a green test run here.
test_fake_store_matches_the_real_modules_surface below is what stops the
two drifting.
"""

import base64
import inspect
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import users as real_user_store  # noqa: E402
from core.db.users import LocalUser  # noqa: E402
from core.web import admin_routes, login_routes  # noqa: E402
from core.web import local_auth as la  # noqa: E402
from core.web.app import create_app  # noqa: E402
from core.web.auth import AuthConfigurationError, AuthSettings, Role  # noqa: E402

KEY = la.load_key(base64.b64encode(b"local-auth-test-key-32-bytes!!!!").decode())
GOOD_PASSWORD = "seven mackerel lantern drift"


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The credential primitives
# ---------------------------------------------------------------------------

def test_a_password_verifies_only_against_the_key_that_hashed_it():
    """The pepper is the whole reason a stolen database is not a password
    list. A hash that verified without the deployment key would mean it
    was never applied."""
    stored = la.hash_password(GOOD_PASSWORD, KEY)
    assert la.verify_password(GOOD_PASSWORD, stored, KEY)
    assert not la.verify_password("something else entirely", stored, KEY)

    other = la.load_key(base64.b64encode(b"a-completely-different-key-32byt").decode())
    with pytest.raises(la.KeyMismatchError, match="different"):
        la.verify_password(GOOD_PASSWORD, stored, other)


def test_the_key_fingerprint_names_the_problem_rather_than_failing_silently():
    """A rotated or lost key fails EVERY login at once. The difference
    between an error naming that and a wave of 'wrong password' is
    whether an operator can diagnose it at all."""
    stored = la.hash_password(GOOD_PASSWORD, KEY)
    other = la.load_key("00" * 32)
    with pytest.raises(la.KeyMismatchError) as exc:
        la.verify_password(GOOD_PASSWORD, stored, other)
    # la.KEY_ENV, never a literal: it is built from settings.ENV_PREFIX,
    # so it already reads PHI_AI_WEB_LOCAL_AUTH_KEY and follows any
    # future prefix change without this file being edited.
    assert la.KEY_ENV in str(exc.value)
    assert la.key_fingerprint(KEY) in str(exc.value)


def test_a_missing_or_short_key_refuses_to_start():
    with pytest.raises(la.LocalAuthConfigurationError, match=la.KEY_ENV):
        la.load_key("")
    with pytest.raises(la.LocalAuthConfigurationError, match="32 bytes"):
        la.load_key(base64.b64encode(b"too short").decode())


@pytest.mark.parametrize("bad,why", [
    ("short", "under the length floor"),
    ("passw0rd", "under the length floor"),
    ("password2026!!", "a blocked word with padding"),
    ("aaaaaaaaaaaaaaaa", "too few distinct characters"),
    ("  spaced out passphrase  ", "leading/trailing space"),
    ("alice-alice-alice", "contains the username"),
])
def test_the_password_policy_refuses_what_it_should(bad, why):
    with pytest.raises(la.PasswordPolicyError):
        la.check_password_policy(bad, username="alice")


def test_the_password_policy_accepts_a_long_passphrase_with_no_symbols():
    """NIST SP 800-63B advises against composition rules. A policy that
    demanded a digit and a symbol here would be rejecting the strongest
    thing a person is likely to actually remember."""
    la.check_password_policy("correct manatee lantern drift", username="alice")


@pytest.mark.parametrize("unix_time,expected", [
    (59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
    (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130"),
])
def test_totp_matches_the_rfc_6238_test_vectors(unix_time, expected):
    """RFC 6238 Appendix B, SHA-1 rows, with that document's own
    "12345678901234567890" seed. An implementation that agrees with the
    RFC agrees with every authenticator app; one verified only against
    itself agrees with nothing."""
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert la._totp_at(secret, unix_time // 30) == expected


def test_a_totp_code_cannot_be_replayed_within_its_own_window():
    secret = la.generate_totp_secret()
    now = 1_700_000_000
    code = la._totp_at(secret, now // 30)
    step = la.verify_totp(secret, code, now=now)
    assert step == now // 30
    assert la.verify_totp(secret, code, now=now, last_used_step=step) is None


def test_an_mfa_secret_round_trips_and_is_bound_to_the_key():
    secret = la.generate_totp_secret()
    stored = la.encrypt_secret(secret, KEY)
    assert secret not in stored, "the shared secret is stored in the clear"
    assert la.decrypt_secret(stored, KEY) == secret
    with pytest.raises(la.KeyMismatchError):
        la.decrypt_secret(stored, la.load_key("11" * 32))


# ---------------------------------------------------------------------------
# Configuration refuses to guess, exactly as it does for the proxy
# ---------------------------------------------------------------------------

def test_local_accounts_and_proxy_trust_cannot_both_be_enabled(monkeypatch):
    """Two front doors, one of which the organisation does not audit."""
    monkeypatch.setenv("PHI_AI_WEB_TRUST_PROXY_AUTH", "true")
    monkeypatch.setenv("PHI_AI_WEB_LOCAL_ACCOUNTS", "true")
    monkeypatch.delenv("PHI_AI_WEB_DEV_IDENTITY", raising=False)
    with pytest.raises(AuthConfigurationError, match="both set"):
        AuthSettings.from_env()


def test_local_accounts_and_the_dev_identity_cannot_both_be_enabled(monkeypatch):
    monkeypatch.delenv("PHI_AI_WEB_TRUST_PROXY_AUTH", raising=False)
    monkeypatch.setenv("PHI_AI_WEB_LOCAL_ACCOUNTS", "true")
    monkeypatch.setenv("PHI_AI_WEB_DEV_IDENTITY", "someone:admin")
    with pytest.raises(AuthConfigurationError, match="both set"):
        AuthSettings.from_env()


def test_local_accounts_alone_is_a_complete_deployment_shape(monkeypatch):
    monkeypatch.delenv("PHI_AI_WEB_TRUST_PROXY_AUTH", raising=False)
    monkeypatch.delenv("PHI_AI_WEB_DEV_IDENTITY", raising=False)
    monkeypatch.setenv("PHI_AI_WEB_LOCAL_ACCOUNTS", "true")
    settings = AuthSettings.from_env()
    assert settings.local_accounts and not settings.trust_proxy_headers


def test_creating_the_app_without_an_account_store_refuses_to_start():
    """Otherwise every sign-in 503s at request time, and the cause is
    invisible from the failure."""
    settings = AuthSettings(trust_proxy_headers=False, local_accounts=True)
    with pytest.raises(RuntimeError, match="account store"):
        create_app(reader=_FakeReader(), auth_settings=settings, audit=_RecordingAudit())


def test_mfa_off_is_allowed_but_must_be_asked_for(monkeypatch):
    monkeypatch.setenv("PHI_AI_WEB_LOCAL_AUTH_MFA", "nonsense")
    monkeypatch.setenv(la.KEY_ENV, base64.b64encode(b"k" * 32).decode())
    with pytest.raises(la.LocalAuthConfigurationError, match="required/optional/off"):
        la.LocalAuthSettings.from_env()


# ---------------------------------------------------------------------------
# A realistic account store
# ---------------------------------------------------------------------------

class _MemoryAccounts:
    """core/db/users.py's surface, enforcing the schema's constraints.

    See this module's docstring for why this is a stub and where the real
    SQL is proven. Every rule below exists because the database enforces
    it: the unique username is the primary key, the role list is
    ck_local_user_roles_role, and resolve_session's four conditions are
    the four in that query's WHERE clause.
    """

    VALID_ROLES = frozenset(role.value for role in Role)

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.roles: dict[str, set] = {}
        self.sessions: dict[str, dict] = {}

    # -- reads
    def _build(self, name):
        row = dict(self.users[name])
        return LocalUser(**row, roles=frozenset(self.roles.get(name, set())))

    def get_user(self, conn, username):
        return self._build(username) if username in self.users else None

    def list_users(self, conn):
        return [self._build(name) for name in sorted(self.users)]

    def is_locked(self, conn, username):
        until = self.users[username]["locked_until"]
        return bool(until and until > _now())

    def count_active_admins(self, conn):
        return sum(1 for name, row in self.users.items()
                   if row["status"] == "active" and "admin" in self.roles.get(name, set()))

    # -- lifecycle
    def create_user(self, conn, *, username, password_hash, created_by,
                    display_name=None, email=None, roles=(), must_change_password=True):
        if username in self.users:
            return False  # the primary key
        for role in roles:
            assert role in self.VALID_ROLES, "ck_local_user_roles_role would reject this"
        self.users[username] = {
            "username": username, "display_name": display_name, "email": email,
            "password_hash": password_hash, "password_changed_at": _now(),
            "must_change_password": must_change_password, "mfa_secret": None,
            "mfa_enrolled_at": None, "mfa_last_step": None, "status": "active",
            "failed_attempts": 0, "locked_until": None, "last_login_at": None,
            "created_at": _now(), "created_by": created_by, "updated_at": _now(),
            "updated_by": created_by,
        }
        self.roles[username] = set(roles)
        return True

    def set_password(self, conn, username, password_hash, actor, must_change=False):
        self.users[username].update(
            password_hash=password_hash, password_changed_at=_now(),
            must_change_password=must_change, failed_attempts=0, locked_until=None,
            updated_by=actor,
        )

    def set_status(self, conn, username, status, actor):
        assert status in ("active", "disabled"), "ck_local_users_status"
        self.users[username].update(status=status, failed_attempts=0,
                                    locked_until=None, updated_by=actor)

    def grant_role(self, conn, username, role, actor):
        assert role in self.VALID_ROLES, "ck_local_user_roles_role would reject this"
        held = self.roles.setdefault(username, set())
        if role in held:
            return False
        held.add(role)
        return True

    def revoke_role(self, conn, username, role):
        held = self.roles.setdefault(username, set())
        if role not in held:
            return False
        held.discard(role)
        return True

    # -- throttling
    def record_failure(self, conn, username, max_failures, lockout_minutes):
        row = self.users[username]
        row["failed_attempts"] += 1
        if row["failed_attempts"] >= max_failures:
            row["locked_until"] = _now() + timedelta(minutes=lockout_minutes)
            return True
        return False

    def record_success(self, conn, username):
        self.users[username].update(failed_attempts=0, locked_until=None,
                                    last_login_at=_now())

    def unlock(self, conn, username, actor):
        self.users[username].update(failed_attempts=0, locked_until=None, updated_by=actor)

    # -- mfa
    def set_mfa_secret(self, conn, username, encrypted_secret, actor):
        self.users[username].update(
            mfa_secret=encrypted_secret,
            mfa_enrolled_at=_now() if encrypted_secret else None,
            mfa_last_step=None, updated_by=actor,
        )

    def record_mfa_step(self, conn, username, step):
        row = self.users[username]
        if row["mfa_last_step"] is not None and row["mfa_last_step"] >= step:
            return False  # the UPDATE ... WHERE mfa_last_step < %s guard
        row["mfa_last_step"] = step
        return True

    # -- sessions
    def create_session(self, conn, session_id, username, lifetime_minutes):
        self.sessions[session_id] = {
            "username": username, "expires_at": _now() + timedelta(minutes=lifetime_minutes),
            "revoked_at": None, "last_seen_at": _now(),
        }

    def resolve_session(self, conn, session_id):
        row = self.sessions.get(session_id)
        if row is None or row["revoked_at"] or row["expires_at"] <= _now():
            return None
        if self.users[row["username"]]["status"] != "active":
            return None
        return self._build(row["username"])

    def touch_session(self, conn, session_id):
        if session_id in self.sessions:
            self.sessions[session_id]["last_seen_at"] = _now()

    def revoke_session(self, conn, session_id, reason):
        if session_id in self.sessions and not self.sessions[session_id]["revoked_at"]:
            self.sessions[session_id]["revoked_at"] = _now()

    def revoke_sessions_for_user(self, conn, username, reason):
        count = 0
        for row in self.sessions.values():
            if row["username"] == username and not row["revoked_at"] and row["expires_at"] > _now():
                row["revoked_at"] = _now()
                count += 1
        return count

    def active_session_count(self, conn, username):
        return sum(1 for row in self.sessions.values()
                   if row["username"] == username and not row["revoked_at"]
                   and row["expires_at"] > _now())

    def purge_expired_sessions(self, conn, older_than_days=7):
        return 0


def test_fake_store_matches_the_real_modules_surface():
    """Drift guard, in the spirit of tests/test_entrypoints.py.

    Every function the routes call on core/db/users.py must exist on the
    stub with the same parameter names, or these tests pass while the
    real deployment fails. Checked structurally rather than trusted.
    """
    for name in dir(_MemoryAccounts):
        if name.startswith("_") or name == "VALID_ROLES":
            continue
        real = getattr(real_user_store, name, None)
        assert callable(real), f"core.db.users has no {name}()"
        real_params = list(inspect.signature(real).parameters)
        fake_params = list(inspect.signature(getattr(_MemoryAccounts, name)).parameters)[1:]
        assert real_params == fake_params, (
            f"{name}() signature drifted: real {real_params} vs stub {fake_params}"
        )


class _FakeConn:
    def close(self):
        pass


class _FakeReader:
    """Only the parts the account pages touch. See tests/test_web.py for
    the full one - nothing here reads clinical content."""

    def stats(self):
        return None

    def verify_audit_chain(self):
        return (True, 0, None)

    def read_audit_events(self, limit=200, actor=None):
        return []


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append({"actor": actor, "action": action,
                            "resource_key": resource_key,
                            "purpose_of_use": purpose_of_use})

    def actions(self):
        return [e["action"] for e in self.events]


def _build(monkeypatch, mfa="off", max_failures=5, **user_kwargs):
    store = _MemoryAccounts()
    monkeypatch.setattr(login_routes, "user_store", store)
    monkeypatch.setattr(admin_routes, "user_store", store)

    settings = la.LocalAuthSettings(key=KEY, mfa=mfa, max_failures=max_failures,
                                    lockout_minutes=15, session_minutes=480)
    accounts = login_routes.LocalAccounts(
        connection_factory=lambda: _FakeConn(), settings=settings
    )
    audit = _RecordingAudit()
    app = create_app(
        reader=_FakeReader(),
        auth_settings=AuthSettings(trust_proxy_headers=False, local_accounts=True),
        audit=audit,
        session_secret_key="test-secret-not-a-real-one",
        local_accounts=accounts,
    )
    client = TestClient(app, base_url="https://records.example.org",
                        follow_redirects=False)
    return client, store, audit


def _csrf(client, path="/login"):
    body = client.get(path).text
    match = re.search(r'name="csrf-token" content="([^"]+)"', body)
    assert match, f"no CSRF token rendered on {path}"
    return match.group(1)


def _add_user(store, username="alice", password=GOOD_PASSWORD, roles=("him",),
              must_change=False, mfa_secret=None):
    store.create_user(
        _FakeConn(), username=username, password_hash=la.hash_password(password, KEY),
        created_by="cli:test", roles=roles, must_change_password=must_change,
    )
    if mfa_secret:
        store.set_mfa_secret(_FakeConn(), username, la.encrypt_secret(mfa_secret, KEY),
                             "cli:test")


def _signin(client, username="alice", password=GOOD_PASSWORD):
    token = _csrf(client, "/login")
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": token})


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def test_an_unauthenticated_request_is_sent_to_the_login_page(monkeypatch):
    client, _, _ = _build(monkeypatch)
    response = client.get("/patients")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_json_api_gets_a_401_rather_than_a_redirect(monkeypatch):
    """A redirect to an HTML form reaches an API client as a 200 full of
    markup, which is a worse failure than an honest 401."""
    client, _, _ = _build(monkeypatch)
    response = client.get("/api/stats")
    assert response.status_code == 401


def test_a_correct_password_signs_in_and_is_audited(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store)
    response = _signin(client)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "auth.login" in audit.actions()
    assert client.get("/patients").status_code == 200


@pytest.mark.parametrize("username,password", [
    ("alice", "the wrong passphrase entirely"),
    ("nobody-at-all", GOOD_PASSWORD),
])
def test_every_sign_in_failure_says_the_same_thing(monkeypatch, username, password):
    """No such user and wrong password must be indistinguishable, or this
    page enumerates an organisation's staff for anyone who asks."""
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    response = _signin(client, username, password)
    assert response.status_code == 401
    assert login_routes.SIGNIN_FAILED in response.text


def test_a_disabled_account_cannot_sign_in_and_says_nothing_different(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store)
    store.set_status(_FakeConn(), "alice", "disabled", "admin")
    response = _signin(client)
    assert response.status_code == 401
    assert login_routes.SIGNIN_FAILED in response.text
    assert "auth.login.disabled" in audit.actions()


def test_repeated_failures_lock_the_account_and_the_lockout_is_audited(monkeypatch):
    client, store, audit = _build(monkeypatch, max_failures=3)
    _add_user(store)
    for _ in range(3):
        assert _signin(client, password="wrong wrong wrong wrong").status_code == 401
    assert "auth.login.lockout" in audit.actions()

    # Correct password, still refused: the lock is checked before it.
    assert _signin(client).status_code == 401
    assert audit.actions().count("auth.login.locked") == 1

    store.unlock(_FakeConn(), "alice", "admin")
    assert _signin(client).status_code == 303


def test_a_login_post_without_a_csrf_token_is_refused(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    client.get("/login")
    assert client.post("/login", data={"username": "alice",
                                       "password": GOOD_PASSWORD}).status_code == 403


# ---------------------------------------------------------------------------
# Second factor
# ---------------------------------------------------------------------------

def test_a_password_alone_does_not_sign_in_when_mfa_is_enrolled(monkeypatch):
    secret = la.generate_totp_secret()
    client, store, _ = _build(monkeypatch, mfa="required")
    _add_user(store, mfa_secret=secret)

    response = _signin(client)
    assert response.headers["location"] == "/login/mfa"
    # Password accepted, but nothing is reachable yet.
    assert client.get("/patients").headers["location"] == "/login"


def test_the_right_code_completes_the_sign_in_and_a_replay_does_not(monkeypatch):
    secret = la.generate_totp_secret()
    client, store, audit = _build(monkeypatch, mfa="required")
    _add_user(store, mfa_secret=secret)
    _signin(client)

    code = la._totp_at(secret, int(time.time()) // 30)
    token = _csrf(client, "/login/mfa")
    assert client.post("/login/mfa", data={"code": code, "csrf_token": token}
                       ).status_code == 303
    assert client.get("/patients").status_code == 200
    assert "auth.login" in audit.actions()

    # A second browser with the same intercepted code gets nothing.
    client2, _, _ = _build(monkeypatch, mfa="required")
    monkeypatch.setattr(login_routes, "user_store", store)
    monkeypatch.setattr(admin_routes, "user_store", store)
    _signin(client2)
    token2 = _csrf(client2, "/login/mfa")
    replay = client2.post("/login/mfa", data={"code": code, "csrf_token": token2})
    assert replay.status_code == 401


def test_a_wrong_code_counts_toward_the_lockout(monkeypatch):
    secret = la.generate_totp_secret()
    client, store, audit = _build(monkeypatch, mfa="required", max_failures=2)
    _add_user(store, mfa_secret=secret)
    _signin(client)
    token = _csrf(client, "/login/mfa")
    assert client.post("/login/mfa", data={"code": "000000", "csrf_token": token}
                       ).status_code == 401
    assert "auth.mfa.failed" in audit.actions()
    assert store.users["alice"]["failed_attempts"] == 1


def test_mfa_required_but_not_enrolled_sends_the_user_to_enrolment(monkeypatch):
    client, store, audit = _build(monkeypatch, mfa="required")
    _add_user(store)
    assert _signin(client).headers["location"] == "/login/enrol"

    body = client.get("/login/enrol").text
    secret = re.search(r'<div class="mono" style="word-break:break-all;user-select:all">([A-Z2-7]+)</div>',
                       body).group(1)
    # Nothing is stored until a code is accepted, so an abandoned
    # enrolment cannot lock anybody out.
    assert store.users["alice"]["mfa_secret"] is None

    token = _csrf(client, "/login/enrol")
    code = la._totp_at(secret, int(time.time()) // 30)
    assert client.post("/login/enrol", data={"code": code, "csrf_token": token}
                       ).status_code == 303
    assert store.users["alice"]["mfa_secret"] is not None
    assert "auth.mfa.enrolled" in audit.actions()
    assert client.get("/patients").status_code == 200


# ---------------------------------------------------------------------------
# The completion gates
# ---------------------------------------------------------------------------

def test_an_administrator_issued_password_must_be_changed_before_anything_else(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store, must_change=True)
    _signin(client)

    assert client.get("/patients").headers["location"] == "/account/password"
    assert client.get("/account/password").status_code == 200

    token = _csrf(client, "/account/password")
    response = client.post("/account/password", data={
        "current_password": GOOD_PASSWORD,
        "new_password": "a different long passphrase",
        "confirm_password": "a different long passphrase",
        "csrf_token": token,
    })
    assert response.status_code == 303
    assert client.get("/patients").status_code == 200


def test_a_password_change_ends_every_other_session(monkeypatch):
    """A password change is what somebody does when they think a
    credential leaked. Sessions that credential opened must not survive
    it."""
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    _signin(client)

    other = TestClient(client.app, base_url="https://records.example.org",
                       follow_redirects=False)
    _signin(other)
    assert other.get("/patients").status_code == 200

    token = _csrf(client, "/account/password")
    client.post("/account/password", data={
        "current_password": GOOD_PASSWORD,
        "new_password": "another entirely separate passphrase",
        "confirm_password": "another entirely separate passphrase",
        "csrf_token": token,
    })
    assert other.get("/patients").headers["location"] == "/login"
    # The browser that made the change keeps working, on a new session.
    assert client.get("/account").status_code == 200


def test_the_wrong_current_password_does_not_change_anything(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    _signin(client)
    before = store.users["alice"]["password_hash"]
    token = _csrf(client, "/account/password")
    response = client.post("/account/password", data={
        "current_password": "not it", "new_password": "a long enough passphrase here",
        "confirm_password": "a long enough passphrase here", "csrf_token": token,
    })
    assert response.status_code == 400
    assert store.users["alice"]["password_hash"] == before


# ---------------------------------------------------------------------------
# Revocation - the reason sessions are rows
# ---------------------------------------------------------------------------

def test_disabling_an_account_ends_its_live_sessions_immediately(monkeypatch):
    """Not "cannot sign in again" - cannot make the next request. A
    signed-cookie session could not have delivered this."""
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    _signin(client)
    assert client.get("/patients").status_code == 200

    store.set_status(_FakeConn(), "alice", "disabled", "admin")
    assert client.get("/patients").headers["location"] == "/login"


def test_a_role_change_lands_on_the_next_request(monkeypatch):
    """Roles are read from the grant table per request, never from the
    cookie - otherwise a revoked permission survives until the user
    happens to sign in again."""
    client, store, _ = _build(monkeypatch)
    _add_user(store, roles=("him", "auditor"))
    _signin(client)
    assert client.get("/audit").status_code == 200

    store.revoke_role(_FakeConn(), "alice", "auditor")
    assert client.get("/audit").status_code == 403


def test_signing_out_revokes_the_session_server_side(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store)
    _signin(client)
    token = _csrf(client, "/account")
    assert client.post("/logout", data={"csrf_token": token}).status_code == 303
    assert store.active_session_count(_FakeConn(), "alice") == 0
    assert "auth.logout" in audit.actions()
    assert client.get("/patients").headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

def test_only_an_administrator_reaches_the_accounts_page(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store, roles=("him",))
    _signin(client)
    assert client.get("/admin/users").status_code == 403
    assert "access.denied" in audit.actions()


def test_an_administrator_creates_an_account_with_a_one_time_password(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _signin(client, "root")

    token = _csrf(client, "/admin/users")
    response = client.post("/admin/users", data={
        "username": "J.Okafor", "display_name": "J Okafor",
        "roles": ["him", "viewer"], "csrf_token": token,
    })
    assert response.status_code == 200
    assert "j.okafor" in store.users, "username was not normalised to its stored form"
    assert store.users["j.okafor"]["must_change_password"] is True
    assert store.roles["j.okafor"] == {"him", "viewer"}
    assert "admin.user.created" in audit.actions()

    issued = re.search(r'user-select:all;word-break:break-all">([^<]+)</div>',
                       response.text)
    assert issued, "the temporary password was not shown to the administrator"
    assert la.verify_password(issued.group(1).strip(),
                              store.users["j.okafor"]["password_hash"], KEY)


def test_a_duplicate_username_is_refused_rather_than_overwriting(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _signin(client, "root")
    before = store.users["root"]["password_hash"]
    token = _csrf(client, "/admin/users")
    response = client.post("/admin/users",
                           data={"username": "root", "csrf_token": token})
    assert response.status_code == 409
    assert store.users["root"]["password_hash"] == before


def test_the_last_administrator_cannot_be_stripped_or_disabled(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _add_user(store, username="clerk", roles=("him",))
    _signin(client, "root")

    token = _csrf(client, "/admin/users/root")
    stripped = client.post("/admin/users/root/roles",
                           data={"roles": ["him"], "csrf_token": token})
    assert stripped.status_code == 400
    assert "admin" in store.roles["root"]

    disabled = client.post("/admin/users/root/status",
                           data={"status": "disabled", "csrf_token": token})
    assert disabled.status_code == 400
    assert store.users["root"]["status"] == "active"

    # With a second administrator, the same change is allowed.
    store.grant_role(_FakeConn(), "clerk", "admin", "root")
    assert client.post("/admin/users/root/roles",
                       data={"roles": ["him"], "csrf_token": token}).status_code == 200
    assert "admin" not in store.roles["root"]


def test_disabling_an_account_from_the_page_ends_its_sessions(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _add_user(store, username="clerk", roles=("him",))
    _signin(client, "root")

    clerk = TestClient(client.app, base_url="https://records.example.org",
                       follow_redirects=False)
    _signin(clerk, "clerk")
    assert clerk.get("/patients").status_code == 200

    token = _csrf(client, "/admin/users/clerk")
    client.post("/admin/users/clerk/status",
                data={"status": "disabled", "csrf_token": token})
    assert clerk.get("/patients").headers["location"] == "/login"
    assert "admin.user.disabled" in audit.actions()


def test_an_administrator_cannot_disable_their_own_account(monkeypatch):
    client, store, _ = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _add_user(store, username="second", roles=("admin",))
    _signin(client, "root")
    token = _csrf(client, "/admin/users/root")
    response = client.post("/admin/users/root/status",
                           data={"status": "disabled", "csrf_token": token})
    assert response.status_code == 400
    assert store.users["root"]["status"] == "active"


def test_resetting_a_password_forces_a_change_and_ends_sessions(monkeypatch):
    client, store, audit = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _add_user(store, username="clerk", roles=("him",))
    _signin(client, "root")

    clerk = TestClient(client.app, base_url="https://records.example.org",
                       follow_redirects=False)
    _signin(clerk, "clerk")

    token = _csrf(client, "/admin/users/clerk")
    response = client.post("/admin/users/clerk/password", data={"csrf_token": token})
    assert response.status_code == 200
    assert store.users["clerk"]["must_change_password"] is True
    assert clerk.get("/patients").headers["location"] == "/login"
    assert "admin.user.password.reset" in audit.actions()

    issued = re.search(r'user-select:all;word-break:break-all">([^<]+)</div>',
                       response.text).group(1).strip()
    assert la.verify_password(issued, store.users["clerk"]["password_hash"], KEY)


def test_clearing_an_enrolment_lets_the_user_enrol_again(monkeypatch):
    secret = la.generate_totp_secret()
    client, store, audit = _build(monkeypatch, mfa="required")
    _add_user(store, username="root", roles=("admin",), mfa_secret=secret)
    _add_user(store, username="clerk", roles=("him",), mfa_secret=secret)

    # Sign root in through both steps.
    _signin(client, "root")
    token = _csrf(client, "/login/mfa")
    client.post("/login/mfa", data={"code": la._totp_at(secret, int(time.time()) // 30),
                                    "csrf_token": token})

    token = _csrf(client, "/admin/users/clerk")
    assert client.post("/admin/users/clerk/mfa",
                       data={"csrf_token": token}).status_code == 200
    assert store.users["clerk"]["mfa_secret"] is None
    assert store.users["clerk"]["mfa_last_step"] is None, (
        "a stale step count would reject the new authenticator's first codes"
    )
    assert "admin.user.mfa.reset" in audit.actions()


def test_an_unknown_role_is_refused_rather_than_quietly_dropped(monkeypatch):
    """A grant that silently confers nothing looks identical, on this
    page, to one that worked."""
    client, store, _ = _build(monkeypatch)
    _add_user(store, username="root", roles=("admin",))
    _signin(client, "root")
    token = _csrf(client, "/admin/users")
    response = client.post("/admin/users", data={
        "username": "newcomer", "roles": ["superuser"], "csrf_token": token,
    })
    assert response.status_code == 400
    assert "newcomer" not in store.users


def test_the_account_administration_permission_carries_no_clinical_read():
    """An account administrator can create the person who reads a chart.
    They cannot read one."""
    from core.web.auth import PERMISSIONS

    admin = PERMISSIONS[Role.ADMIN]
    assert "admin:users" in admin
    for clinical in ("patient:read", "document:read", "imaging:read", "roi:export"):
        assert clinical not in admin


def test_optional_password_expiry_forces_a_change_when_configured(monkeypatch):
    """Off by default, per NIST SP 800-63B. It exists because some
    organisations' own policy requires it, and a documented setting that
    quietly does nothing is worse than not offering one - this project
    has had to remove one of those before."""
    client, store, _ = _build(monkeypatch)
    _add_user(store)
    _signin(client)
    assert client.get("/patients").status_code == 200

    accounts = client.app.state.local_accounts
    client.app.state.local_accounts = login_routes.LocalAccounts(
        connection_factory=accounts.connection_factory,
        settings=la.LocalAuthSettings(key=KEY, mfa="off", password_max_age_days=90),
    )
    store.users["alice"]["password_changed_at"] = _now() - timedelta(days=91)
    assert client.get("/patients").headers["location"] == "/account/password"

    store.users["alice"]["password_changed_at"] = _now() - timedelta(days=89)
    assert client.get("/patients").status_code == 200
# Made by Ryan Gomez & Co. Inc.
