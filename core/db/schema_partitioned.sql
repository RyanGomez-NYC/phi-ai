-- PHI AI Platform: queryable index schema, PARTITIONED variant.
--
-- Used when PHI_AI_PROFILE=large. Identical to schema.sql in every
-- column, constraint and comment that matters - the HARD RULE about
-- clinical content applies here unchanged - differing only in that
-- stored_resources is a partitioned table.
--
-- WHY LIST PARTITIONING ON resource_type, and not hash on patient:
--
--   * The type set is small, known and stable, so the partition list is
--     bounded and readable rather than an opaque number of hash buckets.
--   * Retention and disposal are ALREADY expressed per resource type
--     (retention_years_overrides, and purge scanning by type), so
--     partitions line up with the operations that scan whole ranges -
--     disposal of an expired type prunes to one partition instead of
--     scanning everything.
--   * Adding a resource type is a DDL change either way, and this one is
--     visible: a missing partition fails loudly on insert rather than
--     silently landing rows somewhere unhelpful.
--
-- THE COST, stated rather than buried: restore-by-patient touches every
-- partition instead of one. Each partition is a fraction of the total and
-- carries its own patient index, so the penalty is real but small. The
-- alternative - hashing on patient_reference - would make find_by_type
-- scan every bucket, and find_by_type is what disposal runs across the
-- entire deployment.
--
-- MUST BE CREATED BEFORE INGESTION. Converting a populated table to a
-- partitioned one rewrites it in place; on a large deployment that is
-- days of downtime. Choosing the profile late is the expensive mistake
-- here.
--
-- A DEFAULT partition catches types not listed below, so an unexpected
-- resourceType is stored rather than rejected. Check it periodically: rows
-- landing there mean a type is missing from this list, and a DEFAULT
-- partition that grows large loses the pruning benefit for everything.

CREATE TABLE IF NOT EXISTS stored_resources (

    id                    BIGSERIAL,
    resource_type         TEXT        NOT NULL,
    resource_id           TEXT        NOT NULL,   -- the source EMR's internal FHIR id, opaque
    patient_reference     TEXT,                    -- e.g. "Patient/eAB12cd3" - internal FHIR ref, NULL if not patient-linked
    storage_key           TEXT        NOT NULL,
    storage_version_id    TEXT,
    sha256_hex            TEXT        NOT NULL,
    stored_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_until       TIMESTAMPTZ,

    -- How many FHIR resources this OBJECT holds. 1 under the small
    -- profile, where an object is a resource. Under the large profile an
    -- object is an NDJSON bundle, and this is what lets verification
    -- compare counts against the source WITHOUT decrypting anything -
    -- preserving the property that a verification job needs no clinical
    -- read access. Without it, counting resources in a bundled store
    -- would require opening every bundle.
    resource_count        INTEGER     NOT NULL DEFAULT 1,

    -- Every UNIQUE constraint on a partitioned table must contain the
    -- partition key. resource_type is already part of both, so these
    -- carry exactly the same guarantee they do unpartitioned.
    CONSTRAINT uq_stored_resources_type_id UNIQUE (resource_type, resource_id),
    CONSTRAINT uq_stored_resources_storage_key UNIQUE (resource_type, storage_key)
)
PARTITION BY LIST (resource_type);

-- One partition per supported resource type.
CREATE TABLE IF NOT EXISTS stored_resources_patient
    PARTITION OF stored_resources FOR VALUES IN ('Patient');
CREATE TABLE IF NOT EXISTS stored_resources_encounter
    PARTITION OF stored_resources FOR VALUES IN ('Encounter');
CREATE TABLE IF NOT EXISTS stored_resources_observation
    PARTITION OF stored_resources FOR VALUES IN ('Observation');
CREATE TABLE IF NOT EXISTS stored_resources_condition
    PARTITION OF stored_resources FOR VALUES IN ('Condition');
CREATE TABLE IF NOT EXISTS stored_resources_medicationrequest
    PARTITION OF stored_resources FOR VALUES IN ('MedicationRequest');
CREATE TABLE IF NOT EXISTS stored_resources_documentreference
    PARTITION OF stored_resources FOR VALUES IN ('DocumentReference');
CREATE TABLE IF NOT EXISTS stored_resources_allergyintolerance
    PARTITION OF stored_resources FOR VALUES IN ('AllergyIntolerance');
CREATE TABLE IF NOT EXISTS stored_resources_immunization
    PARTITION OF stored_resources FOR VALUES IN ('Immunization');
CREATE TABLE IF NOT EXISTS stored_resources_procedure
    PARTITION OF stored_resources FOR VALUES IN ('Procedure');
CREATE TABLE IF NOT EXISTS stored_resources_explanationofbenefit
    PARTITION OF stored_resources FOR VALUES IN ('ExplanationOfBenefit');
CREATE TABLE IF NOT EXISTS stored_resources_adverseevent
    PARTITION OF stored_resources FOR VALUES IN ('AdverseEvent');
CREATE TABLE IF NOT EXISTS stored_resources_consent
    PARTITION OF stored_resources FOR VALUES IN ('Consent');
CREATE TABLE IF NOT EXISTS stored_resources_servicerequest
    PARTITION OF stored_resources FOR VALUES IN ('ServiceRequest');
CREATE TABLE IF NOT EXISTS stored_resources_medicationadministration
    PARTITION OF stored_resources FOR VALUES IN ('MedicationAdministration');
CREATE TABLE IF NOT EXISTS stored_resources_diagnosticreport
    PARTITION OF stored_resources FOR VALUES IN ('DiagnosticReport');

-- Anything not listed above. Monitor it: rows here mean a type is
-- missing from the list, and a large DEFAULT partition loses pruning.
CREATE TABLE IF NOT EXISTS stored_resources_default
    PARTITION OF stored_resources DEFAULT;


-- No index on resource_type: it IS the partition key, so the planner
-- prunes on it directly and an index would be dead weight.

CREATE INDEX IF NOT EXISTS idx_stored_resources_patient_reference
    ON stored_resources (patient_reference)
    WHERE patient_reference IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stored_resources_stored_at
    ON stored_resources (stored_at);

-- Generic key/value operational-state store. Currently used for exactly
-- one thing: core/fhir/scheduler.py's incremental high-water mark
-- (SCHEDULER_WATERMARK_KEY, key = 'scheduler_last_successful_run')
-- - see that module's own comment on SCHEDULER_WATERMARK_KEY, and
-- core/db/index.py's read_index_state()/write_index_state().
--
-- THIS TABLE IS THE STORE FOR THAT VALUE, not a mirror of anything
-- else. Holding the watermark as a storage object instead was tried
-- and is gone: it was unreadable in practice (SSE-KMS under the PHI
-- key, which the ingest role deliberately cannot decrypt), and every
-- write of it silently inherited the bucket-level default Object Lock
-- retention, accumulating undeletable locked versions forever. See
-- core/fhir/scheduler.py's own SCHEDULER_WATERMARK_KEY comment.
--
-- This does NOT relax this project's core "the storage backend is
-- always the system of record" invariant stated at the top of this
-- file and in stored_resources's own header above. That invariant
-- governs PHI content - the clinical resources themselves. The
-- scheduler watermark is operational bookkeeping about the ingestion
-- PROCESS, not PHI and not clinical content. Postgres holding the
-- authoritative copy of one small piece of process state is a
-- different question from Postgres ever being authoritative for the
-- data itself; it still isn't, and stored_resources above is still
-- exactly as derived/rebuildable as documented.
--
-- Deliberate, disclosed tradeoff: a deployment with no Postgres index
-- configured gets no persisted watermark at all, and performs a full
-- run every cycle - safe (ingestion is idempotent) but not cheap. See
-- core/fhir/scheduler.py's load_watermark()/save_watermark().
CREATE TABLE IF NOT EXISTS index_state (
    key         TEXT PRIMARY KEY,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    value       TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- Release of information (ROI) requests.
--
-- SAME HARD RULE AS ABOVE, and it constrains this table's shape more than
-- it might first appear. A real ROI request names a requester - "Smith &
-- Associates LLP", an insurer, or the patient themselves - and often
-- references an authorization document. None of that belongs in this
-- database, for exactly the reason the stored_resources comment gives:
-- this index must not itself become a store of personal data.
--
-- So the request SPLITS. Everything identifying lives in an encrypted
-- stored object (roi/request/<id>.json), written through the same
-- envelope encryption as clinical content. This table holds only
-- structural facts: an opaque request id, the opaque patient reference
-- already indexed above, a requester TYPE code (not a name), status,
-- timestamps, the authenticated usernames who acted, and storage keys.
--
-- That split is also what makes a disclosure reproducible. The record set
-- actually produced is stored as its own object, so the question a
-- records custodian is eventually asked - "what exactly did you release,
-- and when?" - is answerable from the storage backend years later rather
-- than reconstructed from a query that may no longer return the same rows.
--
-- 45 CFR 164.528 (accounting of disclosures) is the regulatory hook: an
-- individual may request an accounting of disclosures of their PHI. The
-- fulfilled rows here, joined to the stored export, are that accounting.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS roi_requests (
    id                    BIGSERIAL PRIMARY KEY,
    request_id            TEXT        NOT NULL UNIQUE,  -- opaque, generated
    patient_reference     TEXT        NOT NULL,          -- e.g. "Patient/eAB12cd3"

    -- A CODE, never a name: patient | attorney | payer | employer |
    -- provider | government. Mirrors the requester categories the ROI
    -- print templates in this category are built around.
    requester_type        TEXT        NOT NULL,
    purpose_of_use        TEXT        NOT NULL,

    status                TEXT        NOT NULL DEFAULT 'open',  -- open | fulfilled | denied
    created_by            TEXT        NOT NULL,   -- authenticated username, not a patient identity
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    fulfilled_by          TEXT,
    fulfilled_at          TIMESTAMPTZ,
    denied_reason         TEXT,

    -- Encrypted objects holding the identifying detail and the produced
    -- record set respectively. NULL until written.
    detail_storage_key    TEXT,
    export_storage_key    TEXT,        -- machine-readable FHIR Bundle
    production_storage_key TEXT,       -- paginated PDF for legal review
    record_count          INTEGER,
    withheld_count        INTEGER,

    -- Requested scope. Dates here are the REQUESTED BOUNDARIES an
    -- operator typed, not any patient's clinical dates - a boundary is
    -- a property of the request, not of the individual, so it carries
    -- none of the Safe Harbor concern that keeps service dates out of
    -- stored_resources. Filtering against actual clinical dates
    -- happens by reading the resources themselves; see
    -- core/fhir/clinical_dates.py for why it cannot be done in SQL.
    scope_start           TIMESTAMPTZ,
    scope_end             TIMESTAMPTZ,
    scope_resource_types  TEXT,        -- comma-separated, NULL = all
    scope_encounter_id    TEXT,        -- one encounter, NULL = not encounter-scoped

    CONSTRAINT ck_roi_status CHECK (status IN ('open', 'fulfilled', 'denied'))
);

CREATE INDEX IF NOT EXISTS idx_roi_requests_patient
    ON roi_requests (patient_reference);

CREATE INDEX IF NOT EXISTS idx_roi_requests_status
    ON roi_requests (status, created_at DESC);
-- Made by Ryan Gomez & Co. Inc.
