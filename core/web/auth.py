# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Authentication and authorization boundary for the web interface.

THIS MODULE DELIBERATELY DOES NOT IMPLEMENT AUTHENTICATION ITSELF. It
reads an already-authenticated identity from trusted request headers set
by a reverse proxy in front of it (oauth2-proxy, AWS ALB with OIDC, Azure
App Service Authentication, an Nginx auth_request, and so on).

WHY, since this is the single largest design decision in the interface:
hand-rolling password storage, session management, MFA, lockout and
recovery for a system holding PHI would be strictly worse than delegating
to an identity provider the deploying organisation already runs and
already audits. Products in this category integrate with SSO/LDAP rather
than maintaining their own user directory, and for the same reason. A
hospital has an IdP; it does not want another credential store.

WHAT THAT COSTS, stated plainly: if this application is reachable
WITHOUT the proxy in front of it, anyone who can reach it is whoever
they claim to be in a header. That is not a subtle failure - it is total.
The mitigations here are that the app refuses to start unless the
operator has explicitly declared the deployment shape, and binds to
localhost by default so an accidental exposure is not reachable off-host.

THE ONE EXCEPTION, ADDED DELIBERATELY AND OFF BY DEFAULT: not every
organisation that must retain PHI runs an identity provider. A rural
critical-access hospital, a single-specialty practice, a practice closing
its doors but still holding records for its retention period - these have
real obligations under 45 CFR 164.308(a)(4) and 164.312(a)(2)(i) and
nothing to delegate to. For them,
PHI_AI_WEB_LOCAL_ACCOUNTS=true enables a local credential store:
core/web/local_auth.py (cryptography and policy), core/db/users.py
(storage), core/web/login_routes.py and core/web/admin_routes.py (the
request paths). Read local_auth.py's own docstring before enabling it -
it states what that costs as plainly as the paragraph above states the
proxy's cost. Local accounts and proxy trust are MUTUALLY EXCLUSIVE at
configuration time, below, so a deployment cannot end up with an
authenticating proxy and a password form that bypasses it.

The authorization half - which roles may do what - IS implemented here,
and is shared by all three paths, because it encodes decisions specific
to this platform that no IdP knows: that viewing PHI requires a stated
purpose of use, that psychotherapy notes need a separate grant, and that
disposal is a different privilege from reading.

CONFIGURATION IS READ THROUGH env_var(), NEVER os.environ.get(). This
module decides whether a request is authenticated at all, and every read
in AuthSettings.from_env() below is either defaulted or optional - so a
read that misses the operator's variable does not raise, it decides the
deployment's shape on their behalf. See core/config/settings.py's
env_var() docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from core.config.settings import env_var

log = logging.getLogger("phi-ai.web.auth")


class Role(str, Enum):
    """Application roles, mirroring the IAM roles in deploy/aws/iam.tf.

    Kept deliberately parallel: an application user who can read PHI in
    this UI should correspond to an infrastructure identity that can too,
    so the two layers cannot silently disagree about who may do what.

    THE VALUES ARE ALSO A DATABASE CONSTRAINT. When local accounts are
    enabled, core/db/users_schema.sql's ck_local_user_roles_role check
    lists exactly these strings. Adding a role here without adding it
    there means an administrator cannot grant it; adding it there without
    adding it here means the grant is accepted and confers nothing, which
    is worse. Change both in the same commit.
    """

    VIEWER = "viewer"          # search and read clinical records
    HIM = "him"                # viewer + release of information
    AUDITOR = "auditor"        # audit log and integrity only - NO clinical read
    DISPOSITION = "disposition"  # retention and purge
    ADMIN = "admin"            # configuration and user administration
    ANALYST = "analyst"        # population queries only - NO individual records
    RESEARCHER = "researcher"  # analyst + record-level research reads + cross-record search
    PSYCHOTHERAPY = "psychotherapy"  # psychotherapy-notes access ONLY - see below
    SYSADMIN = "sysadmin"      # every permission, plus the control panel - see below


# What each role may do. Explicit rather than hierarchical: an auditor is
# NOT a viewer with extras - they may read the audit trail and must not
# read clinical content, which a "levels" model expresses badly and gets
# wrong by default.
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({
        "patient:search", "patient:read", "document:read", "imaging:read",
    }),
    Role.HIM: frozenset({
        "patient:search", "patient:read", "document:read", "imaging:read",
        "document:ingest", "roi:create", "roi:export",
    }),
    Role.AUDITOR: frozenset({"audit:read", "audit:verify", "report:read"}),
    Role.DISPOSITION: frozenset({"retention:read", "retention:dispose", "audit:read"}),
    # `admin:users` administers LOCAL accounts only, and only where a
    # deployment enabled them - there is nothing for it to do behind an
    # identity provider, which owns its own directory. It is granted to
    # admin and to nothing else, and it is deliberately NOT paired with
    # any clinical permission: an account administrator can create the
    # user who reads a chart and cannot read one themselves. That
    # separation is the whole reason this is a distinct permission rather
    # than something `admin:config` implies.
    Role.ADMIN: frozenset({"admin:config", "admin:users", "report:read", "audit:read"}),
    # A genuinely different job, and the reason it is a role rather than
    # an extra grant on `him`: someone answering "how many patients have
    # diabetes" for a service-line review needs the population, not the
    # people in it. Cohort counts without patient:read means they cannot
    # open a chart, and without identity:search they cannot turn a cohort
    # into a list of names - which is the whole distinction between
    # analytics and disclosure.
    Role.ANALYST: frozenset({"analytics:query", "report:read"}),
    # Research is a genuinely different job from both analyst and viewer,
    # and the reason it is one role rather than a combination: an
    # organisation grants (and an auditor reviews) research access as ONE
    # unit, with its own purpose-of-use code stamped on every read. An
    # analyst counts the population and cannot open a chart; a viewer
    # opens the chart of the patient in front of them; a researcher does
    # detailed record-level work across MANY charts - including
    # `research:search`, the cross-record clinical text search
    # (core/db/retrieval_schema.sql), which neither of the others holds
    # because searching every chart at once is research, not care or
    # counting.
    Role.RESEARCHER: frozenset({
        "analytics:query", "identity:search", "research:search",
        "patient:search", "patient:read", "document:read", "report:read",
    }),
    # Psychotherapy notes carry their own authorization regime (45 CFR
    # 164.508(a)(2)) and their own storage boundary (a separate bucket
    # under a separate key - runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md).
    # Access to them is therefore its own role holding ONE permission and
    # nothing else - deliberately not folded into viewer, HIM or
    # researcher, so that granting any breadth of general clinical access
    # never quietly includes the one record class the law treats
    # differently. The role confers no general clinical read: someone
    # holding only `psychotherapy` cannot open an ordinary chart.
    Role.PSYCHOTHERAPY: frozenset({"psychotherapy:read"}),
    # The System Administrator holds the wildcard - every permission,
    # including `system:admin` (the control panel) which no enumerated
    # role carries. The wildcard is honest about what a platform
    # superuser is, and it is paired with the same discipline as
    # everything else: Identity.can() grants it, and every action still
    # lands on the audit trail under the administrator's own name. It is
    # the ONLY wildcard; every other role stays an enumerated,
    # minimum-necessary list.
    Role.SYSADMIN: frozenset({"*"}),
}

# Integration monitors (bulk import/export managers, streaming feeds).
# HIM watches records movement as part of records custody; admin runs it.
PERMISSIONS[Role.HIM] = PERMISSIONS[Role.HIM] | {"integration:view"}
PERMISSIONS[Role.ADMIN] = PERMISSIONS[Role.ADMIN] | {"integration:view"}

# The assistant (core/assistant/) is available to every role that can use
# this interface at all. Granting it broadly is safe BECAUSE it is not a
# way to see anything: it has no tool that returns clinical content, and
# the tools that report on the platform each declare the same permission
# the equivalent page here requires, so a viewer asking it about audit
# events gets the same nothing they would get from /audit. See
# core/assistant/tools.py, which is the enumerable list of what it can
# reach.
for _role in Role:
    PERMISSIONS[_role] = PERMISSIONS[_role] | {"assistant:use"}

# Population analytics and name search, granted separately on purpose.
#
# HIM staff need both: a records request arrives naming a person
# (identity:search) and release-of-information work needs counts
# (analytics:query). A viewer gets name search but NOT cohort queries -
# a clinician looking for the right patient is doing lookup, not
# research. Auditor and disposition get neither: both are explicitly not
# clinical-content roles, and a cohort count is clinical content in
# aggregate.
PERMISSIONS[Role.HIM] = PERMISSIONS[Role.HIM] | {"analytics:query", "identity:search"}
PERMISSIONS[Role.VIEWER] = PERMISSIONS[Role.VIEWER] | {"identity:search"}
PERMISSIONS[Role.ADMIN] = PERMISSIONS[Role.ADMIN] | {"analytics:query"}

# Reports are a cross-cutting read: HIM and disposition both need the
# holdings and disclosure figures their own work produces.
PERMISSIONS[Role.HIM] = PERMISSIONS[Role.HIM] | {"report:read"}
PERMISSIONS[Role.DISPOSITION] = PERMISSIONS[Role.DISPOSITION] | {"report:read", "retention:certificate"}

# Assistant operations: usage, performance, compliance and drift metrics
# (core/db/telemetry_schema.sql). Narrower than report:read on purpose -
# the rows name which STAFF member asked how many questions, which is
# activity monitoring, not a holdings figure. Admin owns the platform's
# operation; auditor reviews how a PHI-touching capability is being
# used. Nobody else, including the roles whose usage is being reported.
PERMISSIONS[Role.ADMIN] = PERMISSIONS[Role.ADMIN] | {"assistant:ops"}
PERMISSIONS[Role.AUDITOR] = PERMISSIONS[Role.AUDITOR] | {"assistant:ops"}

# Reading clinical content requires a stated reason, recorded in the
# audit entry. Mirrors the DenyReadWithoutPurposeOfUse IAM condition on
# the restore role - the same rule, enforced at the layer where a human
# can actually be asked for it.
# `imaging:read` is granted to viewer and HIM only, and deliberately NOT
# to auditor, disposition or admin. A DICOM study is clinical content of
# the most identifying kind - the header carries a name and a birth date,
# and the pixels can carry them too as burned-in annotation - so the
# roles that hold no clinical read elsewhere hold none here. The imaging
# viewer is not an exception to the auditor-is-not-a-viewer rule this
# module exists to state.
PURPOSE_REQUIRED_PERMISSIONS = frozenset({
    "patient:read", "document:read", "roi:export", "imaging:read",
    "research:search", "psychotherapy:read",
})

PURPOSES_OF_USE = (
    ("treatment", "Treatment — continuity of care for this patient"),
    ("payment", "Payment — claims, billing, or coverage determination"),
    ("operations", "Health care operations — quality, audit, or administration"),
    ("patient_request", "Patient request — the individual's own right of access"),
    ("legal", "Legal or regulatory — subpoena, investigation, or mandated report"),
    # For the researcher role. HIPAA research use rides on an
    # authorization or an IRB/privacy-board waiver (45 CFR 164.512(i));
    # this code records WHICH kind of access a read was, it does not
    # establish that the approval exists - that stays an organisational
    # control, same as every purpose above.
    ("research", "Research — IRB or privacy-board approved research use"),
)
VALID_PURPOSES = frozenset(code for code, _ in PURPOSES_OF_USE)

#: Purposes a role can plausibly assert. The role always dictates what a
#: user can see and claim: every purpose select offers only these, and
#: the record-opening routes refuse a posted purpose outside them.
ROLE_PURPOSES: dict["Role", tuple[str, ...]] = {}


def _init_role_purposes() -> None:
    ROLE_PURPOSES.update({
        Role.VIEWER: ("treatment", "operations"),
        Role.PSYCHOTHERAPY: ("treatment",),
        Role.HIM: ("payment", "operations", "patient_request", "legal"),
        Role.AUDITOR: ("operations",),
        Role.ANALYST: ("operations",),
        Role.RESEARCHER: ("research", "operations"),
        Role.ADMIN: ("operations",),
        Role.DISPOSITION: ("operations", "legal"),
    })


def role_allowed_purposes(identity: "Identity"):
    """The (code, label) purpose pairs this identity may assert."""
    if not ROLE_PURPOSES:
        _init_role_purposes()
    if "*" in identity.permissions():
        return PURPOSES_OF_USE
    codes: set[str] = set()
    for role in identity.roles:
        codes.update(ROLE_PURPOSES.get(role, ()))
    allowed = tuple((c, l) for c, l in PURPOSES_OF_USE if c in codes)
    return allowed or (PURPOSES_OF_USE[2],)  # operations, never empty


def purpose_allowed(identity: "Identity", code: str) -> bool:
    return any(c == code for c, _ in role_allowed_purposes(identity))


class AuthConfigurationError(RuntimeError):
    pass


class NotAuthenticated(Exception):
    pass


class NeedsLogin(Exception):
    """No usable local session, and this deployment has a login page.

    A DIFFERENT CONDITION FROM NotAuthenticated, which means "identity
    was supposed to arrive and did not" - a proxy misconfiguration, and
    a 401 the operator needs to see. This one is the ordinary state of a
    browser that has not signed in yet, and the right answer to it is
    the login form. core/web/app.py turns this into a redirect for a
    page request and a 401 for the JSON API, where a redirect to an HTML
    form would be a confusing 200.
    """

    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(reason or "sign-in required")


class NotAuthorized(Exception):
    def __init__(self, permission: str):
        self.permission = permission
        super().__init__(f"missing permission: {permission}")


def role_default_purpose(identity: "Identity") -> str:
    """The purpose of use a role's work most plausibly asserts.

    Used ONLY to preselect the per-action purpose fields where data
    actually moves - never recorded on its own. A clinician's default is
    treatment, a researcher's is research, and every records/operations
    role defaults to operations; the person changes it on the specific
    action when the default is wrong, which is exactly where the
    assertion attaches.
    """
    roles = identity.roles
    if Role.VIEWER in roles or Role.PSYCHOTHERAPY in roles:
        return "treatment"
    if Role.RESEARCHER in roles:
        return "research"
    return "operations"


@dataclass(frozen=True)
class Identity:
    username: str
    email: Optional[str]
    roles: frozenset[Role]

    def permissions(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted |= PERMISSIONS.get(role, frozenset())
        return frozenset(granted)

    def can(self, permission: str) -> bool:
        # The System Administrator's wildcard: full control, never
        # exemption - every action is still attributed and audited
        # under their own name like anyone else's.
        granted = self.permissions()
        return "*" in granted or permission in granted

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise NotAuthorized(permission)


@dataclass(frozen=True)
class AuthSettings:
    """How identity reaches this application.

    `trust_proxy_headers` has no default on purpose. An operator must say
    which deployment they are running, because the safe value differs by
    deployment and guessing either way is wrong: defaulting to trusting
    headers makes a direct-exposure mistake catastrophic, and defaulting
    to not trusting them makes a correctly-proxied deployment simply not
    work, which invites disabling the check entirely.
    """

    trust_proxy_headers: bool
    user_header: str = "X-Auth-Request-User"
    email_header: str = "X-Auth-Request-Email"
    groups_header: str = "X-Auth-Request-Groups"
    dev_identity: Optional[str] = None
    # Local credential store (core/web/local_auth.py). Off by default and
    # mutually exclusive with both of the above - see from_env().
    local_accounts: bool = False

    @classmethod
    def from_env(cls) -> "AuthSettings":
        # env_var() rather than os.environ.get() for all three, and this
        # is the read where it matters most: PHI_AI_WEB_LOCAL_ACCOUNTS
        # going unseen means local_accounts=False, which sends a
        # deployment that DID enable local accounts into the "no user
        # could ever be identified" refusal below - a startup failure
        # naming a variable the operator had, in fact, set.
        raw = env_var("WEB_TRUST_PROXY_AUTH")
        dev_identity = env_var("WEB_DEV_IDENTITY")
        local_accounts = (
            env_var("WEB_LOCAL_ACCOUNTS", "") or ""
        ).strip().lower() in ("1", "true", "yes")

        if raw is None and not local_accounts:
            raise AuthConfigurationError(
                "PHI_AI_WEB_TRUST_PROXY_AUTH is not set. This interface does not "
                "authenticate users itself - it reads an identity established by an "
                "authenticating reverse proxy (oauth2-proxy, an OIDC-enabled ALB, Azure "
                "App Service Authentication, or equivalent).\n\n"
                "Set it to 'true' ONLY when such a proxy is in front of this app AND the "
                "app is not reachable except through it. If the app can be reached "
                "directly, anyone reaching it is whoever they put in a header.\n\n"
                "If this deployment has no identity provider at all, set "
                "PHI_AI_WEB_LOCAL_ACCOUNTS=true instead to use local accounts "
                "managed in this application - read runbooks/RUNBOOK_LOCAL_USERS.md "
                "first.\n\n"
                "For local development against synthetic data only, set "
                "PHI_AI_WEB_DEV_IDENTITY=<user>:<role>[,<role>] instead."
            )

        trust = (raw or "").strip().lower() in ("1", "true", "yes")

        if trust and dev_identity:
            raise AuthConfigurationError(
                "PHI_AI_WEB_TRUST_PROXY_AUTH and PHI_AI_WEB_DEV_IDENTITY are "
                "both set. The dev identity bypasses authentication entirely; having it "
                "available in a proxied deployment would silently defeat the proxy."
            )
        if trust and local_accounts:
            raise AuthConfigurationError(
                "PHI_AI_WEB_TRUST_PROXY_AUTH and PHI_AI_WEB_LOCAL_ACCOUNTS are "
                "both set. Pick one. A deployment behind an authenticating proxy that "
                "also serves a password form has two front doors, and the second one is "
                "not the one the organisation audits - a local account would work "
                "whether or not the proxy ever authenticated anybody."
            )
        if local_accounts and dev_identity:
            raise AuthConfigurationError(
                "PHI_AI_WEB_LOCAL_ACCOUNTS and PHI_AI_WEB_DEV_IDENTITY are "
                "both set. The dev identity is served without any credential at all, so "
                "leaving it enabled alongside real accounts means the login page is "
                "decoration."
            )
        if not trust and not dev_identity and not local_accounts:
            raise AuthConfigurationError(
                "PHI_AI_WEB_TRUST_PROXY_AUTH is false, no "
                "PHI_AI_WEB_LOCAL_ACCOUNTS is enabled, and no "
                "PHI_AI_WEB_DEV_IDENTITY is set, so no user could ever be "
                "identified. Refusing to start rather than serving PHI to nobody in "
                "particular."
            )

        return cls(
            trust_proxy_headers=trust,
            user_header=env_var("WEB_USER_HEADER", "X-Auth-Request-User")
            or "X-Auth-Request-User",
            email_header=env_var("WEB_EMAIL_HEADER", "X-Auth-Request-Email")
            or "X-Auth-Request-Email",
            groups_header=env_var("WEB_GROUPS_HEADER", "X-Auth-Request-Groups")
            or "X-Auth-Request-Groups",
            dev_identity=dev_identity,
            local_accounts=local_accounts,
        )


def _parse_roles(raw: str) -> frozenset[Role]:
    roles = set()
    for token in raw.replace(";", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        # Group names commonly arrive namespaced (e.g.
        # "phi-ai-him", "role:him"); match on the trailing segment
        # so an IdP's naming convention does not have to change.
        candidate = token.rsplit(":", 1)[-1].rsplit("-", 1)[-1]
        for role in Role:
            if candidate == role.value:
                roles.add(role)
    return frozenset(roles)


def roles_from_names(names: Iterable[str]) -> frozenset[Role]:
    """Map stored role names onto Role members, dropping anything unknown.

    Used for local accounts, where the names come from
    authn.local_user_roles rather than from an IdP's group claim. An
    unrecognised name is dropped rather than raised on, matching
    _parse_roles above: a role this build does not know about must
    confer nothing, and must not take out the whole request while doing
    so. The schema's CHECK constraint is what stops such a row from
    being written in the first place.
    """
    known = {role.value: role for role in Role}
    resolved = set()
    for name in names or ():
        role = known.get(str(name).strip().lower())
        if role is None:
            log.warning("ignoring unknown role name %r on a local account", name)
            continue
        resolved.add(role)
    return frozenset(resolved)


# The key this identity is filed under inside the signed session cookie.
# Paired with the session cookie's own name ("phi_ai_session", set in
# core/web/app.py) - the two are read together and are kept consistent.
SESSION_KEY = "phi_ai_identity"

# Local sign-in state, kept in the signed session cookie.
#
# LOCAL_SESSION_KEY holds only an opaque session id; everything the
# server decides with it - whose session it is, whether it has been
# revoked, what roles they now hold - lives in authn.local_sessions and
# is re-read per request (core/db/users.py's resolve_session). That is
# what makes "disable this account" and "revoke this session" take
# effect immediately rather than whenever a cookie happens to expire.
#
# LOCAL_PENDING_KEY holds the half-completed sign-in between the
# password step and the TOTP step. It is deliberately NOT a database
# row: nothing has been established yet, and a partial sign-in that
# outlives the browser tab is a loose end. The signed cookie is
# sufficient because the only claim it carries is "this browser got the
# password right at this time", which it cannot forge without the
# session secret.
LOCAL_SESSION_KEY = "local_session"
LOCAL_PENDING_KEY = "local_pending"


def identity_from_session(session) -> Optional[Identity]:
    """Identity established by a completed SMART launch, carried in a
    signed session cookie.

    A second authentication PATH, not a second credential store: the
    clinician was authenticated by their EMR's authorization server, and
    this only remembers the result across page loads. That distinction is
    what keeps this consistent with the decision not to implement
    authentication - no password is stored, verified or reset here.
    """
    raw = (session or {}).get(SESSION_KEY)
    if not raw or not raw.get("username"):
        return None
    return Identity(
        username=raw["username"],
        email=raw.get("email"),
        roles=_parse_roles(",".join(raw.get("roles") or [])),
    )


def store_identity_in_session(session, identity: Identity, **extra) -> None:
    session[SESSION_KEY] = {
        "username": identity.username,
        "email": identity.email,
        "roles": sorted(r.value for r in identity.roles),
        **extra,
    }


def identity_from_headers(headers, settings: AuthSettings) -> Identity:
    """Resolve the caller's identity, or raise NotAuthenticated.

    Header values are attacker-controlled unless a proxy is guaranteed to
    overwrite them, which is exactly what trust_proxy_headers asserts.
    Nothing else in this application reads these headers.
    """
    if settings.local_accounts:
        # Never reached in a correctly wired app - core/web/app.py's
        # current_identity resolves the local session first and raises
        # NeedsLogin when there isn't one. Present so that a future
        # caller that forgets that ordering fails loudly instead of
        # falling through to a header this deployment does not trust.
        raise NeedsLogin("this deployment uses local accounts")

    if not settings.trust_proxy_headers:
        # Development path. Deliberately loud - this is the one code path
        # that fabricates an identity, and it should never be mistaken
        # for a working login.
        username, _, roles_raw = (settings.dev_identity or "").partition(":")
        if not username:
            raise NotAuthenticated("PHI_AI_WEB_DEV_IDENTITY is malformed")
        log.warning(
            "SERVING REQUEST WITH A FABRICATED DEVELOPMENT IDENTITY (%s) - no "
            "authentication occurred. This must never be a production deployment.",
            username,
        )
        # No fallback role, deliberately - the dev path must behave
        # exactly as the proxy path does, and that one grants nothing to
        # an unmapped user. A convenience default here would mean the
        # thing developers exercise daily is not the thing that ships.
        return Identity(
            username=username,
            email=None,
            roles=_parse_roles(roles_raw),
        )

    username = (headers.get(settings.user_header) or "").strip()
    if not username:
        raise NotAuthenticated(
            f"no {settings.user_header} header. Either the authenticating proxy is not "
            "in front of this request, or it is not configured to forward the user."
        )

    roles = _parse_roles(headers.get(settings.groups_header) or "")
    if not roles:
        # An authenticated user with no mapped group gets NO permissions
        # rather than a default role. Defaulting to viewer would mean any
        # successful SSO login could read PHI, which is the opposite of
        # what group mapping is for.
        log.warning("authenticated user %r has no recognised role groups", username)

    return Identity(
        username=username,
        email=(headers.get(settings.email_header) or "").strip() or None,
        roles=roles,
    )


def validate_purpose(purpose: Optional[str]) -> str:
    """Purpose of use must be one of the declared codes.

    Free text is refused: a purpose that is only ever written and never
    compared is not auditable, and 'other' collects everything within a
    month of shipping.
    """
    if not purpose or purpose not in VALID_PURPOSES:
        raise NotAuthorized("valid purpose_of_use is required to view clinical content")
    return purpose
# Made by Ryan Gomez & Co. Inc.
