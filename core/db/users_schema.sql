-- ---------------------------------------------------------------------------
-- authn.* - LOCAL USER ACCOUNTS, for deployments with no identity provider.
--
-- OPTIONAL, AND OFF BY DEFAULT. If this deployment authenticates through
-- an identity provider (PHI_AI_WEB_TRUST_PROXY_AUTH=true), do not
-- install this schema at all: there is nothing here that an IdP does not
-- already do better, and an unused credential store is still a credential
-- store somebody has to protect. Read core/web/auth.py's module docstring
-- for why delegating is the recommendation, and core/web/local_auth.py's
-- for why this exists anyway.
--
-- WHAT IS AND IS NOT HERE.
--
-- These tables describe STAFF, not patients. No table in this schema
-- references a patient, a resource, a storage key, or any clinical
-- content - the strict "no clinical content" rule core/db/schema.sql
-- states for stored_resources holds here too, and holds trivially,
-- because nothing in an account record has any connection to a record
-- subject.
--
-- What IS here is credential material, which is its own kind of
-- sensitive:
--   - password_hash is scrypt over an HMAC of the password under a key
--     held OUTSIDE this database (PHI_AI_WEB_LOCAL_AUTH_KEY). A
--     stolen copy of this table, without that key, is not a password
--     list and cannot be attacked offline.
--   - mfa_secret is the TOTP shared secret, AES-256-GCM encrypted under
--     that same key. Storing it in plaintext beside the password hash
--     would mean one theft defeated both factors, which is not two
--     factors.
-- Neither column is ever read by anything but core/web/local_auth.py.
--
-- THE AUDIT TRAIL IS NOT HERE. Every login, failure, lockout, password
-- change, role grant and account disable is written to the hash-chained
-- audit log (core/audit/), the same place a PHI read goes, for the same
-- reason: an access-management trail that lives in a table an
-- administrator can edit is not evidence. These tables hold current
-- state only. The counters below (failed_attempts, locked_until) are
-- enforcement state, not the record of what happened.
--
-- ACCOUNTS ARE DISABLED, NEVER DELETED. There is no DELETE grant on
-- authn.local_users in any of the three bootstrap files, deliberately.
-- Audit entries name an actor by username; if the account row can be
-- removed, a username in a three-year-old audit entry stops resolving to
-- anyone, and 45 CFR 164.312(a)(2)(i)'s "unique user identification"
-- becomes unique only until somebody tidies up. A departed employee's
-- account is set to 'disabled' and their sessions revoked, which ends
-- their access immediately and keeps their history attributable.
--
-- Cloud-neutral by construction, like every other schema file here: no
-- extension, no non-standard type, nothing a managed Postgres restricts.
-- See core/db/bootstrap_{aws,gcp,azure}.sql for the grants, which are
-- identical across the three.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS authn;

CREATE TABLE IF NOT EXISTS authn.local_users (
    -- Lower-cased at the application boundary
    -- (core/web/local_auth.py's normalise_username) so one person cannot
    -- become two accounts with two audit trails. This value is what the
    -- audit log records as `actor`.
    username              TEXT        PRIMARY KEY,

    display_name          TEXT,
    -- Optional and unverified. There is no mail path in this system -
    -- password resets are administrator-mediated, not emailed - so this
    -- is a contact note for whoever administers accounts, never a
    -- credential or a recovery channel.
    email                 TEXT,

    -- scrypt$n=..,r=..,p=..$<key fingerprint>$<salt>$<hash>. The key
    -- fingerprint is what turns a lost or rotated
    -- PHI_AI_WEB_LOCAL_AUTH_KEY into a named error instead of an
    -- unexplained wave of failed logins.
    password_hash         TEXT        NOT NULL,
    password_changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set on every administrator-issued password. The first thing a new
    -- user does is choose a password the administrator never knew, so a
    -- temporary password conveyed by phone is not a standing credential.
    must_change_password  BOOLEAN     NOT NULL DEFAULT TRUE,

    -- TOTP (RFC 6238). NULL until the user completes enrolment; a
    -- half-finished enrolment is not written here at all, so a user
    -- cannot be locked out by abandoning the page.
    mfa_secret            TEXT,
    mfa_enrolled_at       TIMESTAMPTZ,
    -- The last time step a code was accepted for. A code for a step at
    -- or below this is refused even though it is still arithmetically
    -- valid - a code observed over a shoulder or captured by a phishing
    -- proxy is otherwise reusable for its whole window, which is most of
    -- what the second factor was meant to prevent.
    mfa_last_step         BIGINT,

    status                TEXT        NOT NULL DEFAULT 'active',

    -- Enforcement state for throttling, not history. Reset to zero on a
    -- successful sign-in; the failures themselves are in the audit log.
    failed_attempts       INTEGER     NOT NULL DEFAULT 0,
    locked_until          TIMESTAMPTZ,
    last_login_at         TIMESTAMPTZ,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by            TEXT        NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by            TEXT,

    CONSTRAINT ck_local_users_status CHECK (status IN ('active', 'disabled'))
);

-- ---------------------------------------------------------------------------
-- Role grants.
--
-- One row per (user, role). A SEPARATE TABLE rather than a column on the
-- user, because these roles are not a hierarchy and a user genuinely
-- holds a set of them - see core/web/auth.py's PERMISSIONS map, which
-- states at length why an auditor is not a viewer with extras and why a
-- "level" column would get that wrong by default.
--
-- The CHECK list below MUST match core/web/auth.py's Role enum exactly.
-- Duplicating it here is deliberate: a role name that exists only in the
-- database grants nothing (auth.py's _parse_roles simply ignores it),
-- which would be a silent, invisible failure - an administrator would
-- see the grant on the page and the user would see none of the access.
-- The constraint turns that into an error at the moment of the grant.
-- If you add a role to the enum, add it here in the same change.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS authn.local_user_roles (
    username    TEXT        NOT NULL REFERENCES authn.local_users(username),
    role        TEXT        NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by  TEXT        NOT NULL,

    CONSTRAINT pk_local_user_roles PRIMARY KEY (username, role),
    CONSTRAINT ck_local_user_roles_role CHECK (
        role IN ('viewer', 'him', 'auditor', 'disposition', 'admin', 'analyst',
                 'researcher', 'psychotherapy', 'sysadmin')
    )
);

CREATE INDEX IF NOT EXISTS idx_local_user_roles_role
    ON authn.local_user_roles (role);

-- Re-runnable role-list upgrade. CREATE TABLE IF NOT EXISTS never
-- touches an existing table, so a deployment created before a role
-- existed would keep the old CHECK and refuse the new grant. Dropping
-- and re-adding the constraint here makes re-running this file the
-- upgrade path ('researcher' and 'psychotherapy' added 2026-08 for the
-- assistant's research and psychotherapy-notes access; 'sysadmin' added
-- 2026-08 for the control panel - see core/web/auth.py Role.SYSADMIN).
ALTER TABLE authn.local_user_roles
    DROP CONSTRAINT IF EXISTS ck_local_user_roles_role;
ALTER TABLE authn.local_user_roles
    ADD CONSTRAINT ck_local_user_roles_role CHECK (
        role IN ('viewer', 'him', 'auditor', 'disposition', 'admin', 'analyst',
                 'researcher', 'psychotherapy', 'sysadmin')
    );

-- ---------------------------------------------------------------------------
-- Sessions.
--
-- SERVER-SIDE ROWS, not a self-contained signed cookie, and that is the
-- point. The browser holds only the opaque session_id; everything that
-- decides whether the session is still good lives here, where this
-- application can change it. A signed-cookie session cannot be revoked
-- before it expires, so "disable this user" would have been a promise
-- the system could not keep - a dismissed employee would keep working
-- until their cookie aged out.
--
-- Because roles are read from authn.local_user_roles on every request
-- rather than baked into the cookie at sign-in, a role change also takes
-- effect on the user's next page load, not their next login.
--
-- Deliberately minimal: no IP address, no user agent. Who signed in,
-- when, and from what is recorded in the audit log, which is
-- hash-chained and cannot be quietly edited; a second, mutable copy here
-- would add an attack surface and no evidence. These rows exist to
-- answer one question - "is this cookie still good?" - and are deleted
-- once they can no longer answer it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS authn.local_sessions (
    session_id      TEXT        PRIMARY KEY,
    username        TEXT        NOT NULL REFERENCES authn.local_users(username),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Absolute ceiling, independent of the idle timeout the web app
    -- applies on top (PHI_AI_WEB_IDLE_MINUTES). Idle and old are
    -- different exposures and get different limits.
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    -- 'signout' | 'admin' | 'disabled' | 'password_change'. A code, not
    -- free text, so the reason a session ended is countable.
    revoked_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_local_sessions_username
    ON authn.local_sessions (username);

CREATE INDEX IF NOT EXISTS idx_local_sessions_expires
    ON authn.local_sessions (expires_at);
-- Made by Ryan Gomez & Co. Inc.
