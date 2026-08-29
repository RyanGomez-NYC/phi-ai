-- ---------------------------------------------------------------------------
-- retrieval.* - cross-record clinical text search for the AI assistant.
--
-- READ THIS BEFORE ENABLING IT. These tables hold CLINICAL TEXT IN THE
-- CLEAR - the searchable prose of every indexed resource: condition
-- names, medication names, note text, document titles, narrative
-- sections - keyed to the patient reference each belongs to. That is a
-- materially broader exposure than any other derived store here:
-- stored_resources holds no clinical content at all, cdm.* holds coded
-- events without prose, identity.patient_identity holds names without
-- clinical content. A stolen copy of THIS is readable charts.
--
-- IT EXISTS BECAUSE THE ASSISTANT'S RESEARCH ROLE NEEDS IT. "Which
-- patients mention insulin pump failure" is not answerable from coded
-- OMOP events or from an index of opaque ids; it needs the text. The
-- deploying organisation decides whether that capability is worth this
-- surface; it is OFF by default, requires its own database roles
-- (retrieval_bootstrap_<cloud>.sql), and the application permission
-- that reaches it (`research:search`, held only by the `researcher`
-- role) demands a stated purpose of use and audits every query
-- verbatim before it runs.
--
-- LIKE EVERY DERIVED STORE, IT IS REBUILDABLE AND THE OBJECT STORE
-- WINS. Rows are written by core/db/retrieval_etl.py from the encrypted
-- objects and can be dropped and rebuilt at any time; nothing here is a
-- system of record. Search results carry storage keys so every answer
-- traces back to encrypted bytes, and reading the full record still
-- goes through the audited read path - the snippet is the most this
-- table ever discloses directly.
--
-- PSYCHOTHERAPY NOTES ARE A SEPARATE TABLE WITH A SEPARATE READ ROLE,
-- mirroring their separate bucket and separate KMS key at the storage
-- layer (runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md). The general search
-- role holds no grant on it, so no widening of general research access
-- can quietly include the record class 45 CFR 164.508(a)(2) treats
-- differently - the same reasoning the `psychotherapy` application
-- role states in core/web/auth.py. The ETL writes it ONLY when run
-- with --include-psychotherapy against the psychotherapy store.
--
-- DISPOSAL. A row here must die with the resource it was extracted
-- from. core/fhir/purge.py deletes retrieval rows by storage_key in the
-- same disposal operation as the index and OMOP rows;
-- core/fhir/psychotherapy_purge.py does the same for the psychotherapy
-- table. Indexed text that outlives its record is a retention violation
-- with no upside - the identity index's rule, applied to prose.
--
-- SEARCH IS POSTGRES FULL-TEXT (tsvector + GIN), not an embedding
-- store. Deliberate, and the same reasoning core/assistant/knowledge.py
-- gives for the documentation corpus: embeddings would mean a second
-- network dependency and an external model seeing clinical text at
-- index time. websearch_to_tsquery gives operators quoted phrases and
-- exclusions with no new egress and no new operational surface. The
-- tsvector column is GENERATED so it cannot drift from the text.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS retrieval;

CREATE TABLE IF NOT EXISTS retrieval.clinical_text (
    -- The encrypted object this text was extracted from - the traceable
    -- path back to bytes, and the disposal key.
    storage_key       TEXT        NOT NULL,
    -- Position within the object: 0 for a single-resource object,
    -- 0..n-1 within an NDJSON bundle (see core/storage/layout.py).
    resource_index    INTEGER     NOT NULL DEFAULT 0,

    patient_reference TEXT,
    resource_type     TEXT        NOT NULL,
    resource_id       TEXT,
    -- The resource's own clinical date where one could be parsed
    -- (core/fhir/clinical_dates.py's notion, re-derived at ETL time) -
    -- lets research questions bound their window without opening records.
    clinical_date     DATE,

    -- The searchable prose, extracted by core/db/retrieval_text.py.
    content           TEXT        NOT NULL,
    content_tsv       tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- The source object's content digest (StoredObjectMetadata.sha256_hex)
    -- at extraction time - the ETL's skip-unchanged signal, carried in
    -- the rows themselves so it can never disagree with what is indexed.
    source_digest     TEXT,

    indexed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_retrieval_clinical_text PRIMARY KEY (storage_key, resource_index)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_clinical_tsv
    ON retrieval.clinical_text USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_retrieval_clinical_patient
    ON retrieval.clinical_text (patient_reference);
CREATE INDEX IF NOT EXISTS idx_retrieval_clinical_type
    ON retrieval.clinical_text (resource_type);

-- Psychotherapy notes: same shape, separate table, separate grant.
-- See the header - the separation IS the point, so no view unions them.
CREATE TABLE IF NOT EXISTS retrieval.psychotherapy_text (
    storage_key       TEXT        NOT NULL,
    resource_index    INTEGER     NOT NULL DEFAULT 0,

    patient_reference TEXT,
    resource_type     TEXT        NOT NULL,
    resource_id       TEXT,
    clinical_date     DATE,

    content           TEXT        NOT NULL,
    content_tsv       tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    source_digest     TEXT,

    indexed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_retrieval_psychotherapy_text PRIMARY KEY (storage_key, resource_index)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_psych_tsv
    ON retrieval.psychotherapy_text USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_retrieval_psych_patient
    ON retrieval.psychotherapy_text (patient_reference);
-- Made by Ryan Gomez & Co. Inc.
