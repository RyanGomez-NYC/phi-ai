# Runbook: Disposing of stored PHI (routine and admin-order)

Audience: whoever operates this deployment technically. Covers both
disposal tools this project ships - `core/fhir/purge.py` for the general
object store, and `core/fhir/psychotherapy_purge.py` for psychotherapy
notes specifically - and the Terraform roles both connect as
(`disposition_role_arn` / `psychotherapy_disposition_role_arn`, both
`deploy/aws/outputs.tf`).

> **New as of the 2026-08-17 audit (C4).** Before this pass, neither tool
> existed in its current form: the general purge tool deleted only the
> object in the object store (leaving the Postgres index row and any OMOP
> CDM row behind forever), and the psychotherapy bucket had **no disposal
> path at all** - no role in `deploy/aws/iam.tf` held a delete grant on
> it. Both gaps are closed here.

---

## Why two separate tools, not one with a `--target` flag

Same reasoning `runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md` gives for
`psychotherapy_restore.py` existing separately from `restore.py`: a
different bucket, a different KMS key, and now a different IAM role,
with zero access in either direction between the general `disposition`
role and the `psychotherapy_disposition` role. A single tool with a
`--target general|psychotherapy` flag would need to hold both roles'
credentials-assumption logic and both buckets' access - and a bug
picking the wrong target would be a cross-boundary PHI-handling bug in
the single most access-restricted part of this codebase. Two small,
genuinely separate tools instead.

## What "disposal" actually deletes

**General object store (`core/fhir/purge.py`), per resource key, in this
order:**

1. Its OMOP CDM row, if one exists - `core/db/omop_purge.py`'s
   `delete_by_source_storage_key()`. Skipped entirely if this
   deployment's OMOP layer isn't configured
   (`settings.omop_target_configured()`).
2. Its Postgres index row, if one exists - `core/db/index.py`'s
   `delete_index_entry()`. Skipped entirely if the Postgres index isn't
   configured (`settings.db_target_configured()`).
3. **Every stored version** of the S3 object itself -
   `core/storage/aws_s3.py`'s `delete_all_versions()`, not a
   single-version delete. A key that was ever overwritten (a corrected
   re-ingest) has more than one version; disposal that only removed the
   current version would leave earlier versions permanently recoverable,
   silently.

**Psychotherapy notes (`core/fhir/psychotherapy_purge.py`), per resource
key:** step 3 only. Psychotherapy notes are deliberately never indexed
and never ETL'd into OMOP (see `RUNBOOK_PSYCHOTHERAPY_NOTES.md`'s "Why
the Postgres index never sees this data") - there is no derived row to
clean up, so disposing a note is exactly "delete every stored version."

If steps 1 or 2 fail for a resource, step 3 is **not attempted** for
that resource - the storage object is left exactly as it was, and the
run continues to the next item rather than aborting. See "What a failed
run looks like" below for why this direction of failure is the safe one.

## The two modes, both tools

**`expired` mode** - routine, always available, needs no special IAM
permission beyond an ordinary delete grant. Can only ever touch an
object whose `retention_until` has already passed.

**Read this before relying on that sentence.** It used to be enforced by
S3 Object Lock, independent of this tool's own logic. Object Lock has
been removed from this deployment, so the retention check now lives
entirely in application code — the candidate list is built from recorded
metadata, and each object's retention date is re-read and re-verified
immediately before it is deleted. A bug in that code deletes real records
early, silently and permanently; nothing in storage will stop it.
Objects carrying no recorded retention date are skipped, never treated as
expired.

```bash
python -m core.fhir.purge expired --role-arn $(terraform -chdir=deploy/aws output -raw disposition_role_arn)
python -m core.fhir.purge expired --role-arn <arn> --confirm

python -m core.fhir.psychotherapy_purge expired --role-arn $(terraform -chdir=deploy/aws output -raw psychotherapy_disposition_role_arn)
python -m core.fhir.psychotherapy_purge expired --role-arn <arn> --confirm
```

Defaults to a dry run - prints what would be disposed of and deletes
nothing until `--confirm` is passed.

**`admin-order` mode** - exceptional, removes specifically-named records
*before* their retention date under a stated administrative basis.
**Nothing categorically prevents this mode anymore.** It previously
refused outright when the deployment ran in COMPLIANCE mode, and that
refusal was real because S3 would have rejected the delete regardless.
With Object Lock removed there is no such posture to detect, so the check
is gone rather than left as a reassuring no-op. What remains is entirely
procedural and IAM-level: `deploy/aws/variables.tf`'s
`enable_admin_order_purge = true` (off by default — and now a primary
control rather than a secondary one), a non-empty `--admin-basis` that
becomes a required `AdminBasis` session tag the IAM policy checks before
allowing the delete at all, and typed interactive confirmation. Never
scriptable unattended.

```bash
python -m core.fhir.purge admin-order \
    --role-arn <disposition-role-arn> \
    --resource-type DocumentReference --resource-id eSyn0001Note \
    --admin-basis "Subpoena, Case No. 2026-CV-1234, Superior Court of Example County, dated 2026-08-15" \
    --confirm
```

See each tool's own module docstring for the full `--resource-list`
batch syntax and every guarantee listed above - this runbook summarizes
operational use, not the complete contract.

## Multi-resource batches and FK ordering

A single disposed resource's OMOP row lives in at most one table
(`source_storage_key` is `UNIQUE` per table - `core/db/omop_schema.sql`).
The real hazard is `cdm.person` and `cdm.visit_occurrence`: every other
event table references them with a plain foreign key and **no `ON
DELETE CASCADE`**, deliberately (`core/db/omop_purge.py`'s own
docstring) - a person/visit row disappearing should never silently take
other, independently-retained clinical events with it.

Both `purge.py`'s `expired` and `admin-order` modes sort a
multi-resource batch into FK-safe order before attempting any deletes -
`Encounter` before `Patient`, everything else before both - matching
`omop_purge.py`'s own `_OMOP_DELETE_ORDER` at the table level. Disposing
"this whole patient's record set" in one run works correctly as long as
every dependent resource (their Encounters, Conditions, Procedures,
etc.) is included in the same batch. If it isn't - if a still-retained
Condition references a Patient you're trying to dispose of - the
`cdm.person` delete correctly **fails** with a foreign-key violation.
That failure is the intended, safe outcome (see `omop_purge.py`'s
`delete_by_source_storage_key()` docstring): it means something for that
patient is still supposed to be retained, and disposing the person
record underneath it would silently orphan that fact rather than
respecting it.

## What a failed run looks like

Neither tool aborts a whole run on one resource's failure. Failures are
collected, printed plainly (`FAILED: <key>: <error>`) at the end, and
produce a non-zero exit code - the same `had_errors` discipline
`core/fhir/bulk_scheduler.py`'s own 2026-08-17 H2 fix established. A
non-zero exit is **not a clean run**; do not treat "some objects
disposed" as equivalent to "the batch completed" - investigate every
FAILED line before considering an admin-order removal fulfilled. The
most common cause is the FK-ordering hazard above: a dependent resource
that needed to be in the same batch wasn't.

## Configuring the Postgres connection (general object store only)

`purge.py`'s OMOP/index deletes require `PHI_AI_DISPOSITION_DB_USERNAME`
to be set (`core/config/settings.py`'s `disposition_db_configured()`) -
`terraform output -raw env_fragment` includes this automatically when
`enable_db = true`. Its literal value is `phi_ai_disposition`, matching
the role `core/db/bootstrap_aws.sql` creates. Type it exactly.

Getting this value wrong does not produce an error, which is why it is
worth checking rather than assuming: without a usable
`PHI_AI_DISPOSITION_DB_USERNAME`, `purge.py` falls back to storage-only
disposal - the correct, intended behavior for a deployment with no
Postgres index configured at all, but **not** what you want if you've
actually bootstrapped the database. The result is records deleted from
storage whose index and OMOP rows survive. If you expect index/OMOP rows
to be cleaned up and they aren't, check this first.

`psychotherapy_purge.py` needs no database configuration at all - see
"What 'disposal' actually deletes" above.

## Known gaps

- **AWS only.** Both tools assume AWS (`Settings.cloud_provider != "aws"`
  exits immediately). GCP and Azure have no disposition role or purge
  tooling yet - the same "provider gap, not silently degraded" posture
  `core/storage/base.py`'s `delete_object`/`delete_all_versions`
  docstrings describe for their own AWS-only implementations today.
- **No cross-tool listing.** Neither tool can currently report "what's
  about to expire in the next N days" ahead of running `expired` mode -
  an operator finds out by running the dry run itself. A dedicated
  reporting command would be a reasonable follow-up, not built here.
- **`--resource-list` batches for admin-order mode are not currently
  size-limited.** A very large batch means a very large typed
  confirmation prompt (the printed list) before the count-confirmation
  step - functional, but not designed for thousands of entries at once.
