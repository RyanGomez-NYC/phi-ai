# ---------------------------------------------------------------------------
# Resource group: the Azure container for every resource this stack creates.
# Azure has no direct analog to "an AWS account" as the blast-radius
# boundary - a resource group is the closest equivalent for this project's
# purposes (everything the PHI AI Platform owns lives in one, deletable as a
# unit in dev, protected by prevent_deletion_if_contains_resources in
# versions.tf outside it).
# ---------------------------------------------------------------------------

resource "azurerm_resource_group" "main" {
  name     = "${var.name_prefix}-${var.environment}"
  location = var.azure_location
}

# ---------------------------------------------------------------------------
# Storage account's own identity, used ONLY for the storage account to
# unwrap its own encryption-at-rest key from Key Vault (the
# customer_managed_key wiring below). This is infrastructure-internal -
# application code (scheduler.py, restore.py, etc.) never authenticates
# as this identity; it authenticates as one of the ingest/restore/auditor
# identities in identities.tf instead. Confirmed via
# the azurerm provider's own storage_account docs: customer_managed_key
# "can only be set when... the identity type is UserAssigned" - a
# system-assigned identity does not work for this specific wiring, which
# is why this is a standalone azurerm_user_assigned_identity rather than
# an `identity { type = "SystemAssigned" }` block on the storage account
# itself.
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "storage_cmk" {
  name                = "${var.name_prefix}-storage-cmk"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
}

resource "azurerm_role_assignment" "storage_cmk_key_access" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto Service Encryption User"
  principal_id         = azurerm_user_assigned_identity.storage_cmk.principal_id

  # This role assignment must exist, and have propagated, before Azure
  # will accept the customer_managed_key wiring below - same class of
  # RBAC-propagation gap keyvault.tf's time_sleep.rbac_propagation exists
  # for. depends_on here (rather than relying on the implicit dependency
  # from referencing this resource) makes that ordering requirement
  # explicit for whoever reads this file next.
  depends_on = [azurerm_key_vault_key.store]
}

resource "time_sleep" "storage_cmk_rbac_propagation" {
  depends_on      = [azurerm_role_assignment.storage_cmk_key_access]
  create_duration = "90s"
}

# ---------------------------------------------------------------------------
# PHI store storage account: holds envelope-encrypted PHI ciphertext.
#
# NO IMMUTABILITY, at either level - container or version. This stack
# applied container-level WORM policies until immutability was removed
# (see the NOTE below the container resources); it never used
# version-level WORM. Nothing at the storage layer prevents a blob in
# this account from being overwritten or deleted by a principal holding
# the RBAC permission to do so. Retention is recorded as blob metadata by
# core/storage/azure_blob.py and enforced by nothing. What remains is
# blob versioning and the 7-day soft-delete window configured below:
# bounded recovery after the fact, not prevention.
#
# WHY VERSION-LEVEL WORM WOULD BE HARD TO REINTRODUCE - a one-way door,
# stated here as a constraint on any future change rather than as a
# description of current state: version-level immutable storage support
# must be enabled AT ACCOUNT CREATION. Per Microsoft's own documentation
# (learn.microsoft.com, "Version-level WORM policies for immutable blob
# data"): "Version-level policies require that blob versioning is enabled
# for the storage account ... There's no option to enable version-level
# WORM for pre-existing accounts." This account is not created with that
# support, so adding version-level WORM later means recreating the
# account and migrating every blob - get it right the first time or pay
# for it in a migration.
#
# Container-level WORM does not carry that particular constraint: it is
# supported on both new AND EXISTING containers, which is why it was the
# level this stack used, and which would matter again if a
# psychotherapy-notes-equivalent container (mirroring
# deploy/aws/s3_psychotherapy.tf) were ever added to this SAME account
# after the initial apply rather than needing its own account from day
# one. It has a different permanence problem instead - see the NOTE
# below the container resources.
#
# Blob versioning stays enabled regardless, since Microsoft's docs note
# it "may have a billing impact" only for the STORAGE consumed by kept
# versions, not for enabling the setting itself - and this project's
# write pattern (each stored resource gets its own key, never
# overwritten in normal operation) means old-version storage from actual
# overwrites should stay negligible in practice.
# ---------------------------------------------------------------------------

resource "azurerm_storage_account" "store" {
  # Storage account names are globally unique across all of Azure and
  # immutable once created, so this string is fixed for the life of the
  # account. It is also LENGTH-CONSTRAINED in a way the other two clouds
  # are not: 3-24 characters, lowercase alphanumeric ONLY, no hyphens -
  # which is why var.name_prefix carries no hyphens either. The budget
  # here is name_prefix + "store" (5) + 8 characters of the subscription
  # ID, so the prefix may be at most 11 characters. With the default
  # "phiai" that is 5 + 5 + 8 = 18/24. The subscription ID's leading 8
  # characters are the first block of a GUID - lowercase hex, no hyphen -
  # so the result satisfies the character-class rule by construction.
  name                = "${var.name_prefix}store${substr(data.azurerm_client_config.current.subscription_id, 0, 8)}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  account_tier             = "Standard"
  account_kind             = "StorageV2" # required for blob versioning and for the customer-managed-key and infrastructure-encryption wiring below; it is also what Microsoft's WORM docs require, which mattered when this stack still applied immutability policies and would matter again if any were ever reintroduced
  account_replication_type = "LRS"       # locally-redundant - see "On redundancy" below for the cost/durability tradeoff this makes explicitly

  # Required for the customer_managed_key wiring below - confirmed via
  # the azurerm provider's own docs that a system-assigned identity does
  # not satisfy this requirement, only UserAssigned does. This is the
  # storage-account-level identity from earlier in this file, not one of
  # the application-level ingest/restore/auditor identities.
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.storage_cmk.id]
  }

  https_traffic_only_enabled = true
  min_tls_version            = "TLS1_2"

  # FIXED: see the matching, more detailed comment in
  # deploy/azure/bootstrap/main.tf - this had the identical bug. AWS's
  # public-access-block on s3_store.tf leaves the bucket reachable over
  # the internet for any authenticated IAM caller (that's how
  # core/storage/aws_s3.py itself connects, from a plain dev laptop);
  # public_network_access_enabled = false is a stronger, DATA-PLANE-level
  # network cutoff with no AWS equivalent used elsewhere in this project's
  # dev configuration. core/storage/azure_blob.py's BlobServiceClient
  # talks to this account's data-plane endpoint directly
  # (blob.core.windows.net) via DefaultAzureCredential - with no Private
  # Endpoint/VNet configured (none is, anywhere in this stack yet), that
  # call would fail to connect at all from an ordinary dev laptop or an
  # un-VNet-integrated container, regardless of how correctly RBAC is
  # configured. allow_nested_items_to_be_public is the setting that
  # actually matches what "block public access" means on the AWS side:
  # it prevents any blob/container from being marked anonymously-readable
  # without cutting off authenticated access over the internet.
  # Network-level isolation is the kind of thing
  # RUNBOOK_AWS_SETUP.md's own "Promoting to production" section treats
  # as a later, deliberate step (VPC endpoints), not a dev default that
  # silently makes the stack unreachable.
  public_network_access_enabled   = true
  allow_nested_items_to_be_public = false

  # core/storage/azure_blob.py and core/audit/sink.py's
  # AzureBlobAuditSink both authenticate via DefaultAzureCredential
  # (RBAC/Azure AD) exclusively - neither ever constructs a
  # BlobServiceClient from an account key or connection string. Disabled
  # outright now that identities.tf's role assignments confirm nothing
  # in this stack needs key-based access: every read/write path, ingest,
  # restore, and auditor alike, goes through an RBAC-granted identity.
  # This was left enabled in an earlier pass pending identities.tf
  # existing to confirm that - it now does, so there is no reason to
  # leave a powerful, bearer-token-style shared-secret authentication
  # path available when nothing uses it. Matches this project's existing
  # avoidance of shared secrets elsewhere (Epic's RS384 JWT rather than a
  # client secret; AWS's IAM-based auth with no access keys in .env).
  shared_access_key_enabled = false

  blob_properties {
    # Blob versioning. NOT a precondition for anything else in this file
    # any more - it was written here as the prerequisite for a
    # version-level immutability policy, and no immutability policy of
    # any kind exists in this stack. It stays on for its own sake: with
    # nothing enforcing retention, a retained prior version is one of the
    # only two ways an overwritten or deleted blob is recoverable at all.
    versioning_enabled = true

    delete_retention_policy {
      # 7-day soft delete. Read this as the ONLY deletion protection this
      # account has, not as a convenience layer under something stronger:
      # the container immutability policies that used to sit above it are
      # gone, so a delete by any principal with the RBAC permission
      # succeeds and the blob is recoverable for 7 days and then
      # permanently gone. Sizing this window is therefore a real decision
      # about how long an accidental or malicious deletion can go
      # unnoticed and still be undone - not a formality.
      days = 7
    }
  }

  # infrastructure_encryption_enabled adds a SECOND layer of encryption at
  # the Azure platform level, using a DIFFERENT algorithm/key than the
  # primary SSE layer - defense in depth, at no additional cost (unlike
  # AWS, where nothing analogous to this exists as a toggle; SSE-KMS is
  # already the single encryption layer there). Mirrors the "two
  # independent encryption layers" spirit of this project's own
  # application-level envelope encryption (core/crypto/envelope.py) sitting
  # on top of whatever the storage layer itself does - one more layer
  # never hurts and this one is free.
  infrastructure_encryption_enabled = true

  tags = {
    Name = "${var.name_prefix}-store"
    Role = "phi-ai-data"
  }

  lifecycle {
    # Same reasoning as deploy/aws/s3_store.tf's precondition on
    # phi_retention_days: no hardcoded legal-minimum floor, only a
    # guard against the dev default (1 day) being carried unedited into a
    # real deployment. Still worth catching even though nothing enforces
    # the value - it is recorded on every blob and drives documented
    # disposition.
    precondition {
      condition     = var.environment == "dev" || var.phi_retention_days > 1
      error_message = "phi_retention_days is still at the dev default (1 day) for a non-dev environment. Set it explicitly from the applicable state/federal retention requirement for your data - see docs/COMPLIANCE.md. There is no universal correct value here; do not silence this by copying in an arbitrary number without checking your own state's statute."
    }

    # Required whenever customer-managed-key encryption is applied via
    # the separate azurerm_storage_account_customer_managed_key resource
    # below rather than an inline customer_managed_key block on this
    # resource - confirmed via the azurerm provider's own storage_account
    # docs: "When using the azurerm_storage_account_customer_managed_key
    # resource, you will need to use ignore_changes on the
    # customer_managed_key block." Without this, every subsequent
    # terraform plan would show a spurious diff trying to reset
    # customer_managed_key back to empty, since this resource's own
    # config never sets it directly.
    ignore_changes = [customer_managed_key]
  }
}

# ---------------------------------------------------------------------------
# The actual customer-managed-key wiring. A separate resource, not an
# inline customer_managed_key block on azurerm_storage_account.store
# above - confirmed necessary via the azurerm provider's own docs: the
# storage account, the Key Vault, AND the RBAC grant letting the storage
# account's identity use the key all have to exist and have PROPAGATED
# before this can succeed, which an inline block on the storage account
# resource itself cannot correctly sequence (the storage account would
# need to reference a role assignment that in turn references the
# storage account's own identity - a dependency cycle an inline block
# can't express, which is exactly why the provider documents this
# as requiring the separate resource + ignore_changes pattern).
# ---------------------------------------------------------------------------

resource "azurerm_storage_account_customer_managed_key" "store" {
  storage_account_id        = azurerm_storage_account.store.id
  key_vault_id              = azurerm_key_vault.main.id
  key_name                  = azurerm_key_vault_key.store.name
  user_assigned_identity_id = azurerm_user_assigned_identity.storage_cmk.id

  # Waits for the RBAC grant to have propagated (see
  # time_sleep.storage_cmk_rbac_propagation above) - without this,
  # applying the CMK wiring immediately after granting the role can hit
  # the same AuthorizationFailed race keyvault.tf's own time_sleep exists
  # to absorb.
  depends_on = [time_sleep.storage_cmk_rbac_propagation]
}

# ---------------------------------------------------------------------------
# Audit container: the hash-chained application audit log
# (core/audit/sink.py's AzureBlobAuditSink), kept in its own container
# within this SAME storage account rather than a separate account.
#
# WHY THE SAME ACCOUNT, UNLIKE AWS'S SEPARATE AUDIT BUCKET: core/config/settings.py's
# Settings dataclass has exactly one azure_account_url field - the
# application was designed around one Azure storage account per
# deployment, with separate CONTAINERS distinguishing stored PHI from
# audit data, not separate accounts the way deploy/aws/s3_audit.tf uses
# a wholly separate bucket. Role separation between what can read/write
# stored PHI versus audit data is still enforced - see identities.tf -
# just at the CONTAINER scope within one account
# rather than at the account boundary. Azure RBAC role assignments can
# target a scope as specific as an individual blob container, so this
# does not weaken that separation, only changes where the boundary is
# drawn.
#
# Encryption: this container uses the SAME customer-managed key as the
# store container above (the storage-account-level
# customer_managed_key wiring applies account-wide, to every container in
# it). This is the direct equivalent of deploy/aws/variables.tf's
# separate_audit_key = false option - a documented, deliberate cost/
# complexity tradeoff, not a silent gap. Azure Storage's "encryption
# scopes" feature could give the audit container its own, different key
# within the same account (the closest Azure equivalent to
# separate_audit_key = true), but that adds real complexity this
# installment has not yet researched to the standard the rest of this
# stack's claims are held to - a fast-follow, not a default to reach for
# without first confirming it as carefully as everything else here.
# ---------------------------------------------------------------------------

resource "azurerm_storage_container" "audit" {
  name                  = "audit"
  storage_account_id    = azurerm_storage_account.store.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "store" {
  # The container's own name is "fhir" - it describes the content and
  # carries no product branding, so the rename does not touch it. Note
  # that a container name IS immutable: renaming it later would be a
  # destroy-and-create and would delete every blob in it.
  name                  = "fhir"
  storage_account_id    = azurerm_storage_account.store.id
  container_access_type = "private"
}

# NOTE: azurerm_storage_container_immutability_policy resources for both
# the "fhir" and "audit" containers used to live here, applying
# container-level WORM with an optional irreversible lock. Both are gone,
# in every environment.
#
# Nothing now prevents a blob in these containers from being deleted by a
# principal holding the RBAC permission to do so; retention is recorded
# as blob metadata by core/storage/azure_blob.py and enforced by nothing.
# The account's 7-day soft-delete window (delete_retention_policy, above)
# is the only deletion protection left, and it is a recovery window
# rather than a bar.
#
# KNOWN GAP, and a one-way door in the direction that can still happen:
# an Azure immutability policy, once LOCKED, can never be unlocked.
# Microsoft's own documentation is explicit that "Once an Immutability
# Policy has been locked, it cannot be unlocked" - it holds WORM on that
# container until every affected blob's retention interval expires,
# regardless of what any Terraform configuration later says, and no
# provider can remove it. This stack locks nothing, so nothing here is
# in that state; anyone adding a locked policy later is making a
# permanent decision. An UNLOCKED policy is deletable normally.

# ---------------------------------------------------------------------------
# On redundancy: LRS vs GRS/ZRS, and what "free infrastructure" means here
#
# LRS (locally-redundant storage) keeps three copies of data within a
# single datacenter. It is the cheapest redundancy tier Azure offers and
# the only one this stack uses by default - GRS/GZRS (geo-redundant,
# replicating to a paired region) roughly DOUBLES the per-GB storage rate
# for the added durability.
#
# WHAT THIS PROJECT CAN CONFIRM FROM MICROSOFT'S OWN DOCUMENTATION, AND
# WHAT IT CANNOT (in the spirit of deploy/aws/variables.tf's cost-controls
# section, which states AWS KMS's $1/month floor as a fact because it's
# directly, currently confirmable - this section is deliberately more
# hedged where the equivalent Azure facts were not):
#   - Key Vault Standard tier (keyvault.tf) bills per-operation with no
#     listed monthly minimum or per-key storage fee. CONFIRMED via
#     Microsoft Q&A (learn.microsoft.com/en-us/answers, not a third-party
#     aggregator): $0.03 per 10,000 operations for both key and secret
#     operations on Standard tier, and during an Azure free account's
#     first 12 months, 10,000 Key Vault transactions/month are included
#     at no charge - after that, every operation bills at the standard
#     rate with no free allowance. This project's own tooling could not
#     reliably extract a live, current dollar figure from
#     azure.microsoft.com/en-us/pricing/details/key-vault/ directly (that
#     page renders its price table client-side), so treat $0.03/10k as
#     "confirmed as of this research, re-verify before budgeting a real
#     deployment," not as a number this codebase will keep current on its
#     own. What's structurally certain either way: there is no AWS-KMS-
#     style flat monthly floor for a Standard-tier vault - a low-volume
#     deployment's cost here should be small even past the 12-month window.
#   - Blob Storage (this resource) bills for capacity, transactions, and
#     egress at standard published rates regardless of any free-tier
#     classification. CONFIRMED via Microsoft Q&A: the commonly-cited
#     "5 GB free" Blob Storage allowance is a 12-MONTH free-account
#     benefit, not an always-free one - several third-party sources
#     found during this research incorrectly classified it as always-free,
#     which is exactly the kind of discrepancy worth resolving against
#     Microsoft's own words rather than the majority of secondary sources.
#     Do not assume $0, and do not assume it stays $0 indefinitely even in
#     the free-account window's first year - confirm current LRS Hot tier
#     per-GB pricing for your region on
#     azure.microsoft.com/en-us/pricing/details/storage/blobs/, the same
#     way deploy/aws's docs/COST.md states real dollar figures rather than
#     "free" for the AWS stack.
#   - Managed identities (identities.tf) carry no cost of their own -
#     this is long-standing, uncontested Azure behavior, not a
#     time-limited or quantity-limited offer.
# A more complete, numbers-first cost breakdown (mirroring docs/COST.md)
# is still worth building - this section covers Key Vault and Blob
# Storage, the two pieces with genuine free-tier ambiguity worth
# resolving carefully, but does not yet total up custom role
# definitions, RBAC role assignment count, or the two time_sleep delays'
# effect on apply time (no direct cost, but worth documenting as an
# operational characteristic). Tracked as a fast-follow alongside the
# other gaps in runbooks/RUNBOOK_AZURE_SETUP.md's "Known gaps relative
# to AWS" section, not because the underlying resources are incomplete -
# customer-managed-key wiring, RBAC role separation
# (deploy/azure/identities.tf), and the audit-trail equivalent
# (core/audit/sink.py's AzureBlobAuditSink) are all built and wired
# together as of this stack - but because a genuinely complete cost
# accounting deserves the same dedicated pass docs/COST.md itself got on
# the AWS side, not a few bullet points appended here.
# ---------------------------------------------------------------------------
# Made by Ryan Gomez & Co. Inc.
