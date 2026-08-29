-- PHI AI Platform: assistant telemetry bootstrap, AWS.
--
-- Run ONCE, connected as the RDS master user, after
-- core/db/telemetry_schema.sql. Mirrors core/db/bootstrap_aws.sql's
-- role mechanism; see that file. One role:
--
--   phi_ai_assistant_ops - INSERT (the web worker records every
--       interaction; drift runs record probes) and SELECT (the ops page
--       reads its own summaries). Never UPDATE or DELETE: telemetry is
--       append-only from the application, and correcting it is a
--       database-administrator act, deliberately.
--
-- The table holds metrics and usernames, no clinical content - see
-- telemetry_schema.sql's header for what may never be added to it.

CREATE ROLE phi_ai_assistant_ops WITH LOGIN;
GRANT rds_iam TO phi_ai_assistant_ops;

GRANT USAGE ON SCHEMA aiops TO phi_ai_assistant_ops;
GRANT INSERT, SELECT ON aiops.assistant_interactions TO phi_ai_assistant_ops;
GRANT USAGE, SELECT ON SEQUENCE aiops.assistant_interactions_id_seq TO phi_ai_assistant_ops;
-- Made by Ryan Gomez & Co. Inc.
