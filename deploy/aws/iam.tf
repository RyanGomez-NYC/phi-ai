# ---------------------------------------------------------------------------
# Three separate roles, because "minimum necessary" (164.502(b)) is a
# structural property, not a policy document you write once:
#
#   ingest  - can WRITE stored objects and APPEND audit records.
#             Cannot decrypt PHI. Compromising the long-running ingestion
#             service does not yield readable patient data.
#
#   restore - can READ and decrypt stored objects, for records requests
#             and legal hold. Cannot write or delete. Assumed by a human
#             operator per-request, not left running.
#
#   auditor - can READ the audit log only. No PHI access at all. Lets
#             compliance staff verify the chain without granting them
#             access to the records the chain describes.
#
# Plus two more, scoped ONLY to psychotherapy notes specifically (see
# s3_psychotherapy.tf, kms.tf, runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md):
#
#   psychotherapy_ingest  - writes psychotherapy notes. Structurally
#             identical in spirit to ingest above, but scoped to the
#             separate psychotherapy bucket/key - holds no access to the
#             general store bucket at all, and vice versa.
#
#   psychotherapy_restore - reads and decrypts psychotherapy notes ONLY
#             under one of the three narrow exceptions HIPAA 164.508(a)(2)
#             actually permits without a specific patient authorization.
#             The general restore role above has NO access to this
#             bucket - "records request," which is exactly what restore
#             is for, does not satisfy any of the three exceptions.
#
# Plus a fifth, for planned/routine and exceptional administrator-order
# removal (see purge.py's own module docstring for the full design):
#
#   disposition - can check retention status and delete objects whose
#             retention has ALREADY expired. Cannot decrypt PHI, ever.
#             Can ALSO bypass an active GOVERNANCE-mode lock for
#             specifically-tagged objects, but ONLY when
#             var.enable_admin_order_purge is explicitly true - off by
#             default, and structurally impossible in COMPLIANCE mode
#             regardless of this variable.
#
# Plus a sixth (2026-08-17 audit, C4 - disposal completeness), the
# psychotherapy twin of disposition above:
#
#   psychotherapy_disposition - identical shape to disposition, scoped
#             entirely to the psychotherapy bucket. Added because
#             nothing in this file previously held ANY delete grant on
#             that bucket at all - the single most access-restricted
#             data class in this system was also the one with no
#             disposal path whatsoever, structurally unable to satisfy
#             HIPAA's own disposal requirement (45 CFR 164.310(d)(2)(i))
#             for psychotherapy notes specifically. See
#             core/fhir/psychotherapy_purge.py and
#             runbooks/RUNBOOK_DISPOSITION.md.
#
# NOTE: the ingest role's inability to decrypt PHI depends on the store
# and audit logs using DIFFERENT KMS keys. See the warning on the
# EncryptAuditRecords statement below before setting separate_audit_key
# to false.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect = "Allow"

    # sts:TagSession here is load-bearing, not optional hardening.
    # FOUND AND FIXED: this trust policy previously allowed only
    # sts:AssumeRole. AWS rejects any AssumeRole call that passes
    # session tags unless the trust policy ALSO allows sts:TagSession -
    # and every role using this trust document depends on session tags
    # to function at all (restore's PurposeOfUse, psychotherapy_restore's
    # PsychotherapyException/PsychotherapyAttestation, disposition's
    # AdminBasis in admin-order mode). Without it, every tagged
    # assumption failed at STS with AccessDenied: records-request
    # restores, psychotherapy restores, and admin-order purges were all
    # unavailable. (Expired-mode purge, which passes no tags, still
    # worked.) It failed CLOSED - the Null-condition denies in the role
    # policies below block untagged reads - but the realistic operator
    # workaround, attaching a static PurposeOfUse (or exception/basis)
    # tag to the role itself, would satisfy aws:PrincipalTag
    # PERMANENTLY, silently converting a per-request control into
    # standing access. Do not ever "fix" a tag-related AccessDenied
    # that way; the session tag is the control.
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = length(var.trusted_principal_arns) > 0 ? var.trusted_principal_arns : [local.root_arn]
    }

    # Require MFA when a human principal assumes these roles. Service
    # principals (EC2/ECS/EKS) should be granted access via their own
    # instance/task role listed in trusted_principal_arns, which will not
    # satisfy this condition -- so for those, use the instance profile
    # attachment below instead of sts:AssumeRole.
    #
    # `require_mfa_to_assume_roles` exists for one deployment shape this
    # condition otherwise has no answer for: a service process (the web
    # interface) running OUTSIDE the cloud - a developer workstation -
    # that must hold a TAGGED session, which the instance-profile path
    # cannot provide and an MFA condition blocks for a non-interactive
    # process. Turning it off removes only the second factor on role
    # assumption; the PurposeOfUse session-tag Null-condition denies -
    # the control the comment above calls load-bearing - apply exactly
    # as before. Never set false outside a dev stack holding synthetic
    # data; a production deployment puts the service inside the VPC on
    # an instance profile instead.
    dynamic "condition" {
      for_each = var.require_mfa_to_assume_roles ? [1] : []
      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }
}

# Service-principal trust for workloads running on EC2/ECS.
#
# Deliberately does NOT include sts:TagSession (unlike assume_role
# above): no service-principal path in this codebase passes session
# tags, and granting it here would let a compromised service instance
# mint arbitrary principal tags - the same attribute family the
# restore/disposition denies key on.
data "aws_iam_policy_document" "assume_role_service" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com", "ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Ingest role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ingest" {
  name               = "${local.name_prefix}-ingest"
  assume_role_policy = data.aws_iam_policy_document.assume_role_service.json
  description        = "PHI AI Platform ingestion service: writes encrypted PHI and appends audit records. Cannot decrypt."

  tags = { Role = "phi-ai-ingest" }
}

resource "aws_iam_instance_profile" "ingest" {
  name = "${local.name_prefix}-ingest"
  role = aws_iam_role.ingest.name
}

data "aws_iam_policy_document" "ingest" {
  statement {
    sid    = "WriteStoreObjects"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:PutObjectRetention", # required to set per-object retain-until on write
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
  }

  # REMOVED (2026-08-17 audit, H1; implemented 2026-08-18): this
  # statement used to grant s3:PutObject/s3:GetObject on
  # "${aws_s3_bucket.store.arn}/_state/*" for
  # core/fhir/scheduler.py's incremental-ingest watermark
  # (WATERMARK_KEY = "_state/last_successful_run.json"). It never
  # actually worked - _state/* objects are SSE-KMS under the store
  # key, and this role deliberately holds no kms:Decrypt on that key
  # (see CheckExistingObjects's comment below), so the GetObject half
  # always failed and scheduler.py silently treated every run as a
  # full re-ingest. Worse, independent of that: the store bucket
  # has bucket-level default Object Lock retention
  # (s3_store.tf), and every watermark PutObject here silently
  # inherited it (no ObjectLockMode/ObjectLockRetainUntilDate was ever
  # passed) - so every overwrite was ALSO locked under multi-year
  # retention, accumulating undeletable locked versions on every
  # scheduler cycle, forever. Storing scheduler state in an
  # Object-Lock-enabled bucket was the wrong design, not just
  # misconfigured. The watermark now lives in Postgres's index_state
  # table instead (core/db/schema.sql, core/fhir/scheduler.py's
  # SCHEDULER_WATERMARK_KEY comment has the full account) - already
  # reachable via ConnectToIndexDatabase below, so this statement is
  # simply deleted, not replaced by anything: no S3 access of any kind
  # under _state/ is needed or granted anymore, consistent with this
  # project's minimum-necessary invariant (every access path needs its
  # own deliberate grant - the inverse holds too: a grant nothing
  # legitimately uses anymore should not linger).

  statement {
    sid    = "InspectStoreBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration", # healthcheck verifies lock is on
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.store.arn]
  }

  # HeadObject (used for idempotency checks before re-storing) maps to
  # s3:GetObject. Safe without a KMS grant, but be precise about WHY
  # (an earlier version of this comment claimed GetObject would return
  # ciphertext - it does not): HeadObject returns metadata without any
  # KMS call, while an actual GetObject on these SSE-KMS objects FAILS
  # outright for this role, because S3 calls kms:Decrypt on the
  # caller's behalf for SSE-KMS reads and this role holds no decrypt
  # on the store key. No bytes at all, not "ciphertext only" - and
  # even a principal that somehow got the bytes would hold
  # envelope-encrypted ciphertext under a second, application-layer
  # key wrap. That wrong mental model is exactly what produced the
  # now-removed WriteSchedulerState statement's unreadable-watermark
  # bug (see the comment above where it used to sit) - do not port it
  # to GCP/Azure.
  statement {
    sid       = "CheckExistingObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
  }

  # Wrap DEKs only. Note the absence of kms:Decrypt -- this is the whole
  # point of separating the roles.
  statement {
    sid    = "WrapDataKeys"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.store.arn]
  }

  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention"]
    resources = ["${aws_s3_bucket.audit.arn}/audit/*"]
  }

  # The audit log is hash-chained, so appending requires reading the most
  # recent record to obtain its hash. Read is scoped to the audit prefix.
  statement {
    sid       = "ReadAuditChainTip"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  # WARNING - READ BEFORE SETTING separate_audit_key = false.
  #
  # kms:Decrypt here is unavoidable: the hash chain requires reading the most
  # recent audit record before appending, and S3 needs Decrypt to hand back an
  # SSE-KMS object.
  #
  # When separate_audit_key is TRUE this is scoped to the audit key, and the
  # ingest role still cannot decrypt stored PHI. That is the whole design.
  #
  # When separate_audit_key is FALSE, local.audit_key_arn IS the store key,
  # so this statement grants the ingest role kms:Decrypt on stored PHI and
  # role separation no longer exists. The saving is $1-3/month. Do not make
  # that trade on anything holding real patient data - the validation
  # block on var.separate_audit_key (variables.tf) refuses the plan
  # outside dev. (FOUND AND FIXED: this pointer previously named a
  # precondition on aws_kms_key.audit - a guard that could never fire,
  # because with separate_audit_key=false that resource has count 0 and
  # its preconditions are never evaluated at all. See variables.tf.)
  statement {
    sid    = "EncryptAuditRecords"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = [local.audit_key_arn]
  }

  # Postgres index write access. Scoped to the specific database user
  # (phi_ai_ingest, created by core/db/bootstrap_aws.sql), which
  # itself only holds INSERT on the index table - see that file. This
  # grants only the ability to AUTHENTICATE as that user via IAM; what
  # the user can do once connected is a Postgres-side GRANT, not an IAM
  # concern. Emitted only when the database is provisioned.
  #
  # The dbuser suffix is a Postgres ROLE NAME and must match
  # core/db/bootstrap_aws.sql exactly - an rds-db:connect ARN authorizes
  # exactly one role name, and a name the ARN does not carry
  # authenticates nothing.
  dynamic "statement" {
    for_each = var.enable_db ? [1] : []
    content {
      sid       = "ConnectToIndexDatabase"
      effect    = "Allow"
      actions   = ["rds-db:connect"]
      resources = ["arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.index[0].resource_id}/phi_ai_ingest"]
    }
  }

  # OMOP analytics layer write access - the SAME ingest role/process
  # (core/fhir/scheduler.py, bulk_scheduler.py) opens a SECOND,
  # independent connection as omop_etl (core/db/omop_bootstrap_aws.sql),
  # so it needs its own rds-db:connect grant here, scoped to that
  # specific dbuser - a genuinely separate statement, not a broadening
  # of the one above, matching the same "authenticate only, Postgres
  # itself governs what the role can do once connected" reasoning.
  #
  # FOUND AND FIXED: this statement was missing entirely until now, even
  # though core/db/omop_bootstrap_aws.sql (which creates the omop_etl
  # Postgres role) and the scheduler wiring that connects as it were
  # both already live - meaning every OMOP connection attempt from this
  # role would have failed at the AWS IAM layer, before ever reaching
  # Postgres, regardless of how correctly everything downstream of the
  # connection was built.
  #
  # Gated on var.enable_db, the same condition ConnectToIndexDatabase
  # above uses, rather than a dedicated var.enable_omop this project
  # doesn't have yet - a real simplification: this grants only the
  # ABILITY to attempt the connection, and it stays inert unless an
  # operator has also run core/db/omop_schema.sql,
  # core/db/omop_vocab_schema.sql, and core/db/omop_bootstrap_aws.sql
  # (a separate, deliberate opt-in step - see
  # runbooks/RUNBOOK_OMOP_SETUP.md) to make the omop_etl Postgres role
  # exist at all. A dedicated toggle scoped specifically to OMOP would
  # be a reasonable future refinement, not required for this to be
  # correct today.
  dynamic "statement" {
    for_each = var.enable_db ? [1] : []
    content {
      sid       = "ConnectToOmopDatabase"
      effect    = "Allow"
      actions   = ["rds-db:connect"]
      resources = ["arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.index[0].resource_id}/omop_etl"]
    }
  }

  # Explicit deny beats any future over-broad grant attached to this role.
  statement {
    sid    = "DenyDestructiveActions"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutBucketPolicy",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketVersioning",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:PutKeyPolicy",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ingest" {
  name   = "${local.name_prefix}-ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

# ---------------------------------------------------------------------------
# Restore role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "restore" {
  name               = "${local.name_prefix}-restore"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = "PHI AI Platform restore: reads and decrypts stored PHI for authorized records requests. MFA required."

  # Short session: restore access is per-request, not standing access.
  max_session_duration = 3600

  tags = { Role = "phi-ai-restore" }
}

data "aws_iam_policy_document" "restore" {
  statement {
    sid    = "ReadStoreObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectRetention",
    ]
    resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
  }

  statement {
    sid       = "ListStoreBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.store.arn]
  }

  statement {
    sid       = "UnwrapDataKeys"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.store.arn]
  }

  # Every restore must be audit-logged, so this role appends too.
  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid       = "UseAuditKey"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = [local.audit_key_arn]
  }

  # Postgres index read access, as the SELECT-only phi_ai_reader
  # user (core/db/bootstrap_aws.sql). This role already handles
  # authorized records requests, so "find everything stored for this
  # patient" via the index (core/db/index.py find_by_patient_reference)
  # is a natural fit here rather than a new role. This grants only the
  # ability to authenticate as that user; the SELECT-only privilege is a
  # Postgres GRANT, not an IAM concern.
  #
  # The dbuser suffix is a Postgres ROLE NAME and must match
  # core/db/bootstrap_aws.sql exactly - see ConnectToIndexDatabase on the
  # ingest role above.
  dynamic "statement" {
    for_each = var.enable_db ? [1] : []
    content {
      sid       = "QueryIndexDatabase"
      effect    = "Allow"
      actions   = ["rds-db:connect"]
      resources = ["arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.index[0].resource_id}/phi_ai_reader"]
    }
  }

  statement {
    sid    = "DenyWritesAndDeletes"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "kms:ScheduleKeyDeletion",
    ]
    resources = ["*"]
  }

  # Restoring PHI without a recorded purpose-of-use is exactly the failure
  # mode the minimum-necessary standard exists to prevent. The application
  # requires it; this denies the S3 read outright if the caller's session
  # wasn't tagged with one, so it holds even if someone bypasses the app
  # and uses the AWS CLI directly - AS LONG AS the caller is using THIS
  # role's credentials.
  #
  # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "PurposeOfUse enforcement
  # lives only in role policies, not bucket/key policies"): the qualifier
  # above matters and used to go unstated - this statement lives in the
  # restore role's OWN policy, so it only evaluates for callers who
  # assumed this specific role. A different IAM identity holding its own
  # s3:GetObject + kms:Decrypt grants on the store bucket (a
  # misconfigured policy, an over-broad admin role, anything not modeled
  # here) was never subject to this check at all. s3_store.tf's
  # store_bucket_policy now carries an equivalent
  # DenyReadWithoutPurposeOfUse statement at the BUCKET level, which
  # evaluates for every principal regardless of role - see that file's
  # comment for the full reasoning, including why it exempts ingest and
  # disposition by ARN rather than blocking their existing, already-safe
  # metadata-only reads.
  statement {
    sid       = "DenyReadWithoutPurposeOfUse"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.store.arn}/*"]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PurposeOfUse"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "restore" {
  name   = "${local.name_prefix}-restore"
  role   = aws_iam_role.restore.id
  policy = data.aws_iam_policy_document.restore.json
}

# ---------------------------------------------------------------------------
# Auditor role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "auditor" {
  name                 = "${local.name_prefix}-auditor"
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  description          = "PHI AI Platform auditor: verifies audit chain integrity. No PHI access."
  max_session_duration = 3600

  tags = { Role = "phi-ai-auditor" }
}

data "aws_iam_policy_document" "auditor" {
  statement {
    sid       = "ReadAuditLog"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "s3:ListBucketVersions"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
  }

  statement {
    sid       = "DecryptAuditLog"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [local.audit_key_arn]
  }

  # Read CloudTrail for the out-of-band cross-check described in
  # runbooks/RUNBOOK_INCIDENT_RESPONSE.md. LookupEvents/GetTrailStatus/
  # DescribeTrails cover MANAGEMENT events only (e.g. the KMS
  # Decrypt/GenerateDataKey calls captured by the "All management
  # events" advanced_event_selector in cloudtrail.tf) - see
  # ReadCloudTrailLogFiles below for why this is not sufficient on its
  # own for the actual PHI-object cross-check this role exists to run.
  statement {
    sid       = "ReadCloudTrail"
    effect    = "Allow"
    actions   = ["cloudtrail:LookupEvents", "cloudtrail:GetTrailStatus", "cloudtrail:DescribeTrails"]
    resources = ["*"]
  }

  # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "CloudTrail cross-check
  # limitations"): ReadCloudTrail above cannot perform the cross-check
  # runbooks/RUNBOOK_INCIDENT_RESPONSE.md and
  # runbooks/RUNBOOK_INDEX_MAINTENANCE.md describe. AWS's Event History /
  # LookupEvents API serves MANAGEMENT events only - it does not return
  # S3 DATA events (PutObject/GetObject/DeleteObject on individual
  # objects), which is exactly what "did someone touch this exact S3 key
  # outside the application" needs. Data events are captured by the
  # OTHER advanced_event_selector in cloudtrail.tf ("Store, audit, and
  # psychotherapy bucket object-level events") and are only ever
  # queryable by reading the delivered log files themselves out of
  # aws_s3_bucket.cloudtrail - there is no API equivalent of LookupEvents
  # for them. Before this fix, the auditor role held no read grant on
  # that bucket at all, so there was literally no way for this role to
  # perform the one cross-check it exists to run; both runbooks'
  # documented `aws cloudtrail lookup-events` step for finding a
  # DeleteObject event would silently return nothing regardless of
  # whether the delete happened, and incident response was left with no
  # path but falling back to admin/root credentials - the exact
  # anti-pattern separate, minimum-necessary roles exist to avoid. See
  # both runbooks for the corrected procedure (reading the log files
  # directly) using this grant.
  #
  # No separate KMS grant needed: aws_cloudtrail.main's kms_key_id is
  # local.audit_key_arn (cloudtrail.tf), the same key DecryptAuditLog
  # above already grants kms:Decrypt on.
  #
  # Gated on var.enable_cloudtrail, since aws_s3_bucket.cloudtrail has
  # count = var.enable_cloudtrail ? 1 : 0 (cloudtrail.tf) and does not
  # exist at all when CloudTrail itself is disabled.
  dynamic "statement" {
    for_each = var.enable_cloudtrail ? [1] : []
    content {
      sid       = "ReadCloudTrailLogFiles"
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:ListBucket"]
      resources = [aws_s3_bucket.cloudtrail[0].arn, "${aws_s3_bucket.cloudtrail[0].arn}/*"]
    }
  }

  # No PHI. Not by omission -- explicitly. This deny is unconditional: even
  # with a shared KMS key, the auditor never gets objects out of the store
  # bucket, so it cannot read PHI regardless of key configuration.
  statement {
    sid       = "DenyAllPHIAccess"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = [aws_s3_bucket.store.arn, "${aws_s3_bucket.store.arn}/*"]
  }

  # Emitted ONLY when the audit log has its own key. With a shared key this
  # deny would also match the auditor's audit-log decrypt - and an explicit
  # deny beats an allow in IAM - so including it unconditionally would break
  # audit verification entirely.
  #
  # Dropping it in shared-key mode is safe because the S3 deny above already
  # blocks every path to stored ciphertext: holding kms:Decrypt with nothing
  # to decrypt confers no access to PHI.
  dynamic "statement" {
    for_each = var.separate_audit_key ? [1] : []
    content {
      sid       = "DenyStoreKeyUse"
      effect    = "Deny"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [aws_kms_key.store.arn]
    }
  }
}

resource "aws_iam_role_policy" "auditor" {
  name   = "${local.name_prefix}-auditor"
  role   = aws_iam_role.auditor.id
  policy = data.aws_iam_policy_document.auditor.json
}

# ---------------------------------------------------------------------------
# Psychotherapy notes ingest role
#
# See s3_psychotherapy.tf and kms.tf for the storage/key this writes to,
# and runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for the full reasoning.
# Structurally the same shape as the general ingest role above - write
# and encrypt only, cannot decrypt - but scoped entirely to the separate
# psychotherapy bucket and key. Holds zero access to the general store
# bucket, and the general ingest role holds zero access here.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "psychotherapy_ingest" {
  name               = "${local.name_prefix}-psychotherapy-ingest"
  assume_role_policy = data.aws_iam_policy_document.assume_role_service.json
  description        = "PHI AI Platform psychotherapy notes ingestion: writes encrypted psychotherapy notes to their own bucket/key. Cannot decrypt. No access to the general store bucket."

  tags = { Role = "phi-ai-psychotherapy-ingest" }
}

resource "aws_iam_instance_profile" "psychotherapy_ingest" {
  name = "${local.name_prefix}-psychotherapy-ingest"
  role = aws_iam_role.psychotherapy_ingest.name
}

data "aws_iam_policy_document" "psychotherapy_ingest" {
  statement {
    sid    = "WritePsychotherapyObjects"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:PutObjectRetention",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
  }

  statement {
    sid    = "InspectPsychotherapyBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.psychotherapy.arn]
  }

  statement {
    sid       = "CheckExistingPsychotherapyObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
  }

  statement {
    sid    = "WrapPsychotherapyDataKeys"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.psychotherapy.arn]
  }

  # Same central audit log as the general ingest role - a single,
  # unified record of all store activity is desirable, and the audit
  # log itself never contains clinical content (core/audit/log.py only
  # ever records actor/action/resource_key/purpose), so sharing it does
  # not reopen the separation this role otherwise maintains.
  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention"]
    resources = ["${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid       = "ReadAuditChainTip"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid    = "EncryptAuditRecords"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = [local.audit_key_arn]
  }

  statement {
    sid    = "DenyDestructiveActions"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutBucketPolicy",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketVersioning",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:PutKeyPolicy",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "psychotherapy_ingest" {
  name   = "${local.name_prefix}-psychotherapy-ingest"
  role   = aws_iam_role.psychotherapy_ingest.id
  policy = data.aws_iam_policy_document.psychotherapy_ingest.json
}

# ---------------------------------------------------------------------------
# Psychotherapy notes restore role
#
# The core control for this whole feature. 45 CFR 164.508(a)(2): a
# covered entity must obtain authorization for essentially any use or
# disclosure of psychotherapy notes, with exactly three exceptions - use
# by the note's own originator for treatment, the covered entity's own
# training programs, or defending itself in litigation the patient
# brought. "Records request," which is what the general restore role
# exists for, satisfies none of these - so that role has no access to
# this bucket at all, and this role requires an explicit, narrow
# exception tag that the general restore role's PurposeOfUse tag does
# not substitute for.
#
# WHAT THIS DOES NOT AND CANNOT DO: verify that a claimed exception is
# actually true. That a session tagged "originator-treatment" is really
# being assumed by the note's own original author is an attestation this
# system records (PsychotherapyAttestation, required below) - not
# something infrastructure-level access control can confirm against the
# real world. That verification is an organizational/procedural control,
# not a technical one. See runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "psychotherapy_restore" {
  name               = "${local.name_prefix}-psychotherapy-restore"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = "PHI AI Platform psychotherapy notes restore: reads and decrypts psychotherapy notes ONLY under one of the three narrow HIPAA 164.508(a)(2) exceptions. MFA required. No access via the general restore role."

  # 3600 is AWS's MINIMUM for this attribute, not a chosen value - the
  # API rejects anything below it. This was 1800 for a long time, which
  # made the whole stack fail `terraform validate` and could never have
  # applied.
  #
  # The shorter session this role actually wants is expressed where it
  # can be: max_session_duration is only a CEILING, and the caller picks
  # the real length via DurationSeconds on sts:AssumeRole.
  # core/fhir/psychotherapy_restore.py requests 1800 there, which is what
  # makes a 30-minute session real rather than aspirational.
  max_session_duration = 3600

  tags = { Role = "phi-ai-psychotherapy-restore" }
}

data "aws_iam_policy_document" "psychotherapy_restore" {
  statement {
    sid    = "ReadPsychotherapyObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectRetention",
    ]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
  }

  statement {
    sid       = "ListPsychotherapyBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.psychotherapy.arn]
  }

  statement {
    sid       = "UnwrapPsychotherapyDataKeys"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.psychotherapy.arn]
  }

  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid       = "UseAuditKey"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = [local.audit_key_arn]
  }

  statement {
    sid    = "DenyWritesAndDeletes"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "kms:ScheduleKeyDeletion",
    ]
    resources = ["*"]
  }

  # The core control: deny unless PsychotherapyException is exactly one
  # of the three values HIPAA 164.508(a)(2) actually permits. With
  # StringNotEquals against a list, IAM denies unless the tag's value
  # matches at least one entry in the list - so this fires for anything
  # OTHER than these three, including an absent tag combined with the
  # Null check below for defense in depth.
  #
  # Same scope caveat as the restore role's DenyReadWithoutPurposeOfUse
  # above (2026-08-17 audit, MEDIUM, "PurposeOfUse/psychotherapy tag
  # enforcement lives only in role policies, not bucket/key policies"):
  # this and the two deny statements below only evaluate for callers
  # using THIS role. s3_psychotherapy.tf's psychotherapy_bucket_policy
  # now carries the equivalent three statements at the bucket level -
  # see that file's comment.
  statement {
    sid       = "DenyReadWithoutValidException"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalTag/PsychotherapyException"
      values   = ["originator-treatment", "training-program", "legal-defense"]
    }
  }

  statement {
    sid       = "DenyReadWithoutExceptionTag"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/*"]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PsychotherapyException"
      values   = ["true"]
    }
  }

  # A free-text record of who is attesting to the claimed exception and
  # why - required for every retrieval, not independently verified by
  # IAM. See the module comment above.
  statement {
    sid       = "DenyReadWithoutAttestation"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/*"]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PsychotherapyAttestation"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "psychotherapy_restore" {
  name   = "${local.name_prefix}-psychotherapy-restore"
  role   = aws_iam_role.psychotherapy_restore.id
  policy = data.aws_iam_policy_document.psychotherapy_restore.json
}

# ---------------------------------------------------------------------------
# Disposition role
#
# See core/fhir/purge.py's own module docstring for the full design this
# implements. Two modes, sharply separated here the same way they are in
# that script:
#
#   "expired" mode (core/fhir/purge.py expired) - deletes objects whose
#   retention has ALREADY passed. Needs no special permission beyond an
#   ordinary delete grant, because S3 Object Lock itself already permits
#   deletion once an object's own recorded retention date has elapsed,
#   in BOTH GOVERNANCE and COMPLIANCE modes. Always available, regardless
#   of var.enable_admin_order_purge - this is the routine, expected path
#   HIPAA's own disposal requirement (164.310(d)(2)(i)) contemplates.
#
#   "admin-order" mode (core/fhir/purge.py admin-order) - removes one or
#   more specifically-named records before their retention date, under
#   a stated administrative basis. Requires the delete grant below,
#   emitted ONLY when var.enable_admin_order_purge is true (off by
#   default), and further gated by a required AdminBasis session tag
#   the same way psychotherapy_restore above gates on
#   PsychotherapyException/PsychotherapyAttestation. Has no effect
#   whatsoever in COMPLIANCE mode - see the precondition on this role's
#   policy resource below.
#
# NO KMS PERMISSIONS ON THE STORE KEY, in either mode - this role
# cannot decrypt stored PHI under any circumstance. Deciding whether
# to delete something only requires reading retention metadata, never
# content; s3:GetObject below exposes ciphertext bytes at most, the
# same accepted pattern as the ingest role's own CheckExistingObjects
# statement - without a KMS grant, ciphertext alone cannot become
# readable PHI.
#
# ALSO holds a Postgres rds-db:connect grant (2026-08-17 audit, C4 -
# disposal completeness), scoped to the phi_ai_disposition dbuser
# - see ConnectToDispositionDatabase below and
# core/db/bootstrap_aws.sql/omop_bootstrap_aws.sql for the Postgres-side
# role and its narrow, column-scoped delete grants.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "disposition" {
  name               = "${local.name_prefix}-disposition"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = "PHI AI Platform disposition: deletes objects whose retention has expired (routine), or specifically-named objects under a stated administrative basis when var.enable_admin_order_purge is true (exceptional). MFA required. Cannot decrypt PHI under any circumstance."

  # AWS minimum; see psychotherapy_restore above for why this is 3600 and
  # not the 1800 the comment here used to claim. The 30-minute session is
  # requested via DurationSeconds in core/fhir/purge.py.
  max_session_duration = 3600

  tags = { Role = "phi-ai-disposition" }
}

data "aws_iam_policy_document" "disposition" {
  statement {
    sid    = "InspectStoreRetention"
    effect = "Allow"
    actions = [
      "s3:GetObject", # HeadObject, for get_metadata()'s retention_until lookup - see the module comment above on why this is safe without a KMS grant
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.store.arn, "${aws_s3_bucket.store.arn}/fhir/*"]
  }

  # The actual delete grant, and now the ONLY thing standing between this
  # role and permanent removal of stored PHI. S3 used to refuse it for
  # any object still under an active Object Lock retention regardless of
  # IAM; with the lock removed there is no such backstop, so this grant
  # means exactly what it says at any point in an object's life. The
  # retention date is re-checked in application code instead - see
  # core/fhir/purge.py's _dispose_one(require_expired=True) - which is a
  # weaker guarantee than S3 refusing the call, and deliberately scoped
  # to the store prefix here rather than the whole bucket.
  statement {
    sid       = "DeleteStoreObjects"
    effect    = "Allow"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
  }

  # Every disposition and every admin-order removal is audit-logged
  # BEFORE the delete happens - this role appends to the same central
  # audit log every other role does.
  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid       = "UseAuditKey"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = [local.audit_key_arn]
  }

  # Postgres derived-row cleanup (2026-08-17 audit, C4 - disposal
  # completeness). Scoped to a NEW, separate Postgres role
  # (phi_ai_disposition - core/db/bootstrap_aws.sql,
  # core/db/omop_bootstrap_aws.sql), not phi_ai_ingest/
  # phi_ai_reader/omop_etl/omop_analyst - the same "authenticate
  # only, Postgres itself governs privileges once connected" reasoning
  # as ConnectToIndexDatabase/ConnectToOmopDatabase on the ingest role
  # above. ONE role, ONE statement, spanning both the index and OMOP
  # schemas - unlike ingest's two separate grants for two separate
  # roles, a single disposal operation needs to remove both an index
  # row and any OMOP row for the same resource in one pass, so
  # core/fhir/purge.py connects as this one role for both (see
  # core/db/index.py's delete_index_entry() and
  # core/db/omop_purge.py's delete_by_source_storage_key()).
  #
  # FOUND AND FIXED: before this statement existed, purge.py could
  # delete the storage object but had no way to reach Postgres at all -
  # the index row and any OMOP row for a disposed resource survived
  # every disposal indefinitely, holding identified PHI (OMOP) or
  # producing a false tampering-shaped "orphaned row" finding (index) -
  # see runbooks/RUNBOOK_DISPOSITION.md.
  #
  # The dbuser suffix is a Postgres ROLE NAME and must match
  # core/db/bootstrap_aws.sql exactly - see ConnectToIndexDatabase on the
  # ingest role above.
  dynamic "statement" {
    for_each = var.enable_db ? [1] : []
    content {
      sid       = "ConnectToDispositionDatabase"
      effect    = "Allow"
      actions   = ["rds-db:connect"]
      resources = ["arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.index[0].resource_id}/phi_ai_disposition"]
    }
  }

  # No decrypt of store PHI, ever, under any circumstance - the core
  # property this role exists to preserve even though it holds a delete
  # grant. Deciding what to delete needs metadata, never content.
  #
  # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "disposition deadlocks in
  # shared-audit-key mode"): this statement was previously unconditional
  # - present regardless of var.separate_audit_key - unlike the auditor
  # role's structurally identical DenyStoreKeyUse above, which the
  # auditor role already gates behind `dynamic "statement" { for_each =
  # var.separate_audit_key ? [1] : [] ... }` for exactly this reason.
  # When separate_audit_key is false, local.audit_key_arn IS
  # aws_kms_key.store.arn (see the EncryptAuditRecords comment on the
  # ingest role above) - so an unconditional deny here collided directly
  # with this same role's UseAuditKey Allow statement on that identical
  # key ARN. IAM resolves an explicit Deny over any Allow unconditionally,
  # so in shared-key mode this role could never actually call
  # kms:Decrypt/kms:GenerateDataKey on the audit key - meaning
  # core/fhir/purge.py's audit.record() calls (reading the hash-chain tip
  # before appending, then encrypting the new entry - both required
  # before every single delete, per this module's own AppendAuditRecords
  # comment) would fail with AccessDenied on every run. Not a partial
  # degradation: with the audit append blocked, _run_expired/
  # _run_admin_order fail outright before any object is touched, so this
  # role could not dispose of anything at all whenever a deployment chose
  # the shared-key cost tradeoff - the exact deployment configuration this
  # project explicitly supports (see var.separate_audit_key's own
  # variables.tf validation, which permits false only in dev). Gated the
  # same way the auditor role already solved this identical collision:
  # emitted only when the audit key is genuinely separate from the
  # store key, so it can never collide with UseAuditKey above. Safe to
  # drop entirely in shared-key mode for the same reason the auditor
  # role's comment gives: this role's S3 grants remain scoped to
  # DeleteStoreObjects/InspectStoreRetention only, never
  # s3:GetObject-with-decrypt-into-plaintext, so holding kms:Decrypt on a
  # key with nothing this role ever reads confers no PHI access.
  dynamic "statement" {
    for_each = var.separate_audit_key ? [1] : []
    content {
      sid       = "DenyStoreKeyUse"
      effect    = "Deny"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [aws_kms_key.store.arn]
    }
  }

  # admin-order delete - emitted ONLY when var.enable_admin_order_purge
  # is true. Absent entirely otherwise, so this role holds no path to
  # early deletion unless an operator has deliberately opted in.
  #
  # This grants the delete actions themselves, where it previously
  # granted s3:BypassGovernanceRetention. That permission was meaningful
  # only while Object Lock existed; with the lock gone, leaving the grant
  # (and the AdminBasis condition attached to it) would have meant
  # admin-order deletes were governed by nothing whatsoever.
  dynamic "statement" {
    for_each = var.enable_admin_order_purge ? [1] : []
    content {
      sid       = "AllowAdminOrderDelete"
      effect    = "Allow"
      actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
    }
  }

  # Requires the AdminBasis session tag whenever the delete grant above
  # is present - same DenyReadWithoutPurposeOfUse/
  # DenyReadWithoutAttestation pattern the restore and
  # psychotherapy_restore roles already use: enforced at the AWS layer,
  # not just documented as a script convention, so it holds even if the
  # delete permission were used directly rather than through
  # core/fhir/purge.py.
  dynamic "statement" {
    for_each = var.enable_admin_order_purge ? [1] : []
    content {
      sid       = "DenyAdminOrderDeleteWithoutAdminBasis"
      effect    = "Deny"
      actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.store.arn}/fhir/*"]
      condition {
        test     = "Null"
        variable = "aws:PrincipalTag/AdminBasis"
        values   = ["true"]
      }
    }
  }

  statement {
    sid    = "DenyOtherDestructiveActions"
    effect = "Deny"
    actions = [
      "s3:PutBucketPolicy",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketVersioning",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:PutKeyPolicy",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "disposition" {
  name   = "${local.name_prefix}-disposition"
  role   = aws_iam_role.disposition.id
  policy = data.aws_iam_policy_document.disposition.json

}

# ---------------------------------------------------------------------------
# Psychotherapy notes disposition role (2026-08-17 audit, C4 - disposal
# completeness)
#
# The psychotherapy twin of disposition above - structurally identical,
# scoped entirely to the psychotherapy bucket, reusing the same
# var.enable_admin_order_purge opt-in (not a second variable: one
# organizational decision about whether admin-order early removal is
# available at all, applied consistently across both buckets, rather
# than a second toggle an operator could set inconsistently between
# them without noticing). NO KMS permissions at all, same reasoning as
# disposition above - deciding what to delete needs retention metadata,
# never note content.
#
# FOUND AND FIXED: before this role existed, NOTHING in this file held
# any delete grant on the psychotherapy bucket - psychotherapy_ingest
# above is write-only and psychotherapy_restore is explicitly
# DenyWritesAndDeletes. The most access-restricted data class in this
# system had no disposal path whatsoever, in code or in IAM - see
# core/fhir/psychotherapy_purge.py and runbooks/RUNBOOK_DISPOSITION.md.
#
# No Postgres grant here, unlike disposition above: psychotherapy notes
# are deliberately never indexed and never ETL'd into OMOP (see
# runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md's "Why the Postgres index
# never sees this data") - there is no derived row to clean up.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "psychotherapy_disposition" {
  name               = "${local.name_prefix}-psychotherapy-disposition"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = "PHI AI Platform psychotherapy notes disposition: deletes notes whose retention has expired (routine), or specifically-named notes under a stated administrative basis when var.enable_admin_order_purge is true (exceptional). MFA required. Cannot decrypt notes under any circumstance."

  # AWS minimum; see psychotherapy_restore above. The 30-minute session is
  # requested via DurationSeconds in core/fhir/psychotherapy_purge.py.
  max_session_duration = 3600

  tags = { Role = "phi-ai-psychotherapy-disposition" }
}

data "aws_iam_policy_document" "psychotherapy_disposition" {
  statement {
    sid    = "InspectPsychotherapyRetention"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.psychotherapy.arn, "${aws_s3_bucket.psychotherapy.arn}/notes/*"]
  }

  statement {
    sid       = "DeletePsychotherapyObjects"
    effect    = "Allow"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
  }

  statement {
    sid       = "AppendAuditRecords"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/audit/*"]
  }

  statement {
    sid       = "UseAuditKey"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = [local.audit_key_arn]
  }

  statement {
    sid       = "DenyPsychotherapyKeyUse"
    effect    = "Deny"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.psychotherapy.arn]
  }

  dynamic "statement" {
    for_each = var.enable_admin_order_purge ? [1] : []
    content {
      sid       = "AllowAdminOrderDelete"
      effect    = "Allow"
      actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_admin_order_purge ? [1] : []
    content {
      sid       = "DenyAdminOrderDeleteWithoutAdminBasis"
      effect    = "Deny"
      actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.psychotherapy.arn}/notes/*"]
      condition {
        test     = "Null"
        variable = "aws:PrincipalTag/AdminBasis"
        values   = ["true"]
      }
    }
  }

  statement {
    sid    = "DenyOtherDestructiveActions"
    effect = "Deny"
    actions = [
      "s3:PutBucketPolicy",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketVersioning",
      "kms:ScheduleKeyDeletion",
      "kms:DisableKey",
      "kms:PutKeyPolicy",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "psychotherapy_disposition" {
  name   = "${local.name_prefix}-psychotherapy-disposition"
  role   = aws_iam_role.psychotherapy_disposition.id
  policy = data.aws_iam_policy_document.psychotherapy_disposition.json

}
# Made by Ryan Gomez & Co. Inc.
