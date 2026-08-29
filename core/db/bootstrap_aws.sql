-- PHI AI Platform: one-time database bootstrap, AWS.
--
-- The GCP and Azure siblings are bootstrap_gcp.sql and
-- bootstrap_azure.sql - see runbooks/RUNBOOK_AWS_SETUP.md for the exact
-- psql invocation. Run ONCE, connected as the RDS master user, after
-- deploy/aws/rds.tf has applied and before pointing the application at
-- the database. The master password is a Terraform-generated value that
-- exists only in Terraform state, never in application config, and is
-- not needed again after this script runs.
--
-- Creates two roles, mirroring the ingest/restore IAM separation already
-- used for KMS access elsewhere in this project:
--   phi_ai_ingest  - INSERT only. The scheduler writes new index
--                    rows through this role and can never read,
--                    modify, or delete an existing one. Genuinely
--                    INSERT-only, not just "our code doesn't
--                    happen to SELECT" - see the note in
--                    core/db/index.py's write_index_entry() on why
--                    idempotent writes there deliberately avoid
--                    INSERT ... ON CONFLICT, which would require
--                    granting SELECT here to work at all.
--   phi_ai_reader  - SELECT only. Used for records-request lookups
--                    ("find everything held for this patient").
--                    Cannot write.
--
-- Both roles authenticate via IAM database authentication (rds_iam) -
-- there is no password for either role, ever. The IAM policies granting
-- rds-db:connect to each role's corresponding dbuser resource are in
-- deploy/aws/iam.tf.
--
-- Role creation here (CREATE ROLE ... GRANT rds_iam) is AWS-specific -
-- see bootstrap_gcp.sql and bootstrap_azure.sql for the other two
-- clouds' genuinely different mechanisms (a Terraform-managed
-- google_sql_user resource for GCP; the pgaadauth_create_principal_with_oid()
-- extension function for Azure). The GRANT statements on
-- stored_resources/index_state below this point are identical across
-- all three files by design - see core/db/schema.sql for why the table
-- itself needed no cloud-specific changes at all.

\set ON_ERROR_STOP on

CREATE ROLE phi_ai_ingest WITH LOGIN;
GRANT rds_iam TO phi_ai_ingest;

CREATE ROLE phi_ai_reader WITH LOGIN;
GRANT rds_iam TO phi_ai_reader;

-- Run core/db/schema.sql first (as master, or paste it above this line)
-- so the tables below exist before granting on them.

-- FOUND AND FIXED - explicit schema USAGE (2026-08-17 audit, C1a).
-- Previously absent; the index worked anyway only because
-- stored_resources lives in the default public schema, where PUBLIC
-- retains USAGE by default (PostgreSQL 15+ revoked only CREATE). A
-- standard CIS-style hardening step - REVOKE USAGE ON SCHEMA public
-- FROM PUBLIC - would have silently killed the index: proven against
-- live PostgreSQL 16, where the failure surfaces as the misleading
-- `relation "stored_resources" does not exist` (name resolution
-- skips schemas the role cannot use), not a permission error. These
-- explicit grants make the index's access independent of that
-- default. USAGE grants name resolution only - each role's table
-- privileges remain exactly the narrow grants below.
GRANT USAGE ON SCHEMA public TO phi_ai_ingest, phi_ai_reader;

GRANT INSERT ON stored_resources TO phi_ai_ingest;
GRANT USAGE, SELECT ON SEQUENCE stored_resources_id_seq TO phi_ai_ingest;
GRANT INSERT, SELECT, UPDATE ON index_state TO phi_ai_ingest;

GRANT SELECT ON stored_resources TO phi_ai_reader;
GRANT SELECT ON index_state TO phi_ai_reader;

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

GRANT SELECT, INSERT, UPDATE ON roi_requests TO phi_ai_reader;
GRANT USAGE, SELECT ON SEQUENCE roi_requests_id_seq TO phi_ai_reader;


-- Explicit, not just an absence: neither role can UPDATE or DELETE rows
-- in stored_resources. The index has no update/delete workflow by
-- design - see core/db/schema.sql for why the storage backend stays
-- authoritative.
--
-- Note phi_ai_ingest is NOT granted SELECT on stored_resources
-- here, and that is deliberate, not an oversight - do not "fix" this by
-- adding one. See core/db/index.py's write_index_entry() docstring.
REVOKE UPDATE, DELETE, TRUNCATE ON stored_resources FROM phi_ai_ingest, phi_ai_reader;

-- ---------------------------------------------------------------------------
-- Disposition role (2026-08-17 audit, C4). Deletes the index row for a
-- disposed object - see core/fhir/purge.py's own module
-- docstring and runbooks/RUNBOOK_DISPOSITION.md for the full design.
-- Genuinely separate from phi_ai_ingest/phi_ai_reader above:
-- neither of those ever gains DELETE, and this role gains nothing
-- beyond DELETE plus the one column SELECT its WHERE clause needs -
-- the same minimum-necessary shape the ingest/reader split already
-- uses, applied to a third capability (delete) rather than a third
-- copy of read/write.
--
-- Also granted OMOP-schema privileges, conditionally, in
-- core/db/omop_bootstrap_aws.sql (run AFTER this file - same ordering
-- runbooks/RUNBOOK_OMOP_SETUP.md already documents for omop_etl). ONE
-- Postgres role spans both schemas here - unlike omop_etl/omop_analyst,
-- which are entirely separate from phi_ai_ingest/phi_ai_reader
-- - because a single disposal operation needs to remove both the index
-- row and any OMOP row for the same resource in one pass; see
-- core/db/omop_purge.py.
--
-- Authenticates via IAM database auth (rds_iam), identical mechanism to
-- the two roles above - see deploy/aws/iam.tf's
-- ConnectToDispositionDatabase statement, present only on the
-- disposition IAM role, never on ingest or restore.
-- ---------------------------------------------------------------------------

CREATE ROLE phi_ai_disposition WITH LOGIN;
GRANT rds_iam TO phi_ai_disposition;

GRANT USAGE ON SCHEMA public TO phi_ai_disposition;

GRANT DELETE ON stored_resources TO phi_ai_disposition;
-- Column-scoped SELECT only, matching the exact WHERE clause
-- core/db/index.py's delete_index_entry() runs - Postgres requires
-- SELECT on any column a DELETE's WHERE clause reads, the identical
-- rule already documented for the re-ETL UPDATE in
-- core/db/omop_bootstrap_aws.sql (2026-08-17 audit, C1b). This role
-- cannot read resource_type, patient_reference, sha256_hex, or any
-- other column - it can confirm a storage_key exists and remove that
-- one row, nothing more. No access to index_state at all.
GRANT SELECT (storage_key) ON stored_resources TO phi_ai_disposition;

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

CREATE ROLE phi_ai_imaging WITH LOGIN;
GRANT rds_iam TO phi_ai_imaging;


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
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_studies   TO phi_ai_imaging';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_series    TO phi_ai_imaging';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_instances TO phi_ai_imaging';
END $$;
-- Made by Ryan Gomez & Co. Inc.
