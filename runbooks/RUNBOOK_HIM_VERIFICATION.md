# Runbook: Verifying stored clinical data for Health Information Management

Audience: Health Information Administrators (and delegates they assign)
confirming that clinical data was completely and correctly ingested —
typically before signing off on retiring a legacy EMR, or as part of a
periodic compliance review. No software development background assumed;
where a step needs a technical delegate, that's called out.

> **Read this first: what's actually in each system.**
>
> **S3 holds the clinical data.** Every stored record — the actual
> FHIR resource content, encrypted — lives in S3 and is the system of
> record. If it's not in S3, it wasn't ingested. Note that it is
> versioned and encrypted but **not** write-once: this platform applies
> no S3 Object Lock, so what protects a stored record is detection
> rather than prevention. Step 8 covers exactly what that means for your
> sign-off, and it is worth reading before you attest to anything.
>
> **Postgres holds a structural index, never clinical content.** This is
> a deliberate design decision, stated directly in the schema itself
> (`core/db/schema.sql`): this table holds metadata about stored
> resources, never clinical content — no names, no MRN, no DOB, no SSN,
> no free-text observations. What it *does* hold: resource type
> (`Patient`, `Condition`, `Observation`, etc.), a hash, timestamps, and
> the EMR's own internal opaque reference for the resource — a string
> like `Patient/eAB12cd3ohorD`, not a name or medical record number.
> Compromising this index end to end would not expose a single patient's
> clinical information.
>
> **Practically, this means "verifying ingestion" is two separate
> checks, not one:** using Postgres to confirm *completeness* — did
> every expected record get stored — and using S3, through the
> controlled restore process, to confirm the actual clinical *content*
> is present and intact. This runbook covers both, plus a third check
> (the audit trail) that independently corroborates the first two.

---

## Before you start

- [ ] Access to the Postgres index as the **read-only** role
      (`phi_ai_reader`) — this role can only look things up, never
      change or delete anything. Ask your technical team for a
      connection command (Step 1 below shows what that looks like) if
      you don't have one already.
- [ ] The expected scope of what should have been stored: which
      resource types (Conditions, Medications, Observations, etc.),
      which patient population, and which date range. This should come
      from whoever ran the ingestion project, not be reconstructed from
      scratch here.
- [ ] If you'll be spot-checking specific patients, a way to look up
      their EMR-internal reference (Step 3 explains how).

---

## Step 1 — Connect to the read-only index

This uses a temporary, auto-expiring credential — there is no password
to manage or share. A technical delegate can run this for you, or hand
you a working connection if `psql` (a standard database client) is
already set up on your machine:

```bash
export PGPASSWORD=$(aws rds generate-db-auth-token \
  --hostname <DB_HOST> --port 5432 \
  --username phi_ai_reader --region <REGION>)

psql "host=<DB_HOST> port=5432 dbname=<DB_NAME> user=phi_ai_reader sslmode=require"
```

(`<DB_HOST>`, `<DB_NAME>`, and `<REGION>` come from your deployment —
see `runbooks/RUNBOOK_AWS_SETUP.md` Step 6a if your technical team needs
to find these.)

You're now connected as a role that can only run `SELECT` queries — it
is not possible, through this connection, to modify, delete, or export
anything. Every query below is safe to run as many times as you like.

## Step 2 — Confirm completeness by resource type

```sql
SELECT resource_type, count(*) AS stored_count
FROM stored_resources
GROUP BY resource_type
ORDER BY resource_type;
```

Example output:

```
   resource_type    |  stored_count  
--------------------+----------------
 AllergyIntolerance |           1204
 Condition          |           8831
 DocumentReference  |           5502
 Encounter          |          14209
 Immunization       |           3390
 MedicationRequest  |           6017
 Observation        |          42188
 Patient            |           2150
 Procedure          |           4477
```

(`stored_resources`, and the `stored_at` column used later in this
runbook, are the real table and column names in `core/db/schema.sql` —
type them exactly as shown or the queries will fail.)

**Reconcile this against the source system.** Your EMR's own reporting
tools (or whoever scoped the ingestion project) should be able to tell
you how many records of each type existed in the population being
ingested. These two numbers should match, or be explainably close (see
"If the numbers don't match" below) — this is the core completeness
check, and the one worth the most scrutiny.

## Step 3 — Spot-check specific patients

Pick a handful of patients — ideally including at least one with an
unusually large or unusually thin chart, since those are the cases most
likely to reveal a gap.

**Finding a patient's reference.** The index doesn't use names or MRNs
(see the box above), so you'll look a patient up by the EMR's own
internal reference for them. Your EMR system can show you this for any
patient you already have legitimate access to look up directly — ask
your technical team if it's not obvious where in your EMR's interface
this is exposed for a given patient. It will look like `eAB12cd3ohorD`
(no `Patient/` prefix needed for this specific query, since we're
searching a text field containing it):

```sql
SELECT resource_type, count(*) AS count, min(stored_at) AS first_stored, max(stored_at) AS last_stored
FROM stored_resources
WHERE patient_reference = 'Patient/eAB12cd3ohorD'
GROUP BY resource_type
ORDER BY resource_type;
```

Compare the resource types and counts returned against that patient's
chart in the source system. A patient with allergies, medications, and
recent labs in the source system should show rows for
`AllergyIntolerance`, `MedicationRequest`, and `Observation` here — not
just `Patient`.

## Step 4 — Confirm date-range coverage

If the ingestion project was meant to cover a specific window (e.g.,
"everything through the EMR's decommission date"), confirm nothing
trails off earlier than expected:

```sql
SELECT resource_type, min(stored_at) AS earliest_stored, max(stored_at) AS latest_stored
FROM stored_resources
GROUP BY resource_type
ORDER BY resource_type;
```

`stored_at` is when the record was **written to the object store**, not
the clinical date on the record itself — this confirms *when the
ingestion process ran* for each type, useful for spotting a resource
type that stopped being captured partway through, not for reconstructing
clinical timelines.

## Step 5 — Confirm every record has a retention date set

Every stored record should carry a retention date — this is the recorded
figure that drives the documented disposition process for that record
(it is recorded, not enforced by storage; see Step 8):

```sql
SELECT
  count(*) FILTER (WHERE retention_until IS NULL) AS missing_retention_date,
  count(*) AS total_records
FROM stored_resources;
```

`missing_retention_date` should be `0`. If it isn't, treat that as a
finding requiring investigation before sign-off, not something to note
and move past — a record with no retention date has nothing to drive its
disposition, so it will neither be disposed of on schedule nor be
protected from an early disposal by a rule that cannot find a date to
check.

## Step 6 — Confirm the actual clinical content, not just the index entry

Everything above confirms *that a record was stored*. To confirm the
clinical *content* itself is present, intact, and retrievable, use the
controlled restore process — this is a separate, audited action, not
something available through the read-only connection above by design
(see the box at the top of this document for why).

Ask your technical team to run a restore for one or two of the patients
you spot-checked in Step 3, following `runbooks/RUNBOOK_DATA_RESTORE.md`.
That process automatically re-verifies the record's cryptographic hash
before producing any output — if that verification fails, the process
stops and flags it rather than silently producing possibly-corrupted
data. A successful restore, with content matching what you'd expect from
that patient's chart, is your confirmation that the stored clinical
data is real and intact, not just an index entry pointing at nothing.

This step is logged automatically, including the reason for the
restore — use something like `"HIM verification - EMR retirement
sign-off"` as the documented reason when your technical team runs it,
so the audit trail reflects why this access happened.

## Step 7 — Confirm the audit trail independently corroborates all of this

The steps above tell you *what's in the object store right now*. The
audit trail is a separate, tamper-evident record of *how it got there and
who has touched it since* — ask your technical team to run:

```bash
python -m core.audit.verify
```

A clean result confirms the chain of ingestion activity hasn't been
altered after the fact. If your organization also has cloud-provider
logging enabled (CloudTrail or equivalent), your technical team can
cross-reference the two — they should agree, and a mismatch (activity
in one log but not the other) is itself a finding worth investigating
per `runbooks/RUNBOOK_INCIDENT_RESPONSE.md`, not something to reconcile
away.

## Step 8 — Understand what protects the records, and confirm that

**There is no delete-protection control on this object store.** This
platform applies no S3 Object Lock, so there is no storage-level refusal
to demonstrate — a check written to expect one would fail against a
system working exactly as intended.

What protects stored records now is *detection*, not *prevention*:
every object is versioned, carries a cryptographic checksum verified on
retrieval, and every access or deletion is recorded in a hash-chained
audit log and in the cloud provider's own access log. A deletion is
possible for anyone whose permissions allow it — and it leaves evidence.

Ask your technical team to demonstrate that evidence instead, per
`runbooks/RUNBOOK_AWS_SETUP.md` Step 9: delete a test object, then show
you the surviving prior version, the delete marker, and the corresponding
entry in the access log. The defensible statement for your compliance
record is "we confirmed deletion is detected and attributable," not "we
confirmed deletion is refused."

Two questions worth asking while you are there, because they are now the
controls that matter:

- **Who can delete stored records?** This should be a short, named
  list. It is the primary control, not a secondary one.
- **Is anyone alerted when a deletion happens?** Detection only works if
  someone is looking. See `docs/COMPLIANCE.md` → "Retention and
  integrity".

---

## If the numbers don't match

A discrepancy is a real finding, not necessarily a failure — work
through these in order before escalating:

1. **Check whether ingestion is still running.** If the source system was
   large, the ingestion process may not be finished yet — compare
   `stored_at` timestamps (Step 4) against when the process actually
   started.
2. **Check the resource-type scope.** Confirm the source-system count
   you're comparing against used the same resource-type definitions —
   a source report counting "active medications" won't match an object
   store that captured all `MedicationRequest` statuses, for instance.
3. **Check for a partial run.** Ask your technical team to check the
   ingestion process's own logs for errors on the specific resource
   type that's short — a failed batch is logged, not silently dropped
   (see `core/fhir/scheduler.py` / `core/fhir/bulk_scheduler.py`'s
   per-type error handling).
4. **If none of the above explains it**, treat this as requiring the
   same escalation as a suspected security issue —
   `runbooks/RUNBOOK_INCIDENT_RESPONSE.md` Step 2's audit-trail
   cross-referencing is the right next diagnostic step regardless of
   whether this turns out to be an incident or a mundane gap in
   coverage.

---

## Suggested sign-off checklist

Use this (or your organization's equivalent compliance form) to record
the verification for the project file:

- [ ] Resource-type counts (Step 2) reconciled against source-system
      counts. Discrepancies, if any, explained: _______________
- [ ] Spot-checked ____ patients (Step 3); all showed the expected
      resource types and counts for their known chart contents.
- [ ] Date-range coverage (Step 4) confirmed with no unexpected gaps.
- [ ] Zero records missing a retention date (Step 5).
- [ ] Content-level restore (Step 6) performed and verified for at least
      one spot-checked patient; hash verification passed.
- [ ] Audit trail integrity confirmed (Step 7); cloud-provider log
      cross-reference performed if available.
- [ ] Deletion-detection evidence demonstrated directly, and the list of principals able to delete reviewed (Step 8).
- [ ] Verified by: _______________  Date: _______________
- [ ] Reviewed by (Privacy/Security Officer, if required by policy):
      _______________  Date: _______________

---

## Glossary

- **Object Lock** — an AWS S3 feature, **not used by this platform**, that would make a stored record
  physically impossible to modify or delete until its retention date,
  enforced by AWS itself, not just by application logic.
- **Envelope encryption** — every record is encrypted with its own
  single-use key, which is itself encrypted by a master key held in a
  hardware-backed key management service (KMS). Nothing is ever stored
  as readable plaintext.
- **Hash / SHA-256 digest** — a fixed-length fingerprint of a record's
  exact content. If even one character of the stored data changed,
  the fingerprint would no longer match, which is how tampering or
  corruption is detected.
- **Opaque reference** — an identifier like `Patient/eAB12cd3ohorD`,
  assigned internally by the EMR system itself. It identifies a record
  uniquely but reveals nothing about the patient on its own — unlike a
  name, MRN, or date of birth.
- **Audit trail / hash chain** — a tamper-evident log where each entry
  cryptographically references the one before it, so altering or
  deleting a past entry breaks the chain in a way that's detectable.
- **`phi_ai_reader`** — the specific technical role used for the
  verification steps in this document. It can look up index entries but
  cannot modify, delete, or export anything, and cannot see clinical
  content under any circumstances.
