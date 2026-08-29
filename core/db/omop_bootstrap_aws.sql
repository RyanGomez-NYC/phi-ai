-- PHI AI Platform: OMOP CDM analytics layer bootstrap, AWS.
--
-- Run ONCE, connected as the RDS master user, after core/db/omop_schema.sql
-- AND core/db/omop_vocab_schema.sql have both been applied (both
-- schemas' tables must exist before the grants below can succeed).
-- Mirrors core/db/bootstrap_aws.sql's own role-creation mechanism
-- (CREATE ROLE ... GRANT rds_iam) exactly - see that file for the full
-- reasoning. The GCP and Azure siblings (omop_bootstrap_gcp.sql,
-- omop_bootstrap_azure.sql) both exist and follow each cloud's own
-- role mechanism; see each sibling's own header for its cloud-specific
-- differences (GCP's one-identity-one-role collapse in particular).
--
-- Creates two roles - genuinely NEW roles, distinct from
-- phi_ai_ingest/phi_ai_reader (core/db/bootstrap_aws.sql).
-- That separation is deliberate: it is what keeps stored_resources'
-- own "never holds clinical content" guarantee true and unchanged even
-- though this schema, right next to it in the same database, now does
-- hold identified PHI.
--
-- (omop_etl and omop_analyst keep their names: they describe a role in
-- the OMOP Common Data Model, not this product.)
--
--   omop_etl      - writes derived OMOP rows from the stored resources
--                   via ETL. INSERT on every cdm table. UPDATE on
--                   every cdm table too - unlike the resource store,
--                   OMOP data legitimately gets corrected when a source
--                   resource is re-ingested with a fix, so pure
--                   INSERT-only (the stored_resources pattern) is the
--                   wrong model here. The implemented pattern
--                   (core/db/omop_etl.py's _execute_upsert()): attempt
--                   INSERT first, and on the unique violation a re-ETL
--                   of the same resource produces, follow with a plain
--                   UPDATE ... WHERE <pk> = <deterministic id> instead
--                   of retrying the insert - not an INSERT ... ON
--                   CONFLICT, since evaluating a conflict target
--                   requires SELECT on the conflict-target columns
--                   (the same reasoning core/db/index.py's
--                   write_index_entry() docstring gives for
--                   stored_resources), and this role deliberately
--                   does NOT get broad SELECT (see below).
--
--                   CORRECTED, previously wrong in this header: a
--                   plain UPDATE with a WHERE clause DOES require
--                   SELECT on every column the WHERE condition reads -
--                   Postgres's own documented permission model, and
--                   confirmed against live PostgreSQL 16 by running
--                   this file and then the ETL's own UPDATE as
--                   omop_etl. That is exactly why the narrow pk-column
--                   SELECT grants further down exist; without them the
--                   re-ETL UPDATE fails with "permission denied" on
--                   every event table.
--
--                   SELECT is granted, but narrowly, at the COLUMN
--                   level, only where ETL genuinely cannot proceed
--                   without it: cdm.person(person_id,
--                   person_source_value) and
--                   cdm.visit_occurrence(visit_occurrence_id,
--                   visit_source_value, person_id) - the find-or-create
--                   lookups needed to attach a new clinical event to
--                   the correct existing person/visit rather than
--                   minting a duplicate - plus, on each of the five
--                   clinical event tables, ONLY that table's own
--                   primary-key column, which the re-ETL UPDATE's
--                   WHERE clause reads (see the CORRECTED note above).
--                   This is a genuinely different, narrower shape of
--                   access than "SELECT on the whole table" - omop_etl
--                   can confirm a person_source_value already has a
--                   person_id, and can match an event row by its own
--                   deterministic id, but cannot read a birth date, a
--                   diagnosis code, or any other clinical column
--                   through these grants - verified live: those reads
--                   still fail with permission denied.
--
--   omop_analyst  - SELECT only, full row access, across every cdm
--                   table and the cdm.provenance view. This is real,
--                   broad access to identified PHI by construction -
--                   that is what an analytics role over this schema
--                   necessarily is, and no role name changes that. The
--                   compensating control is NOT built here in SQL: it
--                   is connection-level audit logging (every
--                   omop_analyst connection and, where the Postgres
--                   version/extension support allows, every statement,
--                   logged with the same rigor the audit chain already
--                   applies elsewhere) and an organizational
--                   documented-purpose requirement before this role is
--                   actually granted to a person or service - see
--                   runbooks/RUNBOOK_OMOP_SETUP.md's own account of
--                   this, once written. Cohort- or row-level scoping
--                   (an analyst restricted to a specific population)
--                   is real further hardening worth a future pass, not
--                   included in this first cut.
--
-- Both roles authenticate via IAM database authentication (rds_iam),
-- identical mechanism to core/db/bootstrap_aws.sql - no password for
-- either role, ever.

\set ON_ERROR_STOP on

CREATE ROLE omop_etl WITH LOGIN;
GRANT rds_iam TO omop_etl;

CREATE ROLE omop_analyst WITH LOGIN;
GRANT rds_iam TO omop_analyst;

-- Run core/db/omop_schema.sql first (as master, or paste it above this
-- line) so the tables below exist before granting on them.

-- FOUND AND FIXED - schema USAGE. Postgres grants PUBLIC no USAGE on
-- non-public schemas, so without these two grants every table-level
-- grant below was inert: the very first INSERT INTO cdm.person by
-- omop_etl failed with "permission denied for schema cdm", and
-- omop_analyst's reads and both roles' vocab.concept lookups failed
-- the same way - the whole OMOP layer was dead on arrival. Proven
-- against live PostgreSQL 16 by applying this exact file and running
-- the ETL's own statements as each role (2026-08-17 audit, C1a).
-- USAGE only permits resolving objects inside the schema; every
-- table-level privilege still comes from the explicit grants below,
-- so this adds no read or write access by itself.
GRANT USAGE ON SCHEMA cdm TO omop_etl, omop_analyst;
GRANT USAGE ON SCHEMA vocab TO omop_etl, omop_analyst;

GRANT INSERT, UPDATE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure, cdm.measurement, cdm.observation
    TO omop_etl;

-- Narrow, column-scoped SELECT for find-or-create lookups only - see
-- this file's own header for why this is deliberately not table-wide.
GRANT SELECT (person_id, person_source_value) ON cdm.person TO omop_etl;
GRANT SELECT (visit_occurrence_id, visit_source_value, person_id) ON cdm.visit_occurrence TO omop_etl;

-- FOUND AND FIXED - re-ETL UPDATE privileges (2026-08-17 audit, C1b).
-- Postgres requires SELECT on every column an UPDATE's WHERE clause
-- reads; core/db/omop_etl.py's _execute_upsert() runs
-- UPDATE ... WHERE <pk> = <deterministic id> on a duplicate-key
-- insert, so omop_etl needs SELECT on exactly the pk column of each
-- event table - and nothing else. Without these, the exact scenario
-- the deterministic-ID design exists for (a corrected resource
-- re-ingested) failed with "permission denied" on every event table;
-- person/visit_occurrence survived only because their pk columns
-- happen to fall inside the find-or-create grants above. Proven live,
-- including the minimum-necessary property: with only these pk-column
-- grants, omop_etl still cannot read any clinical column or whole
-- rows - those SELECTs still fail with permission denied.
GRANT SELECT (condition_occurrence_id) ON cdm.condition_occurrence TO omop_etl;
GRANT SELECT (procedure_occurrence_id) ON cdm.procedure_occurrence TO omop_etl;
GRANT SELECT (drug_exposure_id) ON cdm.drug_exposure TO omop_etl;
GRANT SELECT (measurement_id) ON cdm.measurement TO omop_etl;
GRANT SELECT (observation_id) ON cdm.observation TO omop_etl;

-- FOUND AND FIXED - cross-table Observation cleanup (2026-08-17 audit,
-- H7e). A FHIR Observation can switch which OMOP table it belongs in
-- across a re-ETL of the SAME resource - e.g. a lab result initially
-- recorded as a coded value (routed to cdm.observation by
-- core/db/omop_etl.py's write_measurement_or_observation()) later
-- corrected to carry a numeric valueQuantity (routed to
-- cdm.measurement instead). Both tables key on the same
-- deterministic_id("Observation", ...) and the same source_storage_key,
-- but they are DIFFERENT tables - a plain INSERT/UPDATE-by-pk into the
-- new table left the stale row behind in the old one, so the same
-- source Observation appeared in both tables simultaneously with two
-- different values. core/db/omop_etl.py's
-- _execute_upsert_with_cross_table_cleanup() now DELETEs any matching
-- row in the OTHER table (by source_storage_key) in the same
-- transaction as the INSERT into the correct one, which needs DELETE
-- on both tables plus enough SELECT to identify the row to delete -
-- granted narrowly below, matching this file's existing
-- minimum-necessary posture: DELETE is table-wide (Postgres has no
-- column-scoped DELETE), but the accompanying SELECT is scoped to
-- source_storage_key only, the single column the DELETE's WHERE
-- clause reads - proven live that this does NOT extend to reading any
-- clinical column (value_as_number, measurement_concept_id, etc.)
-- through these grants.
GRANT DELETE ON cdm.measurement, cdm.observation TO omop_etl;
GRANT SELECT (source_storage_key) ON cdm.measurement TO omop_etl;
GRANT SELECT (source_storage_key) ON cdm.observation TO omop_etl;

GRANT SELECT ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure, cdm.measurement, cdm.observation,
    cdm.provenance
    TO omop_analyst;

-- vocab.concept (core/db/omop_vocab_schema.sql) is reference
-- terminology data, not PHI - unlike the narrow, column-scoped grants
-- above, full-table SELECT here carries none of the same
-- minimum-necessary concern. Both roles need it: omop_etl to resolve
-- source codes to concept_id at write time (core/db/omop_concepts.py's
-- lookup_concept()), omop_analyst to join concept_id columns back to
-- human-readable concept_name when querying.
GRANT SELECT ON vocab.concept TO omop_etl;
GRANT SELECT ON vocab.concept TO omop_analyst;

-- Explicit, not just an absence: omop_etl cannot DELETE from
-- person/visit_occurrence/condition_occurrence/procedure_occurrence/
-- drug_exposure, and cannot read rows from the five clinical event
-- tables beyond the pk-only and source_storage_key-only columns
-- granted above. omop_analyst cannot write anything. Neither role can
-- touch stored_resources/index_state (core/db/schema.sql) or vice
-- versa - the two schemas' role sets are deliberately disjoint.
--
-- cdm.measurement and cdm.observation are DELIBERATELY EXCLUDED from
-- this REVOKE, unlike the other five tables - omop_etl genuinely does
-- hold DELETE there (granted above, H7e), needed for the cross-table
-- Observation cleanup _execute_upsert_with_cross_table_cleanup()
-- performs. Listing them here would silently undo that grant, since
-- REVOKE executes after GRANT in statement order in this file - this
-- is not an oversight, it's why they're absent from the list below.
REVOKE DELETE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure
    FROM omop_etl;

-- ---------------------------------------------------------------------------
-- Disposition role - OMOP-schema grants (2026-08-17 audit, C4). The
-- phi_ai_disposition role itself is CREATEd in
-- core/db/bootstrap_aws.sql, which MUST be run before this file - same
-- ordering already documented for omop_etl connecting to both schemas.
-- This is the one deliberate exception to "omop_etl/omop_analyst are
-- entirely separate roles from phi_ai_ingest/phi_ai_reader"
-- above: a single disposal operation needs to remove both the index
-- row and any OMOP row for the same resource in one pass, so ONE role
-- spans both schemas here. See core/db/omop_purge.py for the delete
-- logic and the FK-safe table order this depends on.
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA cdm TO phi_ai_disposition;

GRANT DELETE ON
    cdm.person, cdm.visit_occurrence, cdm.condition_occurrence,
    cdm.procedure_occurrence, cdm.drug_exposure, cdm.measurement, cdm.observation
    TO phi_ai_disposition;

-- Column-scoped SELECT only, matching the exact WHERE clause
-- core/db/omop_purge.py's delete_by_source_storage_key() runs on every
-- table - same minimum-necessary reasoning as omop_etl's pk-column
-- grants above; this role cannot read a birth date, a diagnosis code,
-- or any other clinical column through these grants.
GRANT SELECT (source_storage_key) ON cdm.person TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.visit_occurrence TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.condition_occurrence TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.procedure_occurrence TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.drug_exposure TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.measurement TO phi_ai_disposition;
GRANT SELECT (source_storage_key) ON cdm.observation TO phi_ai_disposition;

-- vocab.concept is reference data, not PHI - this role has no need to
-- read it (it deletes rows by source_storage_key, never resolves a
-- concept_id), so no grant here, unlike omop_etl/omop_analyst above.


-- ---------------------------------------------------------------------------
-- cdm.care_site - the facility dimension (core/db/omop_schema.sql).
--
-- omop_etl needs INSERT/UPDATE to record facilities as it sees them on
-- Encounters, plus the same narrow find-or-create SELECT the other
-- dimension writes use. omop_analyst needs full SELECT to answer "how
-- many patients went to this facility" - a care site name is not PHI.
-- ---------------------------------------------------------------------------

GRANT INSERT, UPDATE ON cdm.care_site TO omop_etl;
GRANT SELECT (care_site_id) ON cdm.care_site TO omop_etl;
GRANT SELECT ON cdm.care_site TO omop_analyst;

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
-- ---------------------------------------------------------------------------

CREATE ROLE phi_ai_identity WITH LOGIN;
GRANT rds_iam TO phi_ai_identity;


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
