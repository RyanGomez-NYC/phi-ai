# GCP Zero-to-One Deployment Runbook

**Target:** `runbooks/RUNBOOK_GCP_ZERO_TO_ONE.md` in `RyanGomez-NYC/phi-ai`.
**Status:** DRAFT for review. Verified against repo `main` @ `c69923c` (1.0.0-rc1) and live Google documentation, August 2026.
**Supersedes nothing.** `RUNBOOK_GCP_SETUP.md` starts at "a GCP project with billing enabled" and remains correct from Phase 8 onward. This document covers everything before that, plus the gaps that runbook names but does not close.

---

## Part 0 — Read this before you spend a dollar

### 0.1 What `deploy/gcp` actually provisions today

Verified against the live Terraform, not recollection:

| Created | Not created |
|---|---|
| 1 KMS key ring, **1** crypto key | Organization, folders, projects |
| 2 buckets (store, audit), UBLA + PAP + versioning + CMEK | Org policies |
| Cloud SQL Postgres 16 (only if `enable_db=true`), IAM auth on | VPC, subnets, private networking |
| 3 service accounts (ingest, restore, auditor) | VPC Service Controls perimeter |
| Bootstrap: tfstate bucket only | Cloud Run, Artifact Registry, any compute |
| APIs: storage, cloudkms, iam, sqladmin | Secret Manager |
| | **Data Access audit logs** |
| | Billing budgets, Bucket Lock/retention |

That is roughly a fifth of a compliant deployment. The rest is this document.

### 0.2 Six blockers. Two are irreversible after creation.

Read these before Phase 7, because two of them cannot be fixed by a later `terraform apply` — they require destroying and rebuilding the resource.

**B1 — Cloud SQL has no CMEK. IRREVERSIBLE.** `database.tf` sets no `encryption_key_name`, so the index runs on Google-managed keys. Invariant 6 classes the index as a derived PHI artifact requiring envelope encryption under a CMK. Google's constraint: *"You can't enable customer-managed encryption keys on an existing instance."* Fix before first apply or rebuild the database later. The key must be **regional and co-located** with the instance — multi-region and global keys are rejected.

**B2 — Store and audit share one KMS key. Effectively irreversible.** `kms.tf` creates a single `<prefix>-store-key` used by both buckets. One key grant compromise covers the PHI store *and* the tamper-evident audit log that is supposed to detect the compromise. The AWS stack separates these (`kms.tf` + `s3_audit.tf`); GCP collapses them. Re-keying existing objects means rewriting every object.

**B3 — Cloud SQL is public-IP. INVARIANT 5 VIOLATION.** The runbook's own known-gap list states `db_publicly_accessible` must be `true` "for the connector to work at all." A PHI index reachable from the internet is not minimum-necessary as a structural property. Phase 6 fixes this with private services access; the connector works fine over private IP once the VPC exists — the current requirement is an artifact of there being no VPC, not a Cloud SQL limitation.

**B4 — Data Access audit logs are off. INVARIANT 10 GAP, and it is the default everywhere.** GCP enables Admin Activity and System Event logs always; `ADMIN_READ`, `DATA_READ`, and `DATA_WRITE` are **disabled by default for every service except BigQuery**. A default deployment records no read access to PHI at the cloud layer. The platform's hash-chained log covers platform-mediated access; it does not see someone reading an object directly with a stolen credential. Phase 5.

**B5 — No psychotherapy bucket, key, or role on GCP.** Present on AWS (`s3_psychotherapy.tf`), absent on GCP. Invariant 6 names psychotherapy notes explicitly. Until this exists, GCP cannot serve a deployment whose corpus contains them, and the conformance probe should refuse rather than proceed.

**B6 — No retention enforcement anywhere.** Retention values are recorded as metadata and enforced by nothing. Bucket Lock is available and is irreversible once locked — which is the point.

Also: `core/healthcheck.py` has no GCP support and hard-fails. Phase 12 depends on it.

### 0.3 One decision I made rather than asking

**Do not use Cloud Healthcare API.** Google's FHIR store looks like a natural fit and is HIPAA-covered, but its docs state plainly: *"You can't de-identify CMEK-encrypted resources."* Invariant 6 requires CMEK on every PHI artifact and invariant 9 requires a de-identification boundary, so the native path cannot satisfy both. It is moot here — the platform's system of record is GCS objects (`core/storage/gcp_gcs.py`) and de-identification is in-platform, so Healthcare API adds a second authoritative store in violation of invariant 1 while solving nothing. Skip it. Recorded here so nobody re-proposes it in six months.

### 0.4 Whose compliance this is

Following this runbook end to end produces a deployment with the technical controls in place. **It does not make your organization HIPAA compliant, and nothing in this document should be read as saying it does.**

Compliance belongs to the organization that owns or manages the PHI. This runbook covers technical configuration only. It does not perform your Security Risk Analysis, write your policies, train your workforce, decide who gets access, determine what minimum necessary means for your uses, or detect and notify breaches. Every phase below assumes those exist separately and are someone's named responsibility.

Three phases produce artifacts you are attesting to, not results the platform verified — Phase 2 (BAA scope), Phase 11 (abuse-logging exemption, Speech logging), and Phase 13. The attestation's truth is yours.

See `docs/RESPONSIBILITY.md` for the full allocation and the operator obligation checklist.

---

## Phase 1 — Identity and the Organization

You cannot skip this. Without an Organization resource there are no folders, no inherited org policies, and no practical VPC Service Controls — every control becomes per-project and hand-reapplied. A Gmail-owned project is a dead end for regulated work, and there is no self-service path to migrate one into an org later.

**1.1 Buy a domain** you control (~$12/yr). A subdomain of an existing domain works — e.g. `phi.ryangomez.nyc` — provided you can set DNS TXT records on it.

**1.2 Sign up for Cloud Identity Free.** Not Workspace; Free is sufficient and costs nothing. Cap is 50 users, raised by request form.
→ https://docs.cloud.google.com/identity/docs/how-to/set-up-cloud-identity-admin

**1.3 Verify the domain via DNS TXT.** *This step is what creates the Organization resource.* Nothing before it exists in a hierarchy.
→ https://support.google.com/cloudidentity/answer/7331243

**1.4 Create a break-glass super-admin** on the domain, with a hardware key, credentials stored offline, and used for nothing else. Do not do daily work as super-admin.

**1.5 Create groups before granting anything to a person.** At minimum: `gcp-org-admins@`, `gcp-security@`, `gcp-billing@`, `gcp-developers@`, `phi-data-access@`. Every org-level IAM grant binds to a group, never to a user. This is what makes access reviewable a year from now.

```bash
gcloud organizations list          # capture ORG_ID
export ORG_ID=<numeric-id>
```

---

## Phase 2 — Billing, then the BAA

**2.1 Create the billing account** and link it at the organization so it is org-owned rather than tied to a personal identity.

**2.2 Accept the HIPAA Business Associate Agreement. Before any PHI touches anything.**
Console → **IAM & Admin → Privacy & Security** → `https://console.cloud.google.com/iam-admin/privacy` → "Google Cloud Platform HIPAA Business Associate Addendum" → **Review and Accept**. Once per billing account/org, not per project.
→ https://support.google.com/cloud/answer/6329727

Note a live documentation inconsistency: Google's compliance FAQ still says to "talk to your account manager about entering into a BAA." The console self-service flow is the current mechanism; sales handles negotiated enterprise agreements. If the console path is missing for your account, that is the escalation, not a blocker to work around.

**2.3 Record the covered-products position.** The list lives at https://cloud.google.com/security/compliance/hipaa#covered-products. Per invariant 7, presence of a service on that list is **not** sufficient evidence for a model target — coverage varies below the service level, and the BAA's own text excludes Pre-GA offerings from PHI use regardless of parent-service coverage. Phase 11 turns this into a check.

**2.4 Budgets.** Billing → Cost management → Budgets & alerts. Default thresholds 50/90/100%. Route to Pub/Sub if you want programmatic action.
**A budget is not a spending cap.** Google states explicitly that an alerts-only budget "doesn't automatically cap Google Cloud usage or spending." A runaway embedding job will not be stopped by one. Treat budgets as detection, and set API quotas for control.

---

## Phase 3 — Resource hierarchy

Environment-oriented, so the PHI boundary lands on a folder edge and org policies and the VPC-SC perimeter attach to `Production` wholesale.

```
Organization
├── Common/          → logging, monitoring, security tooling
├── Production/      → PHI. Perimeter attaches here.
│   ├── phi-ai-prod-app
│   ├── phi-ai-prod-data
│   └── phi-ai-prod-logs
└── Non-production/  → synthetic data only, per the data policy
    └── phi-ai-dev
```

```bash
gcloud resource-manager folders create --display-name="Production" --organization=$ORG_ID
gcloud projects create phi-ai-prod-data --folder=<PROD_FOLDER_ID>
gcloud billing projects link phi-ai-prod-data --billing-account=<BILLING_ID>
```

Separate app / data / logs projects give you per-project IAM boundaries for free, which is invariant 5 expressed as structure rather than policy. Non-production never receives real data — and under the project data policy there is no real data anyway, so "non-prod" here means synthetic corpus only, not "de-identified production."

---

## Phase 4 — Organization policies

Apply at the org node so everything inherits. Exact constraint IDs:

| Purpose | Constraint | Type |
|---|---|---|
| US-only resource locations | `constraints/gcp.resourceLocations` | list → `in:us-locations` |
| Require CMEK | `constraints/gcp.restrictNonCmekServices` | list (deny) |
| Restrict CMEK key source | `constraints/gcp.restrictCmekCryptoKeyProjects` | list (allow) |
| No SA key creation | `constraints/iam.managed.disableServiceAccountKeyCreation` | boolean |
| No SA key upload | `constraints/iam.disableServiceAccountKeyUpload` | boolean |
| Cloud SQL no public IP | `constraints/sql.restrictPublicIp` | boolean |
| Cloud SQL no authorized networks | `constraints/sql.restrictAuthorizedNetworks` | boolean |
| Uniform bucket-level access | `constraints/storage.uniformBucketLevelAccess` | boolean |
| Public access prevention | `constraints/storage.publicAccessPrevention` | boolean |
| Domain-restricted sharing | `constraints/iam.allowedPolicyMemberDomains` | list |
| No VM external IPs | `constraints/compute.vmExternalIpAccess` | list |
| Restrict API endpoints (Phase 11) | `constraints/gcp.restrictEndpointUsage` | list |

```bash
gcloud resource-manager org-policies set-policy --organization=$ORG_ID policy.yaml
```

Four things that will bite you:

- **`gcp.resourceLocations` is not retroactive.** Pre-existing resources keep running in whatever region they were created in. Set it before you create anything.
- **`sql.restrictPublicIp` is also not retroactive**, and it will block the current Terraform, which requires public IP. That is intentional — see B3. Do Phase 6 first.
- **`iam.allowedPolicyMemberDomains` can lock you out.** Your own org principal set is not automatically allowed. Include `is:<CUSTOMER_ID>` or you lose the ability to manage IAM.
- **New orgs already carry an enforced security baseline** including `iam.managed.disableServiceAccountKeyCreation` and `iam.allowedPolicyMemberDomains`. Check what is already enforced before writing Terraform that fights it: `gcloud resource-manager org-policies list --organization=$ORG_ID`.

*[UNVERIFIED]* `constraints/compute.skipDefaultNetworkCreation` — appears in Google's SCC IaC-validation list but I could not confirm it on the canonical constraints reference. Confirm with `gcloud resource-manager org-policies list-constraints --organization=$ORG_ID` before relying on it.

---

## Phase 5 — Audit logging (blocker B4)

The single most commonly missed HIPAA control on GCP. Data Access logs are off by default and must be enabled through the **IAM policy's `auditConfigs`**, not through any Logging setting.

```bash
gcloud organizations get-iam-policy $ORG_ID --format=yaml > policy.yaml
```

Add:

```yaml
auditConfigs:
- service: allServices
  auditLogConfigs:
  - logType: ADMIN_READ
  - logType: DATA_READ
  - logType: DATA_WRITE
```

```bash
gcloud organizations set-iam-policy $ORG_ID policy.yaml
```

- `exemptedMembers` suppresses logging per principal. For a PHI workload treat any exemption as a compliance exception requiring a written basis in COMPLIANCE.md, not a tuning knob.
- **Cost is real.** Google warns these logs "can generate large volumes of data" with additional charges. Budget for it. Do not let cost pressure silently narrow scope — if you reduce coverage, that is a documented decision with a basis, per invariant 3.
- Route to a log bucket in `phi-ai-prod-logs` with CMEK. Log-bucket CMEK must be configured **before** bucket creation and **cannot use the `global` region**:

```bash
gcloud logging settings update --folder=<PROD_FOLDER_ID> \
  --kms-key-name=projects/.../cryptoKeys/log-key --kms-location=us-central1
gcloud logging settings describe --folder=<PROD_FOLDER_ID>
```

**On excluding PHI from logs (invariant 2).** Cloud Logging has no global "strip payloads" switch. Sink exclusion filters are applied *after* the Logging API receives the entry — they reduce storage, not exposure. An exclusion filter is not a containment boundary. The durable control is upstream: never emit PHI, and disable request/response logging at each service.

---

## Phase 6 — Networking (blocker B3)

```bash
gcloud compute networks create phi-vpc --subnet-mode=custom \
  --project=phi-ai-prod-data
gcloud compute networks subnets create phi-subnet-us-central1 \
  --network=phi-vpc --region=us-central1 --range=10.10.0.0/20 \
  --enable-private-ip-google-access --enable-flow-logs \
  --project=phi-ai-prod-data
```

**Private connectivity for Cloud SQL.** Two options:

- **Private services access (VPC peering)** — simpler. Allocate a range, create the peering, then create the instance with `--network=phi-vpc --no-assign-ip`.
- **Private Service Connect** — avoids peering-range exhaustion and transitive-peering limits. `--enable-private-service-connect --allowed-psc-projects=...`.

Take PSA unless you already know you need PSC. Either one removes the public-IP requirement that B3 flags — the Cloud SQL Python connector works over private IP; the current `db_publicly_accessible = true` is an artifact of there being no VPC at all.

**VPC Service Controls.** This is the control that stops credential-theft exfiltration; IAM alone does not, because a valid stolen credential is still a valid credential.

```bash
gcloud access-context-manager perimeters dry-run create phi_perimeter \
  --perimeter-title="PHI AI Production" \
  --perimeter-resources=projects/<PROD_DATA_NUM>,projects/<PROD_APP_NUM> \
  --perimeter-restricted-services=storage.googleapis.com,sqladmin.googleapis.com,cloudkms.googleapis.com,aiplatform.googleapis.com \
  --policy=<POLICY_ID>
```

**Run dry-run first, for at least a full ingest cycle.** On enforcement, anything outside the perimeter breaks: your Terraform runner, `gcloud` from a laptop, the Cloud Console, CI. Your Terraform service account must be inside the perimeter or covered by an ingress rule. Note also that VPC-SC ingress policies support only `ANY_IDENTITY`, not specific principals — a coarser boundary than IAM, and it is a boundary *in addition to* IAM rather than a replacement.

*[UNVERIFIED]* Confirm the exact restricted-service strings for Storage, KMS, and SQL against the live supported-products table rather than trusting the list above: https://docs.cloud.google.com/vpc-service-controls/docs/supported-products

---

## Phase 7 — Terraform changes required before first apply

These close B1, B2, B3, B5, B6. **B1 and B2 must be done before the first apply** or you rebuild.

**7.1 Split the KMS keys** (B2). `kms.tf` currently creates one `store` key used by both buckets. Add distinct keys — at minimum `store`, `audit`, and (per 7.4) `psychotherapy` — each with its own IAM binding, so the audit key's encrypter grant is not held by any identity that can write PHI. The auditor SA holds decrypt on the audit key and nothing on the store key.

**7.2 Add CMEK to Cloud SQL** (B1). In `google_sql_database_instance.index`:

```hcl
encryption_key_name = google_kms_crypto_key.sql.id
```

The key must be **regional and co-located with the instance**. Multi-region and global keys are rejected. The `sqladmin` service identity needs `roles/cloudkms.cryptoKeyEncrypterDecrypter`:

```bash
gcloud beta services identity create --service=sqladmin.googleapis.com --project=phi-ai-prod-data
```

**7.3 Private IP** (B3). Replace `ipv4_enabled = var.db_publicly_accessible` with `ipv4_enabled = false` plus `private_network = <phi-vpc self_link>`. Delete `db_allowed_cidr_blocks` rather than leaving it as an unused escape hatch.

**7.4 Psychotherapy store** (B5). Port `s3_psychotherapy.tf` to GCS: separate bucket, separate key, separate service account holding no grant on the main store. Until it exists the conformance probe should refuse a corpus containing those resources rather than proceeding — invariant 3.

**7.5 Retention** (B6). Add `retention_policy` to the store and audit buckets. Bucket Lock is irreversible: the period can be increased but never decreased, and the bucket cannot be deleted until every object ages out. That is the point of it, and it is also why you set the value deliberately rather than copying a default.

**7.6 The five missing service accounts.** The GCP bootstrap SQL expects `{IMAGING_IAM_USER}`, `{SEARCH_IAM_USER}`, `{PSYCH_IAM_USER}`, `{DISPOSITION_IAM_USER}`, `{OPS_IAM_USER}` — Terraform creates only ingest, restore, and auditor. Add the other five as service accounts, `google_sql_user` entries, and IAM bindings, or the bootstrap SQL fails on substitution.

**7.7 Audit log config in Terraform.** Add `google_project_iam_audit_config` so Phase 5 is reproducible rather than a one-time console action.

---

## Phase 8 — Bootstrap and apply

```bash
git clone https://github.com/RyanGomez-NYC/phi-ai && cd phi-ai
cd deploy/gcp/bootstrap
terraform init && terraform apply -var="project=phi-ai-prod-data"   # tfstate bucket
cd ..
cp terraform.tfvars.example terraform.tfvars   # set gcp_project, environment, region
cp backend.hcl.example backend.hcl             # bucket = <project>-tfstate
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
```

**Review the plan against the asserted inventory** before applying — the existing runbook asserts 2 buckets, 1 key ring, 3 SAs; after Phase 7 you should see more keys, more SAs, private-IP SQL, and retention policies. A plan that matches the *old* assertion means your Phase 7 edits did not take.

```bash
terraform apply tfplan
```

Set `phi_retention_days` and `audit_retention_days` deliberately — they default to `1`, which is a development value.

---

## Phase 9 — Database bootstrap

```bash
gcloud sql users set-password postgres --instance=<INSTANCE> --prompt-for-password
```

Then load schema and the five GCP bootstrap files, substituting placeholders with the actual IAM-derived role names:

```
core/db/bootstrap_gcp.sql
core/db/omop_bootstrap_gcp.sql
core/db/retrieval_bootstrap_gcp.sql
core/db/telemetry_bootstrap_gcp.sql
core/db/users_bootstrap_gcp.sql
```

**Understand what the role names will be.** Cloud SQL derives the Postgres role name from the IAM principal's email and truncates `.gserviceaccount.com` because of Postgres's 63-byte identifier limit: `sa-ingest@phi-ai-prod-data.iam.gserviceaccount.com` becomes role `sa-ingest@phi-ai-prod-data.iam`. The name is not choosable and not renameable. This is why the bootstrap SQL only issues `GRANT` and never `CREATE ROLE`.

**The known GCP asymmetry, and how to not make it worse.** One IAM identity maps to exactly one Postgres role. The tempting shortcut is one service account granted membership in several group roles:

```bash
gcloud sql users create SA --instance=I --type=cloud_iam_service_account \
  --database-roles=index,omop,ai     # DON'T
```

With `INHERIT` group roles that session holds the union of all three privilege sets at all times — it collapses the separation rather than expressing it. **Use one service account per database role instead** (Phase 7.6 creates them). Each maps to its own Postgres role, each gets one grant, and minimum-necessary stays structural.

*[UNVERIFIED]* Whether Cloud SQL supports `NOINHERIT` group roles plus per-connection `SET ROLE` for IAM users is not documented. Do not build on it.

**pgvector.** The extension is named `vector`, not `pgvector`. On PG16: `CREATE EXTENSION vector;` (0.8.0), then `ALTER TYPE vector SET (STORAGE = external);`. HNSW is documented on Cloud SQL; *[UNVERIFIED]* IVFFlat support. Do not enable `google_ml_integration` — in-database calls to Gemini embeddings are a separate model-egress path that would bypass the gateway preflight and therefore invariant 7.

---

## Phase 10 — Compute

No GitHub Actions, per project policy. Build locally or with Cloud Build; note that **Cloud Build continuous deployment is unavailable inside a VPC-SC perimeter**, so the image path is a local build pushing to Artifact Registry.

**Artifact Registry — CMEK is creation-time only and irreversible.** It cannot be changed, rotated to a different key, or reverted to Google-managed. Key location must match the repository location.

```bash
gcloud artifacts repositories create phi-ai --repository-format=docker \
  --location=us-central1 --kms-key=projects/.../cryptoKeys/artifact-key
docker build -t us-central1-docker.pkg.dev/phi-ai-prod-app/phi-ai/app:1.0.0-rc1 .
docker push us-central1-docker.pkg.dev/phi-ai-prod-app/phi-ai/app:1.0.0-rc1
```

**Cloud Run** over GKE unless you need sidecars, DaemonSets, or long-lived stateful workers — same controls, far less surface.

```bash
gcloud run deploy phi-ai --image=<IMAGE> --region=us-central1 \
  --ingress=internal --no-allow-unauthenticated \
  --network=phi-vpc --subnet=phi-subnet-us-central1 --vpc-egress=all-traffic \
  --service-account=sa-app@phi-ai-prod-app.iam.gserviceaccount.com
```

For VPC-SC compatibility: org policy `run.allowedIngress` = `internal`, `run.allowedVPCEgress` = `all-traffic`, and the **image registry must be inside the same perimeter**.

**Secret Manager.** CMEK per secret, key co-located with the replica. Bind `roles/secretmanager.secretAccessor` **on the individual secret** — never at project level.

---

## Phase 11 — AI services

**11.1 Naming has changed.** Vertex AI is now **Gemini Enterprise Agent Platform**; Google's docs have physically moved from `/vertex-ai/generative-ai/docs/*` to `/gemini-enterprise-agent-platform/*`, and the whole documentation host moved to `docs.cloud.google.com`. **The API surface is still `aiplatform.googleapis.com`**, so code is unaffected — but any covered-products string match on "Vertex AI" will drift, and model-registry entries and runbook prose need updating.

**11.2 Endpoint restriction — a real preflight, not an attestation.** This is the best finding of this pass. `constraints/gcp.restrictEndpointUsage` restricts which API endpoints a project may call and explicitly supports Generative AI on Gemini Enterprise Agent Platform. It is readable and settable through the Org Policy API.

Set it to deny the global `aiplatform.googleapis.com` and permit only `us-*-aiplatform.googleapis.com`. **This upgrades invariant 7's "non-global endpoint" requirement from an operator attestation to a machine-checkable property** — the gateway preflight can read the policy. Recommend adopting it in `core/governance` preflight; that is an implementation improvement, not an invariant change.
→ https://docs.cloud.google.com/docs/security/compliance/restrict-endpoint-usage

**11.3 Abuse logging — not machine-checkable. Attestation required.** By default Google stores flagged prompts up to **90 days** in the selected region, readable by authorized Google employees. The exemption is a **Google Form**; if approved, Google stores no prompts. There is **no API, no config field, no console indicator** exposing whether the exemption is active. This confirms the recorded GCP asymmetry. The approval email is the evidence artifact — file it, reference it in COMPLIANCE.md.
(Google's abuse-monitoring page says 90 days, the ZDR page says "30+". Treat 90 as governing.)

**11.4 Two retention paths you must hard-block.** **Google Search grounding and Google Maps grounding are not zero-data-retention and cannot be opted out of** (30-day retention). Any PHI path must refuse them outright. Gemini Live session resumption stores up to 24h and is off by default — keep it off.

**11.5 One adjacent property IS queryable.** `GET projects/{PROJECT_ID}/cacheConfig` returns a `disableCache` boolean. Assert it. Small win, take it.

**11.6 The Pre-GA exclusion is the real gate.** The BAA text: *"Do not use Pre-GA offerings … in connection with PHI."* A model can be Pre-GA while its parent service sits on the covered-products list. **Registry checks must gate on model GA status, not service-name membership** — this is invariant 7's "coverage varies below the service level" made concrete.

**11.7 Speech-to-Text.** Default is no logging, which is the safe default. Enablement is a console-only toggle with no API to read or set it, so opt-out state is **not queryable → operator attestation**, same class as 11.3. Use regional endpoints (`us-speech.googleapis.com`); check whether `restrictEndpointUsage` covers `speech.googleapis.com` and use it if so.

---

## Phase 12 — Verification

```bash
python -m core.verify            # cross-flow verification
python -m core.healthcheck       # NOTE: no GCP support yet — hard-fails
scripts/check_fixtures.py        # every fixture HTEST-tagged and seed-reproducible
scripts/pre_push_gates.sh
```

`core/healthcheck.py` having no GCP path is a real gap, not a configuration mistake. Either add GCP support or record the substitute manual checks in COMPLIANCE.md — do not let a hard-fail read as a deployment error.

**Posture checks worth running by hand:**

```bash
gcloud storage buckets describe gs://<store> --format="value(encryption.defaultKmsKeyName,iamConfiguration.uniformBucketLevelAccess.enabled,retentionPolicy)"
gcloud sql instances describe <INSTANCE> --format="value(diskEncryptionConfiguration.kmsKeyName,settings.ipConfiguration.ipv4Enabled)"
gcloud organizations get-iam-policy $ORG_ID --format="yaml(auditConfigs)"
gcloud resource-manager org-policies describe gcp.restrictEndpointUsage --organization=$ORG_ID
```

Then the audit-chain verification and the delete smoke test from `RUNBOOK_GCP_SETUP.md` steps 8–10.

**Say it where the numbers appear:** a green suite against the synthetic corpus is a lower bound on error, not validation.

---

## Phase 13 — Operator attestations

Three GCP properties expose nothing queryable. Each needs a signed attestation with an evidence artifact, filed in COMPLIANCE.md. This is a permanent asymmetry versus AWS, not a gap to be closed later.

| Attestation | Evidence |
|---|---|
| Gemini abuse-logging exemption granted | Google approval email |
| Speech-to-Text data logging not enrolled | Console screenshot + dated sign-off |
| Cloud SQL IAM role separation via one-SA-per-role | Terraform + `\du` output showing disjoint grants |

---

## Known gaps and cost

**Gaps carried forward:** no GCP `core/healthcheck.py`; no CloudTrail-equivalent cross-check of the audit chain; Docker Compose ADC passthrough unverified; index/OMOP role separation permanently collapsed on GCP (documented asymmetry, not a defect).

**Cost drivers, largest first:** Data Access audit logs at PHI volume; embedding and re-embedding the corpus, where a serialization-template revision is a full-corpus cost; Cloud SQL tier (`db-f1-micro` is a dev default and will not carry production); log storage under CMEK; Artifact Registry and egress. Parameterize from `docs/COST.md` rather than guessing, and measure chunks-per-resource against the fixture corpus before committing capacity.

**Recommended first move:** run the whole of this in `Non-production` with the synthetic corpus, end to end, before creating a single production resource. B1, B2, and Artifact Registry CMEK are all creation-time-immutable — the cheapest place to get them wrong is a project you can delete.
