# ---------------------------------------------------------------------------
# CloudTrail: the independent, out-of-band access log.
#
# The application's hash-chained audit log records what the PHI AI Platform
# *did*. CloudTrail records what the AWS APIs *were asked to do*, including
# calls made outside the application entirely (someone using the CLI, a
# compromised credential, an admin poking at the bucket).
#
# The incident response runbook depends on comparing the two: a read that
# appears in CloudTrail but not in the application audit log is evidence
# of out-of-band access, and that inference only works if both exist
# independently.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "cloudtrail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket        = "${local.name_prefix}-cloudtrail-${local.account_id}"
  force_destroy = var.environment == "dev" && var.force_destroy_buckets

  tags = {
    Name = "${local.name_prefix}-cloudtrail"
    Role = "cloudtrail-logs"
  }
}

resource "aws_s3_bucket_versioning" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count                   = var.enable_cloudtrail ? 1 : 0
  bucket                  = aws_s3_bucket.cloudtrail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = local.audit_key_arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "cloudtrail_bucket" {
  count = var.enable_cloudtrail ? 1 : 0

  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail[0].arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name_prefix}"]
    }
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${local.account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name_prefix}"]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.cloudtrail[0].arn, "${aws_s3_bucket.cloudtrail[0].arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # FIXED GAP (2026-08-17 audit, MEDIUM, "CloudTrail cross-check
  # limitations" - the "no TLS floor" sub-claim): this bucket previously
  # denied plaintext HTTP (DenyInsecureTransport above) but, unlike
  # s3_store.tf and s3_audit.tf, had no floor on the TLS *version*
  # itself, and no encryption-enforcement statements at all - the exact
  # asymmetry s3_audit.tf's own DenyOutdatedTLS/DenyWrongEncryptionKey/
  # DenyUnencryptedUploads/DenyMissingEncryptionHeader comment already
  # flagged has "no principled justification" when it closed the same
  # gap between the store and audit buckets. This bucket holds the
  # independent, out-of-band evidentiary record RUNBOOK_INCIDENT_RESPONSE.md
  # and RUNBOOK_INDEX_MAINTENANCE.md cross-reference against the
  # application audit log - it has at least as much reason to carry the
  # same floor as the audit log it sits alongside, not less. The four
  # statements below mirror s3_audit.tf's audit_bucket_policy exactly,
  # adapted only to reference this bucket's own ARN; the encryption key
  # check uses local.audit_key_arn because aws_cloudtrail.main's own
  # kms_key_id (above) is set to that same key, matching how CloudTrail
  # actually delivers log files here.
  statement {
    sid    = "DenyOutdatedTLS"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.cloudtrail[0].arn, "${aws_s3_bucket.cloudtrail[0].arn}/*"]
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
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/*"]
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
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/*"]
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  statement {
    sid    = "DenyMissingEncryptionHeader"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/*"]
    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["true"]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = data.aws_iam_policy_document.cloudtrail_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail ? 1 : 0

  name           = local.name_prefix
  s3_bucket_name = aws_s3_bucket.cloudtrail[0].id
  kms_key_id     = local.audit_key_arn

  # Digest files let you prove log files weren't modified after delivery --
  # the CloudTrail equivalent of the application's hash chain.
  enable_log_file_validation = true

  include_global_service_events = true
  is_multi_region_trail         = true

  # Data events are billed from the FIRST event ($0.10 per 100,000) - unlike
  # management events, there is no free first copy. Every PutObject and
  # GetObject on the store counts, so on a bulk ingestion run this is the
  # single largest CloudTrail line item.
  #
  # Scoped tightly to the three PHI-adjacent buckets rather than all of S3.
  # Losing this means losing the out-of-band record that
  # RUNBOOK_INCIDENT_RESPONSE.md compares against the application audit log
  # to spot access that bypassed the application - keep it on for real PHI.
  #
  # FIXED GAP: this selector previously listed only the store and audit
  # buckets. aws_s3_bucket.psychotherapy (s3_psychotherapy.tf) was added in
  # a later change and never added here - meaning the single most sensitive
  # bucket in this stack, the one built specifically to meet HIPAA
  # 164.508(a)(2)'s narrow-exception authorization standard, had NO
  # independent out-of-band record of object-level access at all. Someone
  # reading a psychotherapy note via the AWS CLI with a compromised
  # credential, entirely outside core/fhir/psychotherapy_restore.py, would
  # have left no CloudTrail data event to compare against the application
  # audit log - exactly the blind spot this selector exists to prevent for
  # the other two buckets.
  dynamic "advanced_event_selector" {
    for_each = var.cloudtrail_data_events ? [1] : []
    content {
      # Free-form label, not an identifier anything resolves by.
      # iam.tf's ReadCloudTrailLogFiles comment quotes this exact string,
      # so change the two together.
      name = "Store, audit, and psychotherapy bucket object-level events"

      field_selector {
        field  = "eventCategory"
        equals = ["Data"]
      }
      field_selector {
        field  = "resources.type"
        equals = ["AWS::S3::Object"]
      }
      field_selector {
        field = "resources.ARN"
        starts_with = [
          "${aws_s3_bucket.store.arn}/",
          "${aws_s3_bucket.audit.arn}/",
          "${aws_s3_bucket.psychotherapy.arn}/",
        ]
      }
    }
  }

  # Management events include every KMS Decrypt/GenerateDataKey call --
  # this is how you detect someone unwrapping DEKs outside the app.
  #
  # The FIRST copy of management events per region is free. This is only
  # billable ($2.00 per 100,000) if the account already has another trail
  # capturing management events in the same region - a real trap, since the
  # second trail silently starts billing for what looked free. Check with:
  #   aws cloudtrail describe-trails --query "trailList[].Name"
  advanced_event_selector {
    name = "All management events"

    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  # NOTE - the outside-dev enforcement of cloudtrail_data_events (and of
  # enable_cloudtrail itself) deliberately does NOT live here anymore.
  # FOUND AND FIXED (2026-08-17 audit, C5): a precondition on this
  # resource only ever evaluated when the trail existed - with
  # enable_cloudtrail=false this resource has count 0, so a prod apply
  # with no CloudTrail at all sailed through every guard about
  # CloudTrail, silently. Both requirements are now validation blocks on
  # their variables (variables.tf), evaluated on every plan regardless
  # of any resource's count.

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
# Made by Ryan Gomez & Co. Inc.
