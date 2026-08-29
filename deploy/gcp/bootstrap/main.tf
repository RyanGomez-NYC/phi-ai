# ---------------------------------------------------------------------------
# Terraform state backend bootstrap for GCP.
#
# Chicken-and-egg: the GCS bucket that stores this stack's state must
# itself exist before you can use it as a backend. Run this ONCE, with
# local state, then configure the backend block in ../versions.tf and
# migrate. Mirrors deploy/aws/bootstrap/main.tf and
# deploy/azure/bootstrap/main.tf's identical reasoning and structure.
#
# Unlike AWS (account ID) and Azure (subscription ID needs a random
# suffix to fit inside a 24-character storage account name limit), GCP
# project IDs are themselves already globally unique and GCS bucket
# names allow up to 222 characters (63 without dots) - so
# "${var.gcp_project}-tfstate" is guaranteed unique with no random
# suffix needed, and carries no product name at all, simpler than either
# of the other two bootstraps.
#
# Keep this stack's own state file (terraform.tfstate in this directory)
# somewhere safe - it is small and rarely changes, but losing it means
# losing Terraform's record of the state bucket itself.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.gcp_project
  region  = var.gcp_region
}

variable "gcp_project" {
  description = "GCP project ID. Must already exist - this bootstrap does not create the project itself, only resources within it."
  type        = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1" # one of the 3 Always Free-eligible regions - see storage.tf's own cost section for why this choice matters beyond just picking a default
}

# GCP requires each API be explicitly enabled per-project before use -
# unlike AWS/Azure, where services are generally available to an
# account/subscription without this extra step. storage.googleapis.com
# is the only one bootstrap itself needs; the main stack's versions.tf
# enables the rest (KMS, IAM) that it additionally requires.
resource "google_project_service" "storage" {
  project            = var.gcp_project
  service            = "storage.googleapis.com"
  disable_on_destroy = false # disabling the API on destroy could break OTHER things in this project that also depend on GCS - this bootstrap should only ever add, never remove, a project-wide capability
}

resource "google_storage_bucket" "tfstate" {
  name     = "${var.gcp_project}-tfstate"
  location = var.gcp_region
  project  = var.gcp_project

  storage_class = "STANDARD"

  # State is not PHI, but it maps the whole deployment - same reasoning as
  # deploy/aws/bootstrap/main.tf's aws_s3_bucket.tfstate and
  # deploy/azure/bootstrap/main.tf's azurerm_storage_account.tfstate.
  uniform_bucket_level_access = true # IAM-only access control, no legacy ACLs - matches this project's convention of avoiding ACL-based access everywhere else
  public_access_prevention    = "enforced"

  versioning {
    enabled = true # state file history - lets you recover from a bad apply corrupting state, the GCS-side equivalent of the same protection AWS/Azure bootstrap buckets already have
  }

  # TLS is not a configurable option here the way it is for
  # aws_s3_bucket/azurerm_storage_account - GCS enforces HTTPS for all
  # API access unconditionally, with no plaintext-HTTP code path to
  # separately deny. Nothing to configure to get the AWS/Azure
  # bootstrap's DenyInsecureTransport-equivalent protection; it is
  # already the only way in.

  depends_on = [google_project_service.storage]
}

output "state_bucket_name" {
  value       = google_storage_bucket.tfstate.name
  description = "Set as `bucket` in the backend block in ../versions.tf / backend.hcl."
}
# Made by Ryan Gomez & Co. Inc.
