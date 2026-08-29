# ---------------------------------------------------------------------------
# Terraform state backend bootstrap.
#
# Chicken-and-egg: the S3 backend that stores state must itself exist
# before you can use it. Run this ONCE, with local state, then configure
# the backend block in ../versions.tf and migrate.
#
# Keep this stack's own state file (terraform.tfstate in this directory)
# somewhere safe -- it is small and rarely changes, but losing it means
# losing Terraform's record of the state bucket.
# ---------------------------------------------------------------------------

terraform {
  # 1.10+ required for S3-native state locking (use_lockfile), which removes
  # the need for a DynamoDB lock table entirely.
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "force_destroy_state_bucket" {
  description = <<-EOT
    Allow `terraform destroy` to empty and delete the Terraform state
    bucket, including every prior state version.

    Defaults to false. This is the only thing left standing between a
    stray `terraform destroy` in this directory and the loss of
    Terraform's record of the whole deployment - the PHI store itself
    survives, but becomes orphaned resources you can only reclaim by
    importing each one by hand. Set true deliberately, tear down, set it
    back.
  EOT
  type        = bool
  default     = false
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "tfstate" {
  # This bucket does not hold PHI; it holds TERRAFORM'S OWN STATE, which
  # makes its name a different kind of commitment than a data bucket's.
  # Once the main stack has been initialized against it, changing this
  # string does not move the state file - it points Terraform at a bucket
  # that does not exist, so the next `init`/`plan` reads an EMPTY state
  # and offers to CREATE a second copy of every bucket, key, database and
  # role, while the real ones keep existing, now unmanaged, still holding
  # PHI and still billing. Recovering means importing every resource by
  # hand. Set it before the first init, then leave it - and keep the
  # backend `key` in ../versions.tf in step with it.
  bucket = "phi-ai-tfstate-${data.aws_caller_identity.current.account_id}"

  # No `prevent_destroy`. This stack carries no undestroyable resources,
  # in keeping with the rest of the deployment.
  #
  # Understand what that leaves you exposed to, because it is NOT the same
  # risk as deleting the store. State is not PHI, but it is Terraform's
  # only record of which buckets, keys, and roles belong to this
  # deployment. Losing it does not delete the store - it ORPHANS it: the
  # resources keep existing and keep billing, and `terraform destroy` can
  # no longer find them. Recovering means importing every resource by hand.
  #
  # Versioning below means the bucket is never empty, so a destroy fails
  # unless force_destroy is set. That is the remaining guardrail.
  force_destroy = var.force_destroy_state_bucket
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      # SSE-S3, not SSE-KMS: state is not PHI, and a customer-managed key
      # here would add $1/month for no meaningful gain. SSE-S3 is free.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# FIXED: this bucket previously had no bucket policy at all - the only
# bucket in this entire project without a TLS-only enforcement statement,
# every other one (s3_store.tf, s3_audit.tf, s3_psychotherapy.tf,
# cloudtrail.tf) denies non-TLS access explicitly. State isn't PHI, but
# per the comment on aws_s3_bucket.tfstate above, it maps the whole
# deployment's resource identifiers and ARNs - worth the same
# one-statement floor the rest of this stack already applies everywhere
# else, for consistency and because there's no real cost or downside to
# adding it.
data "aws_iam_policy_document" "tfstate_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Matches the DenyOutdatedTLS statement every other bucket in this
  # project carries (s3_store.tf, s3_audit.tf, s3_psychotherapy.tf).
  # DenyInsecureTransport above only rejects plaintext HTTP; without this,
  # a client negotiating TLS 1.0/1.1 still gets through.
  statement {
    sid    = "DenyOutdatedTLS"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }

  # DELIBERATELY NOT PORTED from the other buckets: their
  # DenyWrongEncryptionKey / DenyUnencryptedUploads /
  # DenyMissingEncryptionHeader statements. Those assert SSE-KMS with a
  # specific customer-managed key; this bucket uses SSE-S3 (AES256) on
  # purpose - see the encryption block above - so a copied-over KMS
  # condition would deny every write.
  #
  # An AES256-flavoured equivalent is also omitted, and that omission is
  # the deliberate part: this is the bucket holding Terraform's own
  # state, written by Terraform's S3 backend. A deny that the backend
  # does not happen to satisfy on every request locks Terraform out of
  # its own state, which is not a failure you can fix with Terraform.
  # Default encryption on the bucket already guarantees objects land
  # encrypted regardless of what any individual request asks for, so the
  # deny would add no real protection for a genuine lockout risk.
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# NOTE: no DynamoDB lock table.
#
# Terraform 1.10+ supports S3-native state locking via `use_lockfile = true`
# in the backend config, which writes a .tflock object alongside the state
# file. That removes a whole billable resource: DynamoDB on-demand billing
# starts from the first request, and while the cost is pennies, it is a
# second service to provision, secure, and remember to delete.
#
# If you are pinned to Terraform < 1.10, add back an aws_dynamodb_table with
# hash_key "LockID" and set dynamodb_table in the backend block instead.

output "state_bucket" {
  value       = aws_s3_bucket.tfstate.id
  description = "Set as `bucket` in the backend block in ../versions.tf."
}
# Made by Ryan Gomez & Co. Inc.
