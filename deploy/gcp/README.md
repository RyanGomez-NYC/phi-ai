# GCP deployment

Terraform for the GCP PHI AI Platform stack. See
`runbooks/RUNBOOK_GCP_SETUP.md` for the step-by-step walkthrough - this
file is reference for what the stack contains and why.

## What gets created

| Resource | Purpose |
|---|---|
| `google_storage_bucket.store` | Envelope-encrypted PHI ciphertext. Versioned, CMEK, uniform bucket-level access, public access prevention enforced. **No object retention, no Bucket Lock.** |
| `google_storage_bucket.audit` | Hash-chained audit log. Separate bucket, shared key (see below), separate retention *value*. Also no retention policy and no Bucket Lock. |
| `google_kms_key_ring.main` / `google_kms_crypto_key.store` | Symmetric key wrapping per-object data encryption keys. Rotation on by default. |
| `google_service_account.ingest` | Writes PHI, appends audit records. Holds `roles/cloudkms.cryptoKeyEncrypter` on the key - genuinely wrap-only, no decrypt. |
| `google_service_account.restore` | Reads and decrypts PHI for authorized requests. Holds `roles/cloudkms.cryptoKeyDecrypter`. |
| `google_service_account.auditor` | Reads the audit bucket only. No access to `google_storage_bucket.store`, no KMS access at all. |
| `google_sql_database_instance.index` / `google_sql_database.index` | Optional Postgres index (`phi_ai_index`), derived and rebuildable from Cloud Storage. Off by default (`enable_db`). |

## Key design decisions

**One KMS key, not two.** Unlike the AWS stack's separate store/audit
keys, this installment shares one key between both buckets - a
documented cost tradeoff (~$0.06/month for a second key), not a silent
gap. See `kms.tf`'s own comment and the runbook's "Known gaps" section.

**Three service accounts, not one.** Same reasoning as the AWS/Azure
stacks: the ingest identity holds `cryptoKeyEncrypter` but not
`cryptoKeyDecrypter` - compromising the long-running ingestion service
yields ciphertext, not patient data. Unlike Azure, no custom role was
needed for this split; Cloud KMS ships genuine wrap-only and
unwrap-only predefined roles.

**Postgres role names are not free-form here, and that is a real
difference from the other two clouds.** Cloud SQL IAM database
authentication derives the Postgres role name directly from the
authenticating service account's email, so the roles on GCP are
`<prefix>-ingest@<project>.iam` and `<prefix>-restore@<project>.iam` -
not `phi_ai_ingest` / `phi_ai_reader` as on AWS (free-form, created by
`core/db/bootstrap_aws.sql` and named in IAM `rds-db:connect` ARNs) or
on Azure (free-form via `pgaadauth_create_principal_with_oid`). The
same constraint is why index and OMOP writes share one identity here
when AWS and Azure separate them. The DATABASE name (`phi_ai_index`)
is free-form and does match the other clouds. See `database.tf`'s
header.

**No retention enforcement of any kind on either bucket.** Neither
bucket sets `enable_object_retention`, neither sets a bucket-wide
`retention_policy`, and no Bucket Lock is applied anywhere in this
stack. `phi_retention_days` and `audit_retention_days` are declared
policy values that application code records as object metadata; nothing
in GCS reads them. They do not block an early delete and they do not
expire anything on their own. Integrity here is detective (object
versioning, per-object SHA-256, the audit hash chain, Cloud Audit Logs),
not preventive. See docs/COMPLIANCE.md's "Retention and integrity".

**What deletion actually looks like on GCS - it is not S3.** Do not
carry the S3 mental model over. With Object Versioning on, deleting a
live object does not leave a delete marker the way S3 does; GCS keeps
the live generation as a *noncurrent generation*, which remains readable
and restorable. That is the recoverable case. A caller who names a
specific generation deletes that generation outright, with nothing left
to recover. Protection is therefore IAM scoping plus versioning, and
versioning only helps against an unqualified delete.

**`public_access_prevention = "enforced"` and uniform bucket-level access
are access controls, not deletion controls.** They stop the bucket being
exposed publicly and stop per-object ACLs diverging from bucket IAM.
Neither one prevents an authorized principal from deleting objects, and
neither is a substitute for the retention enforcement this stack does
not have.

**Service account impersonation, not just compute attachment.** Unlike
Azure managed identities, GCP service accounts can be impersonated
directly by an authorized principal (`roles/iam.serviceAccountTokenCreator`,
granted to `trusted_principal_members`). This means local development
can genuinely exercise each role's own narrower access, not just a
shared broad identity.

## Warnings

- **Object Retention Lock cannot be enabled on an existing bucket via
  Terraform.** Per Google's own documentation, an existing bucket can
  only gain this feature through the Console. This stack does not enable
  it at creation time either, so adopting it later means either that
  Console path or recreating the buckets - budget for it as a real
  migration, not a variable flip.
- **Bucket Lock is irreversible once applied.** A *locked* retention
  configuration cannot be shortened or removed for the full retention
  duration - not by you, not by Google Support, not at any permission
  level, by design. This stack sets no retention policy and applies no
  lock, so nothing here is locked; if you add one later, understand that
  the decision cannot be undone. See docs/COMPLIANCE.md's "Retention and
  integrity".
- **Cloud KMS key rings cannot be deleted, ever, once created** - a GCP
  platform behavior with no override. `terraform destroy` will not
  remove the key ring; only scheduled crypto key version destruction
  applies. `var.name_prefix` feeds the ring name, so choose it before
  your first apply.
- **Nothing prevents deletion of stored PHI or audit records.** Any
  principal holding `storage.objects.delete` on a bucket can remove
  them. Scope IAM accordingly and alert on delete entries in Cloud Audit
  Logs - that alerting is the control, because no storage-side control
  exists.

## Known gaps

Stated plainly rather than left to be discovered. None of these is an
oversight.

- **No storage-level immutability, on either bucket, in any
  environment.** No `enable_object_retention`, no `retention_policy`, no
  Bucket Lock. Retention is object metadata written by application code
  and enforced by nothing. If your risk assessment requires WORM, this
  stack does not provide it.
- **`GCSStorage.put_object()` applies no per-object retention.** With
  the bucket-wide floor gone there is no second layer behind it either -
  earlier revisions of this file described the bucket policy as still
  covering that gap, which is no longer true. See the runbook's "Known
  gaps" for the full history of that finding.
- **One shared Cloud KMS key** across the object store and audit
  buckets - a documented ~$0.06/month cost tradeoff, not a silent gap.
- **No index/OMOP database role separation on GCP.** AWS and Azure both
  give the index writer and the OMOP ETL writer genuinely separate
  Postgres roles; Cloud SQL's IAM auth model ties one identity to
  exactly one role name, and a single `scheduler.py` process can only
  impersonate one identity at a time, so both connections here use the
  ingest service account's single derived role. This is a platform
  constraint, not a shortcut - see `database.tf`'s header.
- **IAM grants are bucket-level, not prefix-scoped**, because GCS lacks
  S3's resource-ARN prefix scoping without IAM Conditions, which this
  stack does not use. The object store and audit buckets being separate
  is what carries the boundary instead.
