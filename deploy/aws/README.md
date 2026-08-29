# AWS deployment

Terraform for the AWS PHI AI Platform stack. See
`runbooks/RUNBOOK_AWS_SETUP.md` for the step-by-step walkthrough - this
file is reference for what the stack contains and why.

## What gets created

| Resource | Purpose |
|---|---|
| `aws_s3_bucket.store` | Envelope-encrypted PHI ciphertext. Versioned, no Object Lock, SSE-KMS with a CMK, all public access blocked. |
| `aws_s3_bucket.audit` | Hash-chained audit log. Separate bucket, separate key, separate retention. No Object Lock, and no bucket policy denying deletion. |
| `aws_s3_bucket.psychotherapy` | Psychotherapy notes, in their own bucket under their own key - 45 CFR 164.508(a)(2). |
| `aws_s3_bucket.cloudtrail` | CloudTrail delivery, with log file validation on. |
| `aws_kms_key.store` | Wraps per-object data encryption keys. Rotation on, 30-day deletion window. |
| `aws_kms_key.audit` | Encrypts the audit log and CloudTrail. |
| `aws_kms_key.psychotherapy` | Wraps data keys for psychotherapy notes only. |
| `aws_iam_role.ingest` | Writes PHI, appends audit records. **No `kms:Decrypt` on the `aws_kms_key.store` key.** |
| `aws_iam_role.restore` | Reads and decrypts PHI for authorized requests. MFA + purpose-of-use tag required. |
| `aws_iam_role.auditor` | Reads the audit log. Explicitly denied all PHI access. |
| `aws_iam_role.disposition` | Deletes objects whose retention has expired. Cannot decrypt PHI. |
| `aws_db_instance.index` | Postgres index (`phi_ai_index`), derived and rebuildable from S3. |
| `aws_cloudtrail.main` | S3 data events + all management events (captures every KMS call). |

## Key design decisions

**Two KMS keys, not one.** A compromise of `aws_kms_key.store` access
shouldn't also confer the ability to rewrite the audit trail recording
that compromise.

**Three roles, not one.** The long-running ingestion service is the most
exposed component, so it holds `kms:GenerateDataKey` but not
`kms:Decrypt` - compromising it yields ciphertext, not patient data.
Decryption requires separately assuming the restore role with MFA.

**Postgres role names are IAM-load-bearing.** The `rds-db:connect` ARNs
in `iam.tf` name exactly three database roles - `phi_ai_ingest`,
`phi_ai_reader`, `phi_ai_disposition` - and those strings must equal the
roles `core/db/bootstrap_aws.sql` creates and the usernames
`outputs.tf`'s `env_fragment` emits. An ARN that names a role the
database does not have authenticates nothing; the failure is an IAM
AccessDenied before Postgres is ever reached, which does not look like a
naming problem. If you change one, change all three together.

**No Object Lock, in any environment.** No bucket in this stack - store,
audit, CloudTrail or psychotherapy - is created with
`object_lock_enabled`, none carries an
`aws_s3_bucket_object_lock_configuration`, and no write path sends
retention headers. Retention (`phi_retention_days`,
`audit_retention_days`) is a declared policy value recorded as object
metadata by application code: it is enforced by nothing. It does not
stop early deletion and does not trigger deletion at expiry. Integrity
here is detective (versioning, per-object SHA-256, the audit hash chain,
CloudTrail data events), not preventive. See docs/COMPLIANCE.md's
"Retention and integrity".

**What actually stands between a caller and deletion.** Two things, both
of which can be defeated by a sufficiently privileged principal:

1. *IAM scoping.* The ingest, restore and auditor roles are denied
   delete outright. The disposition role's delete grant is gated behind
   `enable_admin_order_purge`, which defaults to `false`.
2. *Bucket versioning.* A `DeleteObject` call with no version ID leaves
   a delete marker and the object body remains recoverable. That is the
   protection - and its limit. A caller who passes an explicit version
   ID, or who calls `delete_all_versions()`, destroys the content
   outright with nothing to recover.

**MFA delete is not configured** on any bucket, so a version-ID delete
needs no second factor beyond whatever the assumed role already required.

**Purpose-of-use enforced in IAM, not just in the app.** The restore role
denies `s3:GetObject` outright when the session lacks a `PurposeOfUse`
principal tag, so the minimum-necessary requirement holds even if someone
bypasses the application and uses the AWS CLI directly. The store bucket
policy carries an equivalent statement, which evaluates for every
principal rather than only for callers who assumed that role.

**No lifecycle expiration rule on stored PHI.** Deletion of PHI is a
documented disposition decision, not something a lifecycle rule should do
silently.

**`core/healthcheck.py` fails the check on a live COMPLIANCE-mode lock.**
It calls `get_object_lock_configuration` and treats a still-active
COMPLIANCE-mode retention rule as a failure. That is intentional in this
posture: a lock this codebase did not create is a hazard, not a feature,
because nothing here knows how to work with it and it cannot be removed.
It is not a bug.

## Warnings

- **Object Lock cannot be enabled after bucket creation.** These buckets
  are created without it, so adopting an enforced retention control means
  recreating every bucket and migrating every object. Decide before your
  first apply, not after.
- **`force_destroy` on every bucket - store, audit, CloudTrail AND
  psychotherapy - is ANDed with `environment == "dev"`.** Setting
  `force_destroy_buckets = true` on a non-dev stack does not quietly arm
  `terraform destroy`; it fails a Terraform `precondition` loudly at plan
  time. That is the intended behavior - a misconfiguration here fails
  explicitly rather than degrading protection silently.
- **`name_prefix` feeds immutable identifiers.** Bucket names, the KMS
  alias, the RDS identifier and every IAM role name derive from it, and
  no provider can rename any of them in place. Choose it before the first
  apply.
- **Nothing prevents deletion of stored PHI.** Any principal granted
  `s3:DeleteObject` can remove records, including audit records. Scope
  IAM accordingly and alert on delete events.
- **S3 data events in CloudTrail are billed per event.** On a
  high-volume deployment this is a real line item. It's on by default
  here because the incident-response story depends on it; budget
  accordingly.

## Known gaps

These are deliberate, understood limitations of the current stack, not
oversights. Each is stated here so an operator can decide whether to
compensate for it before storing real PHI.

- **No storage-level immutability anywhere on AWS.** Retention is
  recorded as object metadata and enforced by nothing. Every
  deletion-prevention claim in this stack reduces to IAM scoping plus
  versioning plus MFA-less access to version IDs. If your risk
  assessment requires WORM, this stack does not provide it, and because
  Object Lock is a create-time-only bucket property it cannot be added
  to these buckets later either - that would be a new bucket and a full
  object migration.
- **The `DenyAuditLogDeletion` bucket policy was removed.** The audit log
  bucket is now deletable by any principal whose IAM policy allows it.
  The hash chain and CloudTrail give **detection** of audit tampering,
  not **prevention** of it. An operator who needs prevention must add a
  bucket policy or SCP of their own.
- **MFA delete is not configured** on the store, audit or CloudTrail
  buckets. Enabling it requires root credentials and cannot be done from
  Terraform, which is why it is not wired in here.
