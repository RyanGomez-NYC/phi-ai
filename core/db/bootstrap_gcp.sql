-- PHI AI Platform: one-time database bootstrap, GCP.
--
-- Run ONCE, connected as the Cloud SQL instance's administrator user,
-- after deploy/gcp/database.tf has applied and before pointing the
-- application at the database. See runbooks/RUNBOOK_GCP_SETUP.md for
-- the exact psql invocation.
--
-- UNLIKE bootstrap_aws.sql and bootstrap_azure.sql, this file does NOT
-- create the phi_ai_ingest/phi_ai_reader roles - Cloud SQL's
-- IAM database authentication model creates them differently: as
-- google_sql_user Terraform resources with type = "CLOUD_IAM_SERVICE_ACCOUNT"
-- (see deploy/gcp/database.tf), which Cloud SQL automatically surfaces
-- as Postgres roles once applied. By the time this script runs, both
-- roles already exist; this script only grants privileges on them - the
-- exact same GRANT statements as the other two clouds' bootstrap files,
-- see core/db/schema.sql for why the table itself needed no
-- cloud-specific changes.
--
-- {INGEST_IAM_USER} and {READER_IAM_USER} below are placeholders -
-- substitute the exact, quoted role names deploy/gcp/database.tf's own
-- ingest_db_iam_user / reader_db_iam_user outputs provide (the ingest
-- and restore service accounts' emails, each with the trailing
-- .gserviceaccount.com stripped, per Cloud SQL's own IAM database
-- authentication username format - see core/db/connection.py's
-- _connect_gcp() docstring for the full explanation of why this format
-- is required rather than a freely-chosen name). These values contain
-- "@" and "." and MUST be double-quoted as Postgres identifiers, e.g.:
--   "phi-ai-ingest@my-project.iam"
-- Do not substitute an unquoted or differently-formatted value - Cloud
-- SQL will not recognize a role name that doesn't exactly match its own
-- derived format.

\set ON_ERROR_STOP on

-- FOUND AND FIXED - explicit schema USAGE (2026-08-17 audit, C1a).
-- Previously absent; worked anyway only because PUBLIC retains USAGE
-- on the public schema by default. A CIS-style REVOKE USAGE ON SCHEMA
-- public FROM PUBLIC would have silently killed the index, with the
-- misleading error `relation "stored_resources" does not exist` -
-- proven against live PostgreSQL 16; see bootstrap_aws.sql's identical
-- fix for the full account.
GRANT USAGE ON SCHEMA public TO {INGEST_IAM_USER}, {READER_IAM_USER};

GRANT INSERT ON stored_resources TO {INGEST_IAM_USER};
GRANT USAGE, SELECT ON SEQUENCE stored_resources_id_seq TO {INGEST_IAM_USER};
GRANT INSERT, SELECT, UPDATE ON index_state TO {INGEST_IAM_USER};

GRANT SELECT ON stored_resources TO {READER_IAM_USER};
GRANT SELECT ON index_state TO {READER_IAM_USER};

-- ---------------------------------------------------------------------------
-- Release of information (roi_requests).
--
-- FOUND AND FIXED: this table has existed in core/db/schema.sql since
-- release of information was added, and NO role was ever granted anything
-- on it in any of the three bootstrap files. Every ROI operation - raising
-- a request, fulfilling it, denying it, listing them - would have failed
-- with a bare Postgres permission error the first time an HIM user tried
-- to use the page. It went unnoticed because a second bug hid it:
-- core/web/__main__.py connected as `settings.db_username`, which is not a
-- field on Settings, so `python -m core.web` died with AttributeError at
-- startup and nobody reached the ROI page at all.
--
-- Granted to the READER role rather than a dedicated one. On GCP a
-- Postgres username IS the service account's email (deploy/gcp/database.tf),
-- so a `phi_ai_roi` role cannot be created portably - the same Cloud
-- SQL IAM constraint README.md names as a genuine architectural difference
-- between the clouds. Role separation is preserved where it carries the
-- weight: the reader's grants on stored_resources are untouched and
-- still SELECT-only, so the index cannot be mutated from the web
-- interface. roi_requests is workflow state, not the index.
--
-- NO DELETE, TO ANY ROLE. These rows are the accounting of disclosures
-- under 45 CFR 164.528 (see core/db/schema.sql's own header on this
-- table). A disclosure record that can be erased is not an accounting, so
-- the absence of a DELETE grant here is a control, not an oversight. A
-- denied or superseded request is closed by UPDATE-ing its status, never
-- by removing the row.
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE ON roi_requests TO {READER_IAM_USER};
GRANT USAGE, SELECT ON SEQUENCE roi_requests_id_seq TO {READER_IAM_USER};


-- Explicit, not just an absence: neither role can UPDATE or DELETE rows
-- in stored_resources. The index has no update/delete workflow by
-- design - see core/db/schema.sql for why the storage backend stays
-- authoritative.
--
-- Note the ingest role is NOT granted SELECT on stored_resources
-- here, and that is deliberate, not an oversight - do not "fix" this by
-- adding one. See core/db/index.py's write_index_entry() docstring.
REVOKE UPDATE, DELETE, TRUNCATE ON stored_resources FROM {INGEST_IAM_USER}, {READER_IAM_USER};

-- ---------------------------------------------------------------------------
-- DICOM imaging index (optional).
--
-- Only granted if you ran core/db/imaging_schema.sql. This role is
-- SEPARATE from the reader deliberately: those tables hold identifying
-- PHI - patient names, birth dates, accession numbers - in queryable
-- columns, which stored_resources explicitly does not. See
-- core/db/imaging_schema.sql's own header for why a DICOM worklist
-- cannot be de-identified and still work.
--
-- SELECT/INSERT/UPDATE, never DELETE. Removing imaging is disposal, and
-- disposal runs through the disposition role and its own procedure
-- (runbooks/RUNBOOK_DISPOSITION.md), not as a side effect of whatever
-- process happens to hold the imaging credentials.
-- ---------------------------------------------------------------------------


-- CONDITIONAL, AND IT HAS TO BE. The comment above always said "only
-- granted if you ran imaging_schema.sql" - but the GRANTs below it were
-- unconditional, and every one of these files sets `\set ON_ERROR_STOP
-- on` a few lines from the top. So on a deployment that had NOT installed
-- the optional imaging schema, which is the default, the documented base
-- setup ran every real grant, then hit "relation dicom_studies does not
-- exist", halted, and exited non-zero - while the setup runbook told the
-- operator that stopping early means "something failed". Nothing had
-- failed, and nothing was missing, but a bootstrap that reports failure
-- stops a deployment just as effectively as one that actually fails.
--
-- to_regclass() returns NULL rather than raising for a missing relation,
-- which is what makes the check itself safe to run when the table is
-- absent. The GRANTs go through EXECUTE because a plain GRANT inside a
-- DO block is still parsed at block-creation time.

DO $$
BEGIN
    IF to_regclass('public.dicom_studies') IS NULL THEN
        RAISE NOTICE 'imaging schema not installed - skipping DICOM grants. This is normal unless you use core/db/imaging_schema.sql.';
        RETURN;
    END IF;
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_studies   TO {IMAGING_IAM_USER}';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_series    TO {IMAGING_IAM_USER}';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_instances TO {IMAGING_IAM_USER}';
END $$;
-- Made by Ryan Gomez & Co. Inc.
