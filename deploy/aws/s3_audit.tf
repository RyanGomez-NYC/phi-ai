# ---------------------------------------------------------------------------
# Audit bucket: stores the hash-chained application audit log.
#
# NO INFRASTRUCTURE IMMUTABILITY. Provisioned deliberately WITHOUT S3
# Object Lock. Nothing at the storage layer prevents an object here from
# being overwritten or deleted by a principal holding the IAM permission
# to do so. What remains is versioning (a prior version survives an
# overwrite), the recorded SHA-256 digest, the hash-chained audit log, and
# CloudTrail data events. Those make change VISIBLE, not IMPOSSIBLE.
#
# Retention is configuration, not enforcement - see the Retention section
# of variables.tf, and core/config/retention_rules.py for the ruleset
# mechanism that supersedes a single flat number. It is recorded on each
# object as metadata and drives documented disposition
# (runbooks/RUNBOOK_DISPOSITION.md). It stops nobody from deleting early,
# and no lifecycle rule deletes anything automatically.
#
# Object Lock cannot be added to an existing bucket, so restoring an
# enforced retention control means recreating this bucket and migrating
# every object.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "audit" {
  bucket = "${local.name_prefix}-audit-${local.account_id}"
  # Gated to dev, and deliberately so. With Object Lock gone this flag is
  # the ONLY thing between `terraform destroy` and the entire audit trail
  # - an audit log a prod `destroy` can erase is not an audit log.
  # `force_destroy_buckets` is documented (variables.tf) as honored only
  # in dev; wiring it unconditionally here broke that promise. Matches the
  # same gate cloudtrail.tf already applies to the trail bucket.
  force_destroy = var.environment == "dev" && var.force_destroy_buckets

  tags = {
    Name = "${local.name_prefix}-audit"
    Role = "audit-log"
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = local.audit_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "audit_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.audit.arn,
      "${aws_s3_bucket.audit.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # FIXED GAP: this bucket previously had no TLS-version floor or
  # encryption-enforcement statements at all, unlike s3_store.tf's
  # policy - an asymmetry with no principled justification. The audit
  # bucket is written to by the exact same kind of controlled
  # application code (core/audit/sink.py's S3AuditSink) that writes to
  # the store bucket, holds its own customer-managed KMS key, and
  # carries HIPAA §164.312(e)(1) transmission-security relevance of its
  # own - it records access PATTERNS to PHI even when it doesn't hold
  # PHI content itself, which is exactly the kind of metadata a
  # HIPAA-regulated entity has reason to protect in transit and at rest
  # with the same rigor. The four statements below mirror
  # s3_store.tf's store_bucket_policy exactly, adapted only to
  # reference this bucket's own ARN and KMS key.
  statement {
    sid    = "DenyOutdatedTLS"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.audit.arn,
      "${aws_s3_bucket.audit.arn}/*",
    ]
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }

  statement {
    sid    = "DenyWrongEncryptionKey"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.audit.arn}/*",
    ]
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [local.audit_key_arn]
    }
  }

  statement {
    sid    = "DenyUnencryptedUploads"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.audit.arn}/*",
    ]
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  # Same fix as s3_store.tf's DenyMissingEncryptionHeader - see that
  # file's comment for the full AWS-documentation-backed reasoning.
  # StringNotEquals alone does not catch a request that omits the
  # encryption header entirely; this Null-based statement does.
  statement {
    sid    = "DenyMissingEncryptionHeader"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.audit.arn}/*",
    ]
    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["true"]
    }
  }

  # NOTE: a "DenyAuditLogDeletion" statement stood here, denying
  # s3:DeleteObject / s3:DeleteObjectVersion / s3:PutLifecycleConfiguration
  # to every principal except the account root. It is gone, along with the
  # rest of this deployment's storage-layer immutability.
  #
  # Be clear about what that costs, because this was the LAST resource-level
  # protection on the audit log: audit records are now deletable by any
  # principal whose IAM policy allows it. The ingest, restore and auditor
  # roles are still denied delete in iam.tf, so this is not an open door -
  # but it is IAM scoping alone, with no bucket-level backstop behind it.
  #
  # What detects tampering now is the hash chain in core/audit/log.py: each
  # record commits to its predecessor, so removing or editing one breaks
  # verification from that point on. Versioning keeps the superseded object
  # and CloudTrail records the delete call. Detection, not prevention -
  # which only works if someone actually runs core/audit/verify.py on a
  # schedule rather than only after an incident.
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.audit]
}
# Made by Ryan Gomez & Co. Inc.
