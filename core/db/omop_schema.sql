-- PHI AI Platform: OMOP CDM analytics layer.
--
-- WHAT THIS IS AND ISN'T. The storage backend
-- (core/storage/base.py) remains the system of record for this project
-- - see core/db/schema.sql's own header for that guarantee, unchanged
-- by this file. Everything below is a SEPARATE, DERIVED, REBUILDABLE
-- materialization: a queryable, identified clinical dataset in the
-- industry-standard OHDSI OMOP Common Data Model (v5.4, stable since
-- 2021), populated by an ETL process reading from the stored objects -
-- not a second copy of the source of record, and not bound by the
-- storage layer's own immutability guarantees. Clinical data
-- legitimately needs correction (a diagnosis gets amended, an order
-- gets cancelled) - immutability is the wrong property for an
-- analytics layer, and if this ever needs to be rebuilt from scratch,
-- the storage backend is what it rebuilds from.
--
-- THIS HOLDS IDENTIFIED PHI. Unlike core/db/schema.sql's
-- stored_resources table - deliberately metadata-only, explicitly
-- never clinical content - the tables below hold real patient dates of
-- birth, diagnoses, medication exposures, and lab values, tied to a
-- persistent person_id. That is a deliberate choice, not an oversight:
-- see the design conversation this schema came out of. It means this
-- schema needs the same order of access-control and audit rigor as the
-- object store itself, applied here at the SQL layer instead of via
-- envelope encryption - see core/db/omop_bootstrap_aws.sql for the role
-- model (omop_etl, omop_analyst - genuinely separate roles from
-- phi_ai_ingest/phi_ai_reader, so the existing index's
-- never-holds-PHI guarantee stays true and unchanged).
--
-- SCOPE: the six OMOP clinical tables with a confident, direct mapping
-- from a FHIR resource type this project already ingests (see
-- core/fhir/emr_profiles.py's EPIC.supported_resources) - person,
-- visit_occurrence, condition_occurrence, procedure_occurrence,
-- drug_exposure, measurement, and observation. Deliberately NOT the
-- full ~40-table CDM speculatively built ahead of any data to put in
-- it. Three resource types are deliberately NOT mapped here yet, each
-- for a specific, stated reason rather than an oversight:
--   DocumentReference -> OMOP's own NOTE table exists for exactly this
--     (free text), but free-text clinical documents carry the same
--     elevated sensitivity this project already treats psychotherapy
--     notes with (runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md) - deferred
--     for its own design pass rather than included by default.
--   ExplanationOfBenefit -> maps toward OMOP's COST table, but a claim
--     does not correspond 1:1 to a single clinical event the way a
--     Condition or Procedure does - needs its own mapping design, not
--     a mechanical port.
--   AllergyIntolerance -> the OMOP convention (observation table, a
--     specific concept class) needs confirming against OHDSI's own
--     current documentation before committing DDL to it, rather than
--     asserted from memory alone.
--
-- THIS DDL SHOULD BE CROSS-CHECKED against OHDSI's own official
-- Postgres DDL/constraint scripts (github.com/OHDSI/CommonDataModel)
-- before relying on it for anything beyond a starting point - the
-- columns below reflect the well-established, stable core of each
-- table, not a field-for-field reproduction of the full v5.4
-- specification's every optional column. Concept_id columns below are
-- typed INTEGER, matching OMOP convention, but deliberately carry NO
-- foreign key constraint to a vocab.concept table here - the
-- Standardized Vocabularies (concept, concept_relationship, and the
-- rest) are a separate, much larger load: OHDSI distributes them
-- through its own Athena repository (athena.ohdsi.org) under
-- per-vocabulary license terms (SNOMED CT US Edition, RxNorm, and
-- LOINC are free; others, e.g. CPT, are not) - this project cannot
-- bundle or redistribute that data, and standard OMOP practice is to
-- load data before applying strict FK constraints on concept
-- references in any case. concept_id = 0 is OMOP's own convention for
-- "source code not yet mapped to a standard concept," not an error
-- state - expect it to appear during initial ETL before mapping work
-- is complete.
--
-- NON-STANDARD EXTENSION, clearly marked as such on every table below:
-- source_storage_key. Not part of the OMOP CDM specification - added
-- so every derived row can be traced back to the exact stored object
-- it came from (core/db/schema.sql's stored_resources.storage_key),
-- which is what actually makes "rebuildable from the storage backend"
-- an operational property rather than just an aspiration. OMOP itself
-- both permits and expects local extensions beyond the core spec for
-- exactly this kind of provenance need. Also UNIQUE on every table
-- below - deliberately, so the same idempotent-insert pattern
-- core/db/index.py's write_index_entry() already uses (INSERT, catch
-- the UniqueViolation, treat a duplicate as a safe no-op) works here
-- too, without needing SELECT access to check for an existing row
-- first. See omop_bootstrap_aws.sql's own comment on why that matters
-- for keeping the ETL role's access narrow.

CREATE SCHEMA IF NOT EXISTS cdm;

-- ---------------------------------------------------------------------------
-- person - maps from FHIR Patient. Anchors the whole model; every
-- clinical event table below carries a person_id foreign key back here.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.person (
    person_id                   BIGINT      NOT NULL,
    gender_concept_id           INTEGER     NOT NULL,
    -- FOUND AND FIXED (2026-08-17 audit, H7a): previously NOT NULL,
    -- which made every Patient resource with no birthDate at all (a
    -- real, valid FHIR Patient - birthDate is optional per the spec)
    -- fail the INSERT outright rather than land in the OMOP layer with
    -- an honestly-unknown birth year. OMOP's own convention for
    -- "unknown" on this specific column is a NULL, not a sentinel
    -- year - core/db/omop_etl.py's write_person() already returns None
    -- for all three of year/month/day_of_birth when Patient.birthDate
    -- is absent (see that module's _parse_fhir_birth_date()); this
    -- column just needs to accept that. Safe to change pre-production:
    -- this schema is explicitly "derived, rebuildable, never
    -- authoritative" (this file's own header) - the stored objects
    -- themselves are untouched by this change.
    year_of_birth                INTEGER,
    month_of_birth               INTEGER,
    day_of_birth                 INTEGER,
    birth_datetime                TIMESTAMPTZ,
    race_concept_id               INTEGER     NOT NULL,
    ethnicity_concept_id          INTEGER     NOT NULL,
    provider_id                   BIGINT,
    care_site_id                  BIGINT,
    person_source_value           TEXT,        -- the FHIR Patient.id (opaque, EMR-internal - same identifier already in the object's storage key, not a real-world MRN)
    gender_source_value           TEXT,
    gender_source_concept_id      INTEGER,
    race_source_value             TEXT,
    race_source_concept_id        INTEGER,
    ethnicity_source_value        TEXT,
    ethnicity_source_concept_id   INTEGER,
    source_storage_key            TEXT        NOT NULL UNIQUE,  -- non-standard extension - see this file's own header

    CONSTRAINT xpk_person PRIMARY KEY (person_id)
);

CREATE INDEX IF NOT EXISTS idx_person_source_value ON cdm.person (person_source_value);

-- ---------------------------------------------------------------------------
-- visit_occurrence - maps from FHIR Encounter.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.visit_occurrence (
    visit_occurrence_id           BIGINT      NOT NULL,
    person_id                     BIGINT      NOT NULL,
    visit_concept_id              INTEGER     NOT NULL,
    visit_start_date               DATE        NOT NULL,
    visit_start_datetime           TIMESTAMPTZ,
    visit_end_date                 DATE        NOT NULL,
    visit_end_datetime             TIMESTAMPTZ,
    visit_type_concept_id          INTEGER     NOT NULL,
    provider_id                    BIGINT,
    care_site_id                   BIGINT,
    visit_source_value             TEXT,        -- the FHIR Encounter.id
    visit_source_concept_id        INTEGER,
    admitted_from_concept_id       INTEGER,
    admitted_from_source_value     TEXT,
    discharged_to_concept_id       INTEGER,
    discharged_to_source_value     TEXT,
    preceding_visit_occurrence_id  BIGINT,
    source_storage_key             TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_visit_occurrence PRIMARY KEY (visit_occurrence_id),
    CONSTRAINT fpk_visit_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id)
);

CREATE INDEX IF NOT EXISTS idx_visit_occurrence_person ON cdm.visit_occurrence (person_id);
CREATE INDEX IF NOT EXISTS idx_visit_source_value ON cdm.visit_occurrence (visit_source_value);

-- ---------------------------------------------------------------------------
-- condition_occurrence - maps from FHIR Condition. Only positive
-- findings belong here per OMOP's own convention - absence of a
-- condition, when explicitly recorded, belongs in cdm.observation
-- instead, not as a negated row here.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.condition_occurrence (
    condition_occurrence_id       BIGINT      NOT NULL,
    person_id                     BIGINT      NOT NULL,
    condition_concept_id          INTEGER     NOT NULL,
    condition_start_date          DATE        NOT NULL,
    condition_start_datetime      TIMESTAMPTZ,
    condition_end_date            DATE,
    condition_end_datetime        TIMESTAMPTZ,
    condition_type_concept_id     INTEGER     NOT NULL,
    condition_status_concept_id   INTEGER,
    stop_reason                   TEXT,
    provider_id                   BIGINT,
    visit_occurrence_id           BIGINT,
    condition_source_value        TEXT,        -- the FHIR Condition.id
    condition_source_concept_id   INTEGER,
    condition_status_source_value TEXT,
    source_storage_key            TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_condition_occurrence PRIMARY KEY (condition_occurrence_id),
    CONSTRAINT fpk_condition_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id),
    CONSTRAINT fpk_condition_visit FOREIGN KEY (visit_occurrence_id) REFERENCES cdm.visit_occurrence (visit_occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_condition_person ON cdm.condition_occurrence (person_id);
CREATE INDEX IF NOT EXISTS idx_condition_concept ON cdm.condition_occurrence (condition_concept_id);
CREATE INDEX IF NOT EXISTS idx_condition_visit ON cdm.condition_occurrence (visit_occurrence_id);

-- ---------------------------------------------------------------------------
-- procedure_occurrence - maps from FHIR Procedure.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.procedure_occurrence (
    procedure_occurrence_id       BIGINT      NOT NULL,
    person_id                     BIGINT      NOT NULL,
    procedure_concept_id          INTEGER     NOT NULL,
    procedure_date                 DATE        NOT NULL,
    procedure_datetime             TIMESTAMPTZ,
    procedure_end_date             DATE,
    procedure_end_datetime         TIMESTAMPTZ,
    procedure_type_concept_id      INTEGER     NOT NULL,
    modifier_concept_id            INTEGER,
    quantity                       INTEGER,
    provider_id                    BIGINT,
    visit_occurrence_id            BIGINT,
    procedure_source_value         TEXT,        -- the FHIR Procedure.id
    procedure_source_concept_id    INTEGER,
    modifier_source_value          TEXT,
    source_storage_key             TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_procedure_occurrence PRIMARY KEY (procedure_occurrence_id),
    CONSTRAINT fpk_procedure_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id),
    CONSTRAINT fpk_procedure_visit FOREIGN KEY (visit_occurrence_id) REFERENCES cdm.visit_occurrence (visit_occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_procedure_person ON cdm.procedure_occurrence (person_id);
CREATE INDEX IF NOT EXISTS idx_procedure_concept ON cdm.procedure_occurrence (procedure_concept_id);
CREATE INDEX IF NOT EXISTS idx_procedure_visit ON cdm.procedure_occurrence (visit_occurrence_id);

-- ---------------------------------------------------------------------------
-- drug_exposure - maps from FHIR MedicationRequest AND FHIR Immunization
-- (vaccines are explicitly in-scope for OMOP's drug domain per OHDSI's
-- own documentation - "Drugs include prescription and over-the-counter
-- medicines, vaccines, and large-molecule biologic therapies").
--
-- A REAL ETL NUANCE, not a schema concern but worth stating here since
-- it shapes how this table gets populated: MedicationRequest is an
-- ORDER, not confirmed administration - drug_type_concept_id should
-- reflect that distinction (e.g. a "prescription written" type concept,
-- not an "administered" one) rather than treating every order as
-- confirmed exposure. Getting this conflated would misrepresent what
-- the source data actually established.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.drug_exposure (
    drug_exposure_id               BIGINT      NOT NULL,
    person_id                      BIGINT      NOT NULL,
    drug_concept_id                INTEGER     NOT NULL,
    drug_exposure_start_date       DATE        NOT NULL,
    drug_exposure_start_datetime   TIMESTAMPTZ,
    drug_exposure_end_date         DATE        NOT NULL,
    drug_exposure_end_datetime     TIMESTAMPTZ,
    verbatim_end_date              DATE,
    drug_type_concept_id           INTEGER     NOT NULL,
    stop_reason                    TEXT,
    refills                        INTEGER,
    quantity                       NUMERIC,
    days_supply                    INTEGER,
    route_concept_id                INTEGER,
    lot_number                      TEXT,
    provider_id                     BIGINT,
    visit_occurrence_id             BIGINT,
    drug_source_value               TEXT,        -- the FHIR MedicationRequest.id or Immunization.id
    drug_source_concept_id          INTEGER,
    route_source_value              TEXT,
    dose_unit_source_value          TEXT,
    source_storage_key              TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_drug_exposure PRIMARY KEY (drug_exposure_id),
    CONSTRAINT fpk_drug_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id),
    CONSTRAINT fpk_drug_visit FOREIGN KEY (visit_occurrence_id) REFERENCES cdm.visit_occurrence (visit_occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_drug_exposure_person ON cdm.drug_exposure (person_id);
CREATE INDEX IF NOT EXISTS idx_drug_exposure_concept ON cdm.drug_exposure (drug_concept_id);
CREATE INDEX IF NOT EXISTS idx_drug_exposure_visit ON cdm.drug_exposure (visit_occurrence_id);

-- ---------------------------------------------------------------------------
-- measurement - maps from FHIR Observation entries with a numeric
-- value_as_number (labs, vitals, scores). Entries without a natural
-- numeric value belong in cdm.observation below instead - the ETL layer
-- decides which table a given Observation resource lands in, not this
-- schema.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.measurement (
    measurement_id                  BIGINT      NOT NULL,
    person_id                       BIGINT      NOT NULL,
    measurement_concept_id          INTEGER     NOT NULL,
    measurement_date                 DATE        NOT NULL,
    measurement_datetime             TIMESTAMPTZ,
    measurement_time                 TEXT,        -- OMOP's own convention: text, for time-of-day when a full datetime isn't available
    measurement_type_concept_id      INTEGER     NOT NULL,
    operator_concept_id              INTEGER,     -- e.g. >, <, = - for reported values like "> 500"
    value_as_number                  NUMERIC,
    value_as_concept_id               INTEGER,
    unit_concept_id                   INTEGER,
    range_low                         NUMERIC,
    range_high                        NUMERIC,
    provider_id                       BIGINT,
    visit_occurrence_id               BIGINT,
    measurement_source_value          TEXT,        -- the FHIR Observation.id
    measurement_source_concept_id     INTEGER,
    unit_source_value                 TEXT,
    unit_source_concept_id            INTEGER,
    value_source_value                TEXT,
    source_storage_key                TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_measurement PRIMARY KEY (measurement_id),
    CONSTRAINT fpk_measurement_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id),
    CONSTRAINT fpk_measurement_visit FOREIGN KEY (visit_occurrence_id) REFERENCES cdm.visit_occurrence (visit_occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_measurement_person ON cdm.measurement (person_id);
CREATE INDEX IF NOT EXISTS idx_measurement_concept ON cdm.measurement (measurement_concept_id);
CREATE INDEX IF NOT EXISTS idx_measurement_visit ON cdm.measurement (visit_occurrence_id);

-- ---------------------------------------------------------------------------
-- observation - maps from FHIR Observation entries with no natural
-- numeric value (clinical facts, e.g. smoking status, a yes/no
-- finding) - the complement of cdm.measurement above.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.observation (
    observation_id                   BIGINT      NOT NULL,
    person_id                        BIGINT      NOT NULL,
    observation_concept_id           INTEGER     NOT NULL,
    observation_date                  DATE        NOT NULL,
    observation_datetime              TIMESTAMPTZ,
    observation_type_concept_id       INTEGER     NOT NULL,
    value_as_number                   NUMERIC,
    value_as_string                   TEXT,
    value_as_concept_id                INTEGER,
    qualifier_concept_id               INTEGER,
    unit_concept_id                    INTEGER,
    provider_id                        BIGINT,
    visit_occurrence_id                BIGINT,
    observation_source_value           TEXT,        -- the FHIR Observation.id
    observation_source_concept_id      INTEGER,
    unit_source_value                  TEXT,
    qualifier_source_value             TEXT,
    value_source_value                 TEXT,
    source_storage_key                 TEXT        NOT NULL UNIQUE,

    CONSTRAINT xpk_observation PRIMARY KEY (observation_id),
    CONSTRAINT fpk_observation_person FOREIGN KEY (person_id) REFERENCES cdm.person (person_id),
    CONSTRAINT fpk_observation_visit FOREIGN KEY (visit_occurrence_id) REFERENCES cdm.visit_occurrence (visit_occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_observation_person ON cdm.observation (person_id);
CREATE INDEX IF NOT EXISTS idx_observation_concept ON cdm.observation (observation_concept_id);
CREATE INDEX IF NOT EXISTS idx_observation_visit ON cdm.observation (visit_occurrence_id);

-- ---------------------------------------------------------------------------
-- Provenance index: every source_storage_key column above should be
-- queryable in both directions (which OMOP rows came from a given
-- stored object, for re-ETL/audit; which stored object a given OMOP
-- row traces back to, for investigation). Indexed on each table
-- individually above is sufficient for the second direction; this view
-- covers the first without needing six separate lookups.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW cdm.provenance AS
    SELECT 'person' AS cdm_table, person_id AS cdm_row_id, source_storage_key FROM cdm.person
    UNION ALL
    SELECT 'visit_occurrence', visit_occurrence_id, source_storage_key FROM cdm.visit_occurrence
    UNION ALL
    SELECT 'condition_occurrence', condition_occurrence_id, source_storage_key FROM cdm.condition_occurrence
    UNION ALL
    SELECT 'procedure_occurrence', procedure_occurrence_id, source_storage_key FROM cdm.procedure_occurrence
    UNION ALL
    SELECT 'drug_exposure', drug_exposure_id, source_storage_key FROM cdm.drug_exposure
    UNION ALL
    SELECT 'measurement', measurement_id, source_storage_key FROM cdm.measurement
    UNION ALL
    SELECT 'observation', observation_id, source_storage_key FROM cdm.observation;


-- ---------------------------------------------------------------------------
-- cdm.care_site - the facility dimension.
--
-- ADDED LATE, AND THE COLUMN IT FILLS WAS DANGLING BEFORE IT. Both
-- cdm.person and cdm.visit_occurrence have carried a care_site_id column
-- since this schema was written, because OMOP defines one - but no
-- care_site table existed and core/db/omop_etl.py never populated the
-- column, so it was NULL on every row ever written. "How many patients
-- were seen at this facility" was therefore unanswerable: not because
-- the data was missing from the stored objects, but because the ETL
-- dropped it on the floor. FHIR Encounter.serviceProvider carries it and
-- was simply never read.
--
-- A DIMENSION, NOT AN EVENT, which is why it breaks this schema's
-- otherwise-universal source_storage_key convention. Every other table
-- here has exactly one source object and declares
-- `source_storage_key TEXT NOT NULL UNIQUE`. A care site is observed
-- across many Encounters, so there is no single source object - the
-- column here records the FIRST object it was seen in, for provenance,
-- and is deliberately nullable and non-unique. Do not add the UNIQUE
-- constraint by analogy with the tables above; it would fail on the
-- second encounter at the same hospital.
--
-- NAMES COME FROM THE SOURCE EMR'S OWN reference.display, not from an
-- Organization resource - this project does not ingest Organization (see
-- core/fhir/emr_profiles.py). That is honest but imperfect: if Epic
-- populated the display text inconsistently across encounters, the same
-- facility can appear under more than one name. The id is authoritative;
-- the name is a label. care_site_name is refreshed on conflict so the
-- most recently seen label wins rather than the first.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdm.care_site (
    care_site_id                  BIGINT      NOT NULL,
    care_site_name                TEXT,
    place_of_service_concept_id   INTEGER     NOT NULL DEFAULT 0,
    location_id                   BIGINT,
    care_site_source_value        TEXT,        -- the FHIR Organization.id
    place_of_service_source_value TEXT,
    first_seen_storage_key        TEXT,        -- provenance only; see above
    CONSTRAINT pk_care_site PRIMARY KEY (care_site_id)
);

CREATE INDEX IF NOT EXISTS idx_care_site_name ON cdm.care_site (care_site_name);

-- Answering "how many patients went to this facility" is a COUNT DISTINCT
-- over person_id filtered by care_site_id, so the index that matters is
-- on the visit side, not here.
CREATE INDEX IF NOT EXISTS idx_visit_occurrence_care_site
    ON cdm.visit_occurrence (care_site_id);

-- The two joins every cohort question makes. Without these, "how many
-- patients have diabetes" is a sequential scan of condition_occurrence on
-- any real deployment.
CREATE INDEX IF NOT EXISTS idx_condition_source_value
    ON cdm.condition_occurrence (condition_source_value);
CREATE INDEX IF NOT EXISTS idx_condition_concept
    ON cdm.condition_occurrence (condition_concept_id);
CREATE INDEX IF NOT EXISTS idx_drug_exposure_concept
    ON cdm.drug_exposure (drug_concept_id);
CREATE INDEX IF NOT EXISTS idx_procedure_concept
    ON cdm.procedure_occurrence (procedure_concept_id);
-- Made by Ryan Gomez & Co. Inc.
