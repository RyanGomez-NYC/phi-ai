-- PHI AI Platform: local account grants, GCP.
--
-- OPTIONAL. Run this ONLY if this deployment has no identity provider
-- and enabled local accounts (PHI_AI_WEB_LOCAL_ACCOUNTS). A
-- deployment behind an identity provider should not install
-- core/db/users_schema.sql at all, and should not run this.
--
-- A SEPARATE FILE from bootstrap_gcp.sql, on the same principle as
-- omop_bootstrap_gcp.sql: an optional layer gets its own bootstrap. Run
-- order, all as the Cloud SQL instance administrator:
--
--   1. core/db/schema.sql
--   2. core/db/bootstrap_gcp.sql
--   3. core/db/users_schema.sql
--   4. THIS FILE
--
-- {READER_IAM_USER} is a placeholder, exactly as in bootstrap_gcp.sql -
-- substitute the quoted role name deploy/gcp/database.tf's
-- reader_db_iam_user output provides (the restore service account's
-- email with the trailing .gserviceaccount.com stripped, per Cloud SQL's
-- own IAM database authentication username format). It contains "@" and
-- "." and MUST be double-quoted as a Postgres identifier, e.g.
--   "phi-ai-restore@my-project.iam"
--
-- See runbooks/RUNBOOK_LOCAL_USERS.md.
--
-- ---------------------------------------------------------------------------
-- WHY THE READER ROLE, AND NOT A ROLE OF ITS OWN.
--
-- On GCP specifically this is not a preference, it is the only thing
-- that works. Cloud SQL IAM database authentication derives the Postgres
-- role name from the authenticating identity's own email, so one
-- identity maps to exactly one role name - a dedicated
-- `phi_ai_authn` role would need a dedicated service account, and
-- Application Default Credentials can impersonate only one identity at a
-- time while the web process needs the index and the account store in
-- the same request. It is the identical constraint that collapsed
-- omop_etl into the ingest identity here (see deploy/gcp/database.tf's
-- own header) and that put roi_requests on the reader role.
--
-- AWS and Azure could separate this and also do not, so that the three
-- clouds' deployment shape stays identical - see
-- core/db/users_bootstrap_aws.sql for that trade stated in full, and
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

GRANT USAGE ON SCHEMA authn TO {READER_IAM_USER};

-- SELECT, INSERT and UPDATE. NOT DELETE, and its absence is the control
-- described in core/db/users_schema.sql's header: an audit entry from
-- years ago names an actor by username, and an account row that can be
-- removed makes that entry name nobody. Accounts are disabled.
GRANT SELECT, INSERT, UPDATE ON authn.local_users TO {READER_IAM_USER};

-- DELETE is granted here and only here, because revoking a role IS
-- removing the grant row - there is no "inactive grant" state, and
-- inventing one would mean every permission check had to remember to
-- filter on it. What was granted and revoked, by whom and when, is in
-- the audit log, which is hash-chained and cannot be edited from this
-- role at all.
GRANT SELECT, INSERT, DELETE ON authn.local_user_roles TO {READER_IAM_USER};

-- Sessions are the one table here that records nothing: who signed in
-- and when is in the audit log. These rows answer "is this cookie still
-- good?", and are deleted once they can no longer answer it (see
-- core/db/users.py's purge_expired_sessions).
GRANT SELECT, INSERT, UPDATE, DELETE ON authn.local_sessions TO {READER_IAM_USER};

-- Explicit, not just an absence - the same statement bootstrap_aws.sql
-- makes about stored_resources. No role may delete an account row.
REVOKE DELETE, TRUNCATE ON authn.local_users FROM {READER_IAM_USER};

-- The ingest role gets nothing here at all, deliberately: the scheduler
-- writes clinical resources and has no business reading, still less
-- writing, the table that holds every staff member's password hash. It
-- is not listed in a single GRANT above, and this comment exists so that
-- the absence reads as a decision rather than an omission.
-- Made by Ryan Gomez & Co. Inc.
