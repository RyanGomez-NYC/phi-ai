-- PHI AI Platform: assistant prompt history and saved prompts.
--
-- CONVENIENCE STATE, NOT THE RECORD OF USE. Every question put to the
-- assistant is already recorded in the tamper-evident audit trail
-- before it is sent (core/web/app.py, core/assistant/session.py); this
-- table exists so a person can see and re-run their own recent
-- prompts, and pin the ones they use daily. Deleting a row here
-- deletes a bookmark, never evidence - which is why, unlike
-- roi_requests, DELETE is granted.
--
-- WHAT A ROW HOLDS: the prompt text a user typed, their username, and
-- bookkeeping. Prompt text can name a patient or a finding, so this
-- table is treated with the same care as the index it lives beside:
-- reachable only through the reader role the web interface already
-- uses, scoped per-user in every query the application makes, and
-- holding no answer text ever - answers live in process memory for the
-- session and in no table at all.
--
-- Applied by an operator as the master user, after core/db/schema.sql:
--
--   psql "..." -f core/db/prompts_schema.sql

CREATE TABLE IF NOT EXISTS assistant_prompts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username    TEXT        NOT NULL,
    prompt      TEXT        NOT NULL,
    page_key    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    saved       BOOLEAN     NOT NULL DEFAULT FALSE,
    label       TEXT,

    CONSTRAINT assistant_prompts_nonempty CHECK (length(trim(prompt)) > 0),
    CONSTRAINT assistant_prompts_label_len CHECK (label IS NULL OR length(label) <= 120)
);

-- The lists the page renders: this user's saved prompts, and this
-- user's most recent history, newest first.
CREATE INDEX IF NOT EXISTS idx_assistant_prompts_user_recent
    ON assistant_prompts (username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assistant_prompts_user_saved
    ON assistant_prompts (username, saved) WHERE saved;

-- Same role the web interface already connects as - the ROI precedent
-- (core/db/schema.sql's roi_requests): workflow state lives with the
-- reader role rather than minting another cross-cloud role. DELETE is
-- granted here and deliberately NOT there; see the header.
GRANT SELECT, INSERT, UPDATE, DELETE ON assistant_prompts TO phi_ai_reader;
-- Made by Ryan Gomez & Co. Inc.
