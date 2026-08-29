# ---------------------------------------------------------------------------
# Cloud KMS: the GCP equivalent of deploy/aws/kms.tf's customer-managed
# KMS key and deploy/azure/keyvault.tf's Key Vault key.
#
# KEY RING LOCATION MUST MATCH THE BUCKET LOCATION. Confirmed against
# the Terraform Registry's own google_storage_bucket documentation: "You
# must pay attention to whether the crypto key is available in the
# location that this bucket is created in" - a bucket in var.gcp_region
# cannot use a CMEK from a key ring in a different location. Both this
# key ring and both buckets (storage.tf) use var.gcp_region, so this is
# satisfied by construction, not left to chance.
#
# Key rings CANNOT BE DELETED, ever, once created - confirmed from
# multiple independent Terraform guides describing this exact GCP
# behavior. This is unlike AWS KMS keys or Azure Key Vault keys, both of
# which support (scheduled, delayed) deletion. Two practical
# consequences, both worth acting on BEFORE the first apply:
#
#   1. "Tearing down the dev stack" in runbooks/RUNBOOK_GCP_SETUP.md is
#      not complete. The key RING outlives any terraform destroy - only
#      the CRYPTO KEY within it, and its versions, can be scheduled for
#      destruction.
#
#   2. The ring name below interpolates var.name_prefix, so the prefix
#      is effectively permanent from the moment this ring is first
#      created. Choose it deliberately up front; a later change does not
#      rename the ring, it creates a second one and leaves the first
#      occupying its name forever. Neither AWS nor Azure has an
#      equivalent unremovable container - do not carry a mental model
#      from either one over to this.
# ---------------------------------------------------------------------------

resource "google_kms_key_ring" "main" {
  name     = "${var.name_prefix}-keyring"
  location = var.gcp_region
  project  = var.gcp_project

  depends_on = [google_project_service.kms]
}

# ---------------------------------------------------------------------------
# The PHI store key itself. Symmetric (GOOGLE_SYMMETRIC_ENCRYPTION),
# matching what core/crypto/envelope.py's GCPKMS class already expects:
# its generate_data_key()/unwrap_data_key() methods call Cloud KMS's
# encrypt/decrypt RPCs directly on a symmetric key - there is no
# separate "GenerateDataKey" server-side operation the way AWS KMS has
# one; the DEK is generated locally (os.urandom(32)) and wrapped via a
# plain encrypt call, the same envelope-encryption pattern as the other
# two clouds, just without that one AWS-specific convenience API.
#
# purpose = "ENCRYPT_DECRYPT" is the provider's own default and is left
# unstated below (matching the several confirmed working Terraform
# examples found during this stack's research, none of which set it
# explicitly for a symmetric key) - stated here in this comment instead,
# so the choice is documented without adding a redundant line that
# would only ever be set to what the provider already defaults to.
# ---------------------------------------------------------------------------

resource "google_kms_crypto_key" "store" {
  # A Cloud KMS crypto key name is an immutable identifier. Once this key
  # exists and has wrapped its first DEK, changing this string does not
  # rename it - it plans DESTROY of this key and CREATE of a
  # differently-named one, and every object wrapped by the destroyed key
  # becomes permanently undecryptable with no recovery path, by design.
  # Set it before the first apply, like var.name_prefix itself.
  name     = "${var.name_prefix}-store-key"
  key_ring = google_kms_key_ring.main.id

  rotation_period = var.key_rotation_period_seconds

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE" # matches AWS's default (non-CloudHSM) and Azure's Standard (non-Premium/Managed-HSM) tier choices elsewhere in this project - see deploy/azure/keyvault.tf's own cost reasoning for why HSM-backed tiers have no place in a stack scoped to free/near-free infrastructure
  }

  destroy_scheduled_duration = "${var.key_destroy_scheduled_duration_seconds}s"

  lifecycle {
    # Terraform's own provider documentation states this plainly: "When
    # Terraform destroys these keys, any data previously encrypted with
    # these keys will be irrecoverable." Deliberately NOT set to
    # prevent_destroy = true here, though - unlike the officially
    # recommended production pattern - because this stack is explicitly
    # scoped to a DEV deployment that should remain tear-down-able (the
    # same "easy to destroy in dev" posture deploy/aws and deploy/azure
    # already establish through variable defaults rather than a
    # Terraform-level hard block). A real deployment should add
    # `prevent_destroy = true` here deliberately, as its own choice, not
    # inherit it silently from this dev-scoped stack.
    precondition {
      condition     = var.environment == "dev" || var.key_rotation_period_seconds != null
      error_message = "key_rotation_period_seconds should not be disabled (null) for a non-dev environment - see this variable's own description in variables.tf."
    }
  }

  depends_on = [google_project_service.kms]
}

# ---------------------------------------------------------------------------
# CMEK requires the GCS service agent - a Google-managed service account
# automatically provisioned per-project, distinct from any
# deployer-created service account (identities.tf) - to hold
# roles/cloudkms.cryptoKeyEncrypterDecrypter on this specific key.
# Without this, per the Terraform provider's own documentation, "If you
# forget this binding, resource creation will fail with a permission
# error" - confirmed from multiple independent, consistent sources
# during this stack's research, not assumed. This is the GCP-side
# equivalent of deploy/azure/storage.tf's storage-account-identity
# grant on its Key Vault key, and of the RBAC-propagation delay that
# grant needed before the storage account's own CMK wiring could
# succeed - the same class of eventual-consistency gap, addressed here
# with the same time_sleep pattern.
# ---------------------------------------------------------------------------

data "google_storage_project_service_account" "gcs" {
  project = var.gcp_project
}

resource "google_kms_crypto_key_iam_member" "gcs_service_agent" {
  crypto_key_id = google_kms_crypto_key.store.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "time_sleep" "kms_iam_propagation" {
  depends_on      = [resource.google_kms_crypto_key_iam_member.gcs_service_agent]
  create_duration = "60s" # absorbs IAM propagation delay before storage.tf's buckets attempt to use this key as their default_kms_key_name - same class of gap deploy/azure/keyvault.tf's time_sleep.rbac_propagation exists to absorb, though GCP IAM has historically propagated somewhat faster than Azure RBAC in practice, hence the shorter window than that file's 90s
}

# ---------------------------------------------------------------------------
# On cost: what's actually confirmed here, stated plainly
#
# CONFIRMED against cloud.google.com/kms/pricing directly: active
# symmetric key versions bill at $0.000082192/hour at the lowest
# published tier - roughly $0.06/month per active version, re-verify at
# that URL before budgeting a real deployment rather than trusting this
# figure to stay current. Cryptographic operations (encrypt/decrypt)
# bill at $0.03 per 10,000 - the same rate AWS KMS charges for symmetric
# operations. Admin operations (key/key-ring management) are free.
#
# ONE IMPORTANT CAVEAT, stated honestly rather than glossed over: Google
# also publishes a genuinely free allowance (100 key versions, 10,000
# cryptographic operations/month) - but that allowance is specifically
# for keys provisioned through "Cloud KMS Autokey," a distinct,
# automated key-provisioning mechanism this stack does NOT use. This
# stack manually defines the key ring and key (google_kms_key_ring/
# google_kms_crypto_key above) specifically so it can be granted the
# precise, narrow IAM bindings identities.tf's role separation needs -
# Autokey's automated provisioning was not researched to the standard
# the rest of this stack's claims are held to, and switching to it
# later to chase this free allowance would be a real architecture
# change, not a toggle. Budget for the ~$0.06/month figure above, not
# $0, for the key this stack actually creates.
# ---------------------------------------------------------------------------
# Made by Ryan Gomez & Co. Inc.
