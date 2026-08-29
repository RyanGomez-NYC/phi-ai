-- PHI AI Platform: clinical retrieval index bootstrap, AWS.
--
-- Run ONCE, connected as the RDS master user, after
-- core/db/retrieval_schema.sql has been applied. Mirrors
-- core/db/bootstrap_aws.sql's role-creation mechanism
-- (CREATE ROLE ... GRANT rds_iam) exactly - see that file for the full
-- reasoning. GCP and Azure siblings follow each cloud's own mechanism;
-- see each sibling's header.
--
-- Three roles, and the split is the security design:
--
--   phi_ai_retrieval_etl     writes both tables. INSERT/UPDATE/DELETE +
--                            SELECT: the ETL is delete-then-insert per
--                            storage key so a re-run is idempotent, and
--                            it reads its own rows to skip unchanged
--                            objects. Holds the psychotherapy table too
--                            - the ETL is the one trusted writer, and a
--                            separate psych writer would double the
--                            operational surface for no read-side gain.
--
--   phi_ai_retrieval_search  SELECT on clinical_text ONLY. The role the
--                            assistant's research search connects as.
--                            NO grant on psychotherapy_text, so no
--                            widening of general research access can
--                            quietly include it - the schema header and
--                            core/web/auth.py both state this rule; this
--                            file is where it is enforced.
--
--   phi_ai_retrieval_psych   SELECT on psychotherapy_text ONLY. A
--                            deployment that never enables assistant
--                            psychotherapy access never registers an
--                            identity for it, and the table is
--                            unreachable however the application is
--                            configured.
--
-- Disposal: phi_ai_disposition (core/db/bootstrap_aws.sql) gains DELETE
-- on both tables, guarded below the same way the imaging grants are -
-- rows here must die in the same disposal operation as the index row.

CREATE ROLE phi_ai_retrieval_etl WITH LOGIN;
GRANT rds_iam TO phi_ai_retrieval_etl;

CREATE ROLE phi_ai_retrieval_search WITH LOGIN;
GRANT rds_iam TO phi_ai_retrieval_search;

CREATE ROLE phi_ai_retrieval_psych WITH LOGIN;
GRANT rds_iam TO phi_ai_retrieval_psych;

GRANT USAGE ON SCHEMA retrieval
    TO phi_ai_retrieval_etl, phi_ai_retrieval_search, phi_ai_retrieval_psych;

GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.clinical_text      TO phi_ai_retrieval_etl;
GRANT SELECT, INSERT, UPDATE, DELETE ON retrieval.psychotherapy_text TO phi_ai_retrieval_etl;

GRANT SELECT ON retrieval.clinical_text      TO phi_ai_retrieval_search;
GRANT SELECT ON retrieval.psychotherapy_text TO phi_ai_retrieval_psych;

-- Disposal wiring. Guarded like the imaging grants in bootstrap_aws.sql:
-- a plain GRANT would abort this whole file under ON_ERROR_STOP when the
-- disposition role does not exist yet (retrieval can be bootstrapped
-- before the index roles on a fresh database).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'phi_ai_disposition') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA retrieval TO phi_ai_disposition';
        EXECUTE 'GRANT DELETE ON retrieval.clinical_text      TO phi_ai_disposition';
        EXECUTE 'GRANT DELETE ON retrieval.psychotherapy_text TO phi_ai_disposition';
        -- DELETE's WHERE clause reads storage_key, and a plain DELETE
        -- with a WHERE requires SELECT on the columns the condition
        -- reads - the same Postgres permission rule
        -- omop_bootstrap_aws.sql documents (and confirmed there against
        -- live PostgreSQL 16). Column-scoped, so the disposition role
        -- can target rows without being able to read anyone's text.
        EXECUTE 'GRANT SELECT (storage_key) ON retrieval.clinical_text      TO phi_ai_disposition';
        EXECUTE 'GRANT SELECT (storage_key) ON retrieval.psychotherapy_text TO phi_ai_disposition';
    END IF;
END $$;
-- Made by Ryan Gomez & Co. Inc.
