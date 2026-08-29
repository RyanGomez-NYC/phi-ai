output "resource_group_name" {
  value       = azurerm_resource_group.main.name
  description = "Resource group holding every resource this stack creates."
}

output "storage_account_name" {
  value       = azurerm_storage_account.store.name
  description = "The storage account name. Not directly needed in .env (the account URL setting uses the full blob endpoint below instead) - useful for `az` CLI commands during setup/verification."
}

output "store_container_name" {
  value       = azurerm_storage_container.store.name
  description = "Set as the storage container setting - core/storage/factory.py's build_storage() passes this as the `container` argument to AzureBlobStorage."
}

output "audit_container_name" {
  value       = azurerm_storage_container.audit.name
  description = "Set as the audit container setting - core/audit/sink.py's AzureBlobAuditSink takes this as its `container` argument."
}

output "azure_account_url" {
  value       = azurerm_storage_account.store.primary_blob_endpoint
  description = "Set as the Azure account URL setting. Both the store and audit containers live in this one storage account - see storage.tf's own comment on why, given core/config/settings.py has exactly one azure_account_url field."
}

output "key_vault_key_name" {
  value       = azurerm_key_vault_key.store.name
  description = "Set as BOTH the store and audit KMS key settings - store and audit share one Key Vault key in this installment (the Azure equivalent of deploy/aws's separate_audit_key = false). See storage.tf's audit container comment for the deliberate reasoning and the encryption-scopes fast-follow that would give them independent keys."
}

output "azure_vault_url" {
  value       = azurerm_key_vault.main.vault_uri
  description = "Set as the Azure vault URL setting."
}

output "ingest_identity_id" {
  description = "Full resource ID of the ingest user-assigned identity. Attach this to the compute resource (Container App, VM) actually running scheduler.py/bulk_scheduler.py in a real deployment - see runbooks/RUNBOOK_AZURE_SETUP.md."
  value       = azurerm_user_assigned_identity.ingest.id
}

output "restore_identity_id" {
  description = "Full resource ID of the restore user-assigned identity. See ingest_identity_id's note - the same attach-to-compute pattern applies."
  value       = azurerm_user_assigned_identity.restore.id
}

output "auditor_identity_id" {
  description = "Full resource ID of the auditor user-assigned identity. See ingest_identity_id's note."
  value       = azurerm_user_assigned_identity.auditor.id
}

# ---------------------------------------------------------------------------
# Principal IDs - distinct from the client_id outputs identities.tf's
# own file already provides. client_id authenticates a running
# application (DefaultAzureCredential); principal_id is the Azure AD
# object ID RBAC role assignments and, critically,
# pgaadauth_create_principal_with_oid() both need. Added here because
# core/db/bootstrap_azure.sql and core/db/omop_bootstrap_azure.sql both
# already anticipated outputs at exactly these names when they were
# written, before database.tf existed to actually provide them.
# ---------------------------------------------------------------------------

output "ingest_identity_principal_id" {
  description = "Azure AD object (principal) ID of the ingest identity - the value core/db/bootstrap_azure.sql's and core/db/omop_bootstrap_azure.sql's {INGEST_PRINCIPAL_ID} placeholders both need."
  value       = azurerm_user_assigned_identity.ingest.principal_id
}

output "restore_identity_principal_id" {
  description = "Azure AD object (principal) ID of the restore identity - the value core/db/bootstrap_azure.sql's {READER_PRINCIPAL_ID} placeholder needs."
  value       = azurerm_user_assigned_identity.restore.principal_id
}

# ---------------------------------------------------------------------------
# Flexible Server outputs - only meaningful (and only emitted) when
# var.enable_db is true. db_host feeds the DB host setting -
# core/db/connection.py's _connect_azure() connects over a plain
# host:port, unlike GCP's instance-connection-name addressing. Unlike
# deploy/gcp/outputs.tf's ingest_db_iam_user (a derived value, since
# GCP forces the Postgres role name to match the identity's own email),
# Azure's pgaadauth model lets the role name be chosen freely - see
# database.tf's own header - so the DB username values below are fixed
# string literals, not computed from any resource attribute, matching
# how deploy/aws's equivalents are also just chosen names rather than
# derived values.
# ---------------------------------------------------------------------------

output "db_host" {
  description = "Set as the DB host setting. Empty unless enable_db is true."
  value       = var.enable_db ? azurerm_postgresql_flexible_server.index[0].fqdn : null
}

output "db_name" {
  description = "Set as the DB name setting. Empty unless enable_db is true."
  value       = var.enable_db ? azurerm_postgresql_flexible_server_database.index[0].name : null
}

# Convenience: emit the .env fragment so you don't hand-copy values across
# a dozen separate outputs. Mirrors deploy/aws/outputs.tf's env_fragment
# exactly in purpose and structure.
#
# Contains no secrets - authentication is via DefaultAzureCredential
# (Azure AD), never a stored key or connection string, matching this
# project's Epic-side avoidance of shared secrets (RS384 JWT, not a
# client secret) and the AWS side's IAM-based auth (no access keys in
# .env there either). The one password this stack DOES generate
# (random_password.db_master in database.tf) is deliberately NOT
# included here - it exists only for the one-time bootstrap step in
# runbooks/RUNBOOK_AZURE_SETUP.md, retrieved directly from Terraform
# state the same way RUNBOOK_AWS_SETUP.md's Step 6a already does for
# RDS, never written into an application-facing .env file.
#
# The database lines only appear when enable_db is true - the same
# conditional-inclusion pattern deploy/aws/outputs.tf and
# deploy/gcp/outputs.tf's own env_fragments already use.
#
# THE DB USERNAME VALUES ARE POSTGRES ROLE NAMES and must match exactly
# what core/db/bootstrap_azure.sql registers via
# pgaadauth_create_principal_with_oid() - phi_ai_ingest and
# phi_ai_reader. A value here that names a role the database does not
# have is an authentication failure, not a cosmetic mismatch.
# PHI_AI_OMOP_ETL_USERNAME is omop_etl, a genuinely separate role
# core/db/omop_bootstrap_azure.sql creates - Azure can keep that
# separation because pgaadauth lets one identity hold two freely-named
# roles, which GCP's Cloud SQL IAM model cannot (see database.tf).
output "env_fragment" {
  description = "Paste into .env, then run install/installer_chatbot.py to add FHIR credentials. See runbooks/RUNBOOK_AZURE_SETUP.md for the full walkthrough, including the identity-attachment step this fragment alone does not cover."
  value       = <<-EOT
    PHI_AI_CLOUD_PROVIDER=azure
    PHI_AI_STORAGE_BUCKET=${azurerm_storage_container.store.name}
    PHI_AI_STORAGE_REGION=${var.azure_location}
    PHI_AI_KMS_KEY_ID=${azurerm_key_vault_key.store.name}
    PHI_AI_AUDIT_BUCKET=${azurerm_storage_container.audit.name}
    PHI_AI_AUDIT_KMS_KEY_ID=${azurerm_key_vault_key.store.name}
    PHI_AI_AZURE_ACCOUNT_URL=${azurerm_storage_account.store.primary_blob_endpoint}
    PHI_AI_AZURE_VAULT_URL=${azurerm_key_vault.main.vault_uri}
    %{if var.enable_db~}
    PHI_AI_DB_HOST=${azurerm_postgresql_flexible_server.index[0].fqdn}
    PHI_AI_DB_NAME=${azurerm_postgresql_flexible_server_database.index[0].name}
    PHI_AI_DB_INGEST_USERNAME=phi_ai_ingest
    PHI_AI_DB_READER_USERNAME=phi_ai_reader
    PHI_AI_OMOP_ETL_USERNAME=omop_etl
    %{endif~}
  EOT
}
# Made by Ryan Gomez & Co. Inc.
