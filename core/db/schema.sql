-- PHI AI Platform: queryable index schema.
--
-- HARD RULE: this table holds metadata about stored resources, never
-- clinical content. No names, no MRN, no DOB, no SSN, no free-text
-- observations - nothing that would make this table itself a store of
-- PHI. The only identifiers here are the source EMR's own internal,
-- opaque FHIR resource IDs (e.g. "eXYz123AbC" from Epic, or the
-- equivalent opaque ID any other FHIR-compliant EMR assigns), which are
-- already exposed today to any principal holding list/read access on
-- the storage bucket (they appear directly in every object's storage
-- key: fhir/{ResourceType}/{id}.json). Indexing them here does not
-- meaningfully increase exposure beyond that existing baseline - it
-- just makes them queryable by SQL instead of by paging through a
-- bucket listing.
--
-- If a future feature needs to index a REAL identifier (MRN, name,
-- DOB, SSN), that is a different design problem - a blind index
-- (keyed HMAC) or field-level encryption, not a plain column - and
-- should get its own review rather than being added quietly here.
--
-- This table itself is EMR-agnostic by construction: resource_type,
-- resource_id, and patient_reference all come from standard FHIR R4
-- conventions (resourceType, id, and the subject/patient reference
-- format respectively), not from anything specific to Epic or any
-- other single source system. Column names are equally cloud-agnostic
-- (storage_key/storage_version_id, not a name tied to any one
-- provider's storage product) - see core/storage/base.py's
-- StoredObjectMetadata, whose own key/version_id fields these columns
-- mirror. The storage bucket/container remains the system of record.
-- This index is derived and rebuildable from its contents; if the two
-- ever disagree, the storage backend wins.

CREATE TABLE IF NOT EXISTS stored_resources (
    id                    BIGSERIAL PRIMARY KEY,
    resource_type         TEXT        NOT NULL,
    resource_id           TEXT        NOT NULL,   -- the source EMR's internal FHIR id, opaque
    patient_reference     TEXT,                    -- e.g. "Patient/eAB12cd3" - internal FHIR ref, NULL if not patient-linked
    storage_key           TEXT        NOT NULL UNIQUE,
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

    CONSTRAINT uq_stored_resources_type_id UNIQUE (resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_stored_resources_type
    ON stored_resources (resource_type);

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
