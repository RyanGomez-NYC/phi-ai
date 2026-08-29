variable "gcp_project" {
  description = "GCP project ID. Must already exist and have billing enabled - this stack does not create the project itself, only resources within it."
  type        = string
}

variable "gcp_region" {
  description = <<-EOT
    GCP region for all resources. Defaults to us-central1 deliberately -
    Cloud Storage's Always Free 5 GB-month allowance only applies to
    US-WEST1, US-CENTRAL1, and US-EAST1, aggregated across all three
    (confirmed against cloud.google.com/storage/pricing directly, not a
    third-party summary). Using any other region does not disable this
    stack, but does forfeit that specific free allowance - see
    storage.tf's own cost section for the fuller picture, including what
    is NOT free regardless of region (Cloud KMS key storage).
  EOT
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = <<-EOT
    Deployment environment. Only affects the defaults for a small number
    of dev-convenience settings - does NOT force phi_retention_days
    to any particular value in prod/staging, and no longer selects an
    immutability strength, because this stack provisions no bucket
    retention policy in any environment. Retention stays independently
    deployer-configurable everywhere; mirrors deploy/aws/variables.tf and
    deploy/azure/variables.tf's identical reasoning exactly.
  EOT
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for all resource names. GCS bucket names must be globally
    unique, so the project ID is folded in automatically (see
    storage.tf) - unlike Azure, this has no tight length ceiling to
    worry about (GCS allows up to 63 characters for a name without
    dots).

    CHOOSE THIS BEFORE YOUR FIRST APPLY AND THEN LEAVE IT ALONE. It is
    not branding: it feeds the GCS bucket names (PHI store and audit),
    the Cloud KMS KEY RING name, the crypto key name, the Cloud SQL
    instance name, and the three service account IDs. No provider can
    rename any of those in place.

    TWO REASONS THIS MATTERS MORE ON GCP THAN ON AWS OR AZURE - do not
    assume the pattern from another cloud carries over:

      1. A Cloud KMS KEY RING CAN NEVER BE DELETED. Not scheduled for
         deletion, not destroyed, not ever - unlike an AWS KMS key or an
         Azure Key Vault key, both of which support delayed deletion.
         Whatever prefix the ring is first created under occupies that
         name permanently, and `terraform destroy` will not reclaim it.

      2. The service account IDs feed the service account EMAILS, and
         Cloud SQL IAM database authentication derives the Postgres ROLE
         name DIRECTLY from that email - one identity maps to exactly
         one Postgres role name, never several, and the name is not
         freely chosen (see database.tf's header). So this prefix also
         fixes the database role names core/db/bootstrap_gcp.sql grants
         on.
  EOT
  type        = string
  default     = "phiai"
}

# ---------------------------------------------------------------------------
# Retention
#
# CONFIGURATION, NOT ENFORCEMENT. No bucket retention policy and no
# Bucket Lock are provisioned - see storage.tf. These values are recorded
# as object metadata by core/storage/gcp_gcs.py and drive documented
# disposition; nothing in GCS stops an early delete or performs a late
# one.
# ---------------------------------------------------------------------------

variable "phi_retention_days" {
  description = <<-EOT
    Intended retention period, in days, for stored PHI. NOT ENFORCED -
    recorded as object metadata only; this stack provisions neither the
    bucket-wide retention_policy floor nor per-object Object Retention
    Lock that previously backed it. Fully deployer-configurable in every
    environment, including prod - same reasoning as
    deploy/aws/variables.tf's phi_retention_days and
    deploy/azure/variables.tf's identical variable: US state medical
    record retention statutes vary genuinely widely (commonly 5-10+
    years for adults; Florida's physician-record minimum is specifically
    5 years), and no hardcoded floor here is defensible as "the
    compliant number" across all 50 states. Set this from YOUR state's
    actual applicable requirement - see docs/COMPLIANCE.md.
  EOT
  type        = number
  default     = 1 # dev-safe default; MUST be set explicitly for any real deployment
}

variable "audit_retention_days" {
  description = <<-EOT
    Intended retention period, in days, for the audit bucket. NOT
    ENFORCED, same as phi_retention_days above. Separately
    configurable from
    phi_retention_days above, matching deploy/aws and
    deploy/azure's identical store/audit retention split. Audit
    retention commonly needs to be LONGER than clinical-record
    retention, not shorter: 45 CFR 164.316(b)(2)(i) requires
    HIPAA-required documentation (which the audit trail is part of) be
    retained 6 years, independent of whatever your state's
    clinical-record retention period is - core/audit/sink.py's
    S3AuditSink defaults to 2192 days (6 years) for exactly this reason
    when no explicit figure is provided elsewhere. Set this deliberately
    rather than assuming it should match phi_retention_days.
  EOT
  type        = number
  default     = 1 # dev-safe default; MUST be set explicitly for any real deployment
}

# NOTE: variable "lock_immutability_policy" used to live here, selecting
# whether the bucket retention policy was locked (GCS Bucket Lock, the
# rough equivalent of AWS Object Lock COMPLIANCE). It is gone along with
# the retention policy itself - see storage.tf. Retention on GCP is now a
# recorded configuration value, not a bucket-enforced control.

variable "trusted_principal_members" {
  description = <<-EOT
    IAM members allowed to impersonate the PHI AI Platform service
    accounts (identities.tf) via roles/iam.serviceAccountTokenCreator -
    GCP's own direct equivalent of AWS's sts:AssumeRole, letting an
    authorized caller borrow a service account's credentials temporarily
    rather than needing that identity permanently attached to compute.
    Unlike Azure, which has no equivalent mechanism at all (see
    runbooks/RUNBOOK_AZURE_SETUP.md's own account of that gap), this is
    a real, first-class GCP capability - see identities.tf's own module
    comment for the full mechanics.

    Each entry must be a fully-qualified IAM member string, e.g.
    "user:you@example.com" for a human, or
    "serviceAccount:other-sa@project.iam.gserviceaccount.com" for
    another service account - not a bare email address. For a dev
    deployment this is typically your own user account:
      echo "user:$(gcloud config get-value account)"

    Left empty by default - an empty list here means no impersonation
    grants happen until you provide one, the same "no implicit trust
    anchor" posture as deploy/aws/variables.tf's trusted_principal_arns
    and deploy/azure/variables.tf's trusted_principal_object_ids.
  EOT
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Cloud KMS
# ---------------------------------------------------------------------------

variable "key_rotation_period_seconds" {
  description = <<-EOT
    Automatic rotation period for the PHI store key, in seconds
    (Terraform's duration-string format, e.g. "7776000s" for 90 days).
    Set to null to disable automatic rotation entirely.

    Cost note: unlike AWS KMS (a flat $1/month per key regardless of
    rotation) and closer in spirit to Azure Key Vault, Cloud KMS bills
    per ACTIVE key version - confirmed against cloud.google.com/kms/pricing
    directly: roughly $0.06/month per active symmetric key version at
    the lowest published tier (Google states this as an hourly rate,
    $0.000082192/hour, which the deployer should re-verify at
    cloud.google.com/kms/pricing before budgeting a real deployment
    rather than trusting this figure to stay current). Each rotation
    creates a new active version; old versions remain active (and
    billed) until explicitly destroyed. More frequent rotation costs
    more, proportionally - unlike AWS, where rotation cost was capped at
    $3/key/month after two retained rotations.
  EOT
  type        = string
  default     = "7776000s" # 90 days
  nullable    = true
}

variable "key_destroy_scheduled_duration_seconds" {
  description = <<-EOT
    How long a Cloud KMS key version stays in "scheduled for
    destruction" state before being permanently destroyed - the GCP
    equivalent of Azure Key Vault's soft-delete retention window
    (deploy/azure/keyvault.tf's soft_delete_retention_days) and, in
    spirit, of not immediately honoring an AWS KMS ScheduleKeyDeletion
    call. Google's own default is 24 hours (86400 seconds) if this is
    left unset; set explicitly here for the same reason this project
    sets other defaults explicitly rather than relying on an unstated
    provider default, even a reasonable one.

    NOTE this window applies to a key VERSION only. The key ring itself
    is never deletable at all - see kms.tf's header.
  EOT
  type        = number
  default     = 86400 # 24 hours - Google's own default, stated explicitly rather than relied upon implicitly
}

# ---------------------------------------------------------------------------
# Cloud SQL (optional secondary index + OMOP analytics layer)
# ---------------------------------------------------------------------------

variable "enable_db" {
  description = <<-EOT
    Whether to provision the Cloud SQL instance backing the lightweight
    Postgres index (core/db/schema.sql) and, optionally, the OMOP CDM
    analytics layer (core/db/omop_schema.sql) - see database.tf.
    Matches deploy/aws/variables.tf's identical enable_db toggle: a
    deployment storing to Cloud Storage without any secondary index
    is fully supported. UNLIKE the storage/KMS resources this stack
    otherwise targets at free/near-free cost, Cloud SQL has no
    comparable ongoing free tier - see database.tf's own cost section
    before enabling this for anything beyond brief testing.
  EOT
  type        = bool
  default     = false
}

variable "db_tier" {
  description = <<-EOT
    Cloud SQL machine tier. Defaults to the cheapest available
    (db-f1-micro, a shared-core tier, ~$7-10/month - see database.tf's
    own cost section) - not covered by Cloud SQL's standard SLA and not
    eligible for committed-use discounts, acceptable tradeoffs for a
    dev deployment but worth reconsidering for a real deployment's own
    availability requirements.
  EOT
  type        = string
  default     = "db-f1-micro"
}

variable "db_publicly_accessible" {
  description = <<-EOT
    Whether the Cloud SQL instance has a public IPv4 address at all.
    Mirrors deploy/aws/variables.tf's db_publicly_accessible - set true
    only to connect directly from a laptop rather than from compute
    already inside the same VPC, and combine with db_allowed_cidr_blocks
    below to restrict which addresses can even attempt a connection.
    False by default: no public IP, no network path to attempt a
    connection from outside GCP at all, regardless of IAM.
  EOT
  type        = bool
  default     = false
}

variable "db_allowed_cidr_blocks" {
  description = <<-EOT
    CIDR blocks allowed to connect to the Cloud SQL instance's public IP,
    when db_publicly_accessible is true. Mirrors
    deploy/aws/variables.tf's db_allowed_cidr_blocks exactly - empty by
    default (no network access at all), the same "nothing implicitly
    trusted" posture this project applies consistently. To connect from
    your own machine:
      curl -s https://checkip.amazonaws.com
    then set this to ["<that-ip>/32"] - a single-address CIDR, not an
    open range.
  EOT
  type        = list(string)
  default     = []
}

variable "db_deletion_protection" {
  description = <<-EOT
    Whether Cloud SQL refuses to delete the instance via
    terraform destroy (or the API directly) without this being
    explicitly turned off first. Blocked ON (required true) outside dev
    by database.tf's own precondition - mirrors
    deploy/aws/variables.tf's db_deletion_protection exactly. Default
    false specifically so a dev stack stays genuinely tear-down-able
    without an extra manual step.
  EOT
  type        = bool
  default     = false
}
# Made by Ryan Gomez & Co. Inc.
