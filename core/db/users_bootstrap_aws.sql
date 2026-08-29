-- PHI AI Platform: local account grants, AWS.
--
-- OPTIONAL. Run this ONLY if this deployment has no identity provider
-- and enabled local accounts (PHI_AI_WEB_LOCAL_ACCOUNTS). A
-- deployment behind oauth2-proxy, an OIDC-enabled ALB, or Azure App
-- Service Authentication should not install core/db/users_schema.sql at
-- all, and should not run this - an unused credential store is still a
-- credential store somebody has to protect.
--
-- A SEPARATE FILE from bootstrap_aws.sql, on the same principle as
-- omop_bootstrap_aws.sql: an optional layer gets its own bootstrap, so
-- the base setup neither depends on it nor has to guard every statement
-- against its absence. Run order:
--
--   1. core/db/schema.sql              (as the RDS master user)
--   2. core/db/bootstrap_aws.sql       (as the RDS master user)
--   3. core/db/users_schema.sql        (as the RDS master user)
--   4. THIS FILE                       (as the RDS master user)
--
-- See runbooks/RUNBOOK_LOCAL_USERS.md.
--
-- ---------------------------------------------------------------------------
-- WHY THE READER ROLE, AND NOT A ROLE OF ITS OWN.
--
-- The honest answer, stated plainly rather than presented as a design:
-- on GCP a Postgres username IS the authenticating service account's own
-- email (deploy/gcp/database.tf, core/db/connection.py), so one identity
-- maps to exactly one role name and a dedicated `phi_ai_authn` role
-- cannot be created there at all. It is the identical Cloud SQL IAM
-- constraint that put roi_requests on the reader role, and the identical
-- one that collapsed omop_etl into the ingest identity on GCP.
--
-- AWS AND AZURE COULD GENUINELY SEPARATE THIS, and today do not - see
-- runbooks/RUNBOOK_LOCAL_USERS.md's "Known gaps". Keeping the three
-- clouds' deployment shape identical was preferred to a separation that
-- exists on two of them; that is a trade, not a claim that separation
-- would be worthless.
--
-- What separation would and would not buy, so the trade is legible: the
-- web process needs both the index and the account store in the same
-- request, so a second role would be a second connection held by the
-- SAME process, defending against a SQL-level mistake in this
-- application rather than against a compromise of it. Real, but narrow.
--
-- What is NOT given up: the reader's grants on stored_resources are
-- untouched and still SELECT-only. The index still cannot be mutated
-- from the web interface.
-- ---------------------------------------------------------------------------

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF to_regclass('authn.local_users') IS NULL THEN
        RAISE EXCEPTION 'authn.local_users does not exist. Run core/db/users_schema.sql first - this file only grants privileges, it does not create the tables.';
    END IF;
END $$;

GRANT USAGE ON SCHEMA authn TO phi_ai_reader;

-- SELECT, INSERT and UPDATE. NOT DELETE, and its absence is the control
-- described in core/db/users_schema.sql's header: an audit entry from
-- years ago names an actor by username, and an account row that can be
-- removed makes that entry name nobody. Accounts are disabled.
GRANT SELECT, INSERT, UPDATE ON authn.local_users TO phi_ai_reader;

-- DELETE is granted here and only here, because revoking a role IS
-- removing the grant row - there is no "inactive grant" state, and
-- inventing one would mean every permission check had to remember to
-- filter on it. What was granted and revoked, by whom and when, is in
-- the audit log, which is hash-chained and cannot be edited from this
-- role at all.
GRANT SELECT, INSERT, DELETE ON authn.local_user_roles TO phi_ai_reader;

-- Sessions are the one table here that records nothing: who signed in
-- and when is in the audit log. These rows answer "is this cookie still
-- good?", and are deleted once they can no longer answer it (see
-- core/db/users.py's purge_expired_sessions).
GRANT SELECT, INSERT, UPDATE, DELETE ON authn.local_sessions TO phi_ai_reader;

-- Explicit, not just an absence - the same statement bootstrap_aws.sql
-- makes about stored_resources. No role may delete an account row.
REVOKE DELETE, TRUNCATE ON authn.local_users FROM phi_ai_reader;

-- The ingest role gets nothing here at all, deliberately: the scheduler
-- writes clinical resources and has no business reading, still less
-- writing, the table that holds every staff member's password hash. It
-- is not listed in a single GRANT above, and this comment exists so that
-- the absence reads as a decision rather than an omission.
-- Made by Ryan Gomez & Co. Inc.
