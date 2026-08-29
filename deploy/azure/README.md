# Azure deployment

Terraform for the Azure PHI AI Platform stack. See
`runbooks/RUNBOOK_AZURE_SETUP.md` for the step-by-step walkthrough -
this file is reference for what the stack contains and why.

## What gets created

| Resource | Purpose |
|---|---|
| `azurerm_storage_account.store` | Holds both containers below. Azure has no separate-account requirement the way AWS/GCP's bucket model does. Shared access keys disabled; blob versioning on; 7-day blob soft delete. |
| `azurerm_storage_container.store` | Envelope-encrypted PHI ciphertext (container name `fhir`). No immutability policy, CMK via Key Vault. |
| `azurerm_storage_container.audit` | Hash-chained audit log. Separate container, same storage account, own retention *value*. **No immutability policy either** - the same posture as the store container. |
| `azurerm_key_vault.main` / `azurerm_key_vault_key.store` | Wraps per-object data encryption keys. |
| `azurerm_user_assigned_identity.ingest` | Writes PHI, appends audit records. Holds a custom wrap-only role (see below) - no unwrap. |
| `azurerm_user_assigned_identity.restore` | Reads and decrypts PHI for authorized requests. |
| `azurerm_user_assigned_identity.auditor` | Reads the audit container only. No access to `azurerm_storage_container.store`, no Key Vault access at all. |
| `azurerm_postgresql_flexible_server.index` | Optional Postgres index (`phi_ai_index`), derived and rebuildable from Blob Storage. Off by default (`enable_db`). |

## Key design decisions

**One storage account, two containers.** Unlike AWS/GCP's separate
buckets, `core/config/settings.py`'s `azure_account_url` is a single
field - the `fhir` and `audit` containers live within one account
rather than in separate accounts. RBAC role assignments can scope to an
individual container, so the ingest/restore/auditor boundary is still
real; it is drawn at the container rather than at the account.

**A hand-written custom role for wrap-only access.** Azure Key Vault's
built-in roles bundle wrap and unwrap together - there is no predefined
"encrypt but not decrypt" role the way AWS IAM and GCP Cloud KMS both
offer. The ingest identity's narrower access required defining a custom
role explicitly, documented in `identities.tf`.

**Postgres role names are chosen freely here, unlike GCP.**
`pgaadauth_create_principal_with_oid()` lets a role name be set
independently of the authenticating managed identity, so the same ingest
identity is registered as both `phi_ai_ingest` (index) and `omop_etl`
(OMOP ETL) - genuine role separation, matching AWS. GCP cannot do this:
Cloud SQL IAM auth derives the role name from the service account email,
one identity to exactly one role. `outputs.tf`'s `env_fragment` emits
`phi_ai_ingest` / `phi_ai_reader` as literals, so those values must stay
in step with `core/db/bootstrap_azure.sql`.

**No immutability policy at all, on either container.** There is no
`azurerm_storage_container_immutability_policy` on `fhir` or on `audit`.
The container-level WORM policy this stack used to create has been
removed; nothing prevents a blob being deleted by a principal with the
RBAC permission to do so, and retention is recorded as blob metadata
only. See docs/COMPLIANCE.md's "Retention and integrity". Note, for
anyone reintroducing it: Azure's WORM immutability policy applies
uniformly to every blob in a container, configured once at the container
level - there is no per-object override mechanism the way S3 Object Lock
and GCS Object Retention Lock both support.

**Version-level WORM is an account-creation-time opt-in.** Azure's
version-level immutability support cannot be added to a storage account
after the fact. This stack does not enable it, so any account created
from this configuration is closed off from it permanently - adopting it
means a new storage account and a data migration. Decide before the
first apply.

**What deletion protection actually remains: one 7-day window.** Blob
versioning is on, and `delete_retention_policy` keeps deleted blobs for 7
days. That soft-delete window is the **only** thing standing between a
delete and permanent loss, and it is a recovery window, not a bar - it
buys you a week to notice a mistake, and it stops a deliberate deletion
not at all. Past 7 days there is nothing to restore from.

**Deletion is gated on RBAC alone.** `shared_access_key_enabled = false`,
so there is no account-key or SAS path into the data plane - every caller
is an Azure AD principal and every delete is an RBAC decision. That is a
genuine strength of this stack's identity model, and it is also the
entire perimeter: get the role assignments wrong and nothing downstream
catches it.

**Managed identities require compute attachment.** Unlike AWS's
`sts:AssumeRole` and GCP's service account impersonation, Azure managed
identities have no equivalent "borrow this identity temporarily from
anywhere" mechanism - they only work attached to a compute resource.
Local development against this stack does not exercise the same role
separation a real deployment gets; see the runbook's own honest account
of this limitation.

## Warnings

- **A locked immutability policy can never be unlocked**, and is
  irreversible for the full retention duration - not shortenable by you,
  by Microsoft Support, or by any principal, matching AWS COMPLIANCE mode
  and GCP's Locked retention mode exactly. This stack creates no such
  policy; if you add one, that decision is permanent.
- **Nothing prevents deletion of stored PHI or audit records.** Any
  principal with a blob-delete RBAC assignment can remove them, and past
  the 7-day soft-delete window the bytes are gone. Route
  `StorageDelete` diagnostic logs somewhere you actually read and alert
  on them - that alerting is the control, because no storage-side
  control exists.
- **`name_prefix` is capped at 11 characters on this cloud.** The storage
  account name is `name_prefix` + `store` + 8 subscription-ID characters
  against a hard 24-character Azure limit, lowercase alphanumeric only.
  Choose it before the first apply - storage account, Key Vault, key and
  identity names are all immutable.
- **RBAC role assignments can take time to propagate** before a
  dependent resource (e.g. the storage account's own CMK wiring) can
  successfully use them - this stack includes a `time_sleep` to absorb
  that delay; a manual apply outside Terraform may need to retry.
- **Local development cannot fully exercise role separation** - see
  "Key design decisions" above. Treat local testing as a functional
  check, not a security-boundary test.

## Known gaps

Stated plainly rather than left to be discovered. None of these is an
oversight.

- **No storage-level immutability on either container, in any
  environment.** No `azurerm_storage_container_immutability_policy`, no
  version-level WORM. Retention is blob metadata written by application
  code and enforced by nothing. If your risk assessment requires WORM,
  this stack does not provide it.
- **Version-level WORM cannot be retrofitted onto a storage account.**
  Azure requires the opt-in at account creation, and this stack does not
  take it. Adopting it is a new account plus a migration, not a Terraform
  variable.
- **The 7-day soft-delete window is the whole recovery story.** It is
  shorter than the equivalent exposure on AWS or GCP, where object
  versions and noncurrent generations persist until something explicitly
  removes them. On Azure, seven days after a delete there is nothing
  left to recover.
- **The ingest identity's Storage Blob Data Contributor grant includes
  delete**, broader than the AWS ingest role, which is denied delete
  outright. Azure ships no built-in "write and read but not delete" blob
  role - see the runbook's Known gaps item 7.
- **`core/db/bootstrap_azure.sql` creates no disposition or imaging
  principal.** Its DICOM grants name `phi_ai_imaging`, which that file
  never creates on Azure, and there is no Azure equivalent of AWS's
  `phi_ai_disposition` role. Closing either needs the imaging and
  disposition managed identities' object IDs exposed as new outputs
  here - a Terraform change, not a SQL edit. Until then, an Azure
  deployment that installs the optional imaging schema fails on those
  grants.
