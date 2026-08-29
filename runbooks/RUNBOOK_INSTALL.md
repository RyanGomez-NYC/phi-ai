# Runbook: Install

A short checklist covering the whole install flow at a glance. All three
clouds - AWS, GCP, and Azure - have a complete Terraform stack (see
README.md's Status section); pick whichever your organization is on and
start with that cloud's own setup runbook -
`runbooks/RUNBOOK_AWS_SETUP.md`, `runbooks/RUNBOOK_GCP_SETUP.md`, or
`runbooks/RUNBOOK_AZURE_SETUP.md` - which is the detailed, step-by-step
version of Steps 1-2 below and should be your actual starting point, not
this document. Use this one as a checklist to confirm you haven't
skipped anything, not as the primary walkthrough.

FOUND AND FIXED (2026-08-17 audit, H8): this section previously said GCP
and Azure's `deploy/` Terraform was "still a stub" - true early in this
project's history, but stale by the time of this audit: all three
clouds' Terraform stacks (versioned storage, no WORM, KMS-backed
envelope encryption, IAM/RBAC role separation, an optional Postgres
index) were already complete, each with its own setup runbook, and
README.md's own Status section already said so correctly. A GCP or Azure
evaluator reading this checklist as their starting point - the audience
this document explicitly serves - would have read "still a stub," taken
it as authoritative since it's the install checklist, and walked away
from a cloud that was actually fully supported.

## Audience
Operator with admin access to the target cloud account and to your EMR
vendor's developer console (Epic's `fhir.epic.com`, Oracle Health's
console, the athenahealth, eClinicalWorks or NextGen portals, or the
MEDITECH Greenfield Workspace - see `docs/EMR_CONNECTORS.md` for each
vendor's registration path).

## Prerequisites

- [ ] A signed Business Associate Agreement (BAA) with your cloud
      provider, active for the target account, before provisioning
      anything.
- [ ] Admin access to provision: object storage, a KMS key, an IAM
      role/service account, and (optional) a Postgres instance for the
      queryable index.
- [ ] A backend app registered with your EMR vendor. For Epic (the
      default vendor): a Backend Services app registered at
      [fhir.epic.com](https://fhir.epic.com), with an RSA keypair
      generated via `scripts/generate_epic_keypair.sh` and the public
      half hosted at a JWK Set URL. **Epic backend services auth does
      not use a client secret** - it uses a signed JWT client assertion
      instead (RFC 7523); there is no shared secret to provision or
      leak. The other SMART Backend Services vendors (Oracle Health,
      eClinicalWorks, MEDITECH, NextGen) use the same keypair model
      through their own portals; athenahealth issues a client secret
      instead. See `docs/EMR_CONNECTORS.md` for each vendor's chapter
      and `runbooks/RUNBOOK_AWS_SETUP.md` Step 6 for the full Epic
      registration walkthrough.
- [ ] Docker and Docker Compose installed on the target host.
- [ ] Decided the retention period, confirmed against `docs/COMPLIANCE.md`
      for your state and data type. There is no Object Lock mode to
      choose anymore - retention is recorded, not enforced. Read
      `docs/COMPLIANCE.md` → "Retention and integrity" before deciding
      whether that posture is acceptable for your data.

## Steps

### 1. Provision infrastructure

For AWS: `runbooks/RUNBOOK_AWS_SETUP.md` Steps 1-4 (state backend
bootstrap, `terraform apply`, capturing outputs into `.env`). For GCP or
Azure: the equivalent steps in `runbooks/RUNBOOK_GCP_SETUP.md` or
`runbooks/RUNBOOK_AZURE_SETUP.md` - same overall shape, genuinely
different mechanics in places (each runbook's own "Known gaps" section
says where). Your chosen cloud's own runbook is the source of truth for
this step; summarizing it again here would only risk drifting out of
sync with it.

This provisions, per `docs/ARCHITECTURE.md`:
- A versioned object storage bucket/container (no immutability lock) for
  ingested PHI, plus a separate one for psychotherapy notes
  (`runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md`) and one for the audit log
- Customer-managed KMS keys
- Least-privilege IAM roles/service accounts, each scoped to only the
  bucket(s)/container(s) and key(s) it needs

Confirm the storage was created **without** immutability (Object Lock /
Object Retention Lock / immutability policy). That is the intended
posture, and nothing in this codebase knows how to work with a locked
bucket. See `docs/COMPLIANCE.md` → "Retention and integrity".

### 2. Run the guided installer

```bash
python3 install/installer_chatbot.py
```

Answer the prompts - cloud provider, storage/KMS values (from Step 1's
Terraform outputs), EMR connection details, and retention settings
including the retention period. If your source EMR is not Epic, also set
`PHI_AI_EMR_VENDOR` in `.env` to the vendor key (`epic`, `cerner`,
`athenahealth`, `eclinicalworks`, `meditech`, or `nextgen`; default
`epic`) - it selects the vendor's capability profile, and it is
validated at startup against the profile table, so a typo refuses to
start next to its cause rather than surfacing as a confusing
authentication failure mid-run. This writes a `.env` file (mode 600, not
committed to version control). If `.env` already has values from Step
1's `terraform output env_fragment` (including the optional
`PHI_AI_DB_*`/`PHI_AI_PSYCHOTHERAPY_*` lines), re-entering the
same storage/KMS values here is safe and just confirms them in place -
anything this script doesn't ask about is preserved, not discarded (see
`install/installer_chatbot.py`'s own module docstring for the 2026-08-17
audit fix that made this true; earlier versions of this script silently
overwrote the whole file).

### 3. Install and start

```bash
./install/install.sh
```

(`install.sh` defaults to `.env` in the current directory; pass a path
explicitly - `./install/install.sh /path/to/other.env` - only if using a
non-default location. It automatically starts only `app` and `scheduler`
if `PHI_AI_FHIR_GROUP_ID` isn't set in your `.env`, avoiding the
`bulk-scheduler` restart-loop a bare `docker compose up -d` would
otherwise cause - see that script's own comments.)

### 4. Post-install verification

- [ ] Run a test ingest cycle against a **non-production** FHIR sandbox
      first, not a live production EMR instance.
- [ ] Confirm audit chain integrity:
      `docker compose exec app python -m core.audit.verify`
- [ ] Confirm a deletion is **detected and attributable**. This is the
      integrity control now - there is no lock to demonstrate, and a
      check expecting the storage layer to refuse a delete would fail
      against a correctly configured deployment. Against the sandbox,
      write a test object, delete it, then confirm the surviving prior
      version and the corresponding entry in the cloud's own access log
      (CloudTrail data events / GCS Data Access logs / Azure storage
      diagnostics). Your cloud's setup runbook gives the exact commands.
- [ ] Confirm TLS is enforced end-to-end (EMR↔app, app↔storage,
      app↔KMS) - every bucket/container's policy denies non-TLS access
      and TLS below 1.2 explicitly; `terraform plan` output from Step 1
      is where to confirm this before it's even deployed.
- [ ] File the completed install checklist as part of your HIPAA Security
      Risk Assessment documentation.

## Rollback

If install fails partway: `docker compose down`, fix the `.env`, re-run
`install.sh`. Infrastructure provisioned by Terraform is not affected by
application-layer failures, so `terraform destroy` is not a
troubleshooting step - it is a way to delete ingested PHI and, on GCP, to
put any surviving ciphertext permanently beyond recovery. Fix the
application layer at the application layer.
