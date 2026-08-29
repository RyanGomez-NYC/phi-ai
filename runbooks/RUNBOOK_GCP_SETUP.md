# GCP Setup Runbook

Provisions the GCP deployment of the PHI AI Platform: Cloud Storage
buckets (versioned and CMEK-encrypted, with **no** object retention and
**no** Bucket Lock - see "Known gaps"), a customer-managed Cloud KMS key,
and three service accounts (ingest/restore/auditor) mirroring the AWS and
Azure stacks' identical role separation. Scoped to GCP's Always Free tier
wherever genuinely possible - see "What this stack actually costs"
below for the honest, confirmed breakdown of what is and isn't free.

Read this whole runbook before running anything. One step here is
irreversible (destroying a Cloud KMS key version, and the key ring that
can never be deleted at all) - know what you're about to do before you
do it, the same caution RUNBOOK_AWS_SETUP.md and RUNBOOK_AZURE_SETUP.md
both open with. Note that locking a retention policy is *not* among the
steps this runbook performs; this stack provisions no retention policy
to lock.

## Prerequisites

- A GCP project with billing enabled. This stack does not create the
  project itself.
- `gcloud` CLI installed and authenticated (`gcloud auth login`,
  `gcloud auth application-default login`).
- Terraform >= 1.10.0.
- Owner or Editor role on the project for initial setup - once the
  stack is applied, day-to-day operators should use the
  ingest/restore/auditor service accounts (via impersonation or compute
  attachment) instead, not your own broad project role.

## 1. Bootstrap the state bucket

```
cd deploy/gcp/bootstrap
terraform init
terraform apply -var="gcp_project=your-project-id"
```

Note the `state_bucket_name` output.

**Whatever name that bucket gets, it keeps.** The name in `backend.hcl`
is how Terraform finds the only record of which buckets, keys and
service accounts belong to this stack. Rename it and the state is
orphaned along with everything it describes.

## 2. Configure the main stack

```
cd ../
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
```

Edit `terraform.tfvars` - at minimum, set `gcp_project`. For anything
beyond a throwaway dev deployment, also set `phi_retention_days` and
`audit_retention_days` from your actual state/federal
retention requirements (see `variables.tf`'s own description of each -
neither this runbook nor that file can tell you the correct number for
your jurisdiction). Understand what setting them does here: they are
recorded as object metadata to drive a documented disposition process,
and no GCS mechanism enforces them in either direction.

Edit `backend.hcl` with the `state_bucket_name` value from step 1.

```
terraform init -backend-config=backend.hcl
```

## 3. Review the plan

```
terraform plan
```

Confirm first that there is **no `destroy` line against any
`google_storage_bucket`, `google_kms_crypto_key` or
`google_sql_database_instance`** - on a first apply there is nothing to
destroy, so any such line means Terraform cannot match a resource it
believes it manages. Stop and find out why before applying; on a crypto
key that plan is not recoverable, by you or by Google.

Then confirm: two Cloud Storage buckets (store, audit), one Cloud KMS key
ring and key, three service accounts, and the IAM bindings connecting
them. (`store` is this stack's Terraform name for the object store's
bucket.) Confirm also that neither bucket carries
`enable_object_retention` or a `retention_policy` block -
their absence is the intended posture, and seeing either one means the
configuration has drifted. If `trusted_principal_members` is
empty, no impersonation grants will appear - that's expected until you
fill it in. If `enable_db` is true, also confirm a
`google_sql_database_instance` and its supporting resources appear - see
Step 4a below.

## 4. Apply

```
terraform apply
```

Capture every output - `terraform output` after a successful apply, or
`terraform output env_fragment` for the ready-to-paste `.env` block
specifically.

## 4a. (Optional) Provision the Postgres index and OMOP analytics layer

Skip this step entirely if you're only writing to Cloud Storage -
`enable_db` defaults to `false` and nothing below applies. **Cloud SQL
has no ongoing free tier** (unlike this stack's storage and KMS costs -
see `database.tf`'s own cost section) - budget roughly $7-10/month
before enabling this for anything beyond brief testing.

Set `enable_db = true` in `terraform.tfvars` and re-run Steps 3-4 - this
provisions `google_sql_database_instance`, a dedicated
`phi_ai_index` database, and the `ingest`/`restore` service
accounts as `CLOUD_IAM_SERVICE_ACCOUNT` Postgres users. (`phi_ai_index`
is the literal database name this stack creates - use it exactly as
printed everywhere below.)

**A real gap worth stating plainly before you continue**: this stack
does not set up Private Service Connect or VPC peering for Cloud SQL -
without a public IP, the Cloud SQL Python Connector (which
`core/db/connection.py`'s `_connect_gcp()` uses) has no network path to
the instance at all, from anywhere. Set `db_publicly_accessible = true`
for this step (and for actually running the application afterward,
unless you separately configure private networking, which this stack
does not do) - not a config toggle you can leave at its `false`
default and work around another way today.

**A second gap, specific to this bootstrap step only**: unlike AWS/Azure
(both of which generate a Terraform-managed master password you can
retrieve from state), this stack does not set an explicit root password
for Cloud SQL's default `postgres` user, so there is nothing to
retrieve. Set one explicitly first - the instance name follows
`${name_prefix}-index` (`phiai-index` with this stack's defaults;
substitute your actual value if you changed that variable, or extract it
from `terraform output instance_connection_name`'s
`project:region:instance` format):

```
gcloud sql users set-password postgres \
  --instance=phiai-index \
  --prompt-for-password
```

Then connect and load the schema:

```
gcloud sql connect phiai-index --user=postgres --database=phi_ai_index
```

Once connected, run `core/db/schema.sql`, then
`core/db/bootstrap_gcp.sql` - substitute that file's own
`{INGEST_IAM_USER}`/`{READER_IAM_USER}` placeholders with this stack's
own `ingest_db_iam_user`/`reader_db_iam_user` outputs (both already
double-quoted, ready to paste):

```
terraform output ingest_db_iam_user
terraform output reader_db_iam_user
```

For the OMOP CDM analytics layer specifically (optional, holds
**identified PHI** - read `core/db/omop_schema.sql`'s own header before
proceeding, and see the OMOP-specific caveats in
`runbooks/RUNBOOK_OMOP_SETUP.md`'s GCP section, including the real
architectural constraint that collapses index/OMOP role separation into
one identity on this cloud specifically), also run
`core/db/omop_schema.sql`, `core/db/omop_vocab_schema.sql`, and
`core/db/omop_bootstrap_gcp.sql` (same `{INGEST_IAM_USER}` placeholder,
same value) - see that runbook for the full walkthrough rather than
duplicating it here.

Add to `.env`:

```
PHI_AI_GCP_CLOUD_SQL_INSTANCE_CONNECTION_NAME=<terraform output instance_connection_name>
PHI_AI_DB_NAME=phi_ai_index
PHI_AI_DB_INGEST_USERNAME=<terraform output ingest_db_iam_user, WITHOUT the surrounding quotes>
PHI_AI_DB_READER_USERNAME=<terraform output reader_db_iam_user, WITHOUT the surrounding quotes>
```

(If you set up OMOP too:
`PHI_AI_OMOP_ETL_USERNAME=<same value as PHI_AI_DB_INGEST_USERNAME>`
- see `deploy/gcp/database.tf`'s own header for why these are
deliberately the same value on GCP.)

## 5. Install dependencies

```
pip install -r requirements.txt --break-system-packages
```

`requirements.txt` already includes `google-cloud-storage` and
`google-cloud-kms` alongside the AWS/Azure SDKs - installing once covers
all three clouds; nothing GCP-specific to add here.

## 6. Register with Epic

Identical across all three clouds - see `docs/EMR_CONNECTORS.md` and
`RUNBOOK_AWS_SETUP.md`'s own account of this step if you haven't done it
before. Nothing about Epic's SMART Backend Services registration or the
RS384 JWT client-assertion flow changes based on which cloud stores
the resulting data. (Ingesting from one of the other five
profiled vendors instead? Register per that vendor's chapter in
`docs/EMR_CONNECTORS.md` and set `PHI_AI_EMR_VENDOR` in `.env` -
also cloud-independent.)

## 7. Authenticate as one of the service accounts

Paste `env_fragment`'s contents into `.env`, then add the FHIR
credentials from step 6.

**A genuine advantage over the Azure stack, worth using**: GCP service
accounts can be impersonated directly from a laptop, unlike Azure
managed identities (which only work attached to compute - see
`RUNBOOK_AZURE_SETUP.md`'s own honest account of that limitation). If
your account is listed in `trusted_principal_members`, you can
authenticate AS any of the three service accounts locally:

```
gcloud auth application-default login --impersonate-service-account=<ingest_service_account_email output>
python -m core.fhir.scheduler --once
```

Swap in `restore_service_account_email` or `auditor_service_account_email`
to test those roles' own narrower access instead - each impersonation is
independently scoped to exactly what that service account can do, so
this is a real test of the role separation, not a workaround that
bypasses it.

## 8. Verify the compliance posture actually applied

```
gcloud storage buckets describe gs://<store_bucket_name> --format="default(objectRetention,retentionPolicy,versioning,encryption,iamConfiguration)"
```

What you are checking for here is partly an **absence**. Confirm that
`objectRetention` and `retentionPolicy` are both missing from the
output - this stack sets neither, and a bucket that reports either one
is not a bucket this configuration produced (see "Known gaps" for why a
locked policy cannot be removed once set). Then confirm the controls
that do exist and that the posture actually depends on:

- `versioning.enabled` is `true` - without it a delete is unrecoverable.
- `encryption.defaultKmsKeyName` names your CMEK, not a Google-managed
  key.
- `iamConfiguration.uniformBucketLevelAccess.enabled` is `true` and
  `iamConfiguration.publicAccessPrevention` is `enforced`. Both are
  access controls, not deletion controls - do not read them as
  retention.

## 9. Confirm deletion is possible, and that it is visible

There is no lock to prove - this stack provisions no bucket retention
policy and no Bucket Lock. What is worth confirming is that a delete
succeeds and leaves evidence, since that visibility is the integrity
control now.

```
BUCKET=<store_bucket_name>
echo "not-phi" | gcloud storage cp - gs://$BUCKET/fhir/smoketest-verify.json
gcloud storage rm gs://$BUCKET/fhir/smoketest-verify.json
```

**Expected: this succeeds.** Note the GCS-specific behavior, which is
not the same as S3's: the delete above names no generation, so the live
object is retained as a noncurrent generation and remains recoverable -
there is no delete marker involved. A delete that names a specific
generation (`gcloud storage rm gs://$BUCKET/obj#<generation>`) removes
that generation's bytes outright, and nothing in this stack stops it.
Then confirm the evidence:

```
# Object Versioning kept the prior generation.
gcloud storage ls -a gs://$BUCKET/fhir/smoketest-verify.json

# Cloud Audit Logs recorded the delete (Data Access logs must be enabled
# for GCS on this project - allow a few minutes for delivery).
gcloud logging read \
  'resource.type="gcs_bucket" AND protoPayload.methodName="storage.objects.delete"' \
  --limit 1 --format='value(timestamp,protoPayload.authenticationInfo.principalEmail)'
```

If no prior generation is listed, versioning is off and you have no
record of what was removed. If the log query returns nothing, GCS Data
Access logging is not enabled and object-level deletes are not recorded
at all - which leaves the audit hash chain as the only tamper signal.
Fix either before storing real PHI. Then remove the leftover
generation with `gcloud storage rm -a`.

## 10. Verify the audit chain

```
python -m core.audit.verify
```

(Requires `PHI_AI_*` env vars set, same as any other tool in this
project - see that module for its own usage if this is your first time
running it.)

This is a detective control over a bucket with no retention enforcement
and no deletion-denying policy: a principal with
`storage.objects.delete` on the audit bucket can remove audit objects,
after which the chain shows a gap and Cloud Audit Logs show who did it.
That is the whole protection - see "Known gaps".

## 11. Start the service

```
python -m core.fhir.scheduler
```

or, for Bulk Data Export instead of incremental per-type polling:

```
python -m core.fhir.bulk_scheduler
```

## 12. Attach service accounts to real compute

Impersonation (step 7) is for local development and testing. A real,
continuously-running deployment should attach the ingest service
account directly to whatever compute actually runs the scheduler -
Cloud Run's own service-account setting, or a Compute Engine instance's
attached service account - rather than relying on impersonation
indefinitely. See that compute product's own documentation for how to
attach a service account; this stack provisions the identity itself
(`identities.tf`), not the compute resource it runs on, the same
bring-your-own-infrastructure boundary `RUNBOOK_AWS_SETUP.md` and
`RUNBOOK_AZURE_SETUP.md` both draw for their own compute layers.

## What this stack actually costs

Confirmed directly against `cloud.google.com/storage/pricing` and
`cloud.google.com/kms/pricing` during this stack's own research, not
assumed or copied from a third party - re-verify at those URLs before
budgeting a real deployment, since published rates can change.

**Genuinely, indefinitely free, if you stay within it:** Cloud
Storage's Always Free tier - 5 GB-months of Standard regional storage,
5,000 Class A operations/month, and 50,000 Class B operations/month,
aggregated across `us-west1`/`us-central1`/`us-east1` specifically
(`gcp_region` defaults to `us-central1` for this reason). Google states
this allowance applies "both during and after the free trial period" -
unlike AWS S3's free tier, which is a 12-month-only benefit, this one
does not expire. A low-volume dev deployment can plausibly stay within
this for a meaningful stretch of time; it is not a guarantee for any
specific real workload. Note that object versioning bills noncurrent
generations at full Standard rates, and with no lifecycle rule deleting
them they accumulate - see `docs/COST.md`.

**Not free, regardless of region or usage:** Cloud KMS key storage.
Roughly $0.06/month per active symmetric key version at the lowest
published tier, plus $0.03 per 10,000 cryptographic operations (the
same rate AWS KMS charges for symmetric operations). A genuine free
allowance does exist (100 key versions, 10,000 operations/month) but
only for keys provisioned through "Cloud KMS Autokey," a different,
automated provisioning mechanism this stack does not use - see
`kms.tf`'s own cost section for why switching to Autokey to chase that
allowance would be a real architecture change, not a toggle, given the
fine-grained per-key IAM bindings `identities.tf` needs. Budget for the
confirmed ~$0.06/month figure, not $0.

Rotating the key more often costs proportionally more (each rotation
creates a new active, billed version) - unlike AWS KMS, where rotation
cost was capped after two retained rotations.

**Also not free, if enabled:** Cloud SQL (Step 4a) - see `database.tf`'s
own cost section. Roughly $7-10/month for the cheapest tier, with no
ongoing free allowance of any kind.

## Known gaps relative to the AWS stack

Stated plainly, matching `RUNBOOK_AZURE_SETUP.md`'s own honesty about
its equivalent gaps, rather than glossed over:

- **No storage-level immutability on either bucket, in any
  environment.** No `enable_object_retention`, no bucket-wide
  `retention_policy`, no Bucket Lock, and no per-object retention set by
  the application. `phi_retention_days`/`audit_retention_days` are
  recorded as object metadata and enforced by nothing. Deletion
  protection is IAM scoping plus object versioning - and versioning only
  helps against a delete that names no generation. If your risk
  assessment requires WORM, this stack does not provide it.
- **A locked retention policy is a one-way door, so this posture cannot
  be reached from a bucket that has one.** A GCS retention policy that
  was locked stays locked for its full duration - not shortenable or
  removable by you, by Google Support, or by any principal at any
  permission level, by design. `terraform apply` of this configuration
  will not undo it. Recovering means moving to fresh buckets, with the
  old ones held and paid for until the period elapses. This is the same
  class of one-way door as AWS Object Lock in COMPLIANCE mode and a
  locked Azure immutability policy.
- **SUPERSEDED (immutability removal).** The mechanism described below no longer exists - this deployment provisions no Object Lock, Bucket Lock, or Azure immutability policy, and no per-object retention is applied. Retained as an audit record of what was fixed at the time. See `docs/COMPLIANCE.md` → "Retention and integrity" for the current posture.
  **FIXED (2026-08-17 audit, H4; implemented 2026-08-18) -
  `core/storage/gcp_gcs.py`'s `GCSStorage.put_object()` did not thread
  `PHI_AI_OBJECT_LOCK_MODE`/`lock_immutability_policy` through to
  the per-object retention call.** It set
  `blob.retention.retain_until_time` but never explicitly set a
  Locked-vs-Unlocked `mode` on the object itself - meaning a deployment
  configured for COMPLIANCE-equivalent (Locked) protection did not
  actually get that mode applied per-object, only via this stack's
  bucket-wide `retention_policy` floor (`storage.tf`), which DID
  correctly honor `lock_immutability_policy` even before this fix. Per
  Google's own documentation an object is protected until both layers
  are satisfied, so the bucket-wide floor provided real protection even
  with this gap - but the two layers were not fully equivalent the way
  the AWS backend's `object_lock_mode` threading already was, and per
  Google's own Python client reference (the `Retention` class's `mode`
  property, which accepts only the literal strings `"Unlocked"` and
  `"Locked"`) an object retention configuration missing `mode` very
  likely fails the underlying API call outright rather than silently
  applying a mode-less retention - the exact same bug class
  `core/storage/aws_s3.py`'s `S3Storage` class already fixed for the
  AWS backend, and `core/storage/azure_blob.py`'s `AzureBlobStorage`
  and `core/audit/sink.py`'s `S3AuditSink` were separately found to have
  in the same audit pass (see those classes' own NOTEs). Fixed by
  setting `blob.retention.mode` alongside the existing
  `retain_until_time` call, translated from
  `PHI_AI_OBJECT_LOCK_MODE`/`lock_immutability_policy`'s
  GOVERNANCE/COMPLIANCE the same way Azure's policy_mode now is (see
  `GCSStorage`'s own NOTE in `core/storage/gcp_gcs.py`). **Verification
  caveat, disclosed honestly rather than left unstated**: this sandbox
  has no live GCS access to exercise a real per-object retention call
  end to end (no PyPI/network egress for `google-cloud-storage` during
  this fix) - the fix was made against Google's documented Python
  client API shape (`docs.cloud.google.com/python/docs/reference/storage`),
  not verified against a live bucket. Re-confirm against a real GCS
  bucket with Object Retention Lock enabled before relying on this in
  production, the same discipline this project applies to every claim
  it cannot fully verify in this sandbox.
- The object store and audit buckets (`store` and `audit` in
  Terraform) share ONE Cloud KMS key
  (`separate_audit_key`-equivalent = false, permanently, in this
  installment) - a deliberate, documented cost tradeoff (saving
  ~$0.06/month), not a silent one. A genuinely separate audit key is
  real hardening, not built here.
- IAM grants are bucket-level, not prefix-scoped. AWS's
  `${bucket_arn}/fhir/*`-style resource ARNs let a single bucket carry
  different IAM scopes for different prefixes; GCS IAM bindings apply
  to the whole bucket unless paired with IAM Conditions (CEL
  expressions), which this stack does not use - not verified to this
  project's standard for correctness, so not attempted rather than
  guessed at. In practice this is a smaller gap than it sounds: the
  object store and audit buckets are already separate, which is the
  primary boundary AWS's own prefix-scoping exists to reinforce
  within a single bucket.
- **Cloud SQL has no private networking configured** (Step 4a) -
  `db_publicly_accessible` effectively has to be `true` for the Cloud
  SQL Python Connector to reach the instance at all today, from
  anywhere including production compute. Private Service Connect or
  VPC peering would close this gap; not built in this installment.
- **Index/OMOP role separation collapses into one identity on this
  cloud specifically** - see `deploy/gcp/database.tf`'s own header.
  Cloud SQL's IAM database authentication ties one Postgres role name
  to exactly one identity, unlike AWS/Azure - a real, structural
  difference in what this platform's own IAM model makes practical,
  not an oversight.
- No Cloud Billing Budget alerts equivalent to `deploy/aws`'s AWS
  Budgets. Cloud Billing Budgets require billing-account-level IAM
  permissions, a different permission scope from everything else this
  stack provisions at the project level - not researched to this
  project's verification standard, so not attempted.
- No psychotherapy-notes bucket/key/role equivalent to
  `deploy/aws/s3_psychotherapy.tf` - AWS-specific work from an earlier
  session, not yet ported to either GCP or Azure.
- **`core/healthcheck.py` does not support GCP** - now confirmed
  directly (2026-08-17 audit, MEDIUM), not just inferred from the
  identical Azure gap: the non-AWS branch previously recorded a `WARN`
  and still returned exit code 0 (`Check.report()` only fails the run
  on an actual `FAIL`), so a GCP deployment's healthcheck - both the
  Docker `healthcheck:` on the `app` service and the post-install
  verification step in `runbooks/RUNBOOK_INSTALL.md` - reported
  success having performed zero compliance checks. Fixed to record a
  genuine `FAIL` (nonzero exit) on GCP/Azure instead, matching
  `RUNBOOK_AZURE_SETUP.md`'s Step 8 / Known gaps item 2, which
  documented the same fix. A real GCP-specific healthcheck (bucket
  retention, KMS state, role-separation probes, mirroring the AWS
  checks) is still not implemented - this fix only makes the interim
  "not implemented" state honest at the exit-code level, it does not
  add the checks themselves. Manual verification (Step 8/9 above)
  remains necessary until that's built.
- Whether Docker Compose passes GCP Application Default Credentials
  into containers correctly has not been verified for this stack
  specifically.

## Tearing down the dev stack

```
terraform destroy
```

One thing this will NOT do, by design:

- **The Cloud KMS key ring survives.** Key rings cannot be deleted
  under any circumstances, ever, once created - a GCP platform
  behavior, not a Terraform limitation. `terraform destroy` will
  schedule the crypto key's versions for destruction (honoring
  `key_destroy_scheduled_duration_seconds`'s window), but the empty key
  ring itself remains in your project permanently. Choose key ring
  names with that permanence in mind, since `name_prefix` is what they
  are built from and nothing can undo the choice afterward.
