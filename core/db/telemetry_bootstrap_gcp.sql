-- PHI AI Platform: assistant telemetry bootstrap, GCP.
--
-- Run ONCE, connected as the Cloud SQL administrator, after
-- core/db/telemetry_schema.sql. Cloud SQL's one-identity-one-role IAM
-- model applies (see omop_bootstrap_gcp.sql's header for the full
-- account): {OPS_IAM_USER} is a service-account-derived role name. A
-- deployment may reuse the web interface's own identity here - the
-- telemetry writer IS the web worker - or provision a dedicated one;
-- either way PHI_AI_ASSISTANT_OPS_USERNAME is set to the role name
-- granted below.

GRANT USAGE ON SCHEMA aiops TO "{OPS_IAM_USER}";
GRANT INSERT, SELECT ON aiops.assistant_interactions TO "{OPS_IAM_USER}";
GRANT USAGE, SELECT ON SEQUENCE aiops.assistant_interactions_id_seq TO "{OPS_IAM_USER}";
-- Made by Ryan Gomez & Co. Inc.
