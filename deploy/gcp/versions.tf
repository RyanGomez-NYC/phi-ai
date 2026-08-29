terraform {
  required_version = ">= 1.10.0" # matches deploy/aws and deploy/azure - no GCP-specific reason to differ

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0" # v6.x - google_storage_bucket's and google_kms_crypto_key's schemas are confirmed current
      # against the Terraform Registry docs as of this writing. This note used to cite
      # google_storage_bucket's enable_object_retention and retention_policy attributes
      # specifically; storage.tf sets neither any more - see its header for why. Re-check
      # the provider's own changelog before bumping past 6.x, the same discipline
      # deploy/aws/versions.tf and deploy/azure/versions.tf apply to their own providers.
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11" # used by kms.tf's time_sleep, to absorb IAM propagation delay before the GCS service agent's new Cloud KMS role grant is used by the storage buckets attempting to encrypt with this key - the same class of eventual-consistency gap deploy/azure/keyvault.tf's time_sleep exists to absorb
    }
  }

  # Remote state backend. See deploy/gcp/bootstrap/ for the one-time setup this
  # references - provision that FIRST. Mirrors deploy/aws/versions.tf and
  # deploy/azure/versions.tf's identical chicken-and-egg reasoning: the bucket
  # holding this stack's state has to exist before Terraform can use it as a
  # backend.
  #
  # This block is intentionally active (not commented out) with only `prefix`
  # set - a standard Terraform "partial backend configuration," the same
  # pattern deploy/azure/versions.tf uses. The one remaining required
  # argument (bucket) comes from deploy/gcp/backend.hcl - copy
  # backend.hcl.example, fill it in with bootstrap's own output; it's
  # gitignored so that value (which embeds your GCP project ID) never
  # reaches this public repo. Init with:
  #   terraform init -backend-config=backend.hcl
  #
  # Simpler than AWS's S3 backend (bucket + a DynamoDB-free lockfile) and
  # Azure's azurerm backend (three separate values) - the GCS backend needs
  # only a bucket name, and handles locking natively via GCS's own
  # object-generation preconditions, no separate lock resource required.
  backend "gcs" {
    # The object-name prefix Terraform's own state lives under inside the
    # state bucket. Set it before the first `init` and then leave it:
    # changing it later does not move the state objects, it points
    # Terraform at a prefix that holds nothing, so the next `init`/`plan`
    # reads an EMPTY state.
    prefix = "phi-ai/gcp"
    # bucket goes in backend.hcl
  }
}

provider "google" {
  project = var.gcp_project
  region  = var.gcp_region
}

# GCP requires each API be explicitly enabled per-project before its
# resources can be created - unlike AWS/Azure, where services are
# generally available to an account/subscription without this extra
# step. All APIs this stack actually uses are enabled here, once,
# rather than scattered as individual depends_on targets throughout the
# other files.
resource "google_project_service" "storage" {
  project            = var.gcp_project
  service            = "storage.googleapis.com"
  disable_on_destroy = false # disabling a project-wide API on destroy could break other things in this project that also depend on it - this stack should only ever add, never remove, a project capability
}

resource "google_project_service" "kms" {
  project            = var.gcp_project
  service            = "cloudkms.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  project            = var.gcp_project
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

# Added alongside deploy/gcp/database.tf - required for
# google_sql_database_instance/google_sql_database/google_sql_user and
# for the Cloud SQL Python Connector (core/db/connection.py's
# _connect_gcp()) to reach the instance at all.
resource "google_project_service" "sqladmin" {
  project            = var.gcp_project
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

data "google_project" "current" {
  project_id = var.gcp_project
}
# Made by Ryan Gomez & Co. Inc.
