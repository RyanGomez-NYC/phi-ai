# Runbook: AWS dev setup

End-to-end setup for a **development** PHI AI Platform deployment on AWS,
ingesting from Epic - the default vendor; for every other vendor in
`core/fhir/emr_profiles.py` the only steps that change are registration
(Step 6, per that
vendor's chapter in `docs/EMR_CONNECTORS.md`) and setting
`PHI_AI_EMR_VENDOR` in `.env`. Use synthetic data only. See "Promoting to
production" at the bottom for what changes.

## Before you start

- [ ] An AWS account you can create IAM roles, KMS keys, and S3/RDS
      resources in, with a payment method attached.
- [ ] Terraform >= 1.10 (S3-native state locking requires it), AWS CLI
      v2, Python 3.11+, Docker + Docker Compose, OpenSSL.
- [ ] `aws sts get-caller-identity` returns your identity.
- [ ] **A BAA with AWS covering this account.** Even for dev this is worth
      having in place before you build the habit of skipping it. AWS
      offers this through AWS Artifact.
- [ ] An Epic developer account at [fhir.epic.com](https://fhir.epic.com)
      (free, self-service) - you'll register a Backend Services client ID
      in Step 6.

> **Read this before you apply anything.** This stack provisions **no S3
> Object Lock**, in any environment. Nothing in AWS prevents stored PHI
> or audit records being deleted by a principal holding
> `s3:DeleteObject`, and the retention period you configure does not
> change that - it is recorded as object metadata to drive documented
> disposition, not enforced by S3. MFA delete is not configured either.
> Integrity here is *detective* (versioning, per-object SHA-256, the
> audit hash chain, CloudTrail), not *preventive*. If your risk analysis
> requires unbypassable retention, stop now: Object Lock can only be
> enabled at bucket creation, so it cannot be added to these buckets
> later. See `docs/COMPLIANCE.md` → "Retention and integrity".

> **AWS free tier note.** This stack targets the free-tier profile
> (`terraform.tfvars.free-tier.example`), which runs at roughly $1/month -
> a customer-managed KMS key has no free-tier allowance and is
> unavoidable. See `docs/COST.md` for the full breakdown, including which
> controls that profile trades away (and why each one is blocked outside
> `dev` by a Terraform precondition). **The RDS Postgres index (Step 6a)
> is a separate matter: its free tier is time-limited (12 months, or
> until signup credits run out on newer accounts) and starts billing
> automatically, with no warning, once it ends - unlike the flat $1/month
> KMS floor, budget for this explicitly.**

---

## Step 1 - Bootstrap the Terraform state backend

Terraform's own state needs somewhere durable to live before it can
manage anything else.

```bash
cd deploy/aws/bootstrap
terraform init
terraform apply
```

Note the `state_bucket` output. Then, back in `deploy/aws/`, copy
`backend.hcl.example` to `backend.hcl` (gitignored - this keeps your AWS
account ID, embedded in the bucket name, out of the public repo) and
fill in the bucket name from that output. Init with:

```bash
cd ..
terraform init -backend-config=backend.hcl
```

It already uses `use_lockfile = true` for state locking - no DynamoDB
table is needed or created. If you ever change `backend.hcl`, re-run
this same command with `-reconfigure`.

**Whatever name the state bucket gets, it keeps.** The bucket name in
`backend.hcl` is how Terraform finds the only record of which buckets,
keys and roles belong to this stack. Rename it and the state is orphaned
along with everything it describes.

## Step 2 - Configure the main stack

```bash
cp terraform.tfvars.free-tier.example terraform.tfvars
```

Edit `terraform.tfvars` - four values are required or `apply` will fail:

- `budget_alert_email` - **required whenever `monthly_budget_usd > 0`**
  (the default). AWS budgets are free; this is the only thing that will
  warn you about runaway spend, and there's no AWS setting that hard-caps
  billing, only alerts.
- `trusted_principal_arns` - your own IAM ARN, so you can assume the
  restore and auditor roles later:
  ```bash
  aws sts get-caller-identity --query Arn --output text
  ```
- `db_allowed_cidr_blocks` - defaults to empty (no network access to the
  Postgres index at all) on purpose. To connect from your own machine,
  add your IP as a `/32`:
  ```bash
  curl -s https://checkip.amazonaws.com
  ```
  then `db_allowed_cidr_blocks = ["<that-ip>/32"]`.
- `db_publicly_accessible` - set `true` if connecting directly from a
  laptop rather than a compute resource already inside the VPC. Combined
  with `db_allowed_cidr_blocks` above, this is still restricted to your
  specific IP, not open to the internet.

Leave the rest of the free-tier defaults as-is for now - each one names
the specific control it trades away for cost, and every one is blocked
outside `dev` by a precondition. (If you'd rather keep full role
separation from the start and don't mind ~$2/month instead of ~$1, use
`terraform.tfvars.example` instead - same required fields apply.) Set
`enable_db = false` here if you don't want the Postgres index at all -
writing to the object store works identically without it.

## Step 3 - Review the plan carefully

```bash
terraform plan -out=tfplan
```

Before applying, confirm in the plan output:

- [ ] **No `destroy` line against any `aws_s3_bucket`, `aws_kms_key` or
      `aws_db_instance`.** On a first apply into an empty account there
      is nothing to destroy, so any such line means Terraform has lost
      track of a resource it believes it manages. Stop and find out why
      before applying - a destroy against a bucket or a key is not
      recoverable, and on a KMS key it makes any surviving ciphertext
      permanently unreadable.
- [ ] `aws_s3_bucket.store` has **no** `object_lock_enabled` argument
      and there is no `aws_s3_bucket_object_lock_configuration` resource.
      That absence is the current, intended posture - if you see either
      of them in the plan, something has drifted from this configuration
      and you should find out why before applying.
- [ ] `aws_iam_role_policy.ingest` contains `kms:GenerateDataKey` but
      **not** `kms:Decrypt` on the store key (`aws_kms_key.store`, the
      object store's CMK). That absence is the point: it means
      compromising the long-running ingestion service does not yield
      readable patient data.
- [ ] Every bucket policy (`aws_s3_bucket_policy.store`, `.audit`,
      `.psychotherapy`) includes `DenyOutdatedTLS`,
      `DenyWrongEncryptionKey`, `DenyUnencryptedUploads`, and
      `DenyMissingEncryptionHeader` statements - these enforce
      encryption independently of the bucket's own default encryption
      config staying correctly set, and all four buckets (including the
      Terraform state bucket) should show a `DenyInsecureTransport`
      statement at minimum. Note that there is **no** `DenyAuditLogDeletion`
      statement on `aws_s3_bucket_policy.audit` - it was removed, so the
      audit bucket's protection is IAM scoping alone (see "Known gaps").
- [ ] The `estimated_monthly_cost_usd` and `cost_warnings` outputs match
      what you expect for the profile you chose in Step 2 - specifically
      check `db_after_free_tier` if `enable_db = true`.

```bash
terraform apply tfplan
```

Takes several minutes - the RDS instance in particular can take 5-10
minutes to become available.

## Step 4 - Capture the outputs

```bash
terraform output -raw env_fragment > ../../.env
cd ../..
```

That writes the bucket names, region, KMS key ARNs
actually deployed, the psychotherapy notes bucket/key (provisioned
unconditionally - see `runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md`), and
(if `enable_db = true`) the Postgres connection details into `.env`.
Nothing secret is in Terraform state - the Epic private key is added in
Step 6 and never passes through Terraform, and the Postgres index has no
password anywhere (see Step 6a).

## Step 5 - Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(A virtualenv avoids fighting your system Python's package management;
if you'd rather skip it, `pip install -r requirements.txt
--break-system-packages` works on Debian/Ubuntu-family systems.)

## Step 6 - Register with Epic and add connection details

(Written for Epic. Ingesting from any other profiled vendor instead?
Register through that vendor's own path - each chapter of
`docs/EMR_CONNECTORS.md` covers it, and the chapters from ModMed onward
end with a step-by-step "Setting it up" - and set `PHI_AI_EMR_VENDOR`
in `.env`. The SMART Backend Services vendors whose profile signs RS384
use the same RSA keypair model generated below; ModMed and Greenway
document ES384 only and need an EC P-384 key (their chapters give the
`openssl ecparam` commands); athenahealth takes a client secret instead.
Everything else in this runbook is vendor-agnostic.)

Epic backend services auth uses a signed JWT client assertion, not a
client secret - generate the keypair first:

```bash
./scripts/generate_epic_keypair.sh
```

This writes `epic_private_key.pem` (keep it, never commit it) and
`epic_public_key.pem` (host this one) to the current directory.

1. Register a client ID at [fhir.epic.com](https://fhir.epic.com), app
   type **Backend Services**.
2. Host `epic_public_key.pem` at a JWK Set URL and register that URL on
   the client ID - static key file upload is no longer accepted for new
   sandbox app registrations. See `docs/EMR_CONNECTORS.md` for the
   detail and the `kid` claim this requires. (The `kid` in
   `deploy/aws/epic_jwks_nonprod.json` has to match whatever you
   registered at Epic. If you publish a different JWK Set, change it
   there to match - the value is an identifier Epic looks up, not a
   name this project is free to choose.)
3. Register an Incoming API for each resource type you plan to ingest
   (matching `EPIC.supported_resources` in `core/fhir/emr_profiles.py`).
   If you also plan to use Bulk Data Export (Step 6b), register those
   four additional Incoming APIs now too, in the same pass.
4. Use the **non-production client ID** to start, against Epic's public
   R4 sandbox (`https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/`).
   Mixing the non-production and production client IDs up against the
   wrong base URL is the most commonly reported first-integration
   failure - there is no generic FHIR sandbox (e.g. HAPI's public test
   server) that will work here, since the auth flow this codebase
   implements is Epic's specific RS384 JWT client-assertion model, not a
   generic OAuth2 client-credentials grant.

Then run the guided installer to finish `.env`:

```bash
python3 install/installer_chatbot.py
```

Confirm infrastructure is provisioned, pick `aws`, and it will prompt for
the Epic base URL, token URL, client ID, the path to
`epic_private_key.pem` (resolved to an absolute path automatically), and
retention settings - re-enter the same
bucket/region/key values `terraform output env_fragment` already gave
you in Step 4 when asked, since the installer collects these fresh
rather than reading the `.env` Step 4 wrote.

## Step 6a - Set up the Postgres index (optional, skip if `enable_db = false`)

The database exists after Step 3's apply, but is empty - no tables, no
application roles - until this step runs. This is a one-time, manual
step by design: it uses the RDS master credential, which the running
application never touches again afterward (see `core/db/connection.py`
and `core/db/bootstrap_aws.sql`).

If `psql` isn't installed:

```bash
brew install libpq
echo 'export PATH="$(brew --prefix libpq)/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

Get the master password Terraform generated (used exactly once, here):

```bash
cd deploy/aws
terraform state pull | python3 -c "
import json, sys
state = json.load(sys.stdin)
for r in state['resources']:
    if r['type'] == 'random_password' and r['name'] == 'db_master':
        print(r['instances'][0]['attributes']['result'])
"
cd ../..
```

Run the schema and bootstrap scripts together, connected as the master
user. This creates the table structure and four IAM-authenticated,
password-less application roles:

- `phi_ai_ingest` - INSERT only on `stored_resources`. Not granted
  SELECT, deliberately; see `core/db/index.py`'s `write_index_entry()`.
- `phi_ai_reader` - SELECT only on the index, plus the release-of-
  information workflow table.
- `phi_ai_disposition` - DELETE on `stored_resources` plus a
  column-scoped SELECT on `storage_key` alone, which is exactly what its
  WHERE clause reads and nothing more. See
  `runbooks/RUNBOOK_DISPOSITION.md`.
- `phi_ai_imaging` - created unconditionally; its grants apply only if
  you installed the optional `core/db/imaging_schema.sql`. See
  `runbooks/RUNBOOK_DICOM_IMAGING.md`.

Those role names, and the `phi_ai_index` database and `phi_ai_master`
user below, are the literal identifiers `core/db/bootstrap_aws.sql` and
the Terraform IAM policies use - type them exactly as printed or the
connection fails:

```bash
psql "host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/schema.sql -f core/db/bootstrap_aws.sql

# LARGE PROFILE: use the partitioned schema instead. It MUST be applied
# before ingestion - converting a populated table to a partitioned one
# rewrites it in place, which on a large deployment is days of downtime.
#
#   -f core/db/schema_partitioned.sql -f core/db/bootstrap_aws.sql
#
# and set PHI_AI_PROFILE=large in .env. See docs/SCALING.md.
```

(`db_endpoint` is the hostname only - no port, no need to extract one.
`db_name`/`db_port` are separate outputs if you need them independently.)

Paste the master password when prompted. Expect a run of `CREATE TABLE`,
`CREATE INDEX`, `CREATE ROLE`, `GRANT ROLE`, `GRANT` and `REVOKE` lines.
If you did **not** install the optional imaging schema, the last thing
printed is a `NOTICE` saying the DICOM grants were skipped - that is the
normal, successful ending, not a failure. If it stops before that,
something did fail: `bootstrap_aws.sql` uses `\set ON_ERROR_STOP on`
specifically so a partial failure halts loudly rather than leaving
inconsistent grants.

Once this completes, the five `PHI_AI_DB_*` lines already in `.env`
from Step 4's `env_fragment` are all that's needed - `scheduler.py` and
`bulk_scheduler.py` both check for `PHI_AI_DB_HOST` on startup and
connect automatically if present, or skip indexing entirely (writes to
the object store are unaffected either way) if not.

## Step 6b - Set up Bulk Data Export (optional)

`core/fhir/bulk_scheduler.py` retrieves an entire patient population in
one pass, unlike `core/fhir/scheduler.py`'s per-type search (which
cannot do population-scale reads at all against Epic - see
`docs/EMR_CONNECTORS.md`). Skip this step if per-type search is enough
for your use case - but see Step 11's note either way, since
`docker compose up -d` starts the `bulk-scheduler` container regardless
of whether this step was completed.

1. If you didn't already in Step 6, register the four Bulk Data
   Incoming APIs on the client ID: **Bulk Data Kick-off**, **Bulk Data
   Status Request**, **Bulk Data File Request**, **Bulk Data Delete
   Request**.
2. Request a sandbox Group FHIR ID by emailing `openepic@epic.com` - it
   is not discoverable through any API. In a real deployment this comes
   from the healthcare organization directly instead.
3. Add to `.env`:
   ```bash
   PHI_AI_FHIR_GROUP_ID=<the Group ID from step 2>
   ```
4. **Test against the mock before spending a real (rate-limited to once
   per 24h) export attempt against the sandbox:**
   ```bash
   python3 scripts/mock_epic_server.py
   ```
   Then, in another terminal, with `.env`'s `FHIR_BASE_URL`/`TOKEN_URL`
   temporarily pointed at `localhost` and `PHI_AI_FHIR_GROUP_ID`
   temporarily set to `eSynGroup0001` (the mock's built-in stub group):
   ```bash
   set -a; source .env; set +a
   python -m core.fhir.bulk_scheduler --once
   ```
5. Once that's clean, restore the real Epic URLs and your real Group ID,
   and run the same command against the sandbox for real.

See `docs/EMR_CONNECTORS.md`'s "Bulk Data Export" section for the full
citations behind why this is a daily-at-most, full-refresh operation,
not an hourly incremental one.

## Step 7 - Verify compliance posture, not just connectivity

```bash
python -m core.healthcheck
```

This checks things a normal healthcheck wouldn't: that no stale Object
Lock is present (a leftover COMPLIANCE-mode rule would silently start
locking every object written to that bucket, irreversibly, and nothing
in this codebase knows how to work with one - so the healthcheck
**fails** on it deliberately, treating it as a hazard rather than a
feature), that versioning is actually on, that encryption uses a
customer-managed CMK rather than an AWS-managed key, that public access
is fully blocked, that key rotation is enabled, and whether the object
store and audit logs share one KMS key (`iam.key_separation`) or are
independent. A deployment can be fully *functional* while failing
several of these - that's exactly the silent failure mode worth
catching.

Expect `iam.role_separation` to report **PASS** only when running as the
ingest role. Running as your admin user it will WARN, correctly, that
your identity can decrypt.

(This is also, by design, the same command run as the Docker
healthcheck for the `app` service - see `docker-compose.yml` - though
there it runs on a much wider interval than you'd use interactively
here, since each run makes 15+ real AWS API calls.)

## Step 8 - Run the end-to-end smoke test

```bash
python scripts/smoke_test_aws.py
```

This writes one synthetic patient record and verifies, in order: KMS
wrapping works, the stored bytes contain no plaintext identifiers, that
the declared retain-until was recorded as object metadata and that no
Object Lock was applied, that decryption round-trips exactly, and that
the audit chain verifies. Note what the retain-until assertion does and
does not prove: it proves the value was *recorded*, not that anything
will *honor* it.

The plaintext-leak assertion is the one to watch. If it ever fails, stop
and do not proceed - it means PHI would be landing on disk readable.

## Step 9 - Confirm deletion is possible, and that it is visible

There is no lock to prove. What is worth proving is the opposite: that a
delete succeeds, and that you can *see* it happen afterwards. That
visibility is the integrity control now.

```bash
BUCKET=$(cd deploy/aws && terraform output -raw store_bucket)
KEY="fhir/Patient/smoketest-verify.json"

echo "not-phi" | aws s3 cp - "s3://$BUCKET/$KEY"
aws s3api delete-object --bucket "$BUCKET" --key "$KEY"
```

The delete should succeed. Note that the call above passes no version
ID, which is the recoverable case: it leaves a delete marker and the
object body survives. The same call *with* `--version-id`, or a
`delete_all_versions()` sweep, destroys the bytes outright and there is
nothing configured to stop it - MFA delete is not enabled on these
buckets. Now confirm the two things that make a delete detectable:

```bash
# 1. Versioning kept the object and added a delete marker.
aws s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" \
  --query '{versions: Versions[].VersionId, deleteMarkers: DeleteMarkers[].VersionId}'

# 2. CloudTrail recorded the DeleteObject call (data events must be on;
#    allow a few minutes for delivery).
aws cloudtrail lookup-events --lookup-attributes \
  AttributeKey=EventName,AttributeValue=DeleteObject \
  --query 'Events[0].[EventTime,Username,EventName]' --output text
```

If the delete marker is absent, versioning is off and you have no record
of what was removed. If CloudTrail shows nothing, `cloudtrail_data_events`
is `false` and object-level deletes are not logged at all - which leaves
the audit hash chain as the only tamper signal. Fix either before
storing anything real.

Clean up the versions the test left behind:

```bash
aws s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" \
  --query '[Versions,DeleteMarkers][].[Key,VersionId]' --output text | \
  while read -r k v; do aws s3api delete-object --bucket "$BUCKET" --key "$k" --version-id "$v"; done
```

## Step 10 - Verify the audit chain independently

```bash
python -m core.audit.verify
```

Restrict to a specific window with `--prefix` if the chain is large -
audit objects are keyed `audit/YYYY/MM/DD/...`, so e.g.
`python -m core.audit.verify --prefix audit/2026/08/` covers one month.
Note that verifying a partial range can only confirm internal linkage
within it, not that it correctly connects to events outside that range
- see the tool's own `--help` for the exact caveat.

Then confirm the same activity appears in CloudTrail, which is the
independent record the incident response runbook depends on:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceType,AttributeValue=AWS::S3::Object \
  --max-results 5
```

CloudTrail data events can lag several minutes. The two logs agreeing is
routine; the useful signal is a read appearing in CloudTrail but *not* in
the application audit log, which indicates access that bypassed the
application entirely. (If you used the free-tier profile with
`cloudtrail_data_events = false`, this step won't show object-level
events - that's the trade-off documented in Step 2's tfvars.)

Both of these are **detective** controls. The audit bucket carries no
bucket policy denying deletion, so a principal with `s3:DeleteObject` on
it can remove audit objects; the hash chain then shows a gap, and
CloudTrail shows who made the call. That is the whole protection - see
"Known gaps".

## Step 11 - Start the real service

```bash
docker compose build
```

If you completed Step 6b (Bulk Data Export):

```bash
docker compose up -d
```

If you skipped Step 6b, start only `app` and `scheduler` explicitly -
the bare `docker compose up -d` above starts every service defined in
`docker-compose.yml`, including `bulk-scheduler`, which will exit
immediately with "PHI_AI_FHIR_GROUP_ID is not set" and then
restart in a loop (`restart: unless-stopped`) if `PHI_AI_FHIR_GROUP_ID`
was never set:

```bash
docker compose up -d app scheduler
```

Then either way:

```bash
docker compose logs -f scheduler
```

`docker compose` reads `.env` both to populate the containers' environment
and to resolve the Epic private-key volume mount - the key is mounted at
the identical absolute path inside the container as outside, so run this
from the repo root where `.env` lives. `app` itself runs no scheduled
process (it's the target for ad-hoc commands like Step 2 of
`RUNBOOK_DATA_RESTORE.md`, run via `docker compose exec app ...`) - the
scheduled ingestion loop lives entirely in the `scheduler` (and, if
started, `bulk-scheduler`) containers.

## Step 12 - Confirm the budget alert

Check your inbox for the AWS Budgets subscription-confirmation email and
click confirm - the alert doesn't fire until you do.

---

## Tearing down the dev stack

```bash
cd deploy/aws
terraform destroy
```

This works immediately - there is no lock to wait out. It does require
`force_destroy_buckets = true`, since versioning means the buckets are
non-empty even after current objects are deleted. On the object store,
audit and CloudTrail buckets that flag is ANDed with
`environment == "dev"`, so it only has an effect here; setting it on a
non-dev stack fails a Terraform precondition at plan time rather than
quietly arming a destroy. The psychotherapy bucket is the exception -
see "Known gaps".

Then verify the KMS key(s) are actually scheduled for deletion - this is
the recurring charge, and `destroy` only *schedules* deletion (30-day
window by design):

```bash
aws kms list-keys --query 'Keys[].KeyId' --output text | \
  xargs -n1 -I{} aws kms describe-key --key-id {} \
  --query 'KeyMetadata.[KeyId,KeyState,Description]' --output text | grep -i phi
```

The bootstrap stack no longer sets `prevent_destroy` on the state bucket,
so it is destroyable like everything else. Versioning still keeps it
non-empty, so a destroy fails until you opt in:

```bash
cd deploy/aws/bootstrap
terraform destroy -var force_destroy_state_bucket=true
```

Do that **last**, and only when you are finished with the whole
deployment. The state bucket is Terraform's only record of which buckets,
keys, and roles belong to this stack. Destroying it before the main stack
does not delete the object store — it *orphans* it, leaving resources that
keep billing and that `terraform destroy` can no longer find. Reclaiming
them means importing each one by hand.

---

## Known gaps

Stated plainly so you can decide whether to compensate before real PHI
is involved. None of these is an oversight.

- **No storage-level immutability, on any bucket, in any environment.**
  Retention is metadata written by application code and enforced by
  nothing. Deletion protection is IAM scoping (ingest/restore/auditor
  denied delete; the disposition role's delete grant gated behind
  `enable_admin_order_purge`, default `false`) plus versioning - and
  versioning only protects against a no-version-ID delete. **MFA delete
  is not configured.**
- **The audit bucket has no `DenyAuditLogDeletion` bucket policy.** It
  was removed. Any principal whose IAM policy permits it can delete
  audit objects. The hash chain and CloudTrail give detection, not
  prevention. Add an SCP or bucket policy of your own if you need
  prevention.
- **The psychotherapy bucket's `force_destroy` is not environment-gated.**
  The object store, audit and CloudTrail buckets AND their
  `force_destroy` with `environment == "dev"`; the psychotherapy bucket
  honors `force_destroy_buckets` in every environment. That is backwards
  given what it holds. Until it is fixed, treat
  `force_destroy_buckets = true` as production-unsafe regardless of the
  preconditions guarding the other buckets.
- **S3 Object Lock is a one-way door, so this posture cannot be reached
  from a locked bucket.** COMPLIANCE mode cannot be shortened or removed
  by anyone, including the AWS account root or AWS Support, for the life
  of the retention period, and `terraform apply` of this configuration
  will not undo it. If you ever enable it by hand on one of these
  buckets, the only way back is fresh buckets and leaving the old ones
  to age out. This is why Step 7's healthcheck treats a lock it did not
  provision as a hard failure.

---

## Promoting to production

Changes required, none of which are automatic:

1. Set `environment = "prod"`. This flips several Terraform
   preconditions from advisory to enforced: `db_deletion_protection`
   must be true, `db_multi_az` should be false only in dev (the
   precondition actually blocks it *on* in dev, to avoid burning free-tier
   hours for no benefit), and `db_publicly_accessible` must be false. It
   also makes `force_destroy_buckets = true` a hard plan-time failure on
   the object store, audit and CloudTrail buckets. `phi_retention_days` is
   **not** forced to
   any particular value by `environment` - both remain fully
   deployer-configurable in every environment, deliberately (see their
   descriptions in `variables.tf`): state medical record retention law
   varies too widely across jurisdictions, and how long you keep records
   is a risk decision for your organization and counsel, not something
   this codebase can correctly force via a hardcoded rule.
   The only related guard is a sanity check against the *dev default*
   (1 day) being carried unedited into prod - it does not impose any
   particular minimum beyond that.
2. Set `phi_retention_days` deliberately, from
   the strictest applicable federal and state requirement for your data
   and jurisdiction - see `docs/COMPLIANCE.md`. Records of minors
   typically run on a much longer clock than adult records. Get the
   retention figure in writing from counsel or your Health Information
   Manager. Understand what setting it does and does not buy you here:
   it is recorded as object metadata to drive a documented disposition
   process, and no S3 mechanism enforces it in either direction - it
   neither blocks an early delete nor expires anything on its own.
3. `separate_audit_key = true`, `cloudtrail_data_events = true`,
   `enable_key_rotation = true` - all blocked to true already outside dev
   by preconditions, but confirm your tfvars doesn't fight them.
4. `trusted_principal_arns` should be the EC2/ECS/EKS role running the
   service, not a human principal.
5. `force_destroy_buckets = false`, `db_publicly_accessible = false` (a
   production Postgres index should be reached from within the VPC only
   - application compute, bastion, or VPN, never directly from the
   internet), `db_deletion_protection = true`. Setting
   `force_destroy_buckets = false` matters most for the psychotherapy
   bucket, which is the one bucket the `environment == "dev"` guard does
   not cover.
6. Complete a HIPAA Security Risk Assessment covering this deployment
   before any real PHI is ingested. The Terraform gives you technical
   safeguards; the risk assessment is a separate, required artifact and
   this repo does not substitute for one. Because retention is not
   technically enforced here, the assessment should address deletion
   risk explicitly - IAM review cadence, delete alerting, and who holds
   `enable_admin_order_purge`.
7. Restrict network access (VPC endpoints for S3 and KMS, so PHI traffic
   never traverses the public internet).
8. Route CloudTrail and the audit log into your existing SIEM, and set an
   alarm on `python -m core.audit.verify` failing, and on any
   `DeleteObject` against the object store or audit buckets.
