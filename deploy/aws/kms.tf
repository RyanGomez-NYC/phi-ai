# ---------------------------------------------------------------------------
# KMS customer-managed keys
#
# Three keys:
#   - store key:        wraps DEKs for general stored PHI
#   - audit key:           encrypts the audit log
#   - psychotherapy key:   wraps DEKs for psychotherapy notes specifically
#
# The psychotherapy key exists for the same reason the audit key does -
# separating it means a compromise of the general store key's grant
# does not also expose psychotherapy notes, and vice versa. See
# s3_psychotherapy.tf and runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for why
# this data gets a genuinely separate boundary rather than a flag within
# the general store - 45 CFR 164.508(a)(2) requires authorization for
# nearly any use or disclosure of psychotherapy notes, unlike the rest of
# the record, and HIPAA's own definition of these notes specifically
# requires they be "maintained separate...in a different location," not
# just distinguished by metadata within the same store.
#
# Unlike separate_audit_key, this key is NOT conditionally optional on
# cost grounds. The $1/month is not a trade-off this codebase offers to
# skip for this specific boundary.
#
# COST: each customer-managed key is $1.00/month with no free-tier
# allowance, rising toward a $3/key/month cap after two rotations. See
# docs/COST.md.
#
# NOTE ON KEY POLICY: the account-root statement below is required. Without
# it, a KMS key can become permanently unmanageable - IAM policies alone
# cannot grant access to a key whose own key policy doesn't delegate to
# IAM. Do not remove it in the name of tightening access.
# ---------------------------------------------------------------------------

locals {
  account_id  = data.aws_caller_identity.current.account_id
  partition   = data.aws_partition.current.partition
  name_prefix = "${var.name_prefix}-${var.environment}"

  root_arn = "arn:${local.partition}:iam::${local.account_id}:root"

  # Resolves to the dedicated audit key when separate_audit_key is true,
  # otherwise falls back to the store key. Every downstream reference uses
  # this local, so the rest of the stack does not need to branch.
  audit_key_arn = var.separate_audit_key ? aws_kms_key.audit[0].arn : aws_kms_key.store.arn
}

# --- Store data key ------------------------------------------------------

resource "aws_kms_key" "store" {
  description = "PHI AI Platform (${var.environment}) - wraps data encryption keys for stored PHI"

  # Annual automatic rotation. Rotation creates new key material while
  # retaining old material, so previously wrapped DEKs remain unwrappable
  # -- existing stored objects stay readable across rotations.
  #
  # Cost: each retained rotation version adds to the monthly key charge,
  # capped at $3/key/month after two rotations. Disabling is only sensible
  # for short-lived dev stacks; the precondition below enforces rotation
  # outside dev.
  enable_key_rotation = var.enable_key_rotation

  # Deletion window: the maximum, because deleting this key
  # cryptographically destroys every object it wraps. A long window is the
  # last line of defense against an accidental or malicious scheduled
  # deletion.
  deletion_window_in_days = 30

  policy = data.aws_iam_policy_document.store_key_policy.json

  lifecycle {
    precondition {
      condition     = var.environment == "dev" || var.enable_key_rotation
      error_message = "enable_key_rotation must be true outside dev. Key rotation is a baseline key-management control and the savings (a few dollars a month) do not justify disabling it for real PHI."
    }
  }

  tags = {
    Name = "${local.name_prefix}-store-key"
    Role = "phi-data-encryption"
  }
}

resource "aws_kms_alias" "store" {
  # A KMS alias name is the identifier callers and operator tooling
  # resolve the key by. Runbooks and any external tooling that looks the
  # key up by alias must use this exact string.
  name          = "alias/${local.name_prefix}-store"
  target_key_id = aws_kms_key.store.key_id
}

data "aws_iam_policy_document" "store_key_policy" {
  # Required: delegate to IAM so the key remains manageable.
  statement {
    sid    = "EnableIAMPolicies"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [local.root_arn]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  # Allow S3 to use the key for server-side encryption on our behalf.
  statement {
    sid    = "AllowS3ServiceUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions = [
      "kms:GenerateDataKey*",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  # When the audit key is shared with the store key (dev cost saving),
  # CloudTrail needs to encrypt log files with this key too. Emitted only in
  # that configuration so the production key policy stays minimal.
  dynamic "statement" {
    for_each = var.separate_audit_key ? [] : [1]
    content {
      sid    = "AllowCloudTrailUseWhenKeyShared"
      effect = "Allow"
      principals {
        type        = "Service"
        identifiers = ["cloudtrail.amazonaws.com"]
      }
      actions = [
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
      ]
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [local.account_id]
      }
    }
  }

  # Defense in depth: refuse any KMS call not made over TLS.
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- Audit log key ---------------------------------------------------------

# Separate key for the audit log. Optional purely on cost grounds: a second
# CMK is another $1-3/month. See the separate_audit_key variable for the
# security property being traded away.
resource "aws_kms_key" "audit" {
  count = var.separate_audit_key ? 1 : 0

  description             = "PHI AI Platform (${var.environment}) - encrypts the tamper-evident audit log"
  enable_key_rotation     = var.enable_key_rotation
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.audit_key_policy[0].json

  # NOTE - the outside-dev enforcement of separate_audit_key deliberately
  # does NOT live here anymore. FOUND AND FIXED (2026-08-17 audit, C5):
  # a precondition on this resource was unfalsifiable - with
  # separate_audit_key=false this resource has count 0, so its
  # preconditions were simply never evaluated, and the exact prod
  # configuration the guard existed to refuse (shared key, ingest role
  # gaining kms:Decrypt on stored PHI via local.audit_key_arn) applied
  # cleanly. With the variable true, the condition was trivially
  # satisfied. Unfalsifiable in both states. The guard is now a
  # validation block on var.separate_audit_key itself (variables.tf),
  # which Terraform evaluates on every plan regardless of any resource's
  # count. The rotation rule needs no twin here: aws_kms_key.store's
  # own always-evaluated precondition enforces enable_key_rotation for
  # the shared variable.

  tags = {
    Name = "${local.name_prefix}-audit-key"
    Role = "audit-log-encryption"
  }
}

resource "aws_kms_alias" "audit" {
  count = var.separate_audit_key ? 1 : 0

  name          = "alias/${local.name_prefix}-audit"
  target_key_id = aws_kms_key.audit[0].key_id
}

data "aws_iam_policy_document" "audit_key_policy" {
  count = var.separate_audit_key ? 1 : 0

  statement {
    sid    = "EnableIAMPolicies"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [local.root_arn]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowS3ServiceUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions = [
      "kms:GenerateDataKey*",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  # CloudTrail needs to encrypt log files with this key.
  statement {
    sid    = "AllowCloudTrailUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions = [
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- Psychotherapy notes key -------------------------------------------

# Always provisioned, not conditional - see the module-level comment
# above for why this specific boundary isn't offered as a cost trade-off.
resource "aws_kms_key" "psychotherapy" {
  description              = "PHI AI Platform (${var.environment}) - wraps data encryption keys for psychotherapy notes specifically, separate from the general store key"
  enable_key_rotation      = var.enable_key_rotation
  deletion_window_in_days  = 30
  policy                   = data.aws_iam_policy_document.psychotherapy_key_policy.json

  lifecycle {
    precondition {
      condition     = var.environment == "dev" || var.enable_key_rotation
      error_message = "enable_key_rotation must be true outside dev - same reasoning as the store key."
    }
  }

  tags = {
    Name = "${local.name_prefix}-psychotherapy-key"
    Role = "psychotherapy-data-encryption"
  }
}

resource "aws_kms_alias" "psychotherapy" {
  name          = "alias/${local.name_prefix}-psychotherapy"
  target_key_id = aws_kms_key.psychotherapy.key_id
}

data "aws_iam_policy_document" "psychotherapy_key_policy" {
  statement {
    sid    = "EnableIAMPolicies"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [local.root_arn]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowS3ServiceUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions = [
      "kms:GenerateDataKey*",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
# Made by Ryan Gomez & Co. Inc.
