-- PHI AI Platform: one-time database bootstrap, Azure.
--
-- Run ONCE, connected as the Flexible Server's Microsoft Entra
-- administrator, after deploy/azure/database.tf has applied and before
-- pointing the application at the database. See
-- runbooks/RUNBOOK_AZURE_SETUP.md for the exact psql invocation.
--
-- Creates the same two roles as bootstrap_aws.sql, using Azure's own
-- mechanism instead of AWS's CREATE ROLE ... GRANT rds_iam:
-- pgaadauth_create_principal_with_oid(), a function the PGAadAuth
-- extension provides automatically once Microsoft Entra authentication
-- is enabled on the server (deploy/azure/database.tf sets
-- authentication.active_directory_auth_enabled = true). Unlike GCP,
-- this lets the role name be freely chosen rather than forced to match
-- the identity's own name format - kept identical to AWS's
-- phi_ai_ingest / phi_ai_reader here for exactly that reason,
-- so the same two role names are meaningful across two of the three
-- clouds (GCP's own IAM-derived naming is a genuine, documented
-- exception - see bootstrap_gcp.sql).
--
--   phi_ai_ingest  - INSERT only. Same role and same reasoning as
--                    bootstrap_aws.sql - see that file and
--                    core/db/index.py's write_index_entry()
--                    docstring for why this is genuinely
--                    INSERT-only, not just unused SELECT access.
--   phi_ai_reader  - SELECT only. Cannot write.
--
-- KNOWN GAP, not a rename artefact: the DICOM grants at the bottom of
-- this file name phi_ai_imaging, and no principal of that name is
-- created anywhere in this file; there is likewise no disposition
-- principal here, although bootstrap_aws.sql creates one. Closing
-- either needs the imaging/disposition managed identities' object IDs
-- as new deploy/azure outputs, so it is a Terraform change rather than
-- an edit here. Until then, an Azure deployment that installs the
-- optional imaging schema will fail on the EXECUTE'd grant.
--
-- {INGEST_PRINCIPAL_ID} and {READER_PRINCIPAL_ID} below are
-- placeholders - substitute the exact object (principal) IDs
-- deploy/azure/database.tf's own ingest_identity_principal_id /
-- restore_identity_principal_id outputs provide (the ingest and restore
-- managed identities already provisioned in identities.tf - this
-- bootstrap grants THOSE existing identities database access, it does
-- not create new ones). 'service' is the correct principal type for a
-- managed identity - confirmed from Microsoft's own documentation
-- examples; do not substitute 'user' or 'group' here.

\set ON_ERROR_STOP on

SELECT * FROM pgaadauth_create_principal_with_oid('phi_ai_ingest', '{INGEST_PRINCIPAL_ID}', 'service', false, false);
SELECT * FROM pgaadauth_create_principal_with_oid('phi_ai_reader', '{READER_PRINCIPAL_ID}', 'service', false, false);

-- Run core/db/schema.sql first (as the Entra administrator, or paste it
-- above this line) so the tables below exist before granting on them.

-- FOUND AND FIXED - explicit schema USAGE (2026-08-17 audit, C1a).
-- Previously absent; worked anyway only because PUBLIC retains USAGE
-- on the public schema by default. A CIS-style REVOKE USAGE ON SCHEMA
-- public FROM PUBLIC would have silently killed the index, with the
-- misleading error `relation "stored_resources" does not exist` -
-- proven against live PostgreSQL 16; see bootstrap_aws.sql's identical
-- fix for the full account.
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
--
-- See the KNOWN GAP note in this file's header: phi_ai_imaging is not
-- created here, so these grants fail on Azure until the imaging managed
-- identity's object ID is exposed as a deploy/azure output.
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
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_studies   TO phi_ai_imaging';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_series    TO phi_ai_imaging';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON dicom_instances TO phi_ai_imaging';
END $$;
-- Made by Ryan Gomez & Co. Inc.
