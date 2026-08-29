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
