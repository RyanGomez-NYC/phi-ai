# ---------------------------------------------------------------------------
# Key Vault: the Azure equivalent of deploy/aws/kms.tf's customer-managed
# KMS key.
#
# SKU CHOICE: "standard", not "premium" or Managed HSM. Standard-tier keys
# are software-protected (FIPS 140 Level 1, per Key Vault's own overview
# docs at learn.microsoft.com/en-us/azure/key-vault/general/overview) -
# this is the direct functional equivalent of a regular AWS KMS customer
# managed key (also software-backed, not a dedicated CloudHSM cluster).
# Premium tier and Managed HSM both step up to different, materially more
# expensive HSM-backed offerings - Managed HSM in particular runs into the
# thousands of dollars a month regardless of usage, which has no place in
# a stack scoped to free/near-free infrastructure. Standard tier bills
# per-operation with no listed monthly minimum or per-key storage fee (see
# storage.tf's "On redundancy" section for the fuller cost discussion, and
# its own caveats about what this project could and couldn't verify from
# Microsoft's own pricing page directly).
#
# CONFIRMED VIA MICROSOFT Q&A (learn.microsoft.com/en-us/answers, not a
# third-party aggregator): during the first 12 months of an Azure free
# account, 10,000 Key Vault transactions/month are included at no charge;
# beyond that, standard tier's own per-operation rate applies with no free
# allowance. A low-volume deployment doing a handful of wrap operations per
# stored resource should stay well under that even without the free
# allowance - but this is the kind of claim worth re-confirming against
# your own actual usage rather than trusting a blanket "it'll be fine."
# ---------------------------------------------------------------------------

resource "azurerm_key_vault" "main" {
  name                = "${var.name_prefix}-kv-${substr(data.azurerm_client_config.current.subscription_id, 0, 8)}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id

  sku_name = "standard"

  # RBAC authorization (Azure role assignments), not the older
  # vault-access-policy model. This is the modern, Microsoft-recommended
  # approach, and it's what lets trusted_principal_object_ids below and
  # identities.tf's ingest/restore/auditor role assignments both work
  # through the same, consistent role-assignment mechanism the rest of
  # Azure RBAC uses - access policies are a separate, vault-specific
  # system that doesn't compose the same way.
  enable_rbac_authorization = true

  purge_protection_enabled = var.purge_protection_enabled

  # Azure's valid range for this setting is 7-90 days. Set to the
  # minimum (7) rather than the 90-day default: this is a dev-scoped
  # stack (see "targeting only free infrastructure" throughout this
  # build), and a shorter window means less time a deleted-but-not-yet-
  # purged vault name stays reserved during iterative dev work - the
  # same practical concern versions.tf's purge_soft_delete_on_destroy
  # comment describes. Reconsider this alongside purge_protection_enabled
  # before any real deployment; a production vault protecting real PHI
  # likely wants the longer window precisely because it delays how
  # quickly a compromised credential could make deleted key material
  # unrecoverable.
  soft_delete_retention_days = 7

  tags = {
    Name = "${var.name_prefix}-keyvault"
    Role = "phi-ai-kms"
  }
}

# ---------------------------------------------------------------------------
# Bootstrapping RBAC on a brand-new vault: the principal running Terraform
# needs its own role grant before it can create a key inside the vault it
# just created. Key Vault has no "creator gets automatic admin" behavior
# once enable_rbac_authorization is on (that implicit-access behavior is
# specific to the older access-policy model) - so without this, the very
# next resource below would fail with an authorization error against a
# vault that, from RBAC's perspective, nobody has been granted anything
# on yet.
# ---------------------------------------------------------------------------

resource "azurerm_role_assignment" "deployer_crypto_officer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# Azure RBAC role assignments are not instantaneous - propagation through
# Azure's authorization cache commonly takes on the order of a minute or
# two, a well-documented source of "AuthorizationFailed" errors when a
# dependent resource is created immediately after the role assignment it
# depends on, even though Terraform's own dependency graph is satisfied
# (the assignment object exists; the PERMISSION hasn't finished
# propagating yet). This wait exists specifically to absorb that gap on a
# first apply, rather than requiring anyone to hit the error once and
# re-run.
resource "time_sleep" "rbac_propagation" {
  depends_on      = [azurerm_role_assignment.deployer_crypto_officer]
  create_duration = "90s"
}

# ---------------------------------------------------------------------------
# The PHI store key itself. RSA, not EC or symmetric (oct/oct-HSM) - matches
# what core/crypto/envelope.py's AzureKMS class already expects: wrap/
# unwrap via RSA-OAEP-256, the same role AWS's symmetric KMS key plays for
# wrapping the AES-256-GCM data-encryption-key generated for every
# stored resource. oct/oct-HSM symmetric keys are Premium-tier-only in
# Key Vault (see learn.microsoft.com/en-us/azure/key-vault/keys/about-keys)
# - another reason RSA on Standard tier is the correct fit here, not just
# the one the application already assumes.
#
# ROTATION (2026-08-17 audit, H5; implemented 2026-08-18). Previously no
# rotation_policy at all - an undocumented parity gap versus
# deploy/gcp/kms.tf's enforced 90-day rotation_period and deploy/aws/kms.tf's
# enforced enable_key_rotation precondition. That was NOT a safe-by-omission
# gap: enabling rotation before core/crypto/envelope.py's AzureKMS fix
# (this same audit pass) would have made things actively worse, not
# better - Azure Key Vault's RSA-OAEP wrap/unwrap is bound to a specific
# key VERSION with no server-side version resolution the way AWS/GCP KMS
# provide (see that class's own docstring for the full mechanics), so
# AzureKMS previously could not survive a rotation at all: every DEK
# wrapped before a rotation would fail to unwrap afterward, permanently,
# for every already-stored object. Configuring automatic rotation
# without fixing that first would have turned a currently-dormant bug
# (nothing was rotating the key) into a routinely-triggered one. Now that
# AzureKMS records the exact versioned key ID alongside every wrapped
# DEK and binds unwrap to that exact version, rotation here is safe -
# each object continues decrypting under whichever version wrapped it,
# indefinitely, regardless of how many times the key has since rotated.
#
# var.key_rotation_days mirrors GCP's key_rotation_period_seconds
# variable shape (a configurable period, nullable to disable) rather than
# AWS's simple enable_key_rotation boolean - AWS's own automatic rotation
# has a fixed ~annual cadence with no period to configure, so a boolean
# is all that toggle needs; Azure's rotation_policy genuinely accepts an
# arbitrary period the same way GCP's does, so the variable shape follows
# the cloud's actual capability rather than copying AWS's simpler case.
# ---------------------------------------------------------------------------

resource "azurerm_key_vault_key" "store" {
  # A Key Vault key name is an immutable identifier. Once this key exists
  # and has wrapped its first DEK, changing this string does not rename
  # it - it plans DESTROY of this key and CREATE of a differently-named
  # one, and Azure Key Vault's RSA-OAEP unwrap is bound to a specific key
  # VERSION with no server-side version resolution (see
  # core/crypto/envelope.py's AzureKMS docstring), so every blob wrapped
  # under the destroyed key becomes permanently undecryptable. Set it
  # before the first apply, like var.name_prefix itself.
  #
  # AZURE-SPECIFIC TWIST worth knowing before assuming a destroy is
  # merely recoverable-by-recreating: Key Vault soft-delete is mandatory
  # and cannot be disabled, so a destroyed key's NAME also stays reserved
  # for the vault's soft_delete_retention_days window - and if
  # var.purge_protection_enabled is true, it cannot be purged early at
  # all, by anyone, including an Owner. Re-creating under the same name
  # would fail until that window elapses.
  name         = "${var.name_prefix}-store-key"
  key_vault_id = azurerm_key_vault.main.id
  key_type     = "RSA"
  key_size     = 2048
  key_opts     = ["wrapKey", "unwrapKey"]

  dynamic "rotation_policy" {
    for_each = var.key_rotation_days != null ? [1] : []
    content {
      automatic {
        time_after_creation = "P${var.key_rotation_days}D"
      }
    }
  }

  depends_on = [time_sleep.rbac_propagation]
}

# ---------------------------------------------------------------------------
# Grant every trusted principal (var.trusted_principal_object_ids) the
# narrower "Key Vault Crypto User" role - can use existing keys to
# wrap/unwrap/encrypt/decrypt/sign/verify, but cannot create, delete, or
# manage keys the way Crypto Officer above can. This is the Key-Vault-side
# half of the same minimum-necessary principle deploy/aws/iam.tf's three
# separate roles embody; the OTHER half - actually separating ingest from
# restore from auditor at the Azure RBAC level, the direct equivalent of
# that file - is identities.tf, not this resource. This grant to trusted
# principals exists alongside identities.tf's own narrower role
# assignments specifically for local development, where a human
# developer's own Azure AD identity needs to exercise both the ingest and
# restore code paths - see identities.tf's module-level comment for why
# Azure managed identities cannot be assumed from a laptop the way AWS
# IAM roles can, which is what makes this broader grant necessary here
# even though identities.tf's own role assignments are already properly
# separated.
# ---------------------------------------------------------------------------

resource "azurerm_role_assignment" "trusted_crypto_user" {
  for_each = toset(var.trusted_principal_object_ids)

  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = each.value
}

# See outputs.tf for this stack's centralized output file (the
# env_fragment-equivalent, matching deploy/aws/outputs.tf's pattern of
# keeping every output in one place) - azure_vault_url and
# key_vault_key_name there expose this vault's URI and this key's name
# respectively, rather than duplicating separate outputs here.
# Made by Ryan Gomez & Co. Inc.
