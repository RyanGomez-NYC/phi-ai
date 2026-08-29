-- ---------------------------------------------------------------------------
-- aiops.* - AI assistant telemetry: usage, performance, compliance
-- posture, and drift-probe results.
--
-- METRICS ONLY, NO CONTENT, AND THAT IS THE DESIGN CONSTRAINT. No
-- question text, no answer text, no tool arguments, no snippets - the
-- audit trail already records questions and every clinical read as
-- disclosures (tamper-evidently, which this table is not), and a
-- second, queryable copy of that content here would be a second PHI
-- surface with none of the audit chain's guarantees. What this table
-- answers is operational: how much is the assistant used and by which
-- roles, how fast and how expensive is it, how often does it refuse or
-- fail, how often does it read PHI, and whether the model's behaviour
-- on a fixed probe suite has drifted (core/assistant/drift.py).
--
-- The one identifying column is username - who asked, never what. That
-- is staff activity data, so the read grant (assistant:ops permission;
-- admin and auditor only) is narrower than the report:read metrics
-- pages. Rows are written by the web worker on every assistant
-- interaction (fire-and-forget: a telemetry failure must never fail an
-- answer) and by drift runs; nothing ever updates or deletes them from
-- the application - one INSERT-only writer role, like the audit trail's
-- posture restated at much lower stakes.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS aiops;

CREATE TABLE IF NOT EXISTS aiops.assistant_interactions (
    id              BIGSERIAL   PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 'interaction' for a user question; 'drift_probe' for one probe of
    -- a drift run (probe_* columns then apply).
    kind            TEXT        NOT NULL DEFAULT 'interaction'
                    CONSTRAINT ck_aiops_kind CHECK (kind IN ('interaction', 'drift_probe')),

    username        TEXT        NOT NULL,
    roles           TEXT,                 -- comma-joined at ask time
    page_key        TEXT,                 -- which page the question came from

    provider        TEXT        NOT NULL,
    model           TEXT        NOT NULL,

    latency_ms      INTEGER,
    input_tokens    INTEGER     NOT NULL DEFAULT 0,
    output_tokens   INTEGER     NOT NULL DEFAULT 0,

    tool_calls      INTEGER     NOT NULL DEFAULT 0,
    tools_used      TEXT,                 -- comma-joined tool names, no arguments
    phi_reads       INTEGER     NOT NULL DEFAULT 0,  -- PHI-bearing tool calls this turn

    refused         BOOLEAN     NOT NULL DEFAULT false,
    truncated       BOOLEAN     NOT NULL DEFAULT false,
    error           BOOLEAN     NOT NULL DEFAULT false,

    -- drift_probe rows only
    probe_name      TEXT,
    probe_passed    BOOLEAN,
    probe_detail    TEXT                  -- which expectation failed, no content
);

CREATE INDEX IF NOT EXISTS idx_aiops_interactions_ts
    ON aiops.assistant_interactions (ts);
CREATE INDEX IF NOT EXISTS idx_aiops_interactions_kind_ts
    ON aiops.assistant_interactions (kind, ts);
-- Made by Ryan Gomez & Co. Inc.
