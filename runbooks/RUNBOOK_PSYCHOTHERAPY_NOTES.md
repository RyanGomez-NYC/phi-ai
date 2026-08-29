# Runbook: Psychotherapy notes

Audience: technical staff setting up or maintaining this capability, and
anyone who needs to retrieve a stored psychotherapy note. Read the
callout below before anything else in this document.

> **Read this first: why this is a separate system, not a setting.**
>
> 45 CFR §164.508(a)(2) requires a covered entity to obtain
> authorization for **nearly any** use or disclosure of psychotherapy
> notes - not just the usual "share with a third party" cases. Only
> three narrow exceptions don't require it: use by the note's own
> author for treatment, the covered entity's own training programs, and
> defending itself in litigation the patient brought. An ordinary
> records request - what `runbooks/RUNBOOK_DATA_RESTORE.md`'s general
> restore role exists for - satisfies none of these.
>
> HIPAA's own definition (45 CFR §164.501) also requires these notes be
> "maintained separate from the rest of the individual's medical
> record" - and real HIPAA training materials are explicit that a
> superficial distinction, like colored paper in the same physical
> chart, does not count as separate. A different S3 *prefix* within the
> same bucket, readable by the same role, is the cloud-storage
> equivalent of colored paper in the same chart. That's why this is a
> **separate S3 bucket, separate KMS key, and separate IAM roles** -
> not a tag on the general object store.

---

## What's built and confirmed

- `deploy/aws/s3_psychotherapy.tf` - a dedicated bucket with the same
  (absent) Object Lock posture as the general object store, provisioned
  unconditionally (not behind a variable) whenever this stack is applied.
- `deploy/aws/kms.tf` - a dedicated KMS key, always provisioned (not an
  optional cost trade-off, unlike `separate_audit_key`).
- `deploy/aws/iam.tf` - four dedicated roles:
  - `psychotherapy_ingest` - write-only, cannot decrypt, no access to
    the general PHI bucket.
  - `psychotherapy_restore` - the core control. Denies every read
    unless the session carries a `PsychotherapyException` tag set to
    exactly one of `originator-treatment` / `training-program` /
    `legal-defense`, plus a `PsychotherapyAttestation` tag recording who
    is claiming the exception and why. The general `restore` role has
    no access to this bucket at all.
  - `psychotherapy_disposition` (2026-08-17 audit, C4 - disposal
    completeness) - deletes notes whose retention has expired
    (routine), or specifically-named notes under a stated
    administrative basis when `enable_admin_order_purge` is true
    (exceptional). Cannot decrypt notes under any circumstance. The
    general `disposition` role has no access to this bucket at all, and
    this role has no access to the general PHI bucket - see
    `runbooks/RUNBOOK_DISPOSITION.md` for full operational detail. Until
    this role existed, **nothing** in this file held any delete grant on
    the psychotherapy bucket - the single most access-restricted data
    class in this system was also the one with no disposal path
    whatsoever, structurally unable to satisfy HIPAA's own disposal
    requirement (45 CFR 164.310(d)(2)(i)) for psychotherapy notes
    specifically. That gap is closed as of this audit pass, not before.
- `core/fhir/client.py`'s `store_psychotherapy_resource()` - writes
  through the separate storage/key, uses a `notes/` key prefix (matching
  the IAM scope exactly), and records a distinct
  `record.write.psychotherapy` audit action. **Deliberately never writes
  to the Postgres index**, even if one is configured on the same client
  - see the note below on why.
- `core/fhir/psychotherapy_restore.py` - the only way this codebase
  retrieves psychotherapy note content. Requires `--exception` (one of
  the three values above) and `--attestation` (free text) on every
  invocation; both become AWS session tags, so the IAM policy enforces
  them even if this script is bypassed. Reuses the same
  `EnvelopeEncryptor`/`AWSKMS` classes the rest of this project uses for
  decryption, and the same `restore_one()` fetch-verify-decrypt logic
  `core/fhir/restore.py` uses (shared via `core/fhir/restore_common.py`)
  - not a separate reimplementation, so a fix to that logic (like the
  sha256 integrity fix documented in `docs/COMPLIANCE.md`) covers both
  restore paths at once. Records a distinct `record.read.psychotherapy`
  audit action to the same hash-chained log `store_psychotherapy_resource()`
  writes to, on top of the independent CloudTrail record the IAM
  session tags already produce - both traces exist for every successful
  retrieval, not just one.
- `core/fhir/psychotherapy_purge.py` (2026-08-17 audit, C4) - the
  psychotherapy-bucket twin of `core/fhir/purge.py`, and the only way
  this codebase disposes of a psychotherapy note. Simpler than the
  general tool in one real way: since notes are never indexed or ETL'd
  (see below), disposal here is exactly "delete every stored version of
  the object," with no derived-row cleanup step. See
  `runbooks/RUNBOOK_DISPOSITION.md` for the full operational runbook
  covering both disposal tools together.
- `deploy/aws/outputs.tf` - `psychotherapy_bucket`,
  `psychotherapy_kms_key_arn`, `psychotherapy_ingest_role_arn`,
  `psychotherapy_restore_role_arn`, and (2026-08-17 audit, C4)
  `psychotherapy_disposition_role_arn` outputs, and `env_fragment`
  includes the bucket/key lines unconditionally (this bucket always
  exists, unlike the optional Postgres index). This was a real,
  previously-open gap, twice: earlier versions of this runbook said the
  first four outputs didn't exist yet, and `psychotherapy_disposition_role_arn`
  specifically didn't exist until the role itself did.

> **The two psychotherapy audit actions matter more than any others in
> this product, so know how to find them.** Every write is recorded as
> `record.write.psychotherapy` and every retrieval as
> `record.read.psychotherapy`, in the same hash-chained log everything
> else uses. These notes are the one data class where §164.508(a)(2)
> requires authorization for nearly any use or disclosure, so the
> complete list of who read what and under which claimed exception is
> the whole point of the trail - for a §164.528 accounting and for any
> internal review.
>
> Over an exported log:
>
> ```bash
> grep -E '"action": *"record\.(read|write)\.psychotherapy"' exported-audit.jsonl
> ```
>
> Cross-reference against CloudTrail for the same window: the IAM
> session tags produce an independent record of every retrieval,
> including the claimed exception and the attestation, so the two should
> agree. A retrieval in CloudTrail with no matching entry here means
> something reached the bucket outside this codebase - see
> `RUNBOOK_INCIDENT_RESPONSE.md`.

All of the above is tested - the storage separation (a psychotherapy
write never touches the general PHI bucket, and vice versa), the "no
fallback" behavior (misconfiguration fails loudly, never silently
degrades to the general object store), the index exclusion, the shared
restore logic's decrypt/integrity-check behavior against both correctly
stored and deliberately tampered data, and (2026-08-17 audit, C4) the
disposition role's delete grant and its complete absence of any KMS
permission - proven against live PostgreSQL 16 and hand-verified S3 stub
behavior the same way the rest of that audit pass's C4 work was. The
Terraform additions were diffed byte-for-byte against the live files
before each change was pushed, to confirm zero risk to the existing roles
and resources around them.

## Why the Postgres index never sees this data

Even *structural* metadata - that a psychotherapy note exists for a
given patient, with no content attached - is treated as sensitive here.
The general Postgres index is queryable by the ordinary read-only role
used for regular records requests (`phi_ai_reader`), and that role has no
business learning "this patient has psychotherapy notes on file," even
without being able to decrypt anything. So there is no index for this
data at all, by design, not as an oversight to fix later. This is also
why `psychotherapy_purge.py` needs no database configuration at all,
unlike `core/fhir/purge.py` - there is no derived row anywhere to delete
alongside the storage object.

## What still needs your team's investigation

Honestly: I could not find verified, specific documentation of exactly
how Epic exposes behavioral health / psychotherapy note content through
its FHIR APIs - general searches on this turned up Epic's general API
catalog, not a confirmed mechanism for this specific case. (The
questions below are phrased for Epic because that is the instance this
section was written against; a deployment sourcing from any of the other
profiled vendors faces the same questions verbatim - substitute that
vendor's documentation and support channel, and expect even less public
detail than Epic offers.) Given you described your instance as having
both a distinct behavioral health module *and* type/category coding, the
concrete questions to resolve before wiring up ingestion:

1. **Does the behavioral health module expose data via the same FHIR
   base URL, or a separate endpoint/scope entirely?** Check your Epic
   instance's own documentation, or ask your Epic team directly - this
   determines whether ingestion needs a new `EMRProfile`-equivalent or
   can reuse the existing connection with a different Incoming API
   registration.
2. **What resource type and search parameters retrieve this content?**
   If it's `DocumentReference` with a specific `.type` coding, get the
   exact code(s) - the standard US Core value set for this field
   contains over 1,000 LOINC codes, so "some `.type` value" isn't
   specific enough to build against with confidence.
3. Once you have both answers, the integration point is simple by
   design: however your team retrieves the resource dict, hand it to
   `client.store_psychotherapy_resource(resource)` - everything after
   that (storage routing, encryption, audit, retention, and now
   disposal) is already built and tested.

**Do not build content-based detection that inspects regular clinical
`DocumentReference` resources after the fact and reroutes the
"psychotherapy-looking" ones.** Per the callout at the top, that's
almost certainly not genuine separation in HIPAA's own sense of the
term, even if it works mechanically.

## Deploying this

1. Review the Terraform plan carefully (same discipline as
   `RUNBOOK_AWS_SETUP.md` throughout) - `terraform plan` should show
   exactly one new bucket, one new KMS key, and four new IAM roles
   (ingest, restore, disposition, plus the general object store's own
   `disposition` role gaining its `ConnectToDispositionDatabase`
   statement if the database is enabled), with zero changes to any
   other existing resource.
2. Set `PHI_AI_PSYCHOTHERAPY_STORAGE_BUCKET` and
   `PHI_AI_PSYCHOTHERAPY_KMS_KEY_ID` in `.env` - both are included
   automatically in `terraform output -raw env_fragment` (see
   `.env.example`), the same as the rest of the storage configuration.
3. Complete the investigation above before building an ingestion
   scheduler for this data.

## Restoring a note

```bash
python -m core.fhir.psychotherapy_restore \
  --resource-type DocumentReference --resource-id <id> \
  --role-arn $(terraform -chdir=deploy/aws output -raw psychotherapy_restore_role_arn) \
  --exception originator-treatment \
  --attestation "Dr. Jane Smith, treating clinician, retrieving own prior note" \
  --output ./restore-output/
```

(**Take the role ARN from the Terraform output, not from memory.** The
role name is built from `name_prefix`, so a deployment that set that
variable to anything other than the default has a correspondingly
different ARN. Substituting the output as shown above removes the
question entirely.)

`--output` is required (fixed 2026-08-17 audit, MEDIUM, "psychotherapy_restore
prints the decrypted note to stdout") - the note is written to a JSON
file under that directory, matching `core/fhir/restore.py`'s own
`--output` convention, rather than being printed to the terminal.
Delete the local plaintext copy once delivered, the same as
`runbooks/RUNBOOK_DATA_RESTORE.md` step 4 already instructs for the
general restore path.

`--role-arn` comes from `terraform output psychotherapy_restore_role_arn`
in `deploy/aws/`. Bucket, region, and KMS key come from `.env`'s
`PHI_AI_PSYCHOTHERAPY_*` settings, not a separate flag - this can
never point at a different bucket than what's actually configured for
this deployment.

Requires an MFA-verified AWS session already in place (the role's trust
policy requires it, the same as the general `restore` role). `--exception`
must be exactly one of the three values - anything else is rejected
before any AWS call is made, and would also be denied by IAM even if it
somehow wasn't. **Those three values must never be changed**: they are
IAM session tag values matched literally by the role's trust and
permission policies, so renaming one in the CLI without changing both
policies in the same apply breaks the control itself, and changing them
in the policies without the CLI locks every legitimate retrieval out.

**What this does not and cannot verify:** that the claimed exception is
actually true - e.g., that the person running this really is the note's
own original author. That's an organizational control (verify identity
and role before running this, the same way you'd verify identity before
handing someone a paper chart), not something IAM or this script can
confirm against the real world. The attestation is a recorded claim,
visible in CloudTrail and the audit trail, not a cryptographic proof.

## Disposing of a note

See `runbooks/RUNBOOK_DISPOSITION.md` for the complete runbook, covering
both `core/fhir/purge.py` (general object store) and this bucket's own
`core/fhir/psychotherapy_purge.py` together. In short:

```bash
python -m core.fhir.psychotherapy_purge expired \
  --role-arn $(terraform -chdir=deploy/aws output -raw psychotherapy_disposition_role_arn) \
  --confirm
```

`psychotherapy_purge.py`'s storage client is bound to
`settings.psychotherapy_storage_bucket` only - it cannot reach, and has
no IAM grant to reach, the general PHI bucket. Use
`core/fhir/purge.py` for that instead; the two tools are not
interchangeable and neither role can assume the other's permissions.
