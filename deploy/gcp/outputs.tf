output "store_bucket_name" {
  value       = google_storage_bucket.store.name
  description = "Set as the storage bucket setting - core/storage/factory.py's build_storage() passes this as the `bucket` argument to GCSStorage."
}

output "audit_bucket_name" {
  value       = google_storage_bucket.audit.name
  description = "Set as the audit bucket setting - core/audit/sink.py's GCSAuditSink takes this as its `bucket` argument."
}

output "kms_key_id" {
  value       = google_kms_crypto_key.store.id
  description = "Set as BOTH the store and audit KMS key settings - store and audit share one Cloud KMS key in this installment. See storage.tf's audit bucket comment for the deliberate reasoning."
}

output "ingest_service_account_email" {
  description = "Full email of the ingest service account. Attach this to the compute resource actually running scheduler.py/bulk_scheduler.py in a real deployment (Cloud Run/Compute Engine's own service-account-attachment mechanism), or grant a trusted principal impersonation access to it for local dev - see runbooks/RUNBOOK_GCP_SETUP.md."
  value       = google_service_account.ingest.email
}

output "restore_service_account_email" {
  description = "Full email of the restore service account. See ingest_service_account_email's note - the same attach-to-compute-or-impersonate pattern applies."
  value       = google_service_account.restore.email
}

output "auditor_service_account_email" {
  description = "Full email of the auditor service account. See ingest_service_account_email's note."
  value       = google_service_account.auditor.email
}

# ---------------------------------------------------------------------------
# Cloud SQL outputs - only meaningful (and only emitted) when
# var.enable_db is true. instance_connection_name feeds directly into
# the Cloud SQL instance connection name setting - see
# core/db/connection.py's _connect_gcp(). ingest_db_iam_user is the
# exact, correctly-quoted role name core/db/bootstrap_gcp.sql's
# {INGEST_IAM_USER} placeholder needs - see database.tf's own header
# for why the SAME value also covers OMOP on GCP specifically, unlike
# AWS/Azure where the two roles are genuinely distinct.
# ---------------------------------------------------------------------------

output "instance_connection_name" {
  description = "Set as the GCP Cloud SQL instance connection name - the Cloud SQL Python Connector's own addressing format (project:region:instance), not a host:port pair. Empty unless enable_db is true."
  value       = var.enable_db ? google_sql_database_instance.index[0].connection_name : null
}

output "ingest_db_iam_user" {
  description = <<-EOT
    Set as BOTH the DB ingest username and the OMOP ETL username on GCP
    specifically - see database.tf's own header for why the same ingest
    service account (and so the same derived Postgres role) covers both
    the lightweight index and OMOP here, unlike AWS/Azure's genuinely
    separate roles. Already double-quoted as a Postgres identifier (the
    value contains "@" and "."), ready to paste directly into
    core/db/bootstrap_gcp.sql's and core/db/omop_bootstrap_gcp.sql's
    {INGEST_IAM_USER} placeholders. Empty unless enable_db is true.
  EOT
  value       = var.enable_db ? "\"${trimsuffix(google_service_account.ingest.email, ".gserviceaccount.com")}\"" : null
}

output "reader_db_iam_user" {
  description = "Set as the DB reader username. Already double-quoted, ready to paste into core/db/bootstrap_gcp.sql's {READER_IAM_USER} placeholder. Empty unless enable_db is true."
  value       = var.enable_db ? "\"${trimsuffix(google_service_account.restore.email, ".gserviceaccount.com")}\"" : null
}

# Convenience: emit the .env fragment so you don't hand-copy values
# across a dozen separate outputs. Mirrors deploy/aws/outputs.tf and
# deploy/azure/outputs.tf's env_fragment exactly in purpose and
# structure.
#
# Contains no secrets - authentication is via Application Default
# Credentials (a gcloud user login or an attached/impersonated service
# account), never a stored key file, matching this project's Epic-side
# avoidance of shared secrets (RS384 JWT, not a client secret) and the
# AWS/Azure sides' identical avoidance of stored credentials in .env.
#
# The database lines only appear when enable_db is true - an operator
# who left the database disabled gets a fragment with no dangling
# references to outputs that don't exist, the same conditional-inclusion
# pattern deploy/aws/outputs.tf's own env_fragment already uses for its
# database section.
#
# ON THE DB USERNAME VALUES: on GCP these are the ingest/restore service
# account emails with ".gserviceaccount.com" trimmed, because Cloud SQL
# IAM auth ties one identity to exactly one Postgres role name - which
# is also why the same value is emitted for both DB_INGEST_USERNAME and
# OMOP_ETL_USERNAME here, and why AWS and Azure can separate those two
# roles while GCP cannot. PHI_AI_DB_NAME comes from
# google_sql_database.index[0].name rather than a repeated literal, so
# it cannot drift from database.tf.
output "env_fragment" {
  description = "Paste into .env, then run install/installer_chatbot.py to add FHIR credentials. See runbooks/RUNBOOK_GCP_SETUP.md for the full walkthrough, including the impersonation/attachment step this fragment alone does not cover."
  value       = <<-EOT
    PHI_AI_CLOUD_PROVIDER=gcp
    PHI_AI_STORAGE_BUCKET=${google_storage_bucket.store.name}
    PHI_AI_STORAGE_REGION=${var.gcp_region}
    PHI_AI_KMS_KEY_ID=${google_kms_crypto_key.store.id}
    PHI_AI_AUDIT_BUCKET=${google_storage_bucket.audit.name}
    PHI_AI_AUDIT_KMS_KEY_ID=${google_kms_crypto_key.store.id}
    PHI_AI_GCP_PROJECT=${var.gcp_project}
    %{if var.enable_db~}
    PHI_AI_GCP_CLOUD_SQL_INSTANCE_CONNECTION_NAME=${google_sql_database_instance.index[0].connection_name}
    PHI_AI_DB_NAME=${google_sql_database.index[0].name}
    PHI_AI_DB_INGEST_USERNAME=${trimsuffix(google_service_account.ingest.email, ".gserviceaccount.com")}
    PHI_AI_DB_READER_USERNAME=${trimsuffix(google_service_account.restore.email, ".gserviceaccount.com")}
    PHI_AI_OMOP_ETL_USERNAME=${trimsuffix(google_service_account.ingest.email, ".gserviceaccount.com")}
    %{endif~}
  EOT
}
# Made by Ryan Gomez & Co. Inc.
