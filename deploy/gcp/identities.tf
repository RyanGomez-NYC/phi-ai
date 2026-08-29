# ---------------------------------------------------------------------------
# Three service accounts, mirroring the identical ingest/restore/
# auditor role separation deploy/aws/iam.tf and deploy/azure/identities.tf
# both use, for the same "minimum necessary" reasoning stated in full in
# deploy/aws/iam.tf's own header:
#
#   ingest  - writes stored objects, cannot decrypt PHI.
#   restore - reads and decrypts stored objects for records requests.
#   auditor - reads the audit log only, no PHI access at all.
#
# GCP's own IAM model is purely additive - there is no explicit-deny
# mechanism the way AWS IAM has one, so "this role cannot decrypt PHI"
# here means simply never granting it a KMS role, not an explicit deny
# statement the way deploy/aws/iam.tf's ingest role carries one. Absence
# of a grant is itself a complete guarantee under GCP's model.
#
# NOTE ON NAMES: account_id below feeds the service account's real,
# immutable email address, and it interpolates var.name_prefix. On GCP
# that matters more than elsewhere, because Cloud SQL IAM database
# authentication derives the Postgres ROLE name directly from this email
# (database.tf's header has the full account) - so the prefix determines
# a database role name too, and both are fixed from the first apply.
# ---------------------------------------------------------------------------

resource "google_service_account" "ingest" {
  account_id   = "${var.name_prefix}-ingest"
  display_name = "PHI AI Platform ingest"
  description  = "Writes encrypted PHI and appends audit records. Cannot decrypt stored data."
  project      = var.gcp_project
}

resource "google_service_account" "restore" {
  account_id   = "${var.name_prefix}-restore"
  display_name = "PHI AI Platform restore"
  description  = "Reads and decrypts stored PHI for authorized records requests. Cannot write."
  project      = var.gcp_project
}

resource "google_service_account" "auditor" {
  account_id   = "${var.name_prefix}-auditor"
  display_name = "PHI AI Platform auditor"
  description  = "Verifies audit chain integrity. No PHI access at all."
  project      = var.gcp_project
}

# ---------------------------------------------------------------------------
# Ingest: write-only on the store bucket, append-only on the audit
# bucket, encrypt-only on the key.
#
# roles/storage.objectViewer alongside objectCreator on the store
# bucket is deliberate, not an oversight - core/storage/gcp_gcs.py's
# put_object() performs a HeadObject-equivalent read for idempotency
# checks before writing, the identical reasoning
# deploy/aws/iam.tf's CheckExistingObjects statement documents for its
# own ingest role. This exposes ciphertext + metadata only: without the
# KMS decrypt grant below (which this role never receives), neither
# read capability can turn into readable PHI.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "ingest_store_create" {
  bucket = google_storage_bucket.store.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_storage_bucket_iam_member" "ingest_store_view" {
  bucket = google_storage_bucket.store.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_storage_bucket_iam_member" "ingest_audit_create" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_storage_bucket_iam_member" "ingest_audit_view" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

# Encrypt-only, built-in role - no custom role needed the way
# deploy/azure/keyvault.tf's Key Vault RBAC model requires for an
# equivalent encrypt-without-decrypt grant. This is the entire reason
# the ingest role cannot decrypt PHI: it never holds
# roles/cloudkms.cryptoKeyDecrypter or the combined
# cryptoKeyEncrypterDecrypter role.
resource "google_kms_crypto_key_iam_member" "ingest_encrypt" {
  crypto_key_id = google_kms_crypto_key.store.id
  role          = "roles/cloudkms.cryptoKeyEncrypter"
  member        = "serviceAccount:${google_service_account.ingest.email}"
}

# ---------------------------------------------------------------------------
# Restore: read/decrypt on the store bucket, append-only on the audit
# bucket (every restore must be audit-logged, same as
# deploy/aws/iam.tf's restore role), decrypt-only on the key.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "restore_store_view" {
  bucket = google_storage_bucket.store.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.restore.email}"
}

resource "google_storage_bucket_iam_member" "restore_audit_create" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.restore.email}"
}

resource "google_storage_bucket_iam_member" "restore_audit_view" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.restore.email}"
}

resource "google_kms_crypto_key_iam_member" "restore_decrypt" {
  crypto_key_id = google_kms_crypto_key.store.id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_service_account.restore.email}"
}

# ---------------------------------------------------------------------------
# Auditor: read-only on the audit bucket. No store bucket access of
# any kind, and no KMS grant at all - under GCP's additive-only IAM
# model, the absence of any KMS binding for this service account IS the
# complete guarantee that it cannot decrypt anything, anywhere. No
# explicit deny statement is possible or necessary the way
# deploy/aws/iam.tf's DenyAllPHIAccess statement is for its own auditor
# role - GCP simply has no grant to revoke in the first place.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "auditor_audit_view" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.auditor.email}"
}

# ---------------------------------------------------------------------------
# Impersonation for local development - GCP's own direct equivalent of
# AWS's sts:AssumeRole (see var.trusted_principal_members's own
# description in variables.tf for the full mechanics). Each trusted
# member gets roles/iam.serviceAccountTokenCreator on ALL THREE service
# accounts individually - a real, per-role impersonation capability
# Azure's managed-identity model has no equivalent for at all (see
# runbooks/RUNBOOK_AZURE_SETUP.md's own account of that gap), letting a
# developer test each role's own narrower access independently rather
# than needing all three permanently attached to compute just to
# develop against them locally.
#
# Empty trusted_principal_members (the default) means this for_each
# produces zero resources - no impersonation grants exist until an
# operator deliberately provides at least one member, the same
# "no implicit trust anchor" posture variables.tf's own description
# states.
# ---------------------------------------------------------------------------

resource "google_service_account_iam_member" "ingest_impersonation" {
  for_each            = toset(var.trusted_principal_members)
  service_account_id  = google_service_account.ingest.name
  role                = "roles/iam.serviceAccountTokenCreator"
  member              = each.value
}

resource "google_service_account_iam_member" "restore_impersonation" {
  for_each            = toset(var.trusted_principal_members)
  service_account_id  = google_service_account.restore.name
  role                = "roles/iam.serviceAccountTokenCreator"
  member              = each.value
}

resource "google_service_account_iam_member" "auditor_impersonation" {
  for_each            = toset(var.trusted_principal_members)
  service_account_id  = google_service_account.auditor.name
  role                = "roles/iam.serviceAccountTokenCreator"
  member              = each.value
}
# Made by Ryan Gomez & Co. Inc.
