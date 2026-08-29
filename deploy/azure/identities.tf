# ---------------------------------------------------------------------------
# Application identities: the direct equivalent of deploy/aws/iam.tf's
# three separate roles (ingest, restore, auditor). Same reasoning applies
# unchanged - "minimum necessary" (164.502(b)) is a structural property,
# not a policy document you write once:
#
#   ingest  - can WRITE stored objects and APPEND audit records.
#             Cannot decrypt PHI. Compromising the long-running ingestion
#             service does not yield readable patient data.
#
#   restore - can READ and decrypt stored objects, for records requests
#             and legal hold. Assumed for a specific operation, not left
#             running continuously the way ingest is.
#
#   auditor - can READ the audit container only. No access to the store
#             container at all, and (unlike ingest/restore) no Key Vault
#             access either - the audit log itself contains no
#             application-level ciphertext requiring a Key Vault unwrap
#             to read; core/audit/log.py's events are actor/action/
#             resource_key/purpose_of_use metadata, never clinical
#             content, so there is nothing here for a Key Vault
#             permission to gate.
#
# A GENUINE ARCHITECTURAL DIFFERENCE FROM AWS, WORTH BEING HONEST ABOUT:
# AWS's sts:AssumeRole lets any principal holding that permission
# temporarily vend short-lived credentials for a specific role, from
# anywhere - a laptop, a CI runner, a container - which is what lets
# core/fhir/restore.py's --role-arn flag work the way it does. Azure user-
# assigned managed identities have no equivalent "assume this identity
# temporarily" mechanism: a managed identity can only be used by being
# ATTACHED to an actual Azure compute resource (a VM, Container App, AKS
# pod), which then authenticates as that identity automatically via the
# instance metadata service. There is no Azure API that lets an arbitrary
# authorized caller borrow a managed identity's credentials on demand the
# way AssumeRole does.
#
# Practical consequence for local development: running scheduler.py or
# core/fhir/restore.py from a plain dev laptop (DefaultAzureCredential
# falling back to `az login`) authenticates as the DEVELOPER'S OWN Azure
# AD identity, not as any of the three identities below. For local
# testing to actually exercise this stack, the developer's own object ID
# needs the same role grants (see the trusted_principal_object_ids loop
# below) - which means, honestly, that role SEPARATION is not really
# enforced when testing as a human from a laptop the way it is once
# these identities are attached to real compute in a production
# deployment. This is a real, structural difference from the AWS side,
# not an oversight - see runbooks/RUNBOOK_AZURE_SETUP.md for how to
# actually attach these identities to compute for a deployment where the
# separation is real.
#
# NOTE ON NAMES: the three azurerm_user_assigned_identity `name` values
# below interpolate var.name_prefix, and a managed identity name is
# immutable once created. Each identity's principal_id is what
# core/db/bootstrap_azure.sql registers as a Postgres role via
# pgaadauth_create_principal_with_oid(), so these identities and that
# bootstrap script are coupled - see var.name_prefix's own description in
# variables.tf.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Custom role: Key Vault wrap-only, no unwrap.
#
# WHY A CUSTOM ROLE: Azure Key Vault's built-in roles do not offer this
# split. Confirmed against Microsoft's own published role-definition JSON
# (github.com/MicrosoftDocs/azure-docs, built-in-roles/security.md):
# "Key Vault Crypto Service Encryption User" grants keys/read,
# keys/wrap/action, AND keys/unwrap/action together - there is no
# built-in role with wrap but not unwrap. This matters here specifically
# because it is the direct Azure equivalent of deploy/aws/iam.tf's
# ingest role having kms:GenerateDataKey but explicitly NOT kms:Decrypt -
# the single most load-bearing security property in that file's own
# design ("compromising the long-running ingestion service does not
# yield readable patient data"). Accepting a built-in role that also
# grants unwrap would be a real, silent regression from the security
# posture this project has already built and tested on AWS, not a
# reasonable substitution - so this defines the narrower role explicitly
# instead.
# ---------------------------------------------------------------------------

resource "azurerm_role_definition" "key_wrap_only" {
  name        = "${var.name_prefix}-key-wrap-only"
  scope       = azurerm_key_vault.main.id
  description = "Wrap (encrypt) using a Key Vault key, without unwrap (decrypt) - the ingest identity's minimum-necessary grant. Mirrors deploy/aws/iam.tf's ingest role having kms:GenerateDataKey but not kms:Decrypt."

  permissions {
    actions     = []
    not_actions = []
    data_actions = [
      "Microsoft.KeyVault/vaults/keys/read",
      "Microsoft.KeyVault/vaults/keys/wrap/action",
    ]
    not_data_actions = []
  }

  assignable_scopes = [azurerm_key_vault.main.id]
}

# ---------------------------------------------------------------------------
# Ingest identity
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "ingest" {
  name                = "${var.name_prefix}-ingest"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
}

resource "azurerm_role_assignment" "ingest_write_store" {
  scope                = azurerm_storage_container.store.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingest.principal_id

  # Storage Blob Data Contributor includes delete - broader than
  # deploy/aws/iam.tf's ingest role, which explicitly denies
  # s3:DeleteObject. Azure has no built-in "write and read but not
  # delete" blob data role; a custom role would be needed to close this
  # gap precisely. Flagged here rather than silently accepted - see
  # runbooks/RUNBOOK_AZURE_SETUP.md's "Known gaps relative to AWS"
  # section.
}

resource "azurerm_role_assignment" "ingest_write_audit" {
  scope                = azurerm_storage_container.audit.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingest.principal_id
}

resource "azurerm_role_assignment" "ingest_wrap_key" {
  scope              = azurerm_key_vault.main.id
  role_definition_id = azurerm_role_definition.key_wrap_only.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.ingest.principal_id
}

# ---------------------------------------------------------------------------
# Restore identity
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "restore" {
  name                = "${var.name_prefix}-restore"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
}

resource "azurerm_role_assignment" "restore_read_store" {
  scope                = azurerm_storage_container.store.resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.restore.principal_id
}

resource "azurerm_role_assignment" "restore_write_audit" {
  scope                = azurerm_storage_container.audit.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor" # every restore is audit-logged - core/fhir/restore.py appends a read event
  principal_id         = azurerm_user_assigned_identity.restore.principal_id
}

resource "azurerm_role_assignment" "restore_unwrap_key" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto Service Encryption User" # wrap AND unwrap - restore needs to decrypt
  principal_id         = azurerm_user_assigned_identity.restore.principal_id
}

# ---------------------------------------------------------------------------
# Auditor identity
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "auditor" {
  name                = "${var.name_prefix}-auditor"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
}

resource "azurerm_role_assignment" "auditor_read_audit" {
  scope                = azurerm_storage_container.audit.resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.auditor.principal_id

  # No Key Vault grant for this identity at all - see the module comment
  # above for why the audit log needs none. No access to the store
  # container either - this is the whole point of a separate auditor
  # identity, matching deploy/aws/iam.tf's auditor role's own
  # DenyAllPHIAccess statement.
}

# ---------------------------------------------------------------------------
# Dev/testing access: grant every trusted principal (a human developer's
# own Azure AD object ID, typically) the UNION of what all three
# identities above can do, so RUNBOOK_AZURE_SETUP.md's local-dev flow can
# actually run scheduler.py and restore.py end to end. See the module
# comment at the top of this file for why this is a real, honestly-
# documented departure from role separation, not an oversight: Azure
# managed identities cannot be assumed from a laptop the way AWS IAM
# roles can.
# ---------------------------------------------------------------------------

resource "azurerm_role_assignment" "trusted_store_contributor" {
  for_each = toset(var.trusted_principal_object_ids)

  scope                = azurerm_storage_container.store.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "trusted_audit_contributor" {
  for_each = toset(var.trusted_principal_object_ids)

  scope                = azurerm_storage_container.audit.resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}

# Note: trusted_principal_object_ids already gets "Key Vault Crypto User"
# (wrap+unwrap+sign+verify) from keyvault.tf's azurerm_role_assignment.trusted_crypto_user -
# broader than the wrap-only ingest identity gets, but appropriate for a
# human developer who needs to exercise both the ingest and restore code
# paths locally.

output "ingest_identity_client_id" {
  description = "Client ID of the ingest identity. For local dev, this identity is not directly usable (see the module comment above) - it exists for attaching to real compute in a production deployment."
  value       = azurerm_user_assigned_identity.ingest.client_id
}

output "restore_identity_client_id" {
  description = "Client ID of the restore identity - see ingest_identity_client_id's note on local-dev usability."
  value       = azurerm_user_assigned_identity.restore.client_id
}

output "auditor_identity_client_id" {
  description = "Client ID of the auditor identity - see ingest_identity_client_id's note on local-dev usability."
  value       = azurerm_user_assigned_identity.auditor.client_id
}
# Made by Ryan Gomez & Co. Inc.
