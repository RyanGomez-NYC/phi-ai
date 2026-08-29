-- PHI AI Platform: local account grants, Azure.
--
-- OPTIONAL. Run this ONLY if this deployment has no identity provider
-- and enabled local accounts (PHI_AI_WEB_LOCAL_ACCOUNTS). A
-- deployment behind an identity provider should not install
-- core/db/users_schema.sql at all, and should not run this.
--
-- A SEPARATE FILE from bootstrap_azure.sql, on the same principle as
-- omop_bootstrap_azure.sql: an optional layer gets its own bootstrap.
-- Run order, all as the Flexible Server's Microsoft Entra administrator:
--
--   1. core/db/schema.sql
--   2. core/db/bootstrap_azure.sql
--   3. core/db/users_schema.sql
--   4. THIS FILE
--
-- No placeholders: bootstrap_azure.sql already registered
-- phi_ai_reader under that exact name via
-- pgaadauth_create_principal_with_oid(), which - unlike GCP - lets a
-- role name be chosen freely rather than derived from the identity.
--
-- See runbooks/RUNBOOK_LOCAL_USERS.md.
--
-- ---------------------------------------------------------------------------
-- WHY THE READER ROLE, AND NOT A ROLE OF ITS OWN.
--
-- Azure COULD have one. pgaadauth_create_principal_with_oid() would
-- happily register the same managed identity under a second role name,
-- exactly as it already does for omop_etl - so unlike GCP, nothing here
-- forces this. It is on the reader role so that the three clouds'
-- deployment shape, bootstrap procedure and runbook stay identical,
-- which was preferred to a separation available on two clouds out of
-- three. Stated as the trade it is; see
-- core/db/users_bootstrap_aws.sql for the fuller account of what that
-- separation would and would not buy, and
-- runbooks/RUNBOOK_LOCAL_USERS.md's "Known gaps".
--
-- What is NOT given up: the reader's grants on stored_resources are
-- untouched and still SELECT-only.
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
