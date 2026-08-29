output "store_bucket" {
  description = "Name of the PHI store bucket. Set as PHI_AI_STORAGE_BUCKET."
  value       = aws_s3_bucket.store.id
}

output "audit_bucket" {
  description = "Name of the audit log bucket. Set as PHI_AI_AUDIT_BUCKET."
  value       = aws_s3_bucket.audit.id
}

output "store_kms_key_arn" {
  description = "ARN of the KMS key wrapping PHI data keys. Set as PHI_AI_KMS_KEY_ID."
  value       = aws_kms_key.store.arn
}

output "audit_kms_key_arn" {
  description = "ARN of the KMS key encrypting the audit log. Equals the store key ARN when separate_audit_key = false."
  value       = local.audit_key_arn
}

# FIXED: this pair of outputs did not exist - RUNBOOK_PSYCHOTHERAPY_NOTES.md
# has said "Terraform outputs will need extending to surface these - not
# yet done" since the psychotherapy bucket/key were added. Now done,
# following the exact same pattern as store_bucket/store_kms_key_arn
# above. Unlike the DB outputs below, these are never conditional - see
# deploy/aws/s3_psychotherapy.tf and kms.tf, both of which provision this
# bucket/key unconditionally for every deployment, not behind a variable.
output "psychotherapy_bucket" {
  description = "Name of the psychotherapy notes bucket. Set as PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET."
  value       = aws_s3_bucket.psychotherapy.id
}

output "psychotherapy_kms_key_arn" {
  description = "ARN of the KMS key wrapping psychotherapy note data keys. Set as PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID."
  value       = aws_kms_key.psychotherapy.arn
}

output "ingest_role_arn" {
  description = "Role the ingestion service runs as. Attach via instance profile / ECS task role."
  value       = aws_iam_role.ingest.arn
}

output "ingest_instance_profile" {
  description = "Instance profile name for attaching the ingest role to EC2."
  value       = aws_iam_instance_profile.ingest.name
}

output "restore_role_arn" {
  description = "Role for authorized PHI restore operations. Assume per-request with MFA."
  value       = aws_iam_role.restore.arn
}

output "auditor_role_arn" {
  description = "Role for audit chain verification. No PHI access."
  value       = aws_iam_role.auditor.arn
}

# FIXED: this pair did not exist either - core/fhir/psychotherapy_restore.py
# needs --role-arn on every invocation (see runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md),
# and there was no output to get it from, unlike restore_role_arn above for
# the general path.
output "psychotherapy_ingest_role_arn" {
  description = "Role the psychotherapy ingest path runs as, once wired up. Attach via instance profile / ECS task role."
  value       = aws_iam_role.psychotherapy_ingest.arn
}

output "psychotherapy_restore_role_arn" {
  description = "Role for authorized psychotherapy note restore operations. Assume per-request with MFA and the required session tags - see runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md."
  value       = aws_iam_role.psychotherapy_restore.arn
}

# FOUND AND FIXED (2026-08-17 audit, C4 - disposal completeness): this
# pair did not exist either, same gap as the psychotherapy_ingest/
# psychotherapy_restore pair above before it was fixed. core/fhir/purge.py
# and core/fhir/psychotherapy_purge.py both require --role-arn on every
# invocation - with no output, an operator had no supported way to learn
# either role's ARN short of reading Terraform state directly. See
# runbooks/RUNBOOK_DISPOSITION.md.
output "disposition_role_arn" {
  description = "Role for routine ('expired') and exceptional ('admin-order') disposal of stored PHI objects. Assume per-invocation with MFA - see runbooks/RUNBOOK_DISPOSITION.md and core/fhir/purge.py."
  value       = aws_iam_role.disposition.arn
}

output "psychotherapy_disposition_role_arn" {
  description = "Role for routine and exceptional disposal of stored psychotherapy notes specifically. No relationship to disposition_role_arn above - see runbooks/RUNBOOK_DISPOSITION.md and core/fhir/psychotherapy_purge.py."
  value       = aws_iam_role.psychotherapy_disposition.arn
}

output "retention_summary" {
  description = "Retention posture actually deployed. Read `enforcement` before treating the day counts as a guarantee."
  value = {
    phi_retention_days = var.phi_retention_days
    audit_retention_days   = var.audit_retention_days

    # Stated flatly so the numbers above cannot be misread as a control.
    enforcement               = "NONE - no S3 Object Lock. These periods are recorded as object metadata and drive manual disposition; any principal with s3:DeleteObject can delete stored data before they elapse."
    early_deletion_blocked_by = var.enable_admin_order_purge ? "IAM permissions only, and the disposition role is granted admin-order delete" : "IAM permissions only"
    detection                 = "bucket versioning, per-object SHA-256, hash-chained audit log, CloudTrail data events"
  }
}

# Convenience: emit the .env fragment so you don't hand-copy ARNs.
# Contains no secrets -- the FHIR private key (Epic backend services auth
# uses a signed JWT, not a client secret - see docs/EMR_CONNECTORS.md) is
# generated separately by scripts/generate_epic_keypair.sh and added by
# the installer chatbot; neither the key nor anything derived from it ever
# passes through Terraform state.
output "env_fragment" {
  description = "Paste into .env, then run install/installer_chatbot.py to add FHIR credentials."
  value       = <<-EOT
    PHI_AI_CLOUD_PROVIDER=aws
    PHI_AI_STORAGE_BUCKET=${aws_s3_bucket.store.id}
    PHI_AI_STORAGE_REGION=${var.aws_region}
    PHI_AI_KMS_KEY_ID=${aws_kms_key.store.arn}
    PHI_AI_AUDIT_BUCKET=${aws_s3_bucket.audit.id}
    PHI_AI_AUDIT_KMS_KEY_ID=${local.audit_key_arn}
    PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET=${aws_s3_bucket.psychotherapy.id}
    PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID=${aws_kms_key.psychotherapy.arn}
    %{if var.enable_db~}
    PHI_AI_DB_HOST=${aws_db_instance.index[0].address}
    PHI_AI_DB_PORT=${aws_db_instance.index[0].port}
    PHI_AI_DB_NAME=${aws_db_instance.index[0].db_name}
    PHI_AI_DB_INGEST_USERNAME=phi_ai_ingest
    PHI_AI_DB_READER_USERNAME=phi_ai_reader
    # FOUND AND FIXED (2026-08-17 audit, C4): this line did not exist -
    # core/fhir/purge.py's disposal completeness fix needs
    # PHI_AI_DISPOSITION_DB_USERNAME set to actually delete the
    # index/OMOP rows for a disposed resource (settings.py's
    # disposition_db_configured()); without it, purge.py silently falls
    # back to storage-only disposal, which is a real, intentional
    # graceful-skip for deployments with no Postgres index at all, but
    # NOT what an operator who ran core/db/bootstrap_aws.sql (which
    # creates this exact role) actually wants. Literal "phi_ai_disposition",
    # same pattern as the ingest/reader lines above - not a Terraform
    # variable, since the role name is fixed by bootstrap_aws.sql, not
    # configurable per-deployment.
    PHI_AI_DISPOSITION_DB_USERNAME=phi_ai_disposition
    %{~endif}
  EOT
}

# ---------------------------------------------------------------------------
# Postgres index
# ---------------------------------------------------------------------------

output "db_endpoint" {
  # FIXED: this description previously said "(host:port)" and instructed
  # setting PHI_AI_DB_HOST "to the host portion only," implying a
  # split was needed - but the value below is aws_db_instance.index[0].address,
  # which the AWS provider documents as host-only to begin with (the
  # combined "host:port" value lives in a *different* attribute,
  # .endpoint, not used here). Two runbooks worked around this by piping
  # the value through `cut -d: -f1` - harmless, since cutting a string
  # with no colon on the first colon just returns it unchanged, but based
  # on a wrong assumption about the shape of this output. Fixed the
  # description to match the actual value, and simplified both runbooks'
  # commands to drop the now-pointless cut.
  description = "RDS hostname (no port - use db_port below for that). Set PHI_AI_DB_HOST directly to this value."
  value       = var.enable_db ? aws_db_instance.index[0].address : null
}

output "db_port" {
  value = var.enable_db ? aws_db_instance.index[0].port : null
}

output "db_name" {
  value = var.enable_db ? aws_db_instance.index[0].db_name : null
}

output "db_resource_id" {
  description = "RDS resource ID, needed to construct the rds-db:connect ARN for any additional IAM principals beyond ingest/restore."
  value       = var.enable_db ? aws_db_instance.index[0].resource_id : null
}

# The file named below is core/db/bootstrap_aws.sql, and it has to be:
# it is the only bootstrap script that creates every role this stack's
# other outputs assume exist - phi_ai_ingest, phi_ai_reader,
# phi_ai_disposition and phi_ai_imaging - along with the USAGE ON SCHEMA
# public and roi_requests grants. env_fragment above emits
# PHI_AI_DISPOSITION_DB_USERNAME, so an operator sent to any other file
# would end up with a database that cannot serve the .env this stack
# just handed them. deploy/aws/rds.tf names the same file in three
# places; they must stay in step.
output "db_bootstrap_reminder" {
  value = var.enable_db ? "Database provisioned but not yet bootstrapped. Run core/db/schema.sql then core/db/bootstrap_aws.sql as the master user before pointing the app at this database - see runbooks/RUNBOOK_AWS_SETUP.md." : null
}

# ---------------------------------------------------------------------------
# Cost transparency
# ---------------------------------------------------------------------------

output "estimated_monthly_cost_usd" {
  description = <<-EOT
    Rough fixed monthly cost of this configuration, before usage. KMS key
    storage is the only unavoidable charge - it applies whether or not the
    keys are used, and has no free-tier allowance. Usage-driven costs (S3
    storage, KMS requests beyond 20,000/month, CloudTrail data events) are
    on top. See docs/COST.md.
  EOT
  value = {
    kms_keys = (var.separate_audit_key ? 2 : 1) + (var.enable_db && var.separate_db_key ? 1 : 0)

    # $1/key/month at rest. Rotation retains prior key versions, and the
    # charge rises toward a $3/key/month cap after two rotations.
    kms_key_storage_now = format(
      "$%.2f",
      ((var.separate_audit_key ? 2 : 1) + (var.enable_db && var.separate_db_key ? 1 : 0)) * 1.00
    )
    kms_key_storage_at_steady_state = var.enable_key_rotation ? format(
      "$%.2f (after ~2 years of rotation)",
      ((var.separate_audit_key ? 2 : 1) + (var.enable_db && var.separate_db_key ? 1 : 0)) * 3.00
    ) : "no rotation; stays flat"

    cloudtrail_data_events = var.cloudtrail_data_events ? "ON - $0.10 per 100k object events, billed from the first event" : "OFF - $0"
    lifecycle_transitions  = var.enable_lifecycle_transitions ? "ON - 128KB minimum billable size per object applies" : "OFF - $0"
    budget_alert           = var.monthly_budget_usd > 0 ? format("alerting at $%g/month", var.monthly_budget_usd) : "NONE - not recommended"

    db_within_free_tier = var.enable_db ? format(
      "$0 for %s single-AZ + %dGB %s storage IF within the free-tier window - see db_after_free_tier",
      var.db_instance_class, var.db_allocated_storage_gb, var.db_storage_type
    ) : "DB disabled"
    db_after_free_tier = var.enable_db ? (
      var.db_instance_class == "db.t4g.micro" || var.db_instance_class == "db.t3.micro" || var.db_instance_class == "db.t2.micro"
      ? "~$12-15/month once the free-tier window ends (12mo legacy accounts, or when signup credits run out) - billing starts AUTOMATICALLY with no warning"
      : format("%s is not a free-tier-eligible class - billing starts immediately, not after a free-tier window", var.db_instance_class)
    ) : "DB disabled"

    unavoidable_fixed_floor = "$1.00/month (one customer-managed KMS key)"
  }
}

output "cost_warnings" {
  description = "Configuration choices with cost or security consequences worth re-reading before apply."
  value = compact([
    var.separate_audit_key ? "" : "SHARED KMS KEY: the ingest role can decrypt stored PHI. Role separation is OFF. Saves $1-3/month. Dev/synthetic data only.",
    var.cloudtrail_data_events ? "" : "NO S3 DATA EVENTS: object-level access is not independently logged, so access that bypassed the application cannot be detected. Dev only.",
    var.enable_key_rotation ? "" : "KMS ROTATION OFF: acceptable only for short-lived dev stacks.",
    var.monthly_budget_usd > 0 ? "" : "NO BUDGET ALERT: nothing will warn you about runaway spend. AWS budgets are free (2 per account).",
    var.enable_lifecycle_transitions ? "COLD TIERING ON: verify your objects exceed 128KB, or transitions will cost MORE than S3 Standard." : "",
    var.enable_db && var.db_storage_type != "gp2" ? "DB STORAGE IS NOT gp2: db_storage_type=${var.db_storage_type} is not free-tier eligible and bills from hour one." : "",
    var.enable_db && var.db_multi_az ? "DB MULTI-AZ ON: doubles free-tier hour consumption / doubles instance cost." : "",
    var.enable_db ? "RDS FREE TIER IS TIME-LIMITED: 12 months (legacy accounts) or until signup credits run out (newer accounts) - unlike the flat KMS cost, this becomes a real ~$12+/month charge automatically, with no warning, when it ends. See docs/COST.md." : "",
  ])
}
# Made by Ryan Gomez & Co. Inc.
