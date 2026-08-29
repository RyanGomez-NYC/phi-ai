# Runbook: Postgres index maintenance and reconciliation

Audience: whoever operates this deployment technically - not a one-time
setup step like `RUNBOOK_AWS_SETUP.md`, but a periodic health check plus
the procedure for the rare case where the index and S3 have drifted.

> **Read this first.** S3 is the system of record; the Postgres index is
> a derived, rebuildable convenience holding structural metadata only -
> see `core/db/schema.sql`. Neither `phi_ai_ingest` nor
> `phi_ai_reader` can UPDATE or DELETE an index row - that's a
> deliberate design decision (`core/db/bootstrap_aws.sql`), not something
> this runbook works around. As of the 2026-08-17 audit (C4), a THIRD
> role, `phi_ai_disposition`, genuinely CAN delete an index row -
> but only the one row for a resource actually being disposed of, via
> `core/fhir/purge.py`, and only as the narrow side effect of removing
> that resource from the object store entirely. See
> `runbooks/RUNBOOK_DISPOSITION.md`. That is a different thing from what
> this runbook's manual cleanup procedure (Step 4) does with the RDS
> **master** credential, the same one used exactly once during initial
> setup (`RUNBOOK_AWS_SETUP.md` Step 6a) and never touched by the running
> application.
>
> Every `phi_ai_*` name in this runbook - the three roles above, the
> `phi_ai_index` database and the `phi_ai_master` user in Step 4 - is a
> literal identifier created by the bootstrap SQL and granted in
> Terraform. Type them exactly as printed or the connection and the
> grants will not match.

---

## Step 1 - Run the reconciliation report

```bash
python -m core.db.reconcile
```

Uses the same permissions the restore role already holds
(`s3:ListBucket` + the read-only `phi_ai_reader` Postgres user) -
no elevated access needed for this step. Makes zero writes to anything.

Exit code `0` means the index and S3 agree exactly. Exit code `2` means
drift was found (see below). Exit code `1` means the tool itself
couldn't run (e.g. `PHI_AI_DB_HOST` isn't set).

## Step 2 - Interpreting the two kinds of drift

**Missing index rows** (an S3 object with no index entry) are expected
in ordinary operation - typically because indexing was enabled after
some ingestion had already happened, or an individual index write failed
and was logged-and-swallowed (see `core/fhir/client.py`'s
`store_resource()`; S3 remains the system of record either way, so the
resource is safely stored regardless).

**Recovery is just re-running the scheduler:**

```bash
python -m core.fhir.scheduler --once
# or, if using bulk export:
python -m core.fhir.bulk_scheduler --once
```

Ingestion is idempotent - `core/db/index.py`'s `write_index_entry()`
silently absorbs a duplicate `(resource_type, resource_id)` rather than
erroring - so this safely backfills anything already in S3 but missing
from the index, without risk of storing anything twice.

**Orphaned index rows** (an index entry with no corresponding S3
object) are a different matter.

> **FIXED (2026-08-17 audit, C4).** This section previously read
> "Nothing in this codebase currently deletes an S3 object after
> storage, so this should be rare" - that was true when it was written,
> and stopped being true the moment `core/fhir/purge.py` gained its
> disposal-completeness fix: routine `expired`-mode disposal and
> exceptional `admin-order` disposal (see
> `runbooks/RUNBOOK_DISPOSITION.md`) now both delete the index row for a
> resource in the SAME operation as removing it from storage. An
> orphaned index row should therefore be **impossible** for a resource
> disposed of through `purge.py` on a deployment with
> `PHI_AI_DISPOSITION_DB_USERNAME` configured - `purge.py` removes
> the index row and the storage object together, or (on a partial
> failure) removes neither, never one without the other. A deployment
> that has never run `purge.py`, or that runs it without the disposition
> database username configured (a supported, storage-only disposal mode
> - see `RUNBOOK_DISPOSITION.md`), can still see genuine drift here: the
> object is gone from storage but its index row was never touched. Do
> not assume every orphaned row you see now is necessarily the
> unauthorized-deletion scenario Step 3 below investigates - it may
> simply be a disposal that ran in storage-only mode. Still worth
> understanding via Step 3 either way, not routine cleanup to wave
> through.

## Step 3 - Investigate before cleaning up

Do not assume an orphaned row is safe to remove just because reconcile
reported it. Find out *why* the S3 object is gone first - given how
this stack is built, a real deletion could only have happened one of a
few ways, and which one matters:

1. **Check CloudTrail for the `DeleteObject` event on that exact S3
   key.** This is the independent, out-of-band record - the same
   cross-reference `RUNBOOK_INCIDENT_RESPONSE.md` uses - and it exists
   regardless of whether the deletion went through this application at
   all.

   > **FIXED (2026-08-17 audit, MEDIUM, "CloudTrail cross-check
   > limitations").** This step previously pointed at `aws cloudtrail
   > lookup-events`, which is wrong for this purpose and would have
   > silently misled an investigation: AWS's Event History /
   > LookupEvents API returns MANAGEMENT events only. `DeleteObject` on
   > an individual S3 object is a DATA event, and LookupEvents never
   > returns those, regardless of trail configuration or how recent the
   > event is - running the command below would return zero results for
   > every real deletion, which reads exactly like "no CloudTrail record
   > exists," the false-negative this whole step exists to avoid. The
   > `auditor` role also held no read grant on the CloudTrail bucket
   > itself until this fix (`deploy/aws/iam.tf`'s `ReadCloudTrailLogFiles`
   > statement), so there was previously no way to run this check
   > correctly at all.
   >
   > Data events are only ever queryable by reading the delivered log
   > files themselves. Assume the `auditor` role for this (or an
   > equivalent principal - never admin/root credentials for a routine
   > lookup, which is the anti-pattern this role exists to avoid), then:
   > ```bash
   > # List delivered log files for the day(s) in question - CloudTrail
   > # delivers roughly every 5 minutes, path is region- and
   > # date-partitioned:
   > aws s3 ls "s3://<cloudtrail-bucket>/AWSLogs/<account-id>/CloudTrail/<region>/<YYYY>/<MM>/<DD>/"
   >
   > # Download, decompress, and search for the exact key. Log files are
   > # gzipped JSON; jq's -e/--arg keeps the key match exact rather than
   > # a substring match that could false-positive on a similar key.
   > for f in *.json.gz; do
   >   gunzip -c "$f" | jq -e --arg key "<the-s3-key>" \
   >     '.Records[] | select(.eventName == "DeleteObject" and .requestParameters.key == $key)'
   > done
   > ```
   > `cloudtrail:LookupEvents` (still granted to `auditor`) remains the
   > right tool for MANAGEMENT events - e.g. confirming which principal
   > called `kms:Decrypt` on the object store's KMS key in a given window
   > - just not for this S3-object-level check.
2. **Confirm who/what performed it.** There is no retention lock to
   defeat: this stack provisions no Object Lock, so a deletion needs only
   an ordinary `s3:DeleteObject` grant. The ingest and restore roles are
   still explicitly denied it (`deploy/aws/iam.tf`), so a deletion means
   either one of the `disposition`/`psychotherapy_disposition` roles
   (whose `admin-order` path is gated on `enable_admin_order_purge`) - or
   the object's retention had already genuinely expired and the
   `disposition` role's routine `expired`-mode path removed it, or
   someone with direct console/CLI access removed it outside this
   application's tooling entirely.
3. **If the CloudTrail entry matches a known, authorized, documented
   action** (a real disposition request via `purge.py`, an approved
   exception) - and, per the Step 2 note above, the matching index row
   is ALSO already gone - there is nothing further to do; `purge.py`
   already completed the cleanup this runbook exists to do manually. If
   the storage object is gone but the index row is NOT, and the
   CloudTrail entry confirms an authorized disposition, proceed to Step
   4 to remove the now-stale index row.
4. **If it doesn't** - no matching authorization, an unfamiliar
   principal, or no CloudTrail entry at all that explains it - stop and
   treat this as a potential security incident instead. Follow
   `RUNBOOK_INCIDENT_RESPONSE.md`, starting from its own audit-trail
   cross-referencing step. Do not clean up the index row as a way of
   "resolving" this; removing the only remaining record that the S3
   object ever existed is the wrong move until you know what happened.

## Step 4 - Manual cleanup (only after Step 3 confirms it's warranted)

Generate the exact, safely-escaped SQL for the rows reconcile found -
this uses the same orphaned-row list the report already computed,
rather than you writing your own "find orphans" query by hand, which
risks drifting from the tool's actual definition of "orphaned":

```bash
python -m core.db.reconcile --print-cleanup-sql
```

This only prints. It does not connect to Postgres as master, and it
does not run anything - copy its output into a `psql` session you start
yourself:

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

psql "host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require"
```

(`db_endpoint` is the hostname only - no port to strip out.
`phi_ai_index` and `phi_ai_master` are the literal database and user
names.)

Paste the master password when prompted, then paste the tool's output.
**Run the `SELECT` preview first and actually read it** - confirm the
rows returned are exactly the ones you investigated and expected, by
count and by key, before running the `DELETE` beneath it. The table is
`stored_resources`.

Record what you did somewhere durable - a change ticket, an incident
record, whatever your organization already uses for this. There is
deliberately no automated log of this specific action inside the
application (the audit log tracks `record.write` / `record.error` /
`record.dispose` / `record.purge.admin_order` events from the
ingest/disposition roles, not master-user maintenance), so this step is
the only record unless you make one.

## Step 5 - Confirm you're back in sync

```bash
python -m core.db.reconcile
```

Should now report zero orphaned rows and exit `0` (or exit `2` with
only *missing* rows remaining, if those are still being backfilled per
Step 2 - that's expected and not a sign the cleanup went wrong).

---

## Suggested cadence

There's no hard requirement to run this on a schedule - drift, per
Step 2, is either self-healing (missing rows), automatically resolved by
`purge.py` itself (orphaned rows from a disposition with the database
configured), or should be rare (orphaned rows from anything else). A
reasonable default is running Step 1 after any significant operational
event (a scheduler outage, a manual S3 operation of any kind, a
storage-only disposition run, standing up the Postgres index against an
object store that already had data in it) rather than as a routine cron
job that would mostly just confirm "still nothing to report."
