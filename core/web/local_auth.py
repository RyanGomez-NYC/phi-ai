# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Local credential store: the deliberate exception to core/web/auth.py.

READ core/web/auth.py's module docstring FIRST. It states, at length,
that this application does not implement authentication, and why: a
hospital already runs an identity provider it audits, and a second
credential store holding PHI is a liability nobody asked for. That is
still the recommended deployment and still the default.

THIS MODULE EXISTS BECAUSE "ALREADY RUNS AN IDP" IS NOT UNIVERSALLY
TRUE. A rural critical-access hospital, a single-specialty practice, a
closing practice keeping records for its retention period, a lab
standing this up for one department - these deployments have real
regulatory obligations under 45 CFR 164.308(a)(4) (access management)
and 164.312(a)(2)(i) (unique user identification) and no SAML/OIDC
provider to delegate to. Before this module their only options were to
run the interface with a fabricated development identity, put it behind
a proxy they do not have, or not deploy it. All three are worse than a
credential store built carefully and stated honestly.

WHAT "CAREFULLY" MEANS HERE, since a hand-rolled password store is
exactly the thing auth.py warns against:

  - Passwords are peppered with a deployment key held OUTSIDE the
    database (PHI_AI_WEB_LOCAL_AUTH_KEY) and then hashed with
    scrypt, memory-hard, at parameters chosen so a stolen database
    alone is not a password list.
  - The same key encrypts TOTP shared secrets at rest, for the same
    reason: a shared secret stored in plaintext beside the password
    hash means one database theft defeats both factors.
  - The key's fingerprint is stored in every hash string, so a wrong or
    rotated key produces a NAMED error rather than an unexplainable
    wave of failed logins.
  - Policy follows NIST SP 800-63B where it and habit disagree: length
    over composition rules, a blocklist, and NO forced periodic
    expiry by default (see PASSWORD_MAX_AGE_DAYS below, which exists
    for organisations whose own policy requires it and is off unless
    they set it).
  - Sessions are SERVER-SIDE rows, not just a signed cookie, so
    disabling a user or revoking a session takes effect on their next
    request rather than whenever their cookie happens to expire. A
    signed-cookie-only session cannot be revoked at all, which would
    make "disable this user" a promise this system could not keep.

WHAT IT STILL COSTS, stated as plainly as auth.py states its own cost:
this is one more place a password can be stolen from, one more thing to
back up, and one more thing to get wrong. An organisation that HAS an
identity provider should use it - PHI_AI_WEB_TRUST_PROXY_AUTH and
this are mutually exclusive at configuration time, below, so nobody
ends up with both a proxy and a bypass around it.

EVERY SETTING BELOW IS READ THROUGH core/config/settings.py's env_var().
This module is security policy expressed as configuration - MFA mode,
lockout thresholds, the password blocklist - and a read that misses the
operator's variable does not error, it silently substitutes a default.
Most of those defaults are the strict ones, so the failure direction was
usually safe; it was never HONEST, and one of them (the blocklist) had
the module quietly doing the exact thing its own comment forbids.

This module holds no database access and no HTTP handling on purpose -
it is the cryptography and the policy, testable without either. See
core/db/users.py for storage and core/web/login_routes.py /
core/web/admin_routes.py for the request paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import struct
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from core.config.settings import ENV_PREFIX, env_var

log = logging.getLogger("phi-ai.web.local_auth")


class LocalAuthConfigurationError(RuntimeError):
    """The deployment asked for local accounts but cannot safely have them."""


class PasswordPolicyError(ValueError):
    """A proposed password was refused. The message is shown to the user."""


class KeyMismatchError(RuntimeError):
    """A stored secret was produced under a different deployment key.

    Raised rather than returning "wrong password", because the two need
    completely different responses: one is a user who mistyped, the
    other is an operator who has lost or rotated
    PHI_AI_WEB_LOCAL_AUTH_KEY and needs to be told so in those
    words.
    """


# ---------------------------------------------------------------------------
# Deployment key
# ---------------------------------------------------------------------------

#: Suffix passed to env_var() - the single source for both the read and
#: the operator-facing name below, so the error text can never name a
#: variable different from the one actually consulted.
KEY_ENV_SUFFIX = "WEB_LOCAL_AUTH_KEY"
#: The name as an operator writes it, built from the prefix and the
#: suffix above rather than spelled out a second time.
KEY_ENV = ENV_PREFIX + KEY_ENV_SUFFIX
_KEY_BYTES = 32


def load_key(raw: Optional[str] = None) -> bytes:
    """The deployment's local-auth key: 32 bytes, base64 or hex.

    Required whenever local accounts are enabled. Deliberately NOT
    derived from PHI_AI_WEB_SESSION_SECRET: that one is expected to
    be rotatable and is regenerated ephemerally in development, and a
    password store that silently invalidates itself on a restart is not
    a password store.
    """
    raw = raw if raw is not None else env_var(KEY_ENV_SUFFIX, "")
    raw = (raw or "").strip()
    if not raw:
        raise LocalAuthConfigurationError(
            f"{KEY_ENV} is not set. Local accounts need a deployment key held outside "
            "the database - it peppers every password hash and encrypts every TOTP "
            "secret, so a stolen database alone yields neither.\n\n"
            "Generate one with:  python -c \"import os,base64;"
            "print(base64.b64encode(os.urandom(32)).decode())\"\n\n"
            "Store it wherever this deployment already stores secrets (AWS Secrets "
            "Manager, GCP Secret Manager, Azure Key Vault) and back it up: losing it "
            "means every local user must have their password reset and their MFA "
            "re-enrolled. See runbooks/RUNBOOK_LOCAL_USERS.md."
        )

    key: Optional[bytes] = None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        key = None
    if key is None or len(key) != _KEY_BYTES:
        try:
            candidate = bytes.fromhex(raw)
        except ValueError:
            candidate = b""
        if len(candidate) == _KEY_BYTES:
            key = candidate

    if key is None or len(key) != _KEY_BYTES:
        raise LocalAuthConfigurationError(
            f"{KEY_ENV} must decode to exactly {_KEY_BYTES} bytes, as base64 or hex. "
            "Refusing to start rather than protecting a PHI system's passwords with a "
            "key of an unknown size."
        )
    return key


# The domain-separation string below is opaque input to a hash: it
# carries no product meaning, is never displayed, and is not a name. It
# is baked into the fingerprint stored in every password hash and every
# encrypted TOTP secret, and is authenticated as AES-GCM associated data
# by encrypt_secret/decrypt_secret - so once a deployment has enrolled
# users, changing it makes verify_password and decrypt_secret raise
# KeyMismatchError for every account and every enrolled second factor.
def key_fingerprint(key: bytes) -> str:
    """Eight hex characters identifying a key without revealing it.

    Recorded in every hash and every encrypted secret so a mismatch is
    detectable and reportable. Eight characters is enough to tell two
    keys apart operationally and far too little to attack the key.
    """
    return hashlib.sha256(b"phi-ai-local-auth-key-id\x00" + key).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# scrypt, from the standard library, rather than Argon2id from a new
# dependency. Argon2id would be the marginally better primitive; scrypt
# is memory-hard, is what Python ships, is explicitly permitted for
# password storage, and does not add a compiled dependency to an image
# that already carries three cloud SDK stacks. The algorithm name is the
# first field of every stored hash so a later move to Argon2id is a
# verify-old/write-new migration rather than a flag day.
SCRYPT_N = 2 ** 15   # 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
# 128 * N * r * p is scrypt's actual working set (32 MiB here). OpenSSL
# enforces a maxmem ceiling that defaults BELOW that, so omitting this
# fails with a bare "memory limit exceeded" rather than hashing.
SCRYPT_MAXMEM = 96 * 1024 * 1024

# scrypt's cost is paid by this process on every login attempt, so an
# unbounded password is a denial of service against the login page. The
# ceiling is far above any real passphrase.
MAX_PASSWORD_BYTES = 1024


def _pepper(key: bytes, password: str) -> bytes:
    """HMAC the password under the deployment key before hashing it.

    This is the pepper: the input to scrypt is not the password, so a
    database stolen without the key cannot be attacked offline at all -
    not slowly, not with a GPU farm, not with a rainbow table.
    Normalised to NFKC first so a password typed on a different keyboard
    or platform produces the same bytes.
    """
    normalised = unicodedata.normalize("NFKC", password).encode("utf-8")
    if len(normalised) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password is longer than {MAX_PASSWORD_BYTES} bytes."
        )
    return hmac.new(key, normalised, hashlib.sha256).digest()


def hash_password(password: str, key: bytes) -> str:
    """Return the storable hash string for `password`.

    Format:  scrypt$n=<N>,r=<R>,p=<P>$<key fingerprint>$<salt>$<hash>
    Every field is needed to verify, and the key fingerprint is what
    turns "wrong key" into a named error instead of a mystery.
    """
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        _pepper(key, password),
        salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM,
    )
    return "$".join([
        "scrypt",
        f"n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}",
        key_fingerprint(key),
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    ])


def verify_password(password: str, stored: str, key: bytes) -> bool:
    """Constant-time check of `password` against a stored hash string.

    Raises KeyMismatchError when the hash was produced under a different
    deployment key - see that exception's docstring for why that is not
    simply reported as a failed login.
    """
    try:
        algorithm, params, fingerprint, salt_b64, hash_b64 = stored.split("$")
    except (ValueError, AttributeError):
        log.error("stored password hash is malformed; treating as no match")
        return False

    if algorithm != "scrypt":
        raise KeyMismatchError(
            f"stored password hash uses unknown algorithm {algorithm!r}. This build "
            "writes and verifies scrypt hashes only."
        )
    if fingerprint != key_fingerprint(key):
        raise KeyMismatchError(
            f"stored password hashes were produced with a different "
            f"{KEY_ENV} (hash carries key {fingerprint}, this deployment has "
            f"{key_fingerprint(key)}). Nobody can log in until the original key is "
            "restored. If it is genuinely lost, every local password must be reset "
            "with `python -m core.web.useradmin reset-password` and every MFA "
            "enrolment redone - see runbooks/RUNBOOK_LOCAL_USERS.md."
        )

    try:
        parsed = dict(part.split("=", 1) for part in params.split(","))
        n, r, p = int(parsed["n"]), int(parsed["r"]), int(parsed["p"])
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        log.error("stored password hash parameters are malformed; treating as no match")
        return False

    try:
        derived = hashlib.scrypt(
            _pepper(key, password),
            salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=SCRYPT_MAXMEM,
        )
    except PasswordPolicyError:
        # An over-length submission is not a match, and must not be an
        # error page that tells an attacker anything.
        return False
    return hmac.compare_digest(derived, expected)


# A verification against a throwaway hash, run when the submitted
# username does not exist. Without it, "no such user" returns in
# microseconds and "wrong password" takes ~100ms, which is a user
# enumeration oracle anyone can read with a stopwatch.
_DUMMY_HASH: Optional[str] = None


def verify_dummy(key: bytes) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(32), key)
    try:
        verify_password("not-the-password", _DUMMY_HASH, key)
    except KeyMismatchError:  # pragma: no cover - dummy is made with this key
        pass


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12

# NIST SP 800-63B calls for screening against commonly-used and breached
# values. A full breach corpus is a large file an operator must supply
# (PHI_AI_WEB_LOCAL_AUTH_BLOCKLIST); this is the floor that catches
# the passwords people actually pick when nobody stops them.
_BUILTIN_BLOCKLIST = frozenset({
    "password", "password1", "password123", "passw0rd", "p@ssw0rd",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "qwerty", "qwertyuiop", "letmein", "welcome", "welcome1", "admin",
    "administrator", "iloveyou", "monkey", "dragon", "sunshine",
    "princess", "football", "baseball", "trustno1", "changeme",
    "hospital", "hospital1", "healthcare", "medical", "patient",
    # This system's own names are the first thing someone types when
    # asked to pick a password for it.
    "phiai", "phi-ai", "platform", "epic", "epic123",
    "correcthorsebatterystaple",
})


def _load_blocklist() -> frozenset[str]:
    # env_var(), not os.environ.get(). A missed read here does not raise -
    # it returns the small built-in list, which is exactly the silent
    # shrink the error path below refuses to allow. Reading the wrong
    # variable name would have produced it anyway.
    path = env_var("WEB_LOCAL_AUTH_BLOCKLIST")
    if not path:
        return _BUILTIN_BLOCKLIST
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            extra = {line.strip().lower() for line in handle if line.strip()}
    except OSError as exc:
        # Loud, and fatal at configuration time rather than silently
        # weaker than the operator believes.
        raise LocalAuthConfigurationError(
            f"{ENV_PREFIX}WEB_LOCAL_AUTH_BLOCKLIST points to {path!r}, which could not "
            f"be read: {exc}. Refusing to fall back to the small built-in list, because "
            "a screening control that silently shrinks is worse than none."
        ) from exc
    return frozenset(_BUILTIN_BLOCKLIST | extra)


def check_password_policy(password: str, username: str = "", blocklist=None) -> None:
    """Raise PasswordPolicyError if `password` may not be used.

    Deliberately NOT a composition rule engine. NIST SP 800-63B
    (5.1.1.2) advises against requiring mixed character classes and
    against periodic rotation, on the evidence that both push people
    toward predictable patterns; length and screening are what actually
    help. HIPAA's password requirement (45 CFR 164.308(a)(5)(ii)(D)) is
    a requirement to HAVE procedures, not a requirement to have those
    particular ones.
    """
    if password != password.strip():
        raise PasswordPolicyError(
            "Password begins or ends with a space. Leading and trailing spaces are "
            "refused because they are invisible and impossible to retype reliably."
        )
    normalised = unicodedata.normalize("NFKC", password)
    if len(normalised) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. A passphrase "
            "of a few unrelated words is both stronger and easier to type than a short "
            "one with symbols in it."
        )
    if len(normalised.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
        )

    folded = normalised.casefold()
    candidates = blocklist if blocklist is not None else _load_blocklist()
    if folded in candidates:
        raise PasswordPolicyError(
            "That password appears on the list of commonly-used passwords and cannot "
            "be used here."
        )
    # Trivially padded variants of a blocked word ("password2026!") are
    # the obvious way around a plain membership test.
    stripped = re.sub(r"[^a-z]", "", folded)
    if stripped and stripped in candidates:
        raise PasswordPolicyError(
            "That password is a common password with characters added around it, which "
            "does not make it harder to guess."
        )
    if username and len(username) >= 3 and username.casefold() in folded:
        raise PasswordPolicyError(
            "Password must not contain the username."
        )
    if len(set(normalised)) < 5:
        raise PasswordPolicyError(
            "Password repeats too few distinct characters to be a real passphrase."
        )


# ---------------------------------------------------------------------------
# TOTP (RFC 6238), for the second factor
# ---------------------------------------------------------------------------

TOTP_DIGITS = 6
TOTP_PERIOD = 30
# One step either side: enough for a phone whose clock is a few seconds
# off, small enough that a shoulder-surfed code expires quickly.
TOTP_WINDOW = 1


def generate_totp_secret() -> str:
    """A fresh 160-bit shared secret, base32, no padding.

    160 bits because that is what RFC 4226 recommends and what every
    authenticator app expects; base32 because the otpauth:// URI format
    and every QR generator use it.
    """
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_uri(secret: str, username: str, issuer: str = "PHI AI Platform") -> str:
    """The otpauth:// URI an authenticator app enrols from."""
    label = quote(f"{issuer}:{username}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={quote(issuer, safe='')}&algorithm=SHA1"
        f"&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


def _totp_at(secret: str, step: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(
    secret: str,
    code: str,
    *,
    now: Optional[int] = None,
    last_used_step: Optional[int] = None,
) -> Optional[int]:
    """Return the time step a valid `code` belongs to, or None.

    REPLAY IS REJECTED, which is why this returns a step rather than a
    boolean: the caller stores it, and a code for a step already used is
    refused even though it is still arithmetically correct for another
    twenty seconds. Without that, a code read over someone's shoulder or
    lifted from a phishing proxy is reusable for its whole window, which
    is most of what a second factor was supposed to stop.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None

    current = int(now if now is not None else time.time()) // TOTP_PERIOD
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        step = current + offset
        if step < 0:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            if last_used_step is not None and step <= int(last_used_step):
                return None
            return step
    return None


# ---------------------------------------------------------------------------
# Secret encryption at rest
# ---------------------------------------------------------------------------

def encrypt_secret(plaintext: str, key: bytes) -> str:
    """AES-256-GCM the TOTP shared secret for storage.

    Same primitive and same shape as core/crypto/envelope.py's object
    encryption - nonce prefixed onto the ciphertext, authenticated, key
    fingerprint carried alongside so a wrong key is a named error. The
    key fingerprint is authenticated as associated data, so a stored
    value cannot be relabelled as belonging to a different key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    fingerprint = key_fingerprint(key)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext.encode("utf-8"), fingerprint.encode("ascii")
    )
    return "v1$" + fingerprint + "$" + base64.b64encode(nonce + ciphertext).decode()


def decrypt_secret(stored: str, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        version, fingerprint, payload_b64 = stored.split("$")
    except (ValueError, AttributeError) as exc:
        raise KeyMismatchError("stored MFA secret is malformed") from exc
    if version != "v1":
        raise KeyMismatchError(f"stored MFA secret has unknown version {version!r}")
    if fingerprint != key_fingerprint(key):
        raise KeyMismatchError(
            f"stored MFA secrets were encrypted with a different {KEY_ENV} "
            f"(secret carries key {fingerprint}, this deployment has "
            f"{key_fingerprint(key)}). Restore the original key, or clear each user's "
            "MFA enrolment with `python -m core.web.useradmin reset-mfa` and have them "
            "enrol again."
        )
    payload = base64.b64decode(payload_b64)
    return AESGCM(key).decrypt(
        payload[:12], payload[12:], fingerprint.encode("ascii")
    ).decode("utf-8")


# ---------------------------------------------------------------------------
# Session identifiers
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    """256 bits of randomness, url-safe.

    The cookie carries ONLY this. Everything else about the session -
    who it belongs to, whether MFA was satisfied, whether an
    administrator has since revoked it - lives in a row this application
    can change, which is the whole reason sessions are not
    self-contained signed cookies here. See core/db/users.py.
    """
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Deployment settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalAuthSettings:
    """How local accounts behave in this deployment.

    Every default is the safe one. MFA is REQUIRED unless an operator
    turns it off in writing, because a password-only login to a PHI
    platform reachable from a browser is the exposure this whole module
    was written to make defensible, not one to leave to preference.
    """

    key: bytes
    mfa: str = "required"          # required | optional | off
    max_failures: int = 5
    lockout_minutes: int = 15
    session_minutes: int = 480     # absolute lifetime; the idle timeout is separate
    password_max_age_days: int = 0  # 0 = no expiry (SP 800-63B); set only if policy demands
    # The label shown in an authenticator app. Changing it affects NEWLY
    # enrolled factors only: totp_uri() is read at enrolment time and the
    # issuer is not part of the shared secret, so every already-enrolled
    # authenticator keeps generating valid codes under its old label.
    issuer: str = "PHI AI Platform"

    @classmethod
    def from_env(cls) -> "LocalAuthSettings":
        mfa = (
            env_var("WEB_LOCAL_AUTH_MFA", "required") or "required"
        ).strip().lower()
        if mfa not in ("required", "optional", "off"):
            raise LocalAuthConfigurationError(
                f"{ENV_PREFIX}WEB_LOCAL_AUTH_MFA must be one of required/optional/off, "
                f"got {mfa!r}."
            )
        if mfa == "off":
            log.warning(
                "%sWEB_LOCAL_AUTH_MFA=off - local accounts are password-only. "
                "This is a single-factor login to a system holding PHI, over a network. "
                "Record the decision and the compensating control in your risk analysis "
                "(45 CFR 164.308(a)(1)(ii)(A)); this deployment cannot claim MFA.",
                ENV_PREFIX,
            )

        def _int(suffix: str, default: int, minimum: int) -> int:
            # Takes the SUFFIX, and builds the operator-facing name from
            # it, so the variable named in an error is by construction
            # the variable that was read.
            name = ENV_PREFIX + suffix
            raw = env_var(suffix)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise LocalAuthConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
            if value < minimum:
                raise LocalAuthConfigurationError(f"{name} must be at least {minimum}, got {value}")
            return value

        return cls(
            key=load_key(),
            mfa=mfa,
            max_failures=_int("WEB_LOCAL_AUTH_MAX_FAILURES", 5, 1),
            lockout_minutes=_int("WEB_LOCAL_AUTH_LOCKOUT_MINUTES", 15, 1),
            session_minutes=_int("WEB_LOCAL_AUTH_SESSION_MINUTES", 480, 5),
            password_max_age_days=_int("WEB_LOCAL_AUTH_PASSWORD_MAX_AGE_DAYS", 0, 0),
            issuer=env_var("WEB_LOCAL_AUTH_ISSUER", "PHI AI Platform") or "PHI AI Platform",
        )


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def normalise_username(raw: str) -> str:
    """Fold a submitted username to its stored form, or raise.

    Lowercased and constrained, so "A.Smith" and "a.smith" cannot become
    two accounts with two audit trails. The audit log records this
    normalised form, which is what makes an entry attributable to
    exactly one account years later.
    """
    candidate = unicodedata.normalize("NFKC", (raw or "")).strip().casefold()
    if not USERNAME_PATTERN.match(candidate):
        raise PasswordPolicyError(
            "Usernames are 2-64 characters, lower case, and may contain letters, "
            "digits, dot, underscore and hyphen. They appear verbatim in the audit "
            "trail, so they are kept to one unambiguous form."
        )
    return candidate
# Made by Ryan Gomez & Co. Inc.
