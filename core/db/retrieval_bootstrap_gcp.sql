-- PHI AI Platform: clinical retrieval index bootstrap, GCP.
--
-- Run ONCE, connected as the Cloud SQL instance's administrator user,
-- after core/db/retrieval_schema.sql has been applied. See
-- runbooks/RUNBOOK_GCP_SETUP.md for the psql invocation.
--
-- THE SAME GENUINE, DELIBERATE DIFFERENCE omop_bootstrap_gcp.sql
-- documents at length: Cloud SQL's IAM database authentication ties a
-- Postgres role name to the authenticating identity's own email - one
-- service account, exactly one role name. This file therefore does NOT
-- create phi_ai_retrieval_etl; the ETL grants go to the SAME
-- IAM-derived role the index ingest already uses. {INGEST_IAM_USER} is
-- the identical placeholder omop_bootstrap_gcp.sql substitutes
-- (deploy/gcp/database.tf's ingest_db_iam_user output), and on GCP
-- PHI_AI_RETRIEVAL_ETL_USERNAME should be set to that same value.
--
-- THE READ-SIDE SEPARATION IS PRESERVED, and that is the part that
-- matters: {SEARCH_IAM_USER} and {PSYCH_IAM_USER} are two DIFFERENT
-- service-account identities (each needing its own GCP service account,
-- the same provisioning note omop_bootstrap_gcp.sql makes for
-- omop_analyst). The general search identity holds no grant on the
-- psychotherapy table, whoever the writer collapses to. A deployment
-- that never enables assistant psychotherapy access simply never
-- provisions {PSYCH_IAM_USER} and skips that block.

GRANT USAGE ON SCHEMA retrieval TO "{INGEST_IAM_USER}";
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.clinical_text      TO "{INGEST_IAM_USER}";
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.psychotherapy_text TO "{INGEST_IAM_USER}";

GRANT USAGE ON SCHEMA retrieval TO "{SEARCH_IAM_USER}";
GRANT SELECT ON retrieval.clinical_text TO "{SEARCH_IAM_USER}";

-- Only for deployments enabling assistant psychotherapy access -
-- delete this block otherwise rather than provisioning an unused
-- identity with a grant this sensitive.
GRANT USAGE ON SCHEMA retrieval TO "{PSYCH_IAM_USER}";
GRANT SELECT ON retrieval.psychotherapy_text TO "{PSYCH_IAM_USER}";

-- Disposal: the disposition identity (bootstrap_gcp.sql's
-- {DISPOSITION_IAM_USER}) needs DELETE plus column-scoped SELECT on the
-- disposal key - the same narrow shape retrieval_bootstrap_aws.sql
-- grants, restated for GCP's identity model.
GRANT USAGE ON SCHEMA retrieval TO "{DISPOSITION_IAM_USER}";
GRANT DELETE ON retrieval.clinical_text      TO "{DISPOSITION_IAM_USER}";
GRANT DELETE ON retrieval.psychotherapy_text TO "{DISPOSITION_IAM_USER}";
GRANT SELECT (storage_key) ON retrieval.clinical_text      TO "{DISPOSITION_IAM_USER}";
GRANT SELECT (storage_key) ON retrieval.psychotherapy_text TO "{DISPOSITION_IAM_USER}";
-- Made by Ryan Gomez & Co. Inc.
