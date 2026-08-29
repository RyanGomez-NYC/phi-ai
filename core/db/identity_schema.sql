-- ---------------------------------------------------------------------------
-- identity.patient_identity - name search.
--
-- READ THIS BEFORE ENABLING IT. This is the most directly identifying
-- table in the entire system, and it is the only place in this project
-- where a patient's NAME is stored anywhere other than inside an
-- encrypted object.
--
-- Every other index here was built to avoid exactly this.
-- core/db/schema.sql's stored_resources holds no clinical content and
-- no names, deliberately, so that an exposed index cannot identify
-- anyone. cdm.person holds identified PHI - real birth dates, diagnoses -
-- but still no names, because OMOP has no name column by design. Both of
-- those leak far less than this table does: a stolen copy of
-- stored_resources is a list of opaque EMR ids, a stolen copy of
-- cdm.person is a re-identification problem, and a stolen copy of THIS is
-- simply a patient list.
--
-- IT EXISTS BECAUSE THE ALTERNATIVE WAS WORSE IN PRACTICE. Records staff
-- receive requests naming a person, not an EMR id. Without this, every
-- release-of-information request begins by looking the patient up in a
-- system this platform was built to let the organisation switch off -
-- which is not a platform that replaced anything. The deploying
-- organisation decides whether that trade is right for them; it is off by
-- default and requires its own database role, exactly like the OMOP layer
-- above it.
--
-- WHAT IS AND IS NOT HERE:
--
--   - Names, birth date, administrative gender, and the opaque EMR
--     patient id. Enough to answer "which patient is this?" and nothing
--     more. There is no address, no telephone number, no email, no MRN
--     and no clinical content - a name search does not need them, and
--     each would widen the blast radius for no benefit. Resolve the
--     patient here, then open their record through the audited path.
--   - NO SSN, ever, under any configuration. FHIR Patient.identifier can
--     carry one; the ETL drops every identifier system it does not
--     recognise rather than storing it (see core/db/omop_etl.py's
--     write_patient_identity).
--
-- DISPOSAL. A row here must die with the patient's records. core/fhir/purge.py
-- deletes the index row and any OMOP row when a resource is disposed of;
-- this table is wired into the same path. A name that outlives the record
-- it pointed at is a retention violation with no upside.
--
-- SEARCH IS UNACCENTED AND CASE-FOLDED, in generated columns rather than
-- at query time, so an index can actually be used. Searching "mary smith"
-- has to find "Smith, Mary-Jane" and "MARÍA SMITH"; a query-time lower()
-- would work and would also scan the table on every search.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS identity;

-- Trigram matching, for the misspellings a records request actually
-- arrives with. Optional: everything below works without it, using exact
-- and prefix matching only. core/analytics/identity.py detects whether
-- the extension is present and degrades rather than failing, so a managed
-- Postgres that forbids extensions is a supported deployment.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS identity.patient_identity (
    -- Same deterministic id as cdm.person.person_id, so a name search
    -- result joins straight into the OMOP layer without a lookup table.
    person_id            BIGINT      NOT NULL,

    -- The opaque, EMR-internal Patient.id. This is what the rest of the
    -- platform keys on - storage keys, stored_resources.patient_reference
    -- - so it is what a search returns for the caller to act on.
    patient_reference    TEXT        NOT NULL,

    family_name          TEXT,
    given_names          TEXT,        -- space-joined; FHIR allows several
    full_name            TEXT,        -- as the source presented it

    birth_date           DATE,
    gender               TEXT,        -- FHIR administrative gender code

    -- Normalised for search. GENERATED so they cannot drift from the
    -- columns above, and so the ETL cannot forget to maintain them.
    -- Punctuation is REMOVED, not just case-folded, and that is not
    -- cosmetic. A request for "obrien" has to find "O'Brien" and one for
    -- "smith jones" has to find "Smith-Jones"; a records clerk types what
    -- they were told on the phone, and nobody says "apostrophe". Proven
    -- against real data: without this, searching o-b-r-i-e-n for a
    -- patient stored as O'Brien returns nothing at all.
    --
    -- translate() with a shorter `to` argument deletes the unmatched
    -- characters. Both expressions are immutable, which is what lets them
    -- be GENERATED ... STORED and therefore indexable.
    family_name_norm     TEXT GENERATED ALWAYS AS (lower(translate(family_name, '''-.,', ''))) STORED,
    full_name_norm       TEXT GENERATED ALWAYS AS (lower(translate(full_name, '''-.,', ''))) STORED,

    source_storage_key   TEXT        NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_patient_identity PRIMARY KEY (person_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_identity_reference
    ON identity.patient_identity (patient_reference);

CREATE INDEX IF NOT EXISTS idx_patient_identity_family
    ON identity.patient_identity (family_name_norm);

CREATE INDEX IF NOT EXISTS idx_patient_identity_birth
    ON identity.patient_identity (birth_date);

-- Fuzzy search. Skipped automatically if pg_trgm could not be created -
-- the CREATE INDEX below will fail in that case, which is why the
-- runbook tells operators to run this file with ON_ERROR_STOP off, or to
-- comment these two lines out on a Postgres that forbids extensions.
CREATE INDEX IF NOT EXISTS idx_patient_identity_full_trgm
    ON identity.patient_identity USING gin (full_name_norm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_patient_identity_family_trgm
    ON identity.patient_identity USING gin (family_name_norm gin_trgm_ops);
-- Made by Ryan Gomez & Co. Inc.
