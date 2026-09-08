-- Components (core/components): the migration ledger and the update
-- journal. Applied lazily by core/components/journal.py's Journal when a
-- connection factory is configured, and by scripts/components.py ledger;
-- safe to re-run. Holds no PHI by design: file names, checksums, job
-- steps, backup locations and acknowledgements.
--
-- These tables are themselves listed by the ledger (every core/db/*.sql
-- is), so the first backfill records this file as applied by the run
-- that created it.

-- The migration ledger: which schema file ran, with what checksum, when,
-- by whom. A note of 'backfill' marks a row written by the one-time
-- backfill for a file whose objects already existed (proposal §14,
-- decision 1); 'backfill; attested: no dump' marks a backfill the
-- operator ran without a pg_dump, on their attestation.
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,                      -- file name under core/db/
    checksum    TEXT NOT NULL,                         -- sha256 of the file as applied
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);

-- The update journal: one row per job. The open job row (finished_at
-- empty) is the lock across every service in the deployment; the partial
-- unique index below lets the database refuse a second one.
CREATE TABLE IF NOT EXISTS platform_updates (
    id           TEXT PRIMARY KEY,
    component    TEXT NOT NULL,
    target       TEXT NOT NULL,
    mode         TEXT NOT NULL,                        -- direct | guided
    status       TEXT NOT NULL,                        -- planned | confirmed | running | verify_failed | recovering | recovered | done | failed
    started_by   TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT '',             -- '' while the job is open
    plan         TEXT NOT NULL DEFAULT '',             -- JSON: what will change, the phrase, the recovery
    previous     TEXT NOT NULL DEFAULT ''              -- JSON: the point of return recorded by Back up
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_updates_open
    ON platform_updates ((1)) WHERE finished_at = '';

-- Each of the five steps of each job, as it completes. Progress persists
-- here so a guided admin can leave and return, and so the updater can
-- read the open job after a restart.
CREATE TABLE IF NOT EXISTS platform_update_steps (
    job_id     TEXT NOT NULL REFERENCES platform_updates(id),
    seq        INTEGER NOT NULL,                       -- 0..4, the five steps in order
    step       TEXT NOT NULL,                          -- Plan | Back up | Apply | Verify | Recover
    status     TEXT NOT NULL DEFAULT 'pending',        -- pending | done | failed | skipped
    outcome    TEXT NOT NULL DEFAULT '',               -- ok | failed | skipped
    actor      TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL DEFAULT '',
    attested   BOOLEAN NOT NULL DEFAULT FALSE,         -- the admin attested; the screen could not verify by machine
    mode       TEXT NOT NULL DEFAULT '',               -- the mode this step ran in
    evidence   TEXT NOT NULL DEFAULT '',               -- JSON
    PRIMARY KEY (job_id, seq)
);

-- Backups and restore rehearsals per store (proposal §6). A verified
-- backup is one the updater checksummed; an unverified one is an
-- attestation (an RDS snapshot the admin marked as taken).
CREATE TABLE IF NOT EXISTS platform_backups (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'backup',         -- backup | rehearsal
    store      TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '',
    checksum   TEXT NOT NULL DEFAULT '',
    verified   BOOLEAN NOT NULL DEFAULT FALSE,
    actor      TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL DEFAULT ''
);

-- "Keep as is until <date> because <reason>", per component, audited as
-- system.component_acknowledged.
CREATE TABLE IF NOT EXISTS platform_component_acks (
    id         INTEGER PRIMARY KEY,
    component  TEXT NOT NULL,
    until_date TEXT NOT NULL,                          -- YYYY-MM-DD
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL DEFAULT ''
);

-- The image digests known to the journal, with their stamps: the
-- previous digests a rollback can return to. KEEP images are kept; older
-- rows stay as the record that a digest was released, marked not kept.
CREATE TABLE IF NOT EXISTS platform_releases (
    digest      TEXT PRIMARY KEY,
    release     TEXT NOT NULL,
    stamp       TEXT NOT NULL DEFAULT '',              -- JSON: BUILD.json
    recorded_at TEXT NOT NULL DEFAULT '',
    kept        BOOLEAN NOT NULL DEFAULT TRUE
);
-- Made by Ryan Gomez & Co. Inc.
