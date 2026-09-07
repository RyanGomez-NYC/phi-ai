-- Platform runtime state: configuration and the model registry
-- (core/web/platform_state.py). Applied lazily by PlatformState when a
-- connection factory is configured; safe to re-run.
--
-- Deliberately small: the integration screens' run histories are
-- operational telemetry seeded in code for the demonstration corpus,
-- and real runs are recorded by the bulk scheduler / delivery services
-- in their own stores. What must survive a restart is what an
-- administrator SET: configuration and registered models.

CREATE TABLE IF NOT EXISTS platform_config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS platform_models (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    slot          TEXT NOT NULL DEFAULT 'other',
    provider      TEXT NOT NULL DEFAULT '',
    model_id      TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    endpoint_url  TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'registered',
    builtin       BOOLEAN NOT NULL DEFAULT FALSE,
    registered_by TEXT NOT NULL DEFAULT '',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Made by Ryan Gomez & Co. Inc.

-- Orchestration (core/orchestration, core/web/orchestration_routes.py).
-- What an operator SET: the current wiring and scope, the exchanges saved
-- by name, and disclosure consents - per receiving system, per chart, per
-- heightened category. Runs are recorded by the run itself (increment 2).
CREATE TABLE IF NOT EXISTS platform_orchestration (
    key     TEXT PRIMARY KEY,           -- 'selection' | 'scope'
    value   TEXT NOT NULL DEFAULT ''    -- JSON
);

CREATE TABLE IF NOT EXISTS platform_exchanges (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    value     TEXT NOT NULL DEFAULT '', -- JSON: sources, targets, purpose, delivery, cadence, scope
    saved_by  TEXT NOT NULL DEFAULT '',
    saved_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS platform_consents (
    consent_key TEXT PRIMARY KEY,       -- system|patient|category
    system      TEXT NOT NULL,
    patient_id  INTEGER NOT NULL,
    category    TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT '',
    granted_by  TEXT NOT NULL DEFAULT '',
    granted_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS platform_orch_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT '',
    value       TEXT NOT NULL DEFAULT ''   -- JSON: the whole run, ledger included
);
