# Runbook: Azure dev setup

End-to-end setup for a **development** PHI AI Platform deployment on
Azure, ingesting from Epic - the default vendor; for every other vendor
in `core/fhir/emr_profiles.py` see `RUNBOOK_AWS_SETUP.md` Step 6's note
and
`docs/EMR_CONNECTORS.md`, and set `PHI_AI_EMR_VENDOR` in `.env`. Use
synthetic data only. Mirrors `runbooks/RUNBOOK_AWS_SETUP.md`'s structure
closely, but this is not a line-by-line port - several steps differ
because Azure's actual mechanics differ from AWS's, not just its naming.
Read "Known gaps relative to AWS" at the bottom before treating this as
equivalent in every respect.

## Before you start

- [ ] An Azure subscription you can create resource groups, storage
      accounts, Key Vaults, and RBAC role assignments in.
- [ ] Terraform >= 1.10, Azure CLI (`az`), Python 3.11+, Docker + Docker
      Compose, OpenSSL.
- [ ] `az login` and `az account show` return your identity and the
      correct subscription.
- [ ] **A BAA with Microsoft covering this subscription.** Even for dev
      this is worth having in place before you build the habit of
      skipping it.
- [ ] An Epic developer account at [fhir.epic.com](https://fhir.epic.com)
      - identical to the AWS runbook's Step 6; Epic registration has
      nothing Azure-specific about it.

> **Read this before you apply anything.** This stack creates **no
> immutability policy**, on either container, in any environment.
> Nothing in Azure prevents a blob - PHI or audit record - being deleted
> by a principal holding the RBAC permission to do so, and the retention
> period you configure does not change that: it is recorded as blob
> metadata to drive documented disposition, not enforced by Azure
> Storage. Blob versioning is on and blob soft delete keeps deleted data
> for **7 days**; that window is the entire recovery story, and it is a
> recovery window rather than a bar. Version-level WORM is not enabled
> and cannot be enabled on this storage account (Azure requires that
> opt-in at account creation time). Integrity here is *detective*, not
> *preventive*. See `docs/COMPLIANCE.md` → "Retention and integrity".

---

## Step 1 - Bootstrap the Terraform state backend

```bash
cd deploy/azure/bootstrap
terraform init
terraform apply
```

Note the three `state_*` outputs. Then, back in `deploy/azure/`, copy
`backend.hcl.example` to `backend.hcl` (gitignored) and fill in those
three values. Init with:

```bash
cd ..
terraform init -backend-config=backend.hcl
```

Unlike AWS's bootstrap, this creates a resource group too (Azure has no
direct equivalent of "an AWS account" as the state-holding container) -
`terraform destroy` in this directory later removes both.

**Whatever those three names are, they keep.** The values in
`backend.hcl` are how Terraform finds the only record of which resources
belong to this stack. Rename them and the state is orphaned along with
everything it describes.

## Step 2 - Configure the main stack

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`. Nothing is strictly required to be changed for
a first dev apply - every default is dev-safe - but you should still set
`trusted_principal_object_ids` to your own Azure AD object ID before
applying, or Step 9's local-dev verification won't have anything to
authenticate with:

```bash
az ad signed-in-user show --query id -o tsv
```

See `terraform.tfvars.example`'s own comments for what each value
controls, and `identities.tf`'s module-level comment for **why**
`trusted_principal_object_ids` exists at all here - it is not simply a
convenience knob the way it might look; Azure managed identities cannot
be assumed from a laptop the way AWS IAM roles can, so this is the only
way local development can actually exercise this stack end to end.

**If you want the Postgres index or OMOP analytics layer** (both
optional, off by default - see Step 4a below for the full walkthrough),
set these here too, since `database.tf`'s own precondition requires
them whenever `enable_db = true` and there is no sensible default this
stack can compute for you:

```
enable_db                   = true
db_aad_admin_object_id      = "<az ad signed-in-user show --query id -o tsv>"
db_aad_admin_principal_name = "<az ad signed-in-user show --query userPrincipalName -o tsv>"
```

## Step 3 - Review the plan carefully

```bash
terraform plan -out=tfplan
```

Before applying, confirm in the plan output:

- [ ] **No `destroy` line against `azurerm_storage_account`,
      `azurerm_key_vault`, `azurerm_key_vault_key` or
      `azurerm_postgresql_flexible_server`.** On a first apply into an
      empty subscription there is nothing to destroy, so any such line
      means Terraform cannot match a resource it believes it manages.
      Stop and find out why before applying - the Key Vault is purged on
      destroy (`purge_soft_delete_on_destroy = true`, see "Tearing
      down"), which makes anything wrapped by its key unreadable.
- [ ] `azurerm_storage_account.store` shows `account_kind = "StorageV2"`
      and a `blob_properties.versioning_enabled = true` block - both
      required preconditions for blob versioning. (`store` here and
      below is the Terraform resource name for the object store.)
      Confirm also `shared_access_key_enabled = false` and the 7-day
      `delete_retention_policy` - with no immutability policy anywhere in
      this stack, those two are the whole of the deletion story.
- [ ] There is **no** `azurerm_storage_container_immutability_policy`
      resource in the plan, for either container. Its absence is the
      intended posture; seeing one means the configuration has drifted.
- [ ] `azurerm_role_assignment.ingest_wrap_key` references
      `azurerm_role_definition.key_wrap_only`, not a built-in Key Vault
      role - this is the custom, wrap-only role that keeps the ingest
      identity unable to decrypt PHI (see `identities.tf`'s own comment
      for why no Azure built-in role provides this split).
- [ ] `azurerm_storage_account_customer_managed_key.store` appears in
      the plan - if it's missing, the storage account is still using
      Microsoft-managed encryption, not the customer-managed key this
      whole stack exists to provision.
- [ ] `azurerm_key_vault_key.store` shows a `rotation_policy` block
      (present whenever `key_rotation_days` is non-null, the default) -
      see `keyvault.tf`'s own comment on why this is safe only as of the
      2026-08-17 audit's H5 fix to `core/crypto/envelope.py`'s AzureKMS
      class, and "Known gaps relative to AWS" item 11 below.
- [ ] If `enable_db = true`: `azurerm_postgresql_flexible_server`,
      `azurerm_postgresql_flexible_server_database`, and
      `azurerm_postgresql_flexible_server_active_directory_administrator`
      all appear - see Step 4a below.

```bash
terraform apply tfplan
```

This takes longer than the AWS apply - the two `time_sleep` resources
(absorbing Azure RBAC propagation delay before the Key Vault key and the
customer-managed-key wiring can be created) add roughly three minutes on
top of actual resource-creation time. This is expected, not a hang.
With `enable_db = true`, Flexible Server provisioning itself typically
adds several more minutes on top of that - also expected.

## Step 4 - Capture the outputs

```bash
terraform output -raw env_fragment > ../../.env
cd ../..
```

Writes the container names, region, Key Vault key name
equivalent, storage account blob endpoint, and Key Vault URI into `.env`
- and, if `enable_db = true`, the database host/name and the three
Postgres usernames (all fixed strings on Azure - see Step 4a's own note
on why, unlike GCP, these don't need to be derived from anything).
Nothing secret is in Terraform state that ends up here - authentication
throughout is via Azure AD (`DefaultAzureCredential`), never a stored
key. The one password this stack DOES generate
(`random_password.db_master`, only when `enable_db = true`) is
deliberately excluded from `env_fragment` - see Step 4a for where it's
actually used.

## Step 4a - (Optional) Bootstrap the Postgres index and OMOP analytics layer

Skip this step entirely if you left `enable_db` at its `false` default.
**Flexible Server has no free tier of any kind** - see `database.tf`'s
own cost section; budget roughly $12-15/month before enabling this for
anything beyond brief testing, genuinely more than either AWS's
free-tier-eligible option or GCP's ~$7-10/month equivalent.

Retrieve the one-time bootstrap password Terraform generated:

```bash
terraform -chdir=deploy/azure output -raw db_master_password
```

(This output doesn't exist as a named output yet by default - retrieve
it via `terraform -chdir=deploy/azure state show
'random_password.db_master[0]'` and read the `result` attribute instead
if the named output isn't present in your checkout.)

Connect as the Microsoft Entra administrator you configured in Step 2
and load the schema:

```bash
psql "host=$(terraform -chdir=deploy/azure output -raw db_host) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/schema.sql -f core/db/bootstrap_azure.sql
```

The `phi_ai_index` database and `phi_ai_master` user above, and the
`phi_ai_ingest`/`phi_ai_reader` roles below, are the literal identifiers
this stack creates - type them exactly as printed or the connection
fails.

`core/db/bootstrap_azure.sql`'s own `{INGEST_PRINCIPAL_ID}`/
`{READER_PRINCIPAL_ID}` placeholders need
`terraform -chdir=deploy/azure output ingest_identity_principal_id` /
`restore_identity_principal_id` substituted in before running - these
are genuinely different from the `ingest_identity_client_id` outputs
`identities.tf` already provides (client_id authenticates a running
application; principal_id is what `pgaadauth_create_principal_with_oid()`
and RBAC assignments both actually need).

For the OMOP CDM analytics layer specifically (optional, holds
**identified PHI** - read `core/db/omop_schema.sql`'s own header before
proceeding, and see `runbooks/RUNBOOK_OMOP_SETUP.md`'s Azure section for
the full walkthrough rather than duplicating it here), also run
`core/db/omop_schema.sql`, `core/db/omop_vocab_schema.sql`, and
`core/db/omop_bootstrap_azure.sql` - the same
`{INGEST_PRINCIPAL_ID}` placeholder and value, registering the SAME
ingest identity under a second, genuinely distinct Postgres role
(`omop_etl`) - unlike GCP, Azure's `pgaadauth` model lets this happen
without collapsing index/OMOP role separation into one identity; see
`deploy/azure/database.tf`'s own header for the full contrast.

`PHI_AI_DB_INGEST_USERNAME`/`PHI_AI_DB_READER_USERNAME`/
`PHI_AI_OMOP_ETL_USERNAME` are already in `.env` from Step 4's
`env_fragment` (fixed strings - `phi_ai_ingest`, `phi_ai_reader`,
`omop_etl` - not derived from any resource attribute, since Azure's
role-naming is freely chosen rather than forced to match an identity's
own name the way GCP's is).

## Step 5 - Install Python dependencies

Identical to `RUNBOOK_AWS_SETUP.md` Step 5:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Step 6 - Register with Epic and add connection details

Identical to `RUNBOOK_AWS_SETUP.md` Step 6 in full - Epic's RS384 JWT
backend-services auth model has nothing cloud-provider-specific about
it. Run `./scripts/generate_epic_keypair.sh`, register at
fhir.epic.com, then:

```bash
python3 install/installer_chatbot.py
```

Confirm infrastructure is provisioned, pick `azure`, and it will prompt
for the Epic connection details plus `PHI_AI_AZURE_ACCOUNT_URL` and
`PHI_AI_AZURE_VAULT_URL` - both already present in `.env` from Step
4's `env_fragment`, so re-enter the same values when asked.

## Step 7 - Authenticate for local development

Run this once per terminal session, before running any ingestion or
restore command locally:

```bash
az login
```

`core/storage/azure_blob.py` and `core/crypto/envelope.py`'s `AzureKMS`
both authenticate via `DefaultAzureCredential`, which - on a plain dev
laptop with no managed identity attached - falls back to your Azure CLI
session. This is why Step 2 asked you to add your own object ID to
`trusted_principal_object_ids`: without that role grant, `az login`
alone gets you an authenticated identity with no permissions on any of
this stack's resources. Because `shared_access_key_enabled = false`,
there is no account-key fallback either - Azure AD is the only way in,
for reads, writes and deletes alike. The same applies to
`core/db/connection.py`'s
`_connect_azure()` if you set up the database in Step 4a - your own
signed-in identity needs to actually be one of the roles registered via
`pgaadauth_create_principal_with_oid()` to connect, which for a
dev operator typically means being the Entra administrator
itself (Step 4a), not `phi_ai_ingest`/`omop_etl` (those are scoped
to the managed identities, meant for real compute, not a human
developer's own login).

There is no local equivalent of `core/fhir/restore.py --role-arn`
switching to a specific narrower role for one command - see "Known gaps
relative to AWS" below for why, and what a real (non-local) deployment
looks like instead.

## Step 8 - Verify compliance posture, not just connectivity

`python -m core.healthcheck` **does not check Azure yet** - it detects a
non-AWS provider and exits nonzero (a genuine FAIL) rather than
silently claiming success. FOUND AND FIXED (2026-08-17 audit, MEDIUM):
until this fix, the non-AWS branch recorded a `WARN`, not a `FAIL` -
and `Check.report()` only returns a nonzero exit code when at least one
`FAIL` is present, so a WARN-only run (the entirety of what this script
did on Azure) actually exited 0. That is precisely the false-PASS this
paragraph and "Known gaps" item 2 below both explicitly claimed did NOT
happen - true of the human-readable `[WARN]` line printed to the
terminal, false of the exit code any monitor, CI step, or Docker
`healthcheck:` block actually observes. Since this script is also the
container healthcheck for the `app` service (`docker-compose.yml`) and
the post-install verification step `runbooks/RUNBOOK_INSTALL.md` sends
every operator to, an Azure deployment could report "healthy" having
performed zero of the checks below. Changed to `FAIL` so the exit code
now genuinely matches what this runbook always intended it to mean.
This is still a real gap, not a silent one, and the verification below
remains manual until a proper Azure equivalent of the AWS bucket/KMS/
role-separation checks is built - mirroring how `RUNBOOK_AWS_SETUP.md`'s
own Step 9 verifies deletion behavior directly via the AWS CLI
rather than trusting a tool for that one specific check:

```bash
# Confirm the storage account is actually using your Key Vault key,
# not Microsoft-managed encryption:
az storage account show \
  --name "$(cd deploy/azure && terraform output -raw storage_account_name)" \
  --query "encryption.keyVaultProperties.keyName" -o tsv
# Expect your key's name (terraform output key_vault_key_name), not empty.

# Confirm NO immutability policy is set (expected - this stack creates none):
az storage container immutability-policy show \
  --account-name "$(cd deploy/azure && terraform output -raw storage_account_name)" \
  --container-name fhir --auth-mode login
# Expect this to report no policy. A policy found here means the
# configuration has drifted from this stack - and if its state is
# "Locked" it cannot be removed, meaning the container must be replaced.

# Confirm the two controls that DO carry the deletion story:
az storage account blob-service-properties show \
  --account-name "$(cd deploy/azure && terraform output -raw storage_account_name)" \
  --query "{versioning:isVersioningEnabled, softDeleteDays:deleteRetentionPolicy.days}"
# Expect versioning true and 7 days. Seven days is the entire recovery
# window - see "Known gaps" item 13.
```

(`--auth-mode login` is not optional here: `shared_access_key_enabled`
is `false` on this account, so key-based CLI access does not work at
all.)

Building a proper `core/healthcheck.py` Azure equivalent - mirroring the
AWS path's bucket/KMS/role-separation checks - is tracked as a fast-
follow, not done in this installment.

## Step 9 - Confirm deletion is possible, and that it is visible

There is no immutability policy to prove - this stack creates none.
What is worth confirming is that a delete succeeds and leaves
evidence, the same discipline as `RUNBOOK_AWS_SETUP.md` Step 9.

```bash
ACCOUNT=$(cd deploy/azure && terraform output -raw storage_account_name)
az storage blob upload \
  --account-name "$ACCOUNT" --auth-mode login \
  --container-name fhir --name smoketest-verify.txt \
  --data "delete-visibility test $(date -u +%Y-%m-%dT%H:%M:%SZ)"

az storage blob delete \
  --account-name "$ACCOUNT" --auth-mode login \
  --container-name fhir --name smoketest-verify.txt
```

**Expected: this succeeds.** Then confirm the evidence:

```bash
# Blob versioning kept the prior version.
az storage blob list --account-name "$ACCOUNT" --auth-mode login \
  --container-name fhir --include v --prefix smoketest-verify.txt \
  --query '[].{name:name, version:versionId, isCurrent:isCurrentVersion}' -o table
```

If no prior version is listed, versioning is off and you have no record
of what was removed. Also confirm that storage diagnostic logging
(`StorageDelete`) is routed somewhere you actually read - without it,
blob deletes are not independently recorded and the audit hash chain is
the only tamper signal. Soft delete (`delete_retention_policy`, 7 days on
this stack) gives you a recovery window for accidents, not a control
against a deliberate deletion - and it is the only recovery window there
is. After seven days there is nothing left to restore, on either
container, including the audit log.

## Step 10 - Verify the audit chain independently

```bash
python -m core.audit.verify
```

This is provider-agnostic - `core.audit.verify` constructs whichever
sink `core/storage/factory.py` resolves for `settings.cloud_provider`,
which now includes `AzureBlobAuditSink` (`core/audit/sink.py`). No
Azure-specific caveat here, unlike Step 8's healthcheck gap.

It is a detective control over a container with no immutability policy:
a principal with blob-delete RBAC on `audit` can remove audit blobs, and
after the 7-day soft-delete window they are unrecoverable. The chain
then shows a gap - that is the signal, and it is all there is.

There is no CloudTrail/Activity-Log-equivalent independent cross-check
in this installment yet - see "Known gaps" below.

## Step 11 - Start the real service

```bash
docker compose build
docker compose up -d
docker compose logs -f scheduler
```

Same caveat as `RUNBOOK_AWS_SETUP.md` Step 11 about `bulk-scheduler`
starting alongside `scheduler` if you haven't configured
`PHI_AI_FHIR_GROUP_ID` - use `docker compose up -d app scheduler`
explicitly if you're not using Bulk Data Export.

Az login-based authentication (Step 7) does not extend into the Docker
containers automatically - see "Known gaps" below for what a container
actually needs.

## Step 12 - Attaching identities to real compute (beyond local dev)

Everything above runs as *your own* Azure AD identity via `az login`.
For an actual deployment - not local testing - the point of
`identities.tf`'s three separate user-assigned identities is to attach
each one to the specific compute resource that should hold it:

```bash
# Example: Azure Container Apps
az containerapp identity assign \
  --name <your-scheduler-container-app> \
  --resource-group "$(cd deploy/azure && terraform output -raw resource_group_name)" \
  --user-assigned "$(cd deploy/azure && terraform output -raw ingest_identity_id)"
```

Once attached, `DefaultAzureCredential` running *inside that specific
compute resource* automatically authenticates as the attached identity
via Azure's instance metadata service - no code change, no credential
file. Attach `restore_identity_id` to whatever runs
`core/fhir/restore.py` on demand (or grant it to specific human
operators via `trusted_principal_object_ids`, accepting the same
reduced-separation tradeoff Step 7 describes for local dev), and
`auditor_identity_id` to whatever runs `core/audit/verify` on a
schedule.

---

## Tearing down the dev stack

```bash
cd deploy/azure
terraform destroy
```

This works immediately - there is no immutability policy to wait out.
It does require the containers to be emptiable, which blob versioning
means is not automatic; delete versions explicitly if `destroy` reports
non-empty containers.

Then confirm the Key Vault is actually gone, not just soft-deleted (see
`versions.tf`'s `purge_soft_delete_on_destroy = true` - this should
happen automatically, but confirm):

```bash
az keyvault list-deleted --query "[?properties.vaultId=='$(cd deploy/azure && terraform output -raw azure_vault_url 2>/dev/null)']"
```

Empty result is expected (fully purged, not lingering in the
soft-deleted state).

---

## Known gaps relative to AWS

Honest accounting, not an exhaustive changelog - see individual `.tf`
file comments for the reasoning behind each:

1. **No Azure equivalent of `sts:AssumeRole`.** Managed identities must
   be attached to compute; they cannot be borrowed by an arbitrary
   authorized caller from a laptop. This is the single biggest
   structural difference from the AWS side and affects Steps 7 and 12
   above directly.
2. **`core/healthcheck.py` does not support Azure** - see Step 8. As of
   the 2026-08-17 audit's MEDIUM fix it fails honestly at the exit-code
   level too (a genuine `FAIL`/nonzero exit, not just a `WARN` line that
   still exited 0 as it did before that fix) rather than pretending to
   check something it doesn't.
3. **No CloudTrail/Activity-Log equivalent yet.** AWS's independent,
   out-of-band cross-check (a read appearing in CloudTrail but not the
   application audit log indicates access that bypassed the
   application) has no Azure counterpart built in this installment.
   Azure Monitor / diagnostic settings would be the natural fit -
   tracked as a fast-follow.
4. **The object store and the audit log share one Key Vault key**, not
   separate ones - the direct equivalent of AWS's
   `separate_audit_key = false` cost-saving option, but there is
   currently no Azure equivalent of `separate_audit_key = true` built.
   Azure Storage's "encryption scopes" feature could give the audit
   container an independent key within the same storage account; not yet
   researched to the standard the rest of this stack's claims are held
   to.
5. **No named `db_master_password` Terraform output yet** - see Step
   4a's own workaround (`terraform state show`) until one is added;
   a real, if minor, usability gap worth closing in a follow-up rather
   than left as a permanent state-inspection requirement.
6. **No budget alerts, no psychotherapy notes container equivalent.**
   Both exist on the AWS side; neither has been built for Azure yet.
7. **The ingest identity's Storage Blob Data Contributor grant includes
   delete**, broader than `deploy/aws/iam.tf`'s ingest role, which
   explicitly denies `s3:DeleteObject`. Azure has no built-in "write and
   read but not delete" blob data role; closing this precisely would
   need another custom role definition, the same pattern
   `identities.tf`'s `key_wrap_only` role already establishes for Key
   Vault - not yet done for storage. With no immutability policy behind
   it (item 13), this grant is now the deletion boundary rather than a
   second line behind one.
8. **Docker Compose does not currently pass Azure credentials into the
   containers.** `docker-compose.yml` was built around AWS's credential
   model (environment variables / mounted credentials file). Running
   Step 11 as written will likely fail to authenticate inside the
   containers even after `az login` succeeds on the host - passing
   through `~/.azure` or using a service connection is not yet wired up
   here. Flagged rather than silently left for someone to discover via
   a confusing runtime error.
9. **No per-resource-type retention override support.**
   `PHI_AI_RETENTION_YEARS_OVERRIDES` (core/fhir/client.py) only
   applies on the AWS path today - `core/storage/azure_blob.py` has no
   equivalent mechanism, so every resource type ingested via Azure gets
   this stack's single configured retention value recorded in its blob
   metadata regardless of any override configured in `.env`. If your
   deployment genuinely needs a longer period for a specific resource
   type (immunization records are a common example - see
   `docs/COMPLIANCE.md`), that is not yet honored on Azure. Note that
   since nothing enforces the recorded value at all (item 13), the
   practical consequence today is a wrong number in your disposition
   records rather than data deleted too early.
10. **Cloud SQL's private-networking gap has no Azure equivalent
    problem, worth noting for contrast**: Flexible Server's firewall
    rules (Step 4a) work against the public endpoint directly, with no
    additional networking prerequisite the way GCP's Cloud SQL
    Connector currently requires - a genuine point in Azure's favor for
    this specific piece, not something to read as parity with GCP's gap.
11. **FIXED (2026-08-17 audit, H5; implemented 2026-08-18) - key
    rotation previously had no safe path at all.** Until this fix,
    `keyvault.tf` configured NO `rotation_policy` on the object store's
    key (`azurerm_key_vault_key.store`), and for good reason it turned
    out: `core/crypto/envelope.py`'s
    `AzureKMS` class bound its one `CryptographyClient` to whatever Key
    Vault reported as the LATEST key version at construction time, and
    reused that same binding for both wrapping new DEKs and unwrapping
    old ones. Unlike AWS/GCP KMS, Azure Key Vault's RSA-OAEP wrap/unwrap
    has no server-side version resolution - a wrapped DEK carries no
    metadata saying which version wrapped it, and a different version's
    key material genuinely cannot unwrap it. So rotating the key at all
    (manually, or via a rotation policy) would have permanently broken
    restoring every object stored before that rotation, with no way
    to recover afterward. This was a real, latent bug independent of
    whether rotation was ever configured - it just had no trigger yet
    because nothing was rotating the key. Fixed by recording the exact
    versioned key ID alongside every wrapped DEK at wrap time and
    binding unwrap to that exact version rather than "whatever is
    latest now" (see `AzureKMS`'s own docstring for the full mechanics).
    `keyvault.tf` now configures 90-day automatic
    rotation by default (`var.key_rotation_days`), matching
    `deploy/gcp/kms.tf`'s existing cadence - safe now, not before.
12. **SUPERSEDED (immutability removal) - the mechanism below no longer
    exists; this stack sets no version-level immutability policy at all.
    Retained as the audit record of what shipped and what was fixed.
    FIXED (2026-08-17 audit, H4; implemented 2026-08-18) - every
    object written via this backend got the irreversible "Locked"
    immutability policy mode, regardless of configuration.**
    `core/storage/azure_blob.py`'s `AzureBlobStorage.put_object()`
    previously hardcoded `policy_mode="Locked"` unconditionally on every
    version-level immutability policy it set, with no way to request the
    reversible Unlocked/GOVERNANCE-equivalent mode instead - a dev stack
    explicitly configured for the reversible mode
    (this runbook's own dev-safe convention) still got every stored
    object permanently, unappealably locked. This was the exact same bug
    class `core/storage/aws_s3.py`'s `S3Storage` class had already fixed
    for the AWS backend ("a real, permanent bug, not a hypothetical one"
    per that class's own NOTE); the fix never propagated here, or to
    `core/audit/sink.py`'s `S3AuditSink`, or to
    `core/storage/gcp_gcs.py`'s `GCSStorage` (see those classes' own
    NOTEs for the matching fixes made in the same audit pass). Fixed by
    threading the configured mode through
    `core/storage/factory.py`'s `build_storage()` into
    `AzureBlobStorage.__init__`, which translates AWS's own
    GOVERNANCE/COMPLIANCE vendor terms into Azure's equivalent
    Unlocked/Locked (see `AzureBlobStorage`'s own NOTE for the full
    mechanics, and the terminology mapping this project's Architecture
    doc documents across all three clouds). Purely a Python-side fix -
    no Terraform change accompanies this item, unlike item 11 above.
13. **No storage-level immutability on either container, in any
    environment.** There is no
    `azurerm_storage_container_immutability_policy` on `fhir` or on
    `audit`. `phi_retention_days`/`audit_retention_days` are
    recorded as blob metadata by application code and enforced by
    nothing - they neither block an early delete nor expire anything.
    Deletion protection is RBAC scoping (item 7) plus blob versioning
    plus the 7-day soft-delete window, and that window is a recovery
    period, not a bar. If your risk assessment requires WORM, this stack
    does not provide it.
14. **Version-level WORM cannot be enabled on this storage account at
    all.** Azure requires that opt-in at account creation time, so it is
    not a setting this stack can turn on later - adopting it means a new
    storage account and a full data migration. This is a genuine
    platform difference from AWS (where Object Lock is likewise
    creation-time-only, but at bucket rather than account granularity)
    and from GCP (where Object Retention Lock can be added to an
    existing bucket, though only via the Console, not Terraform). Do not
    assume the three clouds behave alike here; they do not.
15. **A locked immutability policy is a one-way door.** Once locked, an
    Azure immutability policy cannot be unlocked or shortened by anyone,
    including Microsoft Support, for its full duration -
    `terraform apply` of this configuration will not remove it.
    Recovering means moving to new containers, with the old ones held
    (and paid for) until the period elapses. Same class of one-way door
    as AWS Object Lock COMPLIANCE mode and a locked GCS retention
    policy.
