# ---------------------------------------------------------------------------
# PHI store bucket: holds envelope-encrypted PHI ciphertext.
#
# NO IMMUTABILITY. This stack previously configured two layers here -
# enable_object_retention (the per-object Object Retention Lock
# capability) and a bucket-wide retention_policy floor, optionally
# locked. Both are gone, in every environment. Nothing at the storage
# layer prevents an object here from being overwritten or deleted by a
# principal holding the IAM permission to do so. What remains is object
# versioning (a prior generation survives an overwrite, and deleting a
# live object demotes it to a noncurrent generation rather than
# destroying it - though any specific generation can itself be deleted
# outright), the recorded SHA-256 digest, and the hash-chained audit
# log. Those make change VISIBLE, not IMPOSSIBLE. Retention is recorded
# as object metadata by core/storage/gcp_gcs.py and enforced by nothing.
#
# KNOWN GAP, and a one-way door: GCS Bucket Lock is permanent. Once a
# retention policy is LOCKED on a bucket it can be lengthened but never
# shortened or removed, for the life of the bucket - not by you, not by
# Google Support, not at any permission level. This stack locks nothing,
# so that permanence never binds a deployment made from this repo; the
# cost is that there is no storage-enforced retention floor here at all,
# and adding one later is a decision to make deliberately with full
# knowledge that locking it is irreversible.
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "store" {
  # GCS bucket names are globally unique and immutable once created, so
  # this string is fixed for the life of the bucket. The project ID is
  # folded in to make it globally unique without requiring the deployer
  # to invent a unique prefix.
  name     = "${var.name_prefix}-store-${var.gcp_project}"
  location = var.gcp_region
  project  = var.gcp_project

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true # kept for consistency with deploy/aws and deploy/azure's identical choice, and - with no retention policy and no Bucket Lock behind it - now the only thing that preserves a prior generation of an object through an overwrite or a live-object delete
  }


  encryption {
    default_kms_key_name = google_kms_crypto_key.store.id
  }

  labels = {
    name = "${var.name_prefix}-store"
    role = "phi-ai-data"
  }

  lifecycle {
    # Same reasoning as deploy/aws/s3_store.tf's precondition on
    # phi_retention_days and deploy/azure/storage.tf's identical
    # precondition: no hardcoded legal-minimum floor, only a guard
    # against the dev default (1 day) being carried unedited into a
    # real deployment.
    precondition {
      condition     = var.environment == "dev" || var.phi_retention_days > 1
      error_message = "phi_retention_days is still at the dev default (1 day) for a non-dev environment. Set it explicitly from the applicable state/federal retention requirement for your data - see docs/COMPLIANCE.md. There is no universal correct value here; do not silence this by copying in an arbitrary number without checking your own state's statute."
    }
  }

  depends_on = [
    google_project_service.storage,
    time_sleep.kms_iam_propagation,
  ]
}

# ---------------------------------------------------------------------------
# Audit bucket: the hash-chained application audit log
# (core/audit/sink.py's GCSAuditSink), kept as its own bucket rather
# than a prefix within the store bucket - matching deploy/aws's
# separate audit bucket, and unlike deploy/azure's design (which uses
# separate CONTAINERS within one storage account, because
# core/config/settings.py's azure_account_url is a single field). GCP
# has no equivalent single-account constraint - Settings.gcp_project
# already scopes every GCS/KMS call to one project regardless of how
# many buckets exist within it - so there is no forcing reason to share
# a bucket the way there was for Azure.
#
# Shares the SAME store key (google_kms_crypto_key.store) rather
# than provisioning a second key - the direct equivalent of
# deploy/aws/variables.tf's separate_audit_key = false option, a
# documented, deliberate cost/complexity tradeoff (an extra ~$0.06/month
# active key version, per key.rotation_period_seconds's own cost note),
# not a silent gap. A genuinely separate audit key - so that compromised
# store-key access does not also confer audit-log decrypt - is a
# real, valuable hardening step tracked in
# runbooks/RUNBOOK_GCP_SETUP.md's "Known gaps" section, not built in
# this installment.
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "audit" {
  name     = "${var.name_prefix}-audit-${var.gcp_project}"
  location = var.gcp_region
  project  = var.gcp_project

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }


  encryption {
    default_kms_key_name = google_kms_crypto_key.store.id
  }

  labels = {
    name = "${var.name_prefix}-audit"
    role = "phi-ai-audit"
  }

  lifecycle {
    precondition {
      condition     = var.environment == "dev" || var.audit_retention_days > 1
      error_message = "audit_retention_days is still at the dev default (1 day) for a non-dev environment. Set it explicitly - see docs/COMPLIANCE.md and 45 CFR 164.316(b)(2)(i)'s 6-year documentation-retention requirement, which is commonly the applicable floor for the audit trail specifically, independent of your state's clinical-record retention period."
    }
  }

  depends_on = [
    google_project_service.storage,
    time_sleep.kms_iam_propagation,
  ]
}

# ---------------------------------------------------------------------------
# On cost: what's actually confirmed free here, and what isn't
#
# CONFIRMED against cloud.google.com/storage/pricing directly (not a
# third-party summary): Cloud Storage's Always Free allowance is 5
# GB-months of Standard regional storage, aggregated across US-WEST1,
# US-CENTRAL1, and US-EAST1 specifically - var.gcp_region defaults to
# us-central1 for exactly this reason. Explicitly stated by Google to
# apply "both during and after the free trial period" - unlike AWS S3's
# free tier, which is a 12-month-only benefit, this one does not expire.
# Also includes 5,000 Class A operations and 50,000 Class B operations
# per month.
#
# What this means in practice: a low-volume dev deployment (small FHIR
# resources, typically 1-10 KB each) can plausibly stay within 5 GB for
# a meaningful amount of time, and PutObject/GetObject-equivalent
# operations (Class A/B respectively) have real headroom too - but this
# is not a guarantee for any specific workload, and grows past free
# immediately for anything beyond a small dev stack.
#
# What is NOT free regardless of region or usage: Cloud KMS key storage.
# See kms.tf's own cost section for the confirmed, current figures - the
# same "this project cannot run at literal $0" honesty
# deploy/aws/variables.tf's cost-controls section and
# deploy/azure/storage.tf's own cost section already establish for their
# platforms.
# ---------------------------------------------------------------------------
# Made by Ryan Gomez & Co. Inc.
