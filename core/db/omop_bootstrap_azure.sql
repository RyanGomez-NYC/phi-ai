-- PHI AI Platform: OMOP CDM analytics layer bootstrap, Azure.
--
-- Run ONCE, connected as the Flexible Server's Microsoft Entra
-- administrator, after core/db/omop_schema.sql AND
-- core/db/omop_vocab_schema.sql have both been applied, and after
-- core/db/bootstrap_azure.sql (the lightweight-index bootstrap). See
-- runbooks/RUNBOOK_AZURE_SETUP.md for the exact psql invocation.
--
-- UNLIKE omop_bootstrap_gcp.sql, this file DOES create a genuinely
-- separate omop_etl role - see deploy/azure/database.tf's own header
-- for why: Azure's pgaadauth_create_principal_with_oid() lets a
-- Postgres role name be chosen freely, independent of the
-- authenticating identity's own name, unlike Cloud SQL's IAM model
-- (which forces one identity to exactly one role name). The SAME
-- managed identity (deploy/azure/identities.tf's ingest) is registered
-- here under a SECOND role name, omop_etl - genuinely distinct from
-- phi_ai_ingest (core/db/bootstrap_azure.sql), preserving the same
-- role separation AWS achieves, restated for Azure's own mechanism.
--
-- No omop_analyst equivalent is provisioned here yet - Azure's model
-- could support one the same way AWS's does (a second registration
-- with its own identity), it just hasn't been built. Unlike the GCP
-- sibling, that is a gap, not a structural constraint.
--
-- KNOWN GAPS, both consequences of that missing provisioning rather
-- than of any rename:
--   - The cdm.care_site analyst grant near the bottom of this file is
--     commented out. It named omop_analyst, which this file's own
--     header says Azure does not create; with `\set ON_ERROR_STOP on`
--     it aborted the whole Azure OMOP bootstrap on first run.
--   - The conditional identity.patient_identity block still names
--     phi_ai_identity and phi_ai_disposition, and nothing on Azure
--     registers either principal. It is inside a to_regclass() guard,
--     so it only bites a deployment that installed the optional name
--     search. Closing it needs those managed identities' object IDs
--     exposed as deploy/azure outputs, so it is a Terraform change
--     rather than an edit here.
--
-- {INGEST_PRINCIPAL_ID} below is a placeholder - substitute the exact
-- object (principal) ID deploy/azure/outputs.tf's own
-- ingest_identity_principal_id output provides. This is the SAME value
-- core/db/bootstrap_azure.sql's own {INGEST_PRINCIPAL_ID} placeholder
-- already uses for that file's phi_ai_ingest registration - the
-- identity is identical; only the resulting Postgres role name differs
-- between the two files. 'service' is the correct principal type for a
-- managed identity - confirmed from Microsoft's own documentation
-- examples; do not substitute 'user' or 'group' here.

\set ON_ERROR_STOP on

SELECT * FROM pgaadauth_create_principal_with_oid('omop_etl', '{INGEST_PRINCIPAL_ID}', 'service', false, false);

-- Run core/db/omop_schema.sql and core/db/omop_vocab_schema.sql first
-- (as the Entra administrator, or paste them above this line) so the
-- tables below exist before granting on them.

-- FOUND AND FIXED - schema USAGE (2026-08-17 audit, C1a). Postgres
-- grants PUBLIC no USAGE on non-public schemas, so without these two
-- grants every table-level grant below was inert and the OMOP layer
-- was dead on arrival - the first INSERT INTO cdm.person by omop_etl
-- failed with "permission denied for schema cdm". Proven against live
-- PostgreSQL 16 (see omop_bootstrap_aws.sql's identical fix for the
-- full account). Covers omop_etl only - there is no omop_analyst on
-- Azure yet (see this file's own header).
GRANT USAGE ON SCHEMA cdm TO omop_etl;
GRANT USAGE ON SCHEMA vocab TO omop_etl;

GRANT INSERT, UPDATE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure, cdm.measurement, cdm.observation
    TO omop_etl;

-- Narrow, column-scoped SELECT for find-or-create lookups only - same
-- reasoning as omop_bootstrap_aws.sql's identical grant, and (unlike
-- omop_bootstrap_gcp.sql) genuinely meaningful here: omop_etl holds no
-- other privileges anywhere in this database, so this really is the
-- full extent of what it can read.
GRANT SELECT (person_id, person_source_value) ON cdm.person TO omop_etl;
GRANT SELECT (visit_occurrence_id, visit_source_value, person_id) ON cdm.visit_occurrence TO omop_etl;

-- FOUND AND FIXED - re-ETL UPDATE privileges (2026-08-17 audit, C1b).
-- Postgres requires SELECT on every column an UPDATE's WHERE clause
-- reads; core/db/omop_etl.py's _execute_upsert() updates by each event
-- table's deterministic pk on a duplicate-key insert, so omop_etl
-- needs SELECT on exactly that pk column per event table - and nothing
-- else. Proven against live PostgreSQL 16 (see
-- omop_bootstrap_aws.sql's identical fix for the full account,
-- including the verified minimum-necessary property).
GRANT SELECT (condition_occurrence_id) ON cdm.condition_occurrence TO omop_etl;
GRANT SELECT (procedure_occurrence_id) ON cdm.procedure_occurrence TO omop_etl;
GRANT SELECT (drug_exposure_id) ON cdm.drug_exposure TO omop_etl;
GRANT SELECT (measurement_id) ON cdm.measurement TO omop_etl;
GRANT SELECT (observation_id) ON cdm.observation TO omop_etl;

-- FOUND AND FIXED - cross-table Observation cleanup (2026-08-17 audit,
-- H7e). See omop_bootstrap_aws.sql's identical grant for the full
-- account of the bug (a re-ETL'd Observation switching between
-- cdm.measurement and cdm.observation left a stale row behind in
-- whichever table it no longer belonged in) and the fix
-- (core/db/omop_etl.py's _execute_upsert_with_cross_table_cleanup()).
-- Same minimum-necessary shape as the AWS grant: DELETE is table-wide
-- (Postgres has no column-scoped DELETE), SELECT is scoped to
-- source_storage_key only.
GRANT DELETE ON cdm.measurement, cdm.observation TO omop_etl;
GRANT SELECT (source_storage_key) ON cdm.measurement TO omop_etl;
GRANT SELECT (source_storage_key) ON cdm.observation TO omop_etl;

-- vocab.concept is reference terminology data, not PHI - full-table
-- SELECT here carries none of the minimum-necessary concern the
-- column-scoped grants above exist for. See core/db/omop_concepts.py's
-- lookup_concept().
GRANT SELECT ON vocab.concept TO omop_etl;

-- Explicit, not just an absence: omop_etl cannot DELETE from
-- person/visit_occurrence/condition_occurrence/procedure_occurrence/
-- drug_exposure, and cannot read rows from the five clinical event
-- tables beyond the pk-only and source_storage_key-only columns
-- granted above. omop_etl also has no access whatsoever to
-- stored_resources/index_state (core/db/schema.sql) or vice versa -
-- the two role sets are entirely disjoint, unlike the GCP installment
-- where they necessarily collapse into one identity.
--
-- cdm.measurement and cdm.observation are DELIBERATELY EXCLUDED from
-- this REVOKE, unlike the other five tables - omop_etl genuinely does
-- hold DELETE there (granted above, H7e). See
-- omop_bootstrap_aws.sql's identical REVOKE for the full reasoning on
-- why listing them here would silently undo that grant.
REVOKE DELETE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure
    FROM omop_etl;


-- ---------------------------------------------------------------------------
-- cdm.care_site - the facility dimension (core/db/omop_schema.sql).
--
-- omop_etl needs INSERT/UPDATE to record facilities as it sees them on
-- Encounters, plus the same narrow find-or-create SELECT the other
-- dimension writes use.
--
-- KNOWN GAP: the read-only analyst grant is commented out because
-- Azure provisions no omop_analyst role for it to name - see this
-- file's header. Restore it in the same change that registers an
-- analytics principal via pgaadauth_create_principal_with_oid().
-- ---------------------------------------------------------------------------

GRANT INSERT, UPDATE ON cdm.care_site TO omop_etl;
GRANT SELECT (care_site_id) ON cdm.care_site TO omop_etl;
-- GRANT SELECT ON cdm.care_site TO omop_analyst;   -- see KNOWN GAP above

-- visit_occurrence.care_site_id is written by the same statement that
-- writes the visit, so no new grant is needed there.


-- ---------------------------------------------------------------------------
-- identity.patient_identity - name search.
--
-- READ core/db/identity_schema.sql's HEADER BEFORE APPLYING THIS. It is
-- the only place in this system a patient's name is stored outside an
-- encrypted object, and a copy of this table is a patient list.
--
-- THREE ROLES, AND THE SPLIT IS THE POINT:
--
--   omop_etl          INSERT/UPDATE only. It populates the table and
--                     cannot read it back - so a compromised ETL
--                     credential cannot enumerate patients, only
--                     write rows it already holds in memory.
--   phi_ai_identity   SELECT only. What core/analytics/identity.py
--                     connects as. This is the credential that can
--                     list people, and it is the one to guard.
--   omop_analyst      NOTHING. Deliberate, and worth stating as an
--                     absence rather than leaving to inference:
--                     counting patients with a condition and finding
--                     out who they are are different privileges. An
--                     analyst role that could join cohort results to
--                     names would make every aggregate query a
--                     patient list, which is exactly the property
--                     this separation exists to prevent.
--
-- NO DELETE for anyone except disposition. A name must not outlive the
-- record it points at - core/fhir/purge.py removes the row as part of
-- the same disposal that removes the resource.
--
-- KNOWN GAP, repeated from this file's header because it bites here:
-- Azure registers neither phi_ai_identity nor phi_ai_disposition, so
-- the block below fails on a deployment that installed the optional
-- name-search schema. It needs those identities' object IDs as
-- deploy/azure outputs first.
-- ---------------------------------------------------------------------------

-- Azure creates principals with pgaadauth_create_principal_with_oid();
-- see this file's own header. The role names below must already exist.


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
    EXECUTE 'GRANT USAGE ON SCHEMA identity TO omop_etl, phi_ai_identity, phi_ai_disposition';
    EXECUTE 'GRANT INSERT, UPDATE ON identity.patient_identity TO omop_etl';
    EXECUTE 'GRANT SELECT (person_id) ON identity.patient_identity TO omop_etl';
    EXECUTE 'GRANT SELECT ON identity.patient_identity TO phi_ai_identity';
    EXECUTE 'GRANT DELETE ON identity.patient_identity TO phi_ai_disposition';
    EXECUTE 'GRANT SELECT (person_id, patient_reference) ON identity.patient_identity TO phi_ai_disposition';
END $$;
-- Made by Ryan Gomez & Co. Inc.
