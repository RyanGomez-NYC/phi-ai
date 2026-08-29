# ---------------------------------------------------------------------------
# Cloud SQL: the GCP equivalent of deploy/aws/rds.tf's RDS instance and
# deploy/azure/database.tf's Flexible Server. Backs BOTH the lightweight
# stored-resource index (core/db/schema.sql) and, once
# core/db/omop_bootstrap_gcp.sql is run, the OMOP CDM analytics layer
# (core/db/omop_schema.sql) - one instance, two schemas, matching how
# AWS and Azure both share a single database instance across both
# features.
#
# See core/db/connection.py's _connect_gcp() docstring for the
# application-side mechanism this enables: the Cloud SQL Python
# Connector, using automatic IAM database authentication - no password,
# ever, for either role, matching every other credential in this
# project.
#
# A GENUINE GCP-SPECIFIC DESIGN CONSTRAINT, stated plainly rather than
# worked around silently: unlike AWS (where one IAM role can freely
# authenticate as multiple, independently-named Postgres dbusers - see
# deploy/aws/iam.tf's ConnectToIndexDatabase and ConnectToOmopDatabase
# statements, both scoped to the SAME ingest role) or Azure (where
# pgaadauth_create_principal_with_oid lets a Postgres role name be
# freely chosen), Cloud SQL's IAM database authentication ties the
# Postgres role name DIRECTLY to the authenticating identity's own
# email - one service account maps to exactly one Postgres role name,
# never several. A second, dedicated omop_etl service account would
# also make local development via impersonation
# (runbooks/RUNBOOK_GCP_SETUP.md's own `--impersonate-service-account`
# pattern) impossible for a single scheduler.py process, since
# Application Default Credentials can only impersonate one identity at
# a time - core/fhir/scheduler.py opens both the index and OMOP
# connections from the same process. Given that constraint, this stack
# makes a deliberate, documented choice: the SAME ingest service
# account (identities.tf) is granted BOTH the lightweight-index
# privileges AND the OMOP privileges, on its own single, IAM-derived
# Postgres role. Genuine role separation between "index writer" and
# "OMOP writer" is an AWS/Azure-specific property this stack does not
# replicate on GCP - not an oversight, a real difference in what each
# cloud's own IAM-database-auth model makes practical. Practical
# consequence for the DB ingest username and the OMOP ETL username on
# GCP specifically: set them to the SAME value (ingest_db_iam_user
# output below) - see runbooks/RUNBOOK_OMOP_SETUP.md's GCP note, once
# added, for the exact .env wiring.
#
# THE SAME CONSTRAINT IS WHY THIS DIRECTORY HAS NO phi_ai_ingest /
# phi_ai_reader ROLE NAMES. On AWS those are free-form strings created
# by core/db/bootstrap_aws.sql and named explicitly in iam.tf's
# rds-db:connect ARNs; on Azure they are chosen freely via
# pgaadauth_create_principal_with_oid. Here the role name is whatever
# Cloud SQL derives from the service account email, so it follows
# var.name_prefix and cannot be set independently. The DATABASE name
# below is free-form and does match the other clouds.
#
# Gated on var.enable_db, matching deploy/aws/rds.tf's identical
# optionality - a deployment storing to object storage without any
# secondary index (Postgres or OMOP) is a fully supported configuration
# on every cloud this project targets.
# ---------------------------------------------------------------------------

resource "google_sql_database_instance" "index" {
  count = var.enable_db ? 1 : 0

  name             = "${var.name_prefix}-index"
  database_version = "POSTGRES_16"
  region           = var.gcp_region
  project          = var.gcp_project

  settings {
    # Cheapest tier confirmed during this stack's own research
    # (~$7-10/month) - genuinely NOT free the way this stack's storage
    # and (mostly) KMS costs are; see the cost note at the bottom of
    # this file. Requires edition = "ENTERPRISE" explicitly: shared-core
    # tiers like this one are invalid under the provider's default
    # ENTERPRISE_PLUS edition, confirmed during this stack's own
    # research (attempting db-f1-micro under ENTERPRISE_PLUS fails at
    # apply time with an explicit error naming this exact requirement).
    tier              = var.db_tier
    edition           = "ENTERPRISE"
    availability_type = "ZONAL" # single-zone - matches this whole stack's free/near-free posture; REGIONAL (HA) roughly doubles compute cost

    ip_configuration {
      ipv4_enabled = var.db_publicly_accessible

      dynamic "authorized_networks" {
        for_each = var.db_allowed_cidr_blocks
        content {
          name  = "allowed-${authorized_networks.key}"
          value = authorized_networks.value
        }
      }
    }

    # The setting that makes everything in core/db/connection.py's
    # _connect_gcp() possible at all - without this flag, the instance
    # rejects IAM-authenticated connections outright, independent of
    # any IAM grant this file or identities.tf provisions.
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    backup_configuration {
      enabled = true
    }
  }

  # Same reasoning as deploy/aws/variables.tf's db_deletion_protection -
  # blocked ON in dev (so the stack stays genuinely tear-down-able) and
  # required true outside dev, enforced by the precondition below.
  deletion_protection = var.environment != "dev"

  depends_on = [google_project_service.sqladmin]

  lifecycle {
    precondition {
      condition     = var.environment == "dev" || var.db_deletion_protection
      error_message = "db_deletion_protection must be true outside dev - see this variable's own description in variables.tf."
    }
  }
}

resource "google_sql_database" "index" {
  count = var.enable_db ? 1 : 0

  # A Postgres DATABASE name, matching deploy/aws/rds.tf's db_name and
  # deploy/azure/database.tf's google/azurerm equivalent. Three consumers
  # have to agree on it: core/db/bootstrap_gcp.sql and
  # core/db/omop_bootstrap_gcp.sql connect to this database by name, and
  # the application reads it from .env. outputs.tf's env_fragment emits
  # PHI_AI_DB_NAME from this resource's own attribute rather than
  # repeating the literal, so the fragment cannot drift from what is
  # actually created here.
  name     = "phi_ai_index"
  instance = google_sql_database_instance.index[0].name
  project  = var.gcp_project
}

# ---------------------------------------------------------------------------
# IAM database users - CLOUD_IAM_SERVICE_ACCOUNT type, created here via
# Terraform rather than SQL. core/db/bootstrap_gcp.sql and
# core/db/omop_bootstrap_gcp.sql both only GRANT privileges on these
# roles once they exist - see those files' own headers for the full
# account of why Cloud SQL's IAM auth model creates the role
# differently from AWS/Azure's CREATE ROLE-based bootstrap scripts.
#
# The role name Cloud SQL derives is the service account's own email
# with the trailing .gserviceaccount.com stripped - not a freely-chosen
# string (see this file's own header). trimsuffix() computes that
# exact, correctly-formatted value here rather than requiring an
# operator to derive it by hand.
# ---------------------------------------------------------------------------

resource "google_sql_user" "ingest" {
  count = var.enable_db ? 1 : 0

  name     = trimsuffix(google_service_account.ingest.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.index[0].name
  project  = var.gcp_project
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "restore" {
  count = var.enable_db ? 1 : 0

  name     = trimsuffix(google_service_account.restore.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.index[0].name
  project  = var.gcp_project
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# ---------------------------------------------------------------------------
# roles/cloudsql.client - required for the Cloud SQL Python Connector to
# establish its encrypted tunnel at all (confirmed from the connector
# library's own documentation - see core/db/connection.py's
# _connect_gcp() docstring). Grants only the ABILITY to connect; what
# each role can do once connected is a Postgres-side GRANT
# (core/db/bootstrap_gcp.sql, core/db/omop_bootstrap_gcp.sql), not an
# IAM concern - the same "IAM only governs authentication" split
# deploy/aws/iam.tf's own rds-db:connect statements already establish.
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "ingest_cloudsql_client" {
  count = var.enable_db ? 1 : 0

  project = var.gcp_project
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_project_iam_member" "restore_cloudsql_client" {
  count = var.enable_db ? 1 : 0

  project = var.gcp_project
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.restore.email}"
}

# ---------------------------------------------------------------------------
# On cost: what's actually confirmed here, stated plainly
#
# UNLIKE this stack's storage (genuinely, indefinitely free within
# Cloud Storage's Always Free tier - see storage.tf's own cost section)
# and unlike Cloud KMS's roughly $0.06/month floor, Cloud SQL has NO
# comparable ongoing free tier. Confirmed against
# cloud.google.com/sql/pricing and G2-aggregated pricing research
# during this stack's own work: Cloud SQL's only free offering is a
# general-purpose $300/90-day new-customer credit that applies to ALL
# GCP services, not a database-specific allowance - once that credit is
# exhausted or expires, on-demand billing starts immediately, with no
# warning. The cheapest tier (db-f1-micro, this file's default) runs
# roughly $7-10/month depending on region, plus separate storage
# billing - budget for this explicitly rather than assuming it fits
# alongside the rest of this stack's free/near-free posture.
# ---------------------------------------------------------------------------
# Made by Ryan Gomez & Co. Inc.
