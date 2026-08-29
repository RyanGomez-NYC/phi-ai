-- PHI AI Platform: OMOP standardized vocabulary, structure only.
--
-- This table's STRUCTURE only - it is created EMPTY. The actual
-- vocabulary content (millions of rows: every SNOMED/ICD-10-CM/RxNorm/
-- LOINC/CPT4 code and its mapping to a standard concept) must be
-- downloaded separately from OHDSI's own Athena repository
-- (athena.ohdsi.org) and loaded here - this project cannot bundle or
-- redistribute that data. Licensing is per-vocabulary, not blanket:
-- SNOMED CT US Edition, RxNorm, and LOINC are free to use; several
-- others (e.g. CPT4) require a separate license from their own
-- publisher. See runbooks/RUNBOOK_OMOP_SETUP.md (once written) for the
-- download and load procedure.
--
-- Column set and types match the OMOP CDM v5.4 CONCEPT table - the
-- single most stable, most-referenced table in the entire
-- Standardized Vocabularies. Deliberately just this one table for now,
-- not the full vocabulary schema (concept_relationship,
-- concept_ancestor, vocabulary, domain, concept_class, and others) -
-- concept alone is sufficient for the direct source-code-to-concept_id
-- lookup core/db/omop_concepts.py needs today (see that module's
-- lookup_concept()). concept_relationship in particular - needed for
-- mapping a non-standard source concept to its Standard equivalent via
-- the "Maps to" relationship, rather than assuming a source code is
-- already the standard one - is real, valuable follow-on work, not
-- included here.
--
-- Until this table is populated, every lookup against it returns no
-- rows, and core/db/omop_concepts.py's lookup_concept() falls back to
-- concept_id = 0 - OMOP's own convention for "not yet mapped," not an
-- error condition. ETL against an empty vocabulary is expected to
-- produce concept_id = 0 everywhere except the small set of concepts
-- core/db/omop_concepts.py hardcodes directly (currently: the three
-- verified gender concepts) - see that module's own docstring.

CREATE SCHEMA IF NOT EXISTS vocab;

CREATE TABLE IF NOT EXISTS vocab.concept (
    concept_id        INTEGER     NOT NULL,
    concept_name       TEXT        NOT NULL,
    domain_id           TEXT        NOT NULL,
    vocabulary_id        TEXT        NOT NULL,
    concept_class_id      TEXT        NOT NULL,
    standard_concept       TEXT,        -- 'S' = Standard, 'C' = Classification, NULL = neither
    concept_code             TEXT        NOT NULL,
    valid_start_date          DATE        NOT NULL,
    valid_end_date             DATE        NOT NULL,
    invalid_reason              TEXT,

    CONSTRAINT xpk_concept PRIMARY KEY (concept_id)
);

-- The lookup this project's ETL actually performs: "given a source
-- vocabulary and code, what's the concept_id" - e.g. (vocabulary_id =
-- 'ICD10CM', concept_code = 'I21.9'). Not declared UNIQUE - the real
-- OMOP vocabulary can contain more than one concept_id for the same
-- (vocabulary_id, concept_code) pair across different valid date
-- ranges (a code's meaning changing over time) or concept classes: see
-- core/db/omop_concepts.py's lookup_concept() for how ETL resolves
-- that when it happens, rather than assuming this index alone
-- guarantees a single match.
CREATE INDEX IF NOT EXISTS idx_concept_code_vocab
    ON vocab.concept (vocabulary_id, concept_code);
-- Made by Ryan Gomez & Co. Inc.
