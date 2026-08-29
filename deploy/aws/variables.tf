variable "aws_region" {
  description = "AWS region for all resources. Must be a US region for most US healthcare data residency expectations."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = <<-EOT
    Deployment environment. Tags resources and gates a couple of
    dev-convenience defaults. It does NOT force phi_retention_days to
    any particular value, and no longer selects an immutability strength,
    because this stack provisions no storage-layer immutability in any
    environment. Retention stays independently deployer-configurable
    everywhere; see its description below.
  EOT
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for all resource names. Bucket names are globally unique, so
    the account ID is appended automatically.

    CHOOSE THIS BEFORE YOUR FIRST APPLY AND THEN LEAVE IT ALONE. It is
    not branding: it feeds physical cloud identifiers - the S3 bucket
    names (PHI store, audit log, CloudTrail, psychotherapy notes), the
    KMS alias, the RDS instance identifier and its subnet group and
    security group, the CloudTrail trail name, and the IAM role names.
    No provider can rename any of those in place, so once a stack has
    been applied, changing this value means destroying and recreating
    the resources it names.
  EOT
  type        = string
  default     = "phi-ai"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric with hyphens (S3 bucket naming), 3-32 characters, not starting or ending with a hyphen."
  }
}

# ---------------------------------------------------------------------------
# Retention
#
# CONFIGURATION, NOT ENFORCEMENT. No S3 Object Lock is provisioned on any
# bucket in this stack - see s3_store.tf / s3_audit.tf /
# s3_psychotherapy.tf. The values below are recorded as object metadata by
# core/storage/aws_s3.py and drive documented disposition
# (runbooks/RUNBOOK_DISPOSITION.md); nothing in S3 stops an early delete
# or performs a late one.
# ---------------------------------------------------------------------------

# NOTE: variable "object_lock_mode" used to live here. It is gone, along
# with S3 Object Lock itself. Retention in this stack is now a recorded
# configuration value rather than a storage-enforced control - see the
# bucket definitions in s3_store.tf / s3_audit.tf / s3_psychotherapy.tf
# and docs/COMPLIANCE.md's "Retention and integrity" section. There is no
# GOVERNANCE/COMPLIANCE choice to make anymore, in any environment.
#
# KNOWN GAP, and it is a genuine one-way door: S3 Object Lock cannot be
# added to a bucket after creation, by any provider. Every bucket in this
# stack is created without it, so adopting an enforced retention control
# later is a new bucket plus a full object migration - `terraform apply`
# cannot get there from here. core/healthcheck.py additionally fails a
# stack that reports a live default retention rule, so a lock this
# codebase did not create surfaces as a finding rather than as silent
# reassurance.

variable "phi_retention_days" {
  description = <<-EOT
    Intended retention for stored PHI, in days. NOT ENFORCED - there is
    no Object Lock behind this number. It is recorded as object metadata
    and drives documented disposition (runbooks/RUNBOOK_DISPOSITION.md);
    it neither prevents an early delete nor causes a later one. Fully
    deployer-configurable, with no hardcoded legal-minimum floor beyond
    catching the unedited dev default (see the precondition on
    aws_s3_bucket.store in s3_store.tf) - see docs/COMPLIANCE.md.

    US state medical record retention statutes vary genuinely widely -
    commonly cited range: 5-10+ years for adults, often longer for
    minors ("until age of majority plus N years" in several states).
    Florida's physician-record retention minimum, for example, is 5
    years (1826 days) - shorter than a 6-year floor this variable used
    to enforce outside dev, which would have blocked a Florida deployer
    from setting their own state's correct minimum. Set this from YOUR
    state's actual applicable requirement, whatever that is - do not
    default to a longer number "to be safe" without confirming it's
    actually required, and do not assume a shorter number must be wrong
    without checking your own state's statute first.

    HOW THIS INTERACTS WITH THE APPLICATION-LEVEL SETTING: the
    application computes a per-object retain-until date at write time
    (PHI_AI_RETENTION_YEARS and, per resource type,
    PHI_AI_RETENTION_YEARS_OVERRIDES - see core/config/settings.py
    and core/fhir/client.py) and core/storage/aws_s3.py writes it as the
    `retain-until` user metadata key. That is a RECORD of intended
    disposition, readable by the disposition tooling - S3 does not
    compare it against this Terraform value, does not take the longer of
    the two, and does not honor either one. This variable used to be
    described as "a floor enforced by S3 itself," resolved against the
    application value; with Object Lock removed that resolution does not
    happen anywhere. Use this Terraform-level value as your documented
    baseline; use the application-level override for any resource type
    that needs a genuinely different recorded retention period than the
    rest of the store.
  EOT
  type        = number
  default     = 1 # dev-safe default; MUST be set explicitly for any real deployment
}

variable "audit_retention_days" {
  description = "Intended retention for the audit log bucket, in days. NOT ENFORCED - recorded as object metadata only. HIPAA 164.316(b)(2)(i) requires 6 years for required documentation; audit logs are commonly held at least that long. Fully deployer-configurable with no hardcoded floor - see phi_retention_days above for why this codebase doesn't enforce a specific number."
  type        = number
  default     = 1 # dev-safe default; raise for real deployments per the guidance above
}

# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

variable "trusted_principal_arns" {
  description = <<-EOT
    IAM principal ARNs allowed to assume the PHI AI Platform roles.

    For a dev store this is typically your own IAM user/role ARN so you
    can assume the roles locally. For a real deployment this should be the
    EC2 instance profile / ECS task role / EKS IRSA role that actually
    runs the service - not a human principal.

    Leave empty to allow only the account root as a trust anchor (you will
    then need to grant sts:AssumeRole explicitly elsewhere).
  EOT
  type        = list(string)
  default     = []
}

variable "enable_cloudtrail" {
  description = <<-EOT
    Create a dedicated CloudTrail capturing S3 data events and KMS usage
    for the store. This is the independent, out-of-band log the
    incident response runbook cross-references against the application
    audit log - without it, out-of-band access to PHI cannot be
    distinguished from application access at all.

    Required outside dev - enforced by the validation below on every
    plan. NOTE: this is a deliberate tightening (2026-08-17 audit, C5):
    previously the only related guard lived as a precondition on the
    count-gated trail resource, so setting this false in prod skipped
    the trail AND every guard about it, silently. If your organization
    genuinely runs its own account-wide trail covering these buckets'
    data events, that is a conscious decision to encode here - relax
    this validation deliberately, in a reviewed change, not by flipping
    the variable.
  EOT
  type        = bool
  default     = true

  validation {
    condition     = var.environment == "dev" || var.enable_cloudtrail
    error_message = "enable_cloudtrail must be true outside dev. CloudTrail is the independent record RUNBOOK_INCIDENT_RESPONSE.md's out-of-band access detection depends on; without it that detection does not exist."
  }
}

variable "enable_admin_order_purge" {
  description = <<-EOT
    Grant the disposition role the delete permissions that
    core/fhir/purge.py's "admin-order" mode needs - an administrator
    removing one or more specific records before their retention date,
    under a stated basis. See that module's own docstring for the full
    design.

    Previously this granted s3:BypassGovernanceRetention. That permission
    only meant anything while Object Lock existed; with the lock gone it
    would have been a no-op, so the grant now covers the delete actions
    themselves, conditioned on the AdminBasis session tag.

    OFF by default, deliberately. Note what that default now protects:
    with no lock in place, this variable is a primary control over
    whether early removal of stored PHI is possible at all, rather
    than a secondary one layered on top of a storage guarantee. Most
    deployments should never need this: the routine, expected path for
    removing PHI is core/fhir/purge.py's "expired" mode, which needs no
    special permission and this variable does not affect. Only enable
    this if your organization has a genuine, anticipated need to remove
    specific records early under a stated administrative basis,
    understood as the deliberate, narrow exception it is.

  EOT
  type        = bool
  default     = false
}

variable "force_destroy_buckets" {
  description = "Allow `terraform destroy` to delete non-empty buckets. Honored ONLY when environment == 'dev' - the store, audit, CloudTrail, and psychotherapy-notes buckets each AND this flag with `environment == \"dev\"`, so it has no effect in any non-dev stack. This matters because Object Lock has been removed on all clouds: in a real deployment `force_destroy` would be the only thing standing between a `destroy` and the PHI store, the psychotherapy notes held under 45 CFR 164.508(a)(2), plus the audit trail recording access to both, so it is refused outside dev regardless of this value."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Cost controls
#
# This stack CANNOT run at $0. AWS KMS customer-managed keys cost $1.00 per
# key per month with no free-tier allowance for key storage, charged whether
# or not the key is used. Because the whole encryption design depends on a
# customer-managed key (an AWS-managed key cannot carry the key policy that
# enforces role separation), $1/month is the hard floor.
#
# The variables below bring everything ELSE to zero or near-zero. See
# docs/COST.md for the full breakdown and the monthly estimate.
# ---------------------------------------------------------------------------

variable "separate_audit_key" {
  description = <<-EOT
    Use a second, dedicated KMS key for the audit log (recommended) or share
    the store key (saves $1-3/month).

    SECURITY TRADE-OFF, stated plainly: with a shared key, any principal able
    to decrypt stored PHI can also decrypt the audit log. The design intent
    of two keys is that compromising store access does not also confer the
    ability to read or rewrite the record of that compromise. Sharing is
    defensible for a dev stack holding synthetic data. It is not defensible
    for real PHI, and the validation below enforces that on every plan.

    FOUND AND FIXED (2026-08-17 audit, C5): this guard previously lived
    as a precondition on the aws_kms_key.audit resource - which has
    count = separate_audit_key ? 1 : 0, so with the variable false there
    were zero instances and the precondition never evaluated, and with
    it true the condition was trivially satisfied. Unfalsifiable in both
    states: a prod apply with separate_audit_key=false went through
    cleanly and handed the long-running ingest service kms:Decrypt on
    the PHI store key (see iam.tf's EncryptAuditRecords warning).
    Variable validation runs on every plan regardless of any resource's
    count, which is why the guard lives here now.
  EOT
  type        = bool
  default     = true

  validation {
    condition     = var.environment == "dev" || var.separate_audit_key
    error_message = "separate_audit_key must be true outside dev. Sharing one key means any principal that can decrypt PHI can also decrypt the audit log recording that access - including the ingest role, which needs audit-key decrypt for the hash chain and would therefore gain decrypt on stored PHI itself."
  }
}

variable "enable_key_rotation" {
  description = <<-EOT
    Annual automatic KMS key rotation.

    Cost note: each retained rotation version adds to the monthly key charge,
    capped at $3/key/month after two rotations. On a dev stack that gets
    destroyed within months this buys nothing, since rotation only matters
    across long key lifetimes.

    Required for any real deployment - the precondition below enforces it
    outside dev.
  EOT
  type        = bool
  default     = true
}

variable "cloudtrail_data_events" {
  description = <<-EOT
    Log S3 object-level (data) events for the store and audit buckets.

    Cost: $0.10 per 100,000 events, billed from the FIRST event - unlike
    management events, there is no free first copy. Every PutObject and
    GetObject on the store counts.

    This is the independent, out-of-band record that
    RUNBOOK_INCIDENT_RESPONSE.md cross-references against the application
    audit log to detect access that bypassed the application. Turning it off
    means losing the ability to make that inference, so this should be ON for
    anything holding real PHI. For a dev stack the application audit log
    alone is usually enough to develop against.

    Required outside dev - enforced by the validation below on every
    plan. FOUND AND FIXED (2026-08-17 audit, C5): this requirement
    previously lived as a precondition on the count-gated
    aws_cloudtrail.main resource, so a prod apply with
    enable_cloudtrail=false skipped the trail and this guard with it,
    silently - the same dead-guard shape as separate_audit_key above.
  EOT
  type        = bool
  default     = true

  validation {
    condition     = var.environment == "dev" || var.cloudtrail_data_events
    error_message = "cloudtrail_data_events must be true outside dev. Without S3 data events there is no independent record of object-level access, so a read that bypassed the application cannot be distinguished from one that did not - the core detection the incident response runbook relies on."
  }
}

variable "enable_lifecycle_transitions" {
  description = <<-EOT
    Transition stored objects to STANDARD_IA (90d) and GLACIER_IR (365d).

    OFF BY DEFAULT, and this is a cost decision rather than a free-tier one.
    Both storage classes bill a 128 KB minimum per object regardless of actual
    size. Individual FHIR resources are typically 1-10 KB, so transitioning
    them bills each at 128 KB - often 15-100x the real size - and adds a
    per-object transition request charge on top. For small-object deployments
    the transition costs MORE than leaving everything in S3 Standard.

    Turn this on only if you are storing large objects (DocumentReference
    attachments, imaging, scanned PDFs), or after aggregating small resources
    into larger bundles.
  EOT
  type        = bool
  default     = false
}

variable "monthly_budget_usd" {
  description = "Create an AWS Budget alerting at 50/80/100% of this monthly spend. Set to 0 to skip. AWS provides two budgets at no charge."
  type        = number
  default     = 5
}

variable "budget_alert_email" {
  description = "Email address for budget alerts. Required if monthly_budget_usd > 0. AWS sends a subscription confirmation you must accept."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Postgres index (optional)
#
# S3 remains the system of record for stored PHI, and is authoritative
# over this database in any disagreement - the index is a queryable,
# REBUILDABLE secondary copy containing structural metadata only (resource
# type, S3 key, hash, timestamps, and Epic's own opaque internal patient
# reference). No clinical content, no names, no MRN/DOB/SSN - see
# core/db/schema.sql for the reasoning. "System of record" here means
# authority, not immutability: with Object Lock removed, the store is
# the thing every other layer is rebuilt FROM, not a thing S3 prevents
# anyone from changing (see s3_store.tf's header).
#
# THE FREE TIER HERE IS TIME-LIMITED, UNLIKE KMS. RDS free tier is 750
# hours/month of a single db.t3.micro/db.t4g.micro/db.t2.micro instance
# for 12 months (legacy accounts) or until signup credits run out
# (accounts created after 2025-07-15 - could be well under 12 months).
# When it ends, AWS begins billing automatically at standard rates with
# NO warning and NO auto-stop - roughly $11.68/month for a db.t3.micro
# running continuously, on top of storage. Budget for this explicitly;
# see docs/COST.md.
# ---------------------------------------------------------------------------

variable "enable_db" {
  description = "Provision the RDS Postgres index. False skips deploy/aws/rds.tf entirely - the store works identically via S3 alone either way."
  type        = bool
  default     = true
}

variable "db_instance_class" {
  description = <<-EOT
    RDS instance class. db.t3.micro and db.t4g.micro are both free-tier
    eligible (750 hrs/month combined across ALL RDS instances in the
    account - a second instance anywhere uses up the same pool). Aurora
    is NOT free-tier eligible under any instance class; this stack uses
    standard RDS, not Aurora.
  EOT
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "Storage in GB. Free tier covers 20GB of gp2 storage (NOT gp3 - see db_storage_type)."
  type        = number
  default     = 20
}

variable "db_storage_type" {
  description = <<-EOT
    EASY TO GET WRONG: only "gp2" is free-tier eligible. "gp3" is the
    generally-recommended modern default elsewhere in AWS and is NOT
    covered by RDS free tier - using it bills from hour one even inside
    the free-tier window. Leave this at gp2 unless you have deliberately
    moved past the free tier.
  EOT
  type        = string
  default     = "gp2"

  validation {
    condition     = contains(["gp2", "gp3", "io1", "io2"], var.db_storage_type)
    error_message = "db_storage_type must be a valid RDS storage type."
  }
}

variable "db_multi_az" {
  description = "Multi-AZ deployment. NOT free-tier eligible - it runs a second instance, consuming double the 750-hour pool or billing immediately. Leave false for dev."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. Backups count against the free 20GB backup-storage allowance; a small dev database with a short retention stays well within it."
  type        = number
  default     = 1
}

variable "db_deletion_protection" {
  description = "Prevent accidental terraform destroy / console deletion of the DB instance. Should be true outside dev."
  type        = bool
  default     = false
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Only honored in dev - a final snapshot is cheap insurance anywhere real data might exist."
  type        = bool
  default     = true
}

variable "db_allowed_cidr_blocks" {
  description = <<-EOT
    CIDR blocks allowed to reach the database on port 5432. Defaults to
    EMPTY, meaning nothing can connect until you explicitly open access -
    a database with an open security group is a much bigger exposure than
    an S3 bucket with public access blocked, because a network path is
    all that stands between an attacker and every row in the table.

    For a dev machine connecting directly: your own IP as a /32, e.g.
    ["203.0.113.7/32"] - find it with `curl -s https://checkip.amazonaws.com`.
    For a compute resource in the same VPC: that resource's security
    group, referenced separately - see deploy/aws/rds.tf.
  EOT
  type        = list(string)
  default     = []
}

variable "db_publicly_accessible" {
  description = "Assign the RDS instance a public IP. Should stay false for anything beyond a dev machine connecting directly and briefly; combined with db_allowed_cidr_blocks this is still restricted by security group, but a public endpoint is more exposure than a private one regardless."
  type        = bool
  default     = false
}

variable "separate_db_key" {
  description = <<-EOT
    Use a dedicated KMS key for RDS storage encryption (adds $1-3/month)
    vs. reusing the audit key (default, $0 extra). Reuse is defensible
    here specifically because this database never holds PHI by design -
    see core/db/schema.sql - so its sensitivity tier is closer to the
    audit log's than to the store's. If that changes (e.g. a future
    feature indexes real identifiers), revisit this default.
  EOT
  type        = bool
  default     = false
}

variable "require_mfa_to_assume_roles" {
  description = <<-EOT
    Require aws:MultiFactorAuthPresent on the human-principal trust
    policy for the restore/disposition/auditor/psychotherapy roles.
    Default true, and leave it true anywhere real: the false setting
    exists solely so a dev evaluation stack can run the web interface -
    which needs a PurposeOfUse-TAGGED assumed-role session - from a
    workstation outside the VPC, where no instance profile is available
    and a non-interactive service cannot present a second factor. The
    per-request PurposeOfUse session-tag denies are unaffected either
    way. See the comment on the condition in iam.tf.
  EOT
  type        = bool
  default     = true
}
# Made by Ryan Gomez & Co. Inc.
