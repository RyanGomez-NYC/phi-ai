# Cost

Prices are US East (N. Virginia) list rates verified August 2026. AWS
pricing changes; check https://aws.amazon.com/kms/pricing and
https://aws.amazon.com/cloudtrail/pricing before relying on these numbers.

## The headline: this cannot run at $0

An AWS **customer-managed KMS key costs $1.00/month**, charged whether or
not the key is ever used, with **no free-tier allowance for key storage**.
The free tier covers 20,000 KMS *requests* per month; it does not cover
the key itself.

The architecture requires a customer-managed key. AWS-managed keys
(`aws/s3`) are free but cannot carry a custom key policy, and the key
policy is what enforces the ingest/restore/auditor separation described in
`deploy/aws/README.md`. Without it, "the ingestion service cannot decrypt
patient data" stops being true.

So: **$1.00/month is the KMS floor**, and it is only the floor with
`enable_db = false`. The default configuration provisions RDS, which
adds ~$12-15/month once the free-tier window closes. KMS itself goes
$1.00 -> $2.00 with a separate audit key, and rises toward ~$6.00 only
where key rotation is on - which is the production configuration, not
dev.

## What the free tier actually is now

AWS replaced the legacy free tier for accounts created on or after
**July 15, 2025**. New accounts choose a Free or Paid plan and receive
**$100 in credits at signup, up to $200 total** by completing onboarding
activities. The Free plan lasts **up to six months, or until credits run
out**, whichever comes first. Accounts created before that date remain on
the legacy 12-month tier.

Practically: this stack is credit-funded, not free. At ~$1-2/month, $200
in credits covers it comfortably for the plan's duration - but credits
expire, and the KMS charge continues afterward.

**A budget alerts; it does not cap.** There is no AWS setting that
hard-stops billing. `deploy/aws/budget.tf` creates alerts at 50/80/100% of
actual spend plus a forecast alert. AWS provides two budgets at no charge.
Set `budget_alert_email` - it is the only thing that will tell you
something is wrong.

## Line by line

| Item | Rate | Free tier? | Notes |
|---|---|---|---|
| KMS customer-managed key | $1.00/key/month | **No** | Unavoidable. Rises toward a $3/key/month cap after two rotations. |
| KMS symmetric requests | $0.03 per 10,000 | 20,000/month | The app calls `GenerateDataKey` **once per ingested resource**. |
| S3 storage | ~$0.023/GB/month | Credit-funded | Billed on *every* version, current and noncurrent. Nothing expires them - see trap 4. |
| S3 PUT/GET | $0.005 / $0.0004 per 1,000 | Credit-funded | One PUT per resource, plus one per audit event. |
| CloudTrail management events | $0 first copy per region | **Yes, first trail** | $2.00/100k for any *additional* trail in the same region. |
| CloudTrail **data** events | $0.10 per 100,000 | **No** | Billed from the first event. No free copy. |
| RDS Postgres index (`enable_db = true`, the default) | ~$12-15/month | 12mo, then **No** | `deploy/aws/outputs.tf:224`: billing starts AUTOMATICALLY when the window ends. Set `enable_db = false` to skip `rds.tf` entirely - the store works identically via S3 alone. |
| S3 versioning | $0 | n/a | The feature is free; the stored versions are not. **Object Lock is not enabled on any bucket in this stack** - see `deploy/aws/README.md`. |
| AWS Budgets | $0 for first two | Yes | |
| DynamoDB state lock | - | - | **Removed.** Now uses S3-native locking (Terraform 1.10+). |

## Four traps worth knowing about

**1. Cold-tier transitions cost more than they save here.**
`STANDARD_IA` and `GLACIER_IR` bill a **128 KB minimum per object**
regardless of real size. FHIR resources are typically 1-10 KB, so a 3 KB
Patient record transitioned to IA bills as 128 KB - roughly 40x its actual
size - plus a per-object transition request charge. `STANDARD_IA` also has
a 30-day minimum duration, `GLACIER_IR` a 90-day minimum.

This is why `enable_lifecycle_transitions` defaults to **false**. Turn it
on only for large objects (`DocumentReference` attachments, imaging,
scanned PDFs), or after aggregating small resources into bundles.

**2. The second-trail trap.** The first copy of management events per
region is free. If your account already has a trail - many do, from a
security tool or an Organizations trail - this stack's trail is a *second*
copy at $2.00 per 100,000 events. Check before applying:

```bash
aws cloudtrail describe-trails --query "trailList[].{Name:Name,MultiRegion:IsMultiRegionTrail}"
```

**3. KMS requests scale with resource count, not data volume.** Envelope
encryption generates a unique data key per object, which is the property
that keeps one compromised DEK from exposing the whole object store. It
also means ingesting 500,000 FHIR resources is 500,000 KMS calls (~$1.44
after the free 20,000). Cheap, but it scales with record count - an EMR
retirement moving 50M resources is ~$150 in KMS requests alone.

`bucket_key_enabled = true` is already set, which cuts the *separate*
S3-side SSE-KMS calls dramatically. It does not affect the application's
own per-object `GenerateDataKey` calls.

**4. Versioning bills what you thought you deleted.** This trap used to
read "Object Lock means you cannot stop paying" - objects under an active
COMPLIANCE retention could not be deleted at all, so an accidental 10-year
lock on test data was a 10-year bill. **That risk is gone: no bucket in
this stack enables Object Lock, so nothing is undeletable and nothing
binds you to keep paying for it.** What replaces it is smaller per object
but easier to miss, because it looks like it worked:

- Versioning is on, and there is **no lifecycle expiration rule** on
  stored PHI - that absence is deliberate (deletion of PHI is a
  documented disposition decision, not something a lifecycle rule should
  do silently), but it means noncurrent versions are never cleaned up by
  anything.
- A `DeleteObject` with no version ID does not free any storage. It adds
  a delete marker; the object body becomes a noncurrent version and keeps
  billing at the full ~$0.023/GB rate, indefinitely. `aws s3 ls` stops
  showing it, and the bill does not move.
- Overwrites behave the same way. Re-ingesting a resource that already
  exists retains the previous copy as a noncurrent version. An idempotent
  ingestion run that re-writes unchanged resources therefore grows storage
  monotonically even when the record count is flat.

So "deleting" data does not stop the charge unless you delete the
*versions*, which the AWS runbook's Step 9 cleanup snippet shows how to
do. Check what is actually accumulating with:

```bash
aws s3api list-object-versions --bucket <store_bucket> \
  --query 'length(Versions)' --output text
```

At FHIR resource sizes this is cents, not dollars, for a dev stack - the
reason it is a trap is that it grows quietly and no alert fires on it. On
a 50M-resource migration with re-runs, budget for it deliberately. The
same dynamic applies to GCS noncurrent generations and Azure blob
versions; Azure's 7-day soft-delete window is the one place it self-limits.

## Configurations

### Free-tier dev - ~$1.00/month, then ~$13-16/month
`terraform.tfvars.free-tier.example`. One shared KMS key, no rotation, no
data events, no transitions.

**Gives up:** role separation (ingest can decrypt PHI), independent
object-access logging, key rotation. Synthetic data only. Terraform
preconditions block all three outside dev.

### Recommended dev - ~$2.00/month
Same, but `separate_audit_key = true` and `cloudtrail_data_events = true`.
Preserves the actual security architecture. The extra dollar is the
cheapest part of this project.

### Production - ~$18-21/month fixed, plus usage
Two keys with rotation at steady state, data events on. Usage scales with
object-store size and access volume; at 100M stored resources expect
storage and KMS request charges to dominate the fixed key cost by orders
of magnitude. Note that storage figure includes noncurrent versions
(trap 4), which no retention control is holding in place and no lifecycle
rule is removing.

## Tearing down

```bash
cd deploy/aws && terraform destroy
```

Then verify the KMS keys are actually scheduled for deletion - they are
the recurring charge, and `terraform destroy` schedules rather than
immediately deletes them (30-day window by design, since deleting a key
destroys every object it wraps):

```bash
aws kms list-keys --query 'Keys[].KeyId' --output text | \
  xargs -n1 -I{} aws kms describe-key --key-id {} \
  --query 'KeyMetadata.[KeyId,KeyState,Description]' --output text | grep -i phi
```

A key in `PendingDeletion` still bills until the window closes. A key that
survived a failed destroy bills indefinitely - this is the most common way
a "deleted" dev stack keeps charging.
