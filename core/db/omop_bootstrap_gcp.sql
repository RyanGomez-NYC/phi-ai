-- PHI AI Platform: OMOP CDM analytics layer bootstrap, GCP.
--
-- Run ONCE, connected as the Cloud SQL instance's administrator user,
-- after core/db/omop_schema.sql AND core/db/omop_vocab_schema.sql have
-- both been applied, and after core/db/bootstrap_gcp.sql (the
-- lightweight-index bootstrap). See runbooks/RUNBOOK_GCP_SETUP.md for
-- the exact psql invocation.
--
-- A GENUINE, DELIBERATE DIFFERENCE FROM omop_bootstrap_aws.sql AND
-- omop_bootstrap_azure.sql, not an oversight: this file does NOT create
-- a separate omop_etl role. See deploy/gcp/database.tf's own header for
-- the full reasoning - Cloud SQL's IAM database authentication ties a
-- Postgres role name directly to the authenticating identity's own
-- email, one service account to exactly one role name, never several.
-- A dedicated omop_etl service account would also break local
-- development via impersonation for a single scheduler.py process,
-- which needs both the index and OMOP connections simultaneously.
--
-- Given that constraint, THIS FILE GRANTS OMOP PRIVILEGES TO THE SAME
-- IAM-derived ROLE core/db/bootstrap_gcp.sql already granted the
-- lightweight-index privileges to - {INGEST_IAM_USER} below is the
-- IDENTICAL placeholder, substituted with the IDENTICAL value
-- (deploy/gcp/database.tf's ingest_db_iam_user output). On GCP
-- specifically, PHI_AI_DB_INGEST_USERNAME and
-- PHI_AI_OMOP_ETL_USERNAME should be set to the SAME value - see
-- that env var's own note in deploy/gcp/outputs.tf's env_fragment.
--
-- One real consequence worth stating plainly: the narrow, column-scoped
-- SELECT omop_bootstrap_aws.sql grants specifically to keep the ETL
-- role's read access minimal (person_id/person_source_value only, not
-- full-row PHI) is NOT independently meaningful here, since this same
-- role already holds INSERT on stored_resources too - the
-- minimum-necessary boundary between "index writer" and "OMOP writer"
-- that AWS/Azure maintain as two genuinely separate roles collapses to
-- "one identity holds both" on GCP. Real, not hidden - noted here
-- rather than glossed over.
--
-- omop_analyst (the read-only, human-facing role) is NOT provisioned by
-- this file or by deploy/gcp/database.tf - it would need its own
-- dedicated GCP service account (a human/analytics identity, not the
-- automated pipeline's), not yet built. See
-- runbooks/RUNBOOK_OMOP_SETUP.md's own note on this once the GCP
-- section is added.
--
-- FOUND AND FIXED - grants naming roles this cloud never creates. The
-- cdm.care_site block near the bottom of this file previously granted
-- to omop_etl and omop_analyst by name, and the conditional identity
-- block granted to omop_etl. Per the paragraphs above, NEITHER ROLE
-- EXISTS ON GCP. With `\set ON_ERROR_STOP on` a few lines below, the
-- unconditional care_site grant aborted the entire GCP OMOP bootstrap
-- on the first run, after the earlier grants had already applied -
-- leaving a half-configured database and a non-zero exit the runbook
-- tells the operator means "something failed". omop_etl is now
-- {INGEST_IAM_USER} throughout, matching every other grant here; the
-- omop_analyst grant is commented out with a KNOWN GAP note rather
-- than left as a statement that cannot succeed.
--
-- {INGEST_IAM_USER} below is a placeholder - substitute the exact,
-- already-quoted value deploy/gcp/database.tf's own ingest_db_iam_user
-- output provides (the SAME value core/db/bootstrap_gcp.sql's own
-- {INGEST_IAM_USER} placeholder already uses).

\set ON_ERROR_STOP on

-- FOUND AND FIXED - schema USAGE (2026-08-17 audit, C1a). Postgres
-- grants PUBLIC no USAGE on non-public schemas, so without these two
-- grants every table-level grant below was inert and the OMOP layer
-- was dead on arrival - the first INSERT INTO cdm.person failed with
-- "permission denied for schema cdm". Proven against live
-- PostgreSQL 16 (see omop_bootstrap_aws.sql's identical fix for the
-- full account). Granted to the single collapsed role, per this
-- file's own header - there is no separate omop_etl or omop_analyst
-- on GCP to grant.
GRANT USAGE ON SCHEMA cdm TO {INGEST_IAM_USER};
GRANT USAGE ON SCHEMA vocab TO {INGEST_IAM_USER};

GRANT INSERT, UPDATE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure, cdm.measurement, cdm.observation
    TO {INGEST_IAM_USER};

-- Narrow, column-scoped SELECT for find-or-create lookups - same
-- reasoning as omop_bootstrap_aws.sql's identical grant, even though
-- (per this file's own header) it does not achieve full role
-- separation here the way it does on AWS/Azure, since {INGEST_IAM_USER}
-- already holds INSERT on stored_resources via
-- core/db/bootstrap_gcp.sql regardless.
GRANT SELECT (person_id, person_source_value) ON cdm.person TO {INGEST_IAM_USER};
GRANT SELECT (visit_occurrence_id, visit_source_value, person_id) ON cdm.visit_occurrence TO {INGEST_IAM_USER};

-- FOUND AND FIXED - re-ETL UPDATE privileges (2026-08-17 audit, C1b).
-- Postgres requires SELECT on every column an UPDATE's WHERE clause
-- reads; core/db/omop_etl.py's _execute_upsert() updates by each event
-- table's deterministic pk on a duplicate-key insert, so the role
-- needs SELECT on exactly that pk column per event table - and nothing
-- else. Proven against live PostgreSQL 16 (see
-- omop_bootstrap_aws.sql's identical fix for the full account,
-- including the verified minimum-necessary property).
GRANT SELECT (condition_occurrence_id) ON cdm.condition_occurrence TO {INGEST_IAM_USER};
GRANT SELECT (procedure_occurrence_id) ON cdm.procedure_occurrence TO {INGEST_IAM_USER};
GRANT SELECT (drug_exposure_id) ON cdm.drug_exposure TO {INGEST_IAM_USER};
GRANT SELECT (measurement_id) ON cdm.measurement TO {INGEST_IAM_USER};
GRANT SELECT (observation_id) ON cdm.observation TO {INGEST_IAM_USER};

-- FOUND AND FIXED - cross-table Observation cleanup (2026-08-17 audit,
-- H7e). See omop_bootstrap_aws.sql's identical grant for the full
-- account of the bug (a re-ETL'd Observation switching between
-- cdm.measurement and cdm.observation left a stale row behind in
-- whichever table it no longer belonged in) and the fix
-- (core/db/omop_etl.py's _execute_upsert_with_cross_table_cleanup()).
-- Same minimum-necessary shape as the AWS grant: DELETE is table-wide
-- (Postgres has no column-scoped DELETE), SELECT is scoped to
-- source_storage_key only.
GRANT DELETE ON cdm.measurement, cdm.observation TO {INGEST_IAM_USER};
GRANT SELECT (source_storage_key) ON cdm.measurement TO {INGEST_IAM_USER};
GRANT SELECT (source_storage_key) ON cdm.observation TO {INGEST_IAM_USER};

-- vocab.concept is reference terminology data, not PHI - full-table
-- SELECT here carries none of the minimum-necessary concern the
-- column-scoped grants above exist for. See core/db/omop_concepts.py's
-- lookup_concept().
GRANT SELECT ON vocab.concept TO {INGEST_IAM_USER};

-- Explicit, not just an absence: {INGEST_IAM_USER} cannot DELETE from
-- any cdm table beyond the narrow measurement/observation grant above
-- (added for H7e's cross-table cleanup only), and cannot read rows
-- from the five clinical event tables beyond the pk-only and
-- source_storage_key-only columns granted above - same reasoning as
-- omop_bootstrap_aws.sql's identical REVOKE.
REVOKE DELETE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure
    FROM {INGEST_IAM_USER};


-- ---------------------------------------------------------------------------
-- cdm.care_site - the facility dimension (core/db/omop_schema.sql).
--
-- The ETL identity needs INSERT/UPDATE to record facilities as it sees
-- them on Encounters, plus the same narrow find-or-create SELECT the
-- other dimension writes use. On GCP that identity is
-- {INGEST_IAM_USER}, not a separate omop_etl role - see this file's
-- header.
--
-- KNOWN GAP: the read-only analyst grant is commented out because GCP
-- provisions no omop_analyst role for it to name (this file's header
-- says so, and deploy/gcp/database.tf creates no such service
-- account). Restore it once an analytics service account exists, using
-- that account's own IAM-derived role name.
-- ---------------------------------------------------------------------------

GRANT INSERT, UPDATE ON cdm.care_site TO {INGEST_IAM_USER};
GRANT SELECT (care_site_id) ON cdm.care_site TO {INGEST_IAM_USER};
-- GRANT SELECT ON cdm.care_site TO {ANALYST_IAM_USER};   -- see KNOWN GAP above

-- visit_occurrence.care_site_id is written by the same statement that
-- writes the visit, so no new grant is needed there.


-- ---------------------------------------------------------------------------
-- identity.patient_identity - name search.
--
-- READ core/db/identity_schema.sql's HEADER BEFORE APPLYING THIS. It is
-- the only place in this system a patient's name is stored outside an
-- encrypted object, and a copy of this table is a patient list.
--
-- THREE ROLES, AND THE SPLIT IS THE POINT - though on GCP the first of
-- them is {INGEST_IAM_USER}, since Cloud SQL gives the ETL identity no
-- separate role name of its own:
--
--   {INGEST_IAM_USER}    INSERT/UPDATE only. It populates the table and
--                        cannot read it back - so a compromised ETL
--                        credential cannot enumerate patients, only
--                        write rows it already holds in memory.
--   {IDENTITY_IAM_USER}  SELECT only. What core/analytics/identity.py
--                        connects as. This is the credential that can
--                        list people, and it is the one to guard.
--   the analyst identity NOTHING. Deliberate, and worth stating as an
--                        absence rather than leaving to inference:
--                        counting patients with a condition and finding
--                        out who they are are different privileges. An
--                        analyst role that could join cohort results to
--                        names would make every aggregate query a
--                        patient list, which is exactly the property
--                        this separation exists to prevent.
--
-- NO DELETE for anyone except disposition. A name must not outlive the
-- record it points at - core/fhir/purge.py removes the row as part of
-- the same disposal that removes the resource.
-- ---------------------------------------------------------------------------

-- GCP usernames ARE service-account emails (deploy/gcp/database.tf),
-- so substitute {IDENTITY_IAM_USER} and {DISPOSITION_IAM_USER} below as
-- this file's header describes.


-- CONDITIONAL, for the reason the imaging grants in
-- core/db/bootstrap_*.sql are: identity.patient_identity exists only if
-- the operator applied core/db/identity_schema.sql, name search is
-- off by default, and these files halt on the first error. An
-- unconditional grant here would make the OMOP bootstrap fail for every
-- deployment that enabled the analytics layer without enabling name
-- search - which is the combination the runbook actively recommends
-- starting from.

DO $$
BEGIN
    IF to_regclass('identity.patient_identity') IS NULL THEN
        RAISE NOTICE 'identity schema not installed - skipping name-search grants. This is normal unless you use core/db/identity_schema.sql.';
        RETURN;
    END IF;
    EXECUTE 'GRANT USAGE ON SCHEMA identity TO {INGEST_IAM_USER}, {IDENTITY_IAM_USER}, {DISPOSITION_IAM_USER}';
    EXECUTE 'GRANT INSERT, UPDATE ON identity.patient_identity TO {INGEST_IAM_USER}';
    EXECUTE 'GRANT SELECT (person_id) ON identity.patient_identity TO {INGEST_IAM_USER}';
    EXECUTE 'GRANT SELECT ON identity.patient_identity TO {IDENTITY_IAM_USER}';
    EXECUTE 'GRANT DELETE ON identity.patient_identity TO {DISPOSITION_IAM_USER}';
    EXECUTE 'GRANT SELECT (person_id, patient_reference) ON identity.patient_identity TO {DISPOSITION_IAM_USER}';
END $$;
-- Made by Ryan Gomez & Co. Inc.
