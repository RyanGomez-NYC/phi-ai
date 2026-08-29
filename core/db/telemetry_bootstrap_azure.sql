-- PHI AI Platform: assistant telemetry bootstrap, Azure.
--
-- Run ONCE, connected as the Flexible Server's Entra administrator,
-- after core/db/telemetry_schema.sql. Azure's
-- pgaadauth_create_principal_with_oid() registers a freely-named role
-- (see omop_bootstrap_azure.sql's header); {OPS_PRINCIPAL_ID} is the
-- managed identity's object ID - the web interface's own identity is
-- the natural choice, registered here under this second role name.

SELECT * FROM pgaadauth_create_principal_with_oid(
    'phi_ai_assistant_ops', '{OPS_PRINCIPAL_ID}', 'service', false, false);

GRANT USAGE ON SCHEMA aiops TO phi_ai_assistant_ops;
GRANT INSERT, SELECT ON aiops.assistant_interactions TO phi_ai_assistant_ops;
GRANT USAGE, SELECT ON SEQUENCE aiops.assistant_interactions_id_seq TO phi_ai_assistant_ops;
-- Made by Ryan Gomez & Co. Inc.
