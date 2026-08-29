-- ---------------------------------------------------------------------------
-- DICOM imaging index.
--
-- READ THIS BEFORE ENABLING IT. This schema holds IDENTIFYING PHI:
-- patient names, dates of birth, sex, accession numbers, referring
-- physicians and study descriptions, in plain columns, queryable. That is
-- a materially different and broader access surface than
-- core/db/schema.sql's stored_resources index, whose own header states
-- it holds no clinical content and no identifiers beyond the EMR's opaque
-- patient reference. Nothing here weakens that rule - this is a separate
-- set of tables, behind a separate database role, opt-in and off by
-- default, deliberately shaped like the OMOP analytics layer
-- (core/db/omop_schema.sql) rather than like the lightweight index.
--
-- WHY IT CANNOT BE OTHERWISE. A DICOM viewer's worklist is a list of
-- patients, dates and study descriptions - that IS the query surface
-- QIDO-RS specifies (DICOM PS3.18 §10.6), and the OHIF viewer this
-- platform embeds sends exactly those search keys. A de-identified
-- imaging index would return studies no records clerk could identify,
-- which is not a privacy control, it is a broken feature. So the control
-- here is access, not omission: a separate role, a separate grant, an
-- explicit opt-in, and every read audited.
--
-- STORAGE REMAINS THE SYSTEM OF RECORD, exactly as for FHIR resources.
-- Every row here is derivable by re-reading the objects under the
-- `dicom/` prefix. If this index is lost, corrupted or drifts, the
-- imaging is not lost - it is unindexed, and can be rebuilt.
--
-- BURNED-IN ANNOTATION IS NOT ADDRESSED BY ANY COLUMN HERE. Pixel data
-- can itself carry a name or an accession number, rendered in by the
-- modality. See core/dicom/model.py's header and
-- runbooks/RUNBOOK_DICOM_IMAGING.md.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dicom_studies (
    study_instance_uid       TEXT        PRIMARY KEY,

    -- Joins imaging to the rest of the platform. In an Epic-sourced
    -- deployment this is the same opaque identifier that appears in
    -- stored_resources.patient_reference, so a patient's imaging and
    -- their FHIR records resolve to each other with no second identity
    -- map. Where the source PACS used a different identifier space it
    -- will not join, and that is a data problem to fix at ingest.
    patient_reference        TEXT,
    patient_id               TEXT,

    -- Identifying attributes. Present because QIDO-RS requires them, not
    -- because the platform wants them - see this file's header.
    patient_name             TEXT,
    patient_birth_date       TEXT,       -- DICOM DA, YYYYMMDD, kept verbatim
    patient_sex              TEXT,
    accession_number         TEXT,
    study_date               TEXT,       -- DICOM DA
    study_time               TEXT,       -- DICOM TM
    study_description        TEXT,
    referring_physician_name TEXT,

    -- Denormalised for the worklist: a viewer shows modality and counts
    -- per study without opening the series. Recomputed on every ingest of
    -- the study rather than incremented, so a re-ingest is idempotent.
    modalities               TEXT,       -- comma-separated, e.g. "CT,SR"
    series_count             INTEGER     NOT NULL DEFAULT 0,
    instance_count           INTEGER     NOT NULL DEFAULT 0,

    stored_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_until          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dicom_studies_patient
    ON dicom_studies (patient_reference);
CREATE INDEX IF NOT EXISTS idx_dicom_studies_accession
    ON dicom_studies (accession_number);
CREATE INDEX IF NOT EXISTS idx_dicom_studies_date
    ON dicom_studies (study_date);
CREATE INDEX IF NOT EXISTS idx_dicom_studies_retention
    ON dicom_studies (retention_until);


CREATE TABLE IF NOT EXISTS dicom_series (
    series_instance_uid  TEXT PRIMARY KEY,
    study_instance_uid   TEXT NOT NULL REFERENCES dicom_studies (study_instance_uid)
                              ON DELETE CASCADE,
    modality             TEXT NOT NULL,
    series_number        INTEGER,
    series_description   TEXT,
    body_part_examined   TEXT,
    instance_count       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dicom_series_study
    ON dicom_series (study_instance_uid);


CREATE TABLE IF NOT EXISTS dicom_instances (
    sop_instance_uid     TEXT PRIMARY KEY,
    series_instance_uid  TEXT NOT NULL REFERENCES dicom_series (series_instance_uid)
                              ON DELETE CASCADE,
    -- Denormalised so WADO-RS can authorise and locate an instance from
    -- its full three-UID path in one query rather than three.
    study_instance_uid   TEXT NOT NULL,

    sop_class_uid        TEXT NOT NULL,
    instance_number      INTEGER,
    rows                 INTEGER,
    columns              INTEGER,
    bits_allocated       INTEGER,
    number_of_frames     INTEGER NOT NULL DEFAULT 1,
    transfer_syntax_uid  TEXT NOT NULL,

    -- Where the encrypted object lives, and what it hashed to when
    -- written. Same integrity contract as stored_resources: the digest
    -- covers the STORED bytes (nonce + ciphertext), not the plaintext.
    storage_key          TEXT NOT NULL UNIQUE,
    storage_version_id   TEXT,
    sha256_hex           TEXT NOT NULL,
    size_bytes           BIGINT NOT NULL DEFAULT 0,

    stored_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dicom_instances_series
    ON dicom_instances (series_instance_uid, instance_number);
CREATE INDEX IF NOT EXISTS idx_dicom_instances_study
    ON dicom_instances (study_instance_uid);
-- Made by Ryan Gomez & Co. Inc.
