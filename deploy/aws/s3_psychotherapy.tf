# ---------------------------------------------------------------------------
# Psychotherapy notes bucket: a separate storage boundary (45 CFR 164.508(a)(2)).
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
# KNOWN GAP, and a one-way door: S3 Object Lock cannot be added to a
# bucket after creation. This bucket is created without it, so adopting
# an enforced retention control later means a new bucket and a full
# object migration, not an edit here.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "psychotherapy" {
  bucket = "${local.name_prefix}-psychotherapy-${local.account_id}"

  # FOUND AND FIXED: this read `force_destroy = var.force_destroy_buckets`
  # with no environment gate, and the comment here defended that as
  # deliberate - "with Object Lock gone there is no storage-layer reason a
  # non-dev bucket should be harder to tear down than a dev one." That
  # reasoning inverts the actual risk. The store, audit and CloudTrail
  # buckets all AND this flag with `environment == "dev"` (s3_store.tf,
  # s3_audit.tf, cloudtrail.tf), so the bucket holding psychotherapy notes
  # - the single most access-restricted data class in this system, needing
  # its own specific authorization under 45 CFR 164.508(a)(2) even where
  # other PHI does not - had the WEAKEST accidental-destruction guard in
  # the entire stack. An operator who set force_destroy_buckets = true on a
  # prod stack would have found the general store protected by its gate
  # and these notes not.
  #
  # Now gated to dev, matching s3_store.tf exactly. With Object Lock gone
  # this flag is the ONLY thing between `terraform destroy` and the
  # contents of this bucket - which is an argument for the gate, not
  # against it. variables.tf documents `force_destroy_buckets` as honored
  # only in dev; wiring it unconditionally here broke that promise for the
  # one bucket where breaking it mattered most.
  force_destroy = var.environment == "dev" && var.force_destroy_buckets

  lifecycle {
    # Same reasoning as aws_s3_bucket.store in s3_store.tf: no
    # hardcoded legal-minimum floor, only a guard against the dev
    # default being carried unedited into a real deployment. This bucket
    # reuses the same retention figure as the general store rather than
    # declaring a second one.
    precondition {
      condition     = var.environment == "dev" || var.phi_retention_days > 1
      error_message = "phi_retention_days is still at the dev default (1 day) for a non-dev environment. This bucket reuses the same retention figure as the general store - see docs/COMPLIANCE.md."
    }
    # force_destroy is now silently ANDed to false outside dev, so an
    # operator who set it expecting it to work would never find out. Fail
    # loudly instead - the same precondition s3_store.tf already carries,
    # added here so the most sensitive bucket in the stack is not the one
    # that stays silent about a misconfiguration the others shout about.
    precondition {
      condition     = var.environment == "dev" || !var.force_destroy_buckets
      error_message = "force_destroy_buckets = true is refused outside a dev environment. It has no effect on the psychotherapy notes bucket in a non-dev stack (it ANDs the flag with environment == \"dev\", matching the store/audit/CloudTrail buckets); with Object Lock removed it would otherwise let a single `terraform destroy` erase psychotherapy notes held under 45 CFR 164.508(a)(2). Leave it false for any real deployment."
    }
  }

  tags = {
    Name = "${local.name_prefix}-psychotherapy"
    Role = "psychotherapy-notes"
  }
}

resource "aws_s3_bucket_versioning" "psychotherapy" {
  bucket = aws_s3_bucket.psychotherapy.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "psychotherapy" {
  bucket = aws_s3_bucket.psychotherapy.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.psychotherapy.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "psychotherapy" {
  bucket                  = aws_s3_bucket.psychotherapy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "psychotherapy_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.psychotherapy.arn,
      "${aws_s3_bucket.psychotherapy.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # FIXED GAP: added to match s3_store.tf's store_bucket_policy,
  # which already had this statement - this bucket, holding the single
  # most access-restricted category of data in this system, should not
  # have a WEAKER transmission-security floor than the general store.
  statement {
    sid    = "DenyOutdatedTLS"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.psychotherapy.arn,
      "${aws_s3_bucket.psychotherapy.arn}/*",
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
      "${aws_s3_bucket.psychotherapy.arn}/*",
    ]
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.psychotherapy.arn]
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
      "${aws_s3_bucket.psychotherapy.arn}/*",
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
      "${aws_s3_bucket.psychotherapy.arn}/*",
    ]
    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["true"]
    }
  }

  # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "PurposeOfUse/psychotherapy
  # tag enforcement lives only in role policies, not bucket/key
  # policies"): the same gap as s3_store.tf's DenyReadWithoutPurposeOfUse
  # (see that file's comment for the full reasoning), but for this
  # bucket it is worse - this holds the single most access-restricted
  # data class in the system (45 CFR 164.508(a)(2)), and
  # psychotherapy_restore's three role-level deny statements
  # (DenyReadWithoutValidException, DenyReadWithoutExceptionTag,
  # DenyReadWithoutAttestation - iam.tf) only ever evaluate for callers
  # who assumed that specific role. Any other IAM identity holding its
  # own s3:GetObject + kms:Decrypt on this bucket could read
  # psychotherapy notes with none of the three required attestation
  # tags, and nothing at the bucket level would refuse it. Mirrored here
  # as three bucket-policy statements, matching the role-level ones
  # exactly in substance (StringNotEquals against the three permitted
  # exception values, plus two Null checks for the exception and
  # attestation tags respectively).
  #
  # Exempts aws_iam_role.psychotherapy_ingest and
  # aws_iam_role.psychotherapy_disposition by ARN, same reasoning as
  # s3_store.tf: psychotherapy_ingest's CheckExistingPsychotherapyObjects
  # and psychotherapy_disposition's InspectPsychotherapyRetention (both
  # iam.tf) legitimately call s3:GetObject for HeadObject-only metadata
  # checks and carry none of these tags - and neither role holds
  # kms:Decrypt on aws_kms_key.psychotherapy (WrapPsychotherapyDataKeys
  # on ingest grants GenerateDataKey/DescribeKey only; disposition's
  # DenyPsychotherapyKeyUse denies it outright), so a blanket deny would
  # break two already-safe flows without closing any real gap - those
  # roles' GetObject calls can only ever return ciphertext they cannot
  # read as PHI regardless of this bucket policy.
  statement {
    sid    = "DenyReadWithoutValidException"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.psychotherapy.arn}/notes/*",
    ]
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalTag/PsychotherapyException"
      values   = ["originator-treatment", "training-program", "legal-defense"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.psychotherapy_ingest.arn,
        aws_iam_role.psychotherapy_disposition.arn,
      ]
    }
  }

  statement {
    sid    = "DenyReadWithoutExceptionTag"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.psychotherapy.arn}/notes/*",
    ]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PsychotherapyException"
      values   = ["true"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.psychotherapy_ingest.arn,
        aws_iam_role.psychotherapy_disposition.arn,
      ]
    }
  }

  statement {
    sid    = "DenyReadWithoutAttestation"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.psychotherapy.arn}/notes/*",
    ]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PsychotherapyAttestation"
      values   = ["true"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.psychotherapy_ingest.arn,
        aws_iam_role.psychotherapy_disposition.arn,
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "psychotherapy" {
  bucket = aws_s3_bucket.psychotherapy.id
  policy = data.aws_iam_policy_document.psychotherapy_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.psychotherapy]
}
# Made by Ryan Gomez & Co. Inc.
