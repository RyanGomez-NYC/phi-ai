variable "azure_location" {
  description = "Azure region for all resources. Must be a US region for most US healthcare data residency expectations - see docs/COMPLIANCE.md."
  type        = string
  default     = "eastus"
}

variable "environment" {
  description = <<-EOT
    Deployment environment. Only affects the defaults for a small number of
    dev-convenience settings - does NOT force phi_retention_days to any
    particular value in prod/staging, and no longer selects an immutability
    strength, because this stack provisions no WORM/immutability policy at
    either the container or version level in any environment (see storage.tf).
    Retention stays independently deployer-configurable everywhere; see its
    own description below for why (state retention law varies too much to
    hardcode to `environment`) - mirrors deploy/aws/variables.tf and
    deploy/gcp/variables.tf's identical reasoning exactly.
  EOT
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for all resource names. No hyphens: storage account names
    can't contain them.

    CHOOSE THIS BEFORE YOUR FIRST APPLY AND THEN LEAVE IT ALONE. It is
    not branding: it feeds the storage account name (globally unique
    across all of Azure), the Key Vault name, the Key Vault KEY name, the
    resource group name, the Flexible Server name, the custom role
    definition name, and the four managed identity names. No provider can
    rename any of those in place.

    AN AZURE-SPECIFIC TWIST, worth knowing before assuming a destroy is
    merely recoverable-by-recreating: Key Vault soft-delete is MANDATORY
    and cannot be disabled, so a destroyed vault or key keeps its NAME
    reserved for soft_delete_retention_days - and if
    purge_protection_enabled is true, it cannot be purged early at all,
    by anyone, including an Owner. A re-create under the same name would
    fail until that window elapses.

    A HARD LENGTH CEILING, unlike AWS and GCP: Azure storage account
    names are 3-24 characters, lowercase alphanumeric ONLY. storage.tf
    builds the account name as name_prefix + "store" (5 characters) + 8
    characters of the subscription ID, so:

        "phiai"        ->  5 + 5 + 8 = 18 / 24
        11 characters  -> 11 + 5 + 8 = 24 / 24  (the maximum)

    Eleven characters is therefore the ceiling on any prefix you choose
    here. The 8 subscription-ID characters are the leading block of a
    GUID - lowercase hex, no hyphen - so they satisfy the character-class
    rule automatically; your prefix has to as well.
  EOT
  type        = string
  default     = "phiai" # no hyphens: storage account names can't contain them
}

# ---------------------------------------------------------------------------
# Retention
#
# CONFIGURATION, NOT ENFORCEMENT. No container-level and no version-level
# immutability policy is provisioned - see storage.tf. These values are
# recorded as blob metadata by core/storage/azure_blob.py and drive
# documented disposition; nothing in Azure Storage stops an early delete
# or performs a late one. The account's 7-day soft-delete window is the
# only deletion protection behind them, and it is a recovery window, not
# a bar.
# ---------------------------------------------------------------------------

variable "phi_retention_days" {
  description = <<-EOT
    Intended retention period, in days, for stored PHI. NOT ENFORCED -
    recorded as blob metadata only; this stack provisions no immutability
    policy at either the container or version level to back it. Fully
    deployer-configurable in every environment, including
    prod - same reasoning as deploy/aws/variables.tf's phi_retention_days:
    US state medical record retention statutes vary genuinely widely (commonly
    5-10+ years for adults; Florida's physician-record minimum is specifically
    5 years), and no hardcoded floor here is defensible as "the compliant
    number" across all 50 states. Set this from YOUR state's actual applicable
    requirement - see docs/COMPLIANCE.md.

    Separately from enforcement: the application does not currently layer a
    per-object override on top the way core/fhir/client.py's per-resource-type
    retention overrides do for the AWS path; that override support does not yet
    exist for the Azure storage backend (core/storage/azure_blob.py) - see
    runbooks/RUNBOOK_AZURE_SETUP.md's "Known gaps relative to AWS" section for
    the current state of that gap.
  EOT
  type        = number
  default     = 1 # dev-safe default; MUST be set explicitly for any real deployment
}

variable "audit_retention_days" {
  description = <<-EOT
    Intended retention period, in days, for the audit container. NOT
    ENFORCED, same as phi_retention_days above - separately
    configurable from phi_retention_days, matching
    deploy/aws/variables.tf's identical phi_retention_days/audit_retention_days
    split. Audit retention commonly needs to be LONGER than clinical-record
    retention, not shorter: 45 CFR 164.316(b)(2)(i) requires HIPAA-required
    documentation (which the audit trail is part of) be retained 6 years,
    independent of whatever your state's clinical-record retention period is
    - core/audit/sink.py's S3AuditSink defaults to 2192 days (6 years) for
    exactly this reason when no explicit figure is provided. Set this
    deliberately rather than assuming it should match phi_retention_days.
  EOT
  type        = number
  default     = 1 # dev-safe default; MUST be set explicitly for any real deployment
}

# NOTE: variable "lock_immutability_policy" used to live here, selecting
# whether the container immutability policy was Locked (irreversible, the
# rough equivalent of AWS Object Lock COMPLIANCE). It is gone along with
# the immutability policies themselves - see storage.tf. Retention on
# Azure is now a recorded configuration value, not a container-enforced
# control.

variable "trusted_principal_object_ids" {
  description = <<-EOT
    Azure AD (Entra ID) object IDs of principals allowed to be assigned the
    PHI AI Platform RBAC roles (identities.tf, and the Key Vault Crypto User
    grant in keyvault.tf) - users, groups, or service principals. For a
    dev deployment this is typically your own user object ID:
      az ad signed-in-user show --query id -o tsv
    For a real deployment this should be the managed identity of the compute
    resource actually running the service, not a human principal - see
    deploy/aws/variables.tf's trusted_principal_arns for the identical AWS-side
    guidance.

    Left empty by default - unlike the AWS stack's IAM trust policy (which
    falls back to account root), Azure RBAC role assignments require an
    explicit principal ID with no equivalent "assume via root" fallback, so an
    empty list here means no role assignments happen until you provide one.
  EOT
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Key Vault
# ---------------------------------------------------------------------------

variable "purge_protection_enabled" {
  description = <<-EOT
    Whether to enable purge protection on the Key Vault (keyvault.tf).
    Note the contrast with the retention settings above, which are freely
    changeable precisely because nothing enforces them: this one is
    genuinely irreversible once turned on, and is now the only
    irreversible storage-side protection this stack offers on any cloud.

    DISABLED (false, the default): a deleted vault or key enters Azure's
      mandatory soft-delete state (on by default, cannot be turned off -
      see versions.tf's provider features block) but can still be purged
      early by a sufficiently-privileged principal, and the vault name
      frees up once purged. This is what makes the dev teardown flow
      (`terraform destroy` with purge_soft_delete_on_destroy = true in
      versions.tf) actually work without leaving a name reserved.

    ENABLED (true): once on, per Microsoft's own documentation this cannot
      be turned back off for the lifetime of the vault - even an
      Owner-level principal cannot force an early purge before the
      soft-delete retention window elapses, which is exactly the point
      for a production key protecting real PHI (a compromised or
      malicious admin credential cannot destroy the encryption key out
      from under an active investigation). Weigh that permanence
      deliberately before making it your default: it is the same class of
      one-way door as the WORM controls this project removed, and the
      reason it survived that removal is that it protects the key rather
      than freezing the data.
  EOT
  type        = bool
  default     = false
}

variable "key_rotation_days" {
  description = <<-EOT
    Automatic rotation period for the PHI store Key Vault key (keyvault.tf's
    azurerm_key_vault_key.store), in days. Set to null to disable
    rotation entirely - not recommended outside dev, mirroring
    deploy/gcp/variables.tf's key_rotation_period_seconds description
    exactly.

    Safe to enable (default: 90 days, matching GCP's default) as of the
    2026-08-17 audit's H5 fix: core/crypto/envelope.py's AzureKMS class
    now records the exact Key Vault key VERSION that wrapped each
    stored object's data encryption key, and binds every unwrap to
    that specific version rather than whatever is currently latest - so
    a rotation no longer breaks restoring anything stored before it.
    Before that fix, enabling rotation here would have been actively
    dangerous, not just unconfigured: every object stored under a
    since-rotated key version would have become permanently
    unrestorable. See that class's own docstring for the full mechanics
    of why Azure's RSA-OAEP wrap/unwrap has no server-side version
    resolution the way AWS/GCP KMS decrypt operations do.

    A period this codebase cannot correctly default to "no rotation" any
    more safely than deploy/aws/variables.tf's enable_key_rotation or
    deploy/gcp/variables.tf's key_rotation_period_seconds already default
    to rotation-on - see those variables' own descriptions for the
    shared reasoning (a stale, never-rotated encryption key sitting
    behind the sole barrier protecting stored PHI is a worse default
    than the modest operational cost of periodic rotation).
  EOT
  type        = number
  default     = 90
  nullable    = true
}

# ---------------------------------------------------------------------------
# Azure Database for PostgreSQL Flexible Server (optional secondary
# index + OMOP analytics layer)
# ---------------------------------------------------------------------------

variable "enable_db" {
  description = <<-EOT
    Whether to provision the Flexible Server instance backing the
    lightweight Postgres index (core/db/schema.sql) and, optionally,
    the OMOP CDM analytics layer (core/db/omop_schema.sql) - see
    database.tf. Matches deploy/aws/variables.tf and
    deploy/gcp/variables.tf's identical enable_db toggle: a deployment
    storing to Blob Storage without any secondary index is fully
    supported. Flexible Server has NO free tier of any kind, unlike
    this stack's storage/Key Vault costs - see database.tf's own cost
    section before enabling this for anything beyond brief testing.
  EOT
  type        = bool
  default     = false
}

variable "db_sku_name" {
  description = <<-EOT
    Flexible Server compute tier, in the provider's tier_Family_size
    format. Defaults to the cheapest available (B_Standard_B1ms, a
    Burstable tier, ~$12-15/month - see database.tf's own cost section)
    - Burstable tiers do not support high_availability, an acceptable
    tradeoff for a dev deployment but worth reconsidering for a real
    deployment's own availability requirements.
  EOT
  type        = string
  default     = "B_Standard_B1ms"
}

variable "db_storage_mb" {
  description = "Flexible Server storage, in MB. 32768 (32 GB) is the documented minimum this project's own research confirmed - left as an explicit default rather than relying on the provider's own default."
  type        = number
  default     = 32768
}

variable "db_publicly_accessible" {
  description = <<-EOT
    Whether the Flexible Server instance has a public network endpoint
    at all. Mirrors deploy/aws/variables.tf's db_publicly_accessible -
    set true only to connect directly from a laptop rather than from
    compute already inside the same VNet, and combine with
    db_allowed_ip_addresses below to restrict which addresses can even
    attempt a connection. False by default: no public endpoint, no
    network path to attempt a connection from outside Azure at all,
    regardless of RBAC/Entra auth.
  EOT
  type        = bool
  default     = false
}

variable "db_allowed_ip_addresses" {
  description = <<-EOT
    Individual IP addresses allowed to connect to the Flexible Server
    instance's public endpoint, when db_publicly_accessible is true.
    Unlike deploy/aws/variables.tf's db_allowed_cidr_blocks and
    deploy/gcp/variables.tf's db_allowed_cidr_blocks (both CIDR-based),
    Flexible Server's own firewall rule resource takes a start/end IP
    range rather than CIDR notation - each entry here becomes a
    single-address range (see database.tf's own comment). To connect
    from your own machine:
      curl -s https://checkip.amazonaws.com
    then add that address as a single entry - not a CIDR block.
  EOT
  type        = list(string)
  default     = []
}

variable "db_aad_admin_object_id" {
  description = <<-EOT
    Azure AD object ID of the initial Microsoft Entra administrator for
    the Flexible Server instance - the identity that first connects and
    runs core/db/bootstrap_azure.sql/core/db/omop_bootstrap_azure.sql.
    Required (no sensible default) whenever enable_db is true - see
    database.tf's own precondition. Typically your own object ID for a
    dev deployment:
      az ad signed-in-user show --query id -o tsv
  EOT
  type        = string
  default     = ""
}

variable "db_aad_admin_principal_name" {
  description = <<-EOT
    Display name/UPN of the principal identified by
    db_aad_admin_object_id above - Azure's own
    azurerm_postgresql_flexible_server_active_directory_administrator
    resource requires both the object ID and this name, and does not
    derive one from the other. For a dev deployment, typically your own
    sign-in name:
      az ad signed-in-user show --query userPrincipalName -o tsv
  EOT
  type        = string
  default     = ""
}

variable "db_aad_admin_principal_type" {
  description = "Principal type for db_aad_admin_object_id - \"User\" for a human developer (the common dev case), \"ServicePrincipal\" for an application registration or managed identity acting as the initial administrator instead."
  type        = string
  default     = "User"
}
# Made by Ryan Gomez & Co. Inc.
