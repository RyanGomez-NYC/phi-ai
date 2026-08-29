# ---------------------------------------------------------------------------
# PHI store bucket: holds envelope-encrypted PHI ciphertext.
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

resource "aws_s3_bucket" "store" {
  # S3 bucket names are globally unique and immutable, so this string is
  # fixed for the life of the bucket once applied. The account ID suffix
  # is what makes it globally unique.
  bucket = "${local.name_prefix}-store-${local.account_id}"


  # Gated to dev, and deliberately so. With Object Lock gone this flag is
  # the ONLY thing between `terraform destroy` and every stored PHI
  # object - there is no storage-layer backstop left to catch a mistake.
  # `force_destroy_buckets` is documented (variables.tf) as honored only
  # in dev; wiring it unconditionally here broke that promise and let a
  # single prod `destroy` erase the store irrecoverably. Matches the
  # same gate cloudtrail.tf already applies to the trail bucket.
  force_destroy = var.environment == "dev" && var.force_destroy_buckets

  lifecycle {
    # Retention values stay genuinely deployer-configurable in every
    # environment, including prod - see their descriptions in
    # variables.tf. US state medical record retention law varies widely
    # (Florida's physician-record minimum is 5 years, which an earlier,
    # stricter 6-year floor here would have blocked a Florida deployer
    # from setting correctly), and Terraform cannot safely encode 50
    # states' law as one number. A hardcoded floor asserting otherwise
    # creates false confidence rather than real compliance - all the more
    # so now, since nothing enforces these values anyway.
    #
    # The one thing still worth catching: the dev DEFAULT (1 day) carried
    # unchanged into a real deployment, which is far more likely an
    # unedited template than anyone's actual state-law figure.
    precondition {
      condition     = var.environment == "dev" || var.phi_retention_days > 1
      error_message = "phi_retention_days is still at the dev default (1 day) for a non-dev environment. Set it explicitly from the applicable state/federal retention requirement for your data - see docs/COMPLIANCE.md. There is no universal correct value here; do not silence this by copying in an arbitrary number without checking your own state's statute."
    }
    # force_destroy is silently ANDed to false outside dev, so an operator
    # who set it expecting it to work would never find out. Fail loudly
    # instead: a request to make the PHI store tear-downable in a
    # non-dev stack is almost certainly a mistake, and a silent no-op hides
    # both the mistake and the fact that the guard is doing its job.
    precondition {
      condition     = var.environment == "dev" || !var.force_destroy_buckets
      error_message = "force_destroy_buckets = true is refused outside a dev environment. It has no effect on the store/audit/CloudTrail/psychotherapy-notes buckets in a non-dev stack (they each AND it with environment == \"dev\"); with Object Lock removed it would otherwise let a single `terraform destroy` erase stored PHI, the psychotherapy notes held under 45 CFR 164.508(a)(2), and the audit trail recording access to both. Leave it false for any real deployment."
    }
  }

  tags = {
    Name = "${local.name_prefix}-store"
    Role = "phi-ai-data"
  }
}

resource "aws_s3_bucket_versioning" "store" {
  bucket = aws_s3_bucket.store.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "store" {
  bucket = aws_s3_bucket.store.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.store.arn
    }
    # Reduces KMS API calls (and cost) by caching the data key for
    # repeated writes to the same bucket. Does not weaken the encryption.
    #
    # Note this affects only S3's own SSE-KMS calls. The application's
    # envelope encryption still calls GenerateDataKey once per stored
    # object, which is what makes KMS request cost scale with record count.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "store" {
  bucket                  = aws_s3_bucket.store.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "store" {
  bucket = aws_s3_bucket.store.id

  # Cold-tier transitions are OFF by default, and that is a cost decision,
  # not a capability gap.
  #
  # STANDARD_IA and GLACIER_IR both bill a 128 KB MINIMUM PER OBJECT
  # regardless of the object's real size. Individual FHIR resources are
  # typically 1-10 KB, so transitioning them bills each at 128 KB - often
  # 15-100x their actual size - and adds a per-object transition request
  # charge on top. For a store of many small JSON resources, tiering down
  # costs more than staying in S3 Standard.
  #
  # Enable this only for large objects (DocumentReference attachments,
  # imaging, scanned PDFs), or after aggregating small resources into
  # larger bundles. See docs/COST.md.
  dynamic "rule" {
    for_each = var.enable_lifecycle_transitions ? [1] : []
    content {
      id     = "tier-down-cold-store-data"
      status = "Enabled"

      filter {
        prefix = "fhir/"
      }

      transition {
        days          = 90
        storage_class = "STANDARD_IA"
      }

      transition {
        days          = 365
        storage_class = "GLACIER_IR" # instant retrieval: keeps restores fast
      }

      # Deliberately NO expiration rule. Deletion of stored PHI is a
      # policy decision requiring documented disposition, not something a
      # lifecycle rule should do silently.
    }
  }

  # Clean up incomplete multipart uploads, which otherwise accrue storage
  # cost invisibly.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.store]
}

# --- Bucket policy: hard requirements enforced at the bucket, not the app ---

data "aws_iam_policy_document" "store_bucket_policy" {
  # HIPAA 164.312(e)(1) transmission security: reject anything not over TLS.
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.store.arn,
      "${aws_s3_bucket.store.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Reject TLS versions below 1.2.
  statement {
    sid    = "DenyOutdatedTLS"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.store.arn,
      "${aws_s3_bucket.store.arn}/*",
    ]
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }

  # Reject any upload that isn't encrypted with OUR key. Prevents a
  # misconfigured client from writing PHI under SSE-S3 or unencrypted.
  statement {
    sid    = "DenyWrongEncryptionKey"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.store.arn}/*",
    ]
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.store.arn]
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
      "${aws_s3_bucket.store.arn}/*",
    ]
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  # FIXED GAP: StringNotEquals above only fires when the encryption
  # header IS PRESENT but wrong - confirmed against AWS's own published
  # guidance ("How to Prevent Uploads of Unencrypted Objects to Amazon
  # S3," AWS Security Blog), which pairs that exact StringNotEquals
  # statement with a separate Null-based one specifically because a
  # request that OMITS the header entirely does not satisfy
  # StringNotEquals's match condition at all - the statement simply
  # doesn't evaluate, so it can't deny. In practice this bucket's own
  # default encryption (aws_s3_bucket_server_side_encryption_configuration.store,
  # above) already applies SSE-KMS with this same key to any request
  # that omits the header, so the object itself still ends up correctly
  # encrypted - but that means this policy's protection was silently
  # contingent on that separate default-encryption resource staying
  # correctly configured, rather than being the independent guarantee a
  # bucket-policy-level deny is supposed to provide. A future change
  # that altered or removed the default encryption config, combined with
  # a client that also omits the header, would previously have produced
  # a genuinely unencrypted write with nothing at the bucket-policy layer
  # to stop it.
  statement {
    sid    = "DenyMissingEncryptionHeader"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.store.arn}/*",
    ]
    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["true"]
    }
  }

  # FOUND AND FIXED (2026-08-17 audit, MEDIUM, "PurposeOfUse enforcement
  # lives only in role policies, not bucket/key policies"): iam.tf's
  # restore role has a DenyReadWithoutPurposeOfUse statement, but that
  # statement lives in the restore role's OWN policy, so it only ever
  # evaluates for callers who assumed that specific role. iam.tf's
  # comment on it ("holds even if someone bypasses the app") was true
  # only for someone bypassing the app while still using the restore
  # role's credentials - a separate IAM identity holding its own
  # s3:GetObject + kms:Decrypt grants on this bucket (a misconfigured
  # policy, an over-broad admin role, anything not modeled here) could
  # read stored PHI with no PurposeOfUse tag at all, and nothing in
  # this stack would refuse it. A bucket policy evaluates for EVERY
  # principal regardless of which IAM identity or role they hold, which
  # is what closes that gap for real.
  #
  # Deliberately NOT a blanket copy of the restore role's statement:
  # aws_iam_role.ingest's CheckExistingObjects and
  # aws_iam_role.disposition's InspectStoreRetention (both iam.tf)
  # legitimately call s3:GetObject against this prefix for HeadObject-only
  # metadata checks (idempotency-before-write, retention-before-delete)
  # and carry no PurposeOfUse tag at all - a blanket deny would break
  # both of those legitimate, already-safe flows. "Already-safe" is not
  # incidental: neither role holds kms:Decrypt on aws_kms_key.store (see
  # WrapDataKeys on ingest and the "No decrypt of store PHI, ever"
  # comment on disposition, both iam.tf), so their GetObject calls can
  # only ever return ciphertext they cannot read as PHI regardless of
  # this bucket policy - excluding them here does not reopen any access
  # this stack doesn't already grant them by design. Every OTHER
  # principal - the actual threat this finding describes - is denied
  # unless tagged, exactly like the role-level statement already
  # requires for the restore role itself.
  statement {
    sid    = "DenyReadWithoutPurposeOfUse"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.store.arn}/fhir/*",
    ]
    condition {
      test     = "Null"
      variable = "aws:PrincipalTag/PurposeOfUse"
      values   = ["true"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.ingest.arn,
        aws_iam_role.disposition.arn,
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "store" {
  bucket = aws_s3_bucket.store.id
  policy = data.aws_iam_policy_document.store_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.store]
}
# Made by Ryan Gomez & Co. Inc.
