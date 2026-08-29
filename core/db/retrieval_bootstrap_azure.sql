-- PHI AI Platform: clinical retrieval index bootstrap, Azure.
--
-- Run ONCE, connected as the Flexible Server's Microsoft Entra
-- administrator, after core/db/retrieval_schema.sql has been applied.
-- See runbooks/RUNBOOK_AZURE_SETUP.md for the psql invocation.
--
-- Like omop_bootstrap_azure.sql and unlike the GCP sibling, Azure's
-- pgaadauth_create_principal_with_oid() lets a Postgres role name be
-- chosen freely, so this file DOES create genuinely separate roles,
-- preserving the same three-way split retrieval_bootstrap_aws.sql
-- documents (its header carries the full reasoning):
--
--   phi_ai_retrieval_etl     both tables, write + read (delete-then-
--                            insert idempotent re-runs)
--   phi_ai_retrieval_search  SELECT on clinical_text ONLY
--   phi_ai_retrieval_psych   SELECT on psychotherapy_text ONLY
--
-- {ETL_PRINCIPAL_ID} / {SEARCH_PRINCIPAL_ID} / {PSYCH_PRINCIPAL_ID} are
-- placeholders for the managed identities' object (principal) IDs - the
-- ETL one can be the ingest identity's ID registered under a second
-- role name, exactly how omop_bootstrap_azure.sql registers omop_etl.
-- A deployment not enabling assistant psychotherapy access deletes the
-- psych block rather than registering an unused principal.

SELECT * FROM pgaadauth_create_principal_with_oid(
    'phi_ai_retrieval_etl', '{ETL_PRINCIPAL_ID}', 'service', false, false);
SELECT * FROM pgaadauth_create_principal_with_oid(
    'phi_ai_retrieval_search', '{SEARCH_PRINCIPAL_ID}', 'service', false, false);

GRANT USAGE ON SCHEMA retrieval TO phi_ai_retrieval_etl, phi_ai_retrieval_search;

GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.clinical_text      TO phi_ai_retrieval_etl;
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.psychotherapy_text TO phi_ai_retrieval_etl;

GRANT SELECT ON retrieval.clinical_text TO phi_ai_retrieval_search;

-- Only for deployments enabling assistant psychotherapy access.
SELECT * FROM pgaadauth_create_principal_with_oid(
    'phi_ai_retrieval_psych', '{PSYCH_PRINCIPAL_ID}', 'service', false, false);
GRANT USAGE ON SCHEMA retrieval TO phi_ai_retrieval_psych;
GRANT SELECT ON retrieval.psychotherapy_text TO phi_ai_retrieval_psych;

-- Disposal: same narrow shape as the AWS sibling, guarded because
-- nothing on Azure registers the disposition principal yet
-- (omop_bootstrap_azure.sql documents the identical gap for
-- phi_ai_disposition - a Terraform provisioning gap, not a rename).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'phi_ai_disposition') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA retrieval TO phi_ai_disposition';
        EXECUTE 'GRANT DELETE ON retrieval.clinical_text      TO phi_ai_disposition';
        EXECUTE 'GRANT DELETE ON retrieval.psychotherapy_text TO phi_ai_disposition';
        EXECUTE 'GRANT SELECT (storage_key) ON retrieval.clinical_text      TO phi_ai_disposition';
        EXECUTE 'GRANT SELECT (storage_key) ON retrieval.psychotherapy_text TO phi_ai_disposition';
    END IF;
END $$;
-- Made by Ryan Gomez & Co. Inc.
