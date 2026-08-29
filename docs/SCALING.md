# Scaling: from under 1 TB to over 200 TB

## The number that decides everything is object count, not bytes

Nothing in this system scales with terabytes. Everything that strains
scales with **how many objects those terabytes are divided into**.

| 100 TB of… | Objects | Index size | Verdict |
|---|---|---|---|
| DICOM studies @ 100 MB | 1.1 million | <1 GB | trivial |
| Scanned documents @ 1 MB | 110 million | ~41 GB | fine, with sizing |
| Scanned documents @ 500 KB | 220 million | ~82 GB | fine, with sizing |
| FHIR JSON @ 5 KB | **22 billion** | ~8 TB | needs a different design |

Work out your object count before using any figure in this document. A
200 TB imaging deployment is a smaller engineering problem than a 20 TB
store of discrete FHIR resources.

> **The DICOM row above counts STUDIES. This platform stores INSTANCES.**
> `core/dicom/` writes one encrypted object per SOP instance, so that a
> viewer can fetch a single slice without decrypting a whole study - which
> means a 100 MB CT is not one object but often one to two thousand. The
> same 100 TB of imaging is therefore closer to a billion objects
> than to a million. Estimate instances, not studies, before choosing a
> storage profile. See `runbooks/RUNBOOK_DICOM_IMAGING.md`.

---

## Choosing a profile

```bash
PHI_AI_PROFILE=small    # default
PHI_AI_PROFILE=large
```

| | `small` | `large` |
|---|---|---|
| Storage layout | one object per resource | NDJSON bundle per (patient, type) |
| Index | one row per resource, unpartitioned | one row per bundle, LIST-partitioned on `resource_type` |
| Schema | `core/db/schema.sql` | `core/db/schema_partitioned.sql` |
| Integrity granularity | per resource | per bundle |
| Disposal granularity | per resource | per bundle |
| Suits | up to ~200–500M resources | beyond that |

**This is a deployment shape, not a tuning knob.** Changing it on a
populated object store migrates nothing — new resources go to the new
layout while existing ones stay put. Choose before ingesting.

### What `large` buys, at 20 billion resources

| | `small` | `large` |
|---|---|---|
| Storage objects | 20,000,000,000 | **40,000,000** |
| Index rows | 20 billion | **40 million** |
| Index size | 7.3 TB | **15 GB** |
| One-time PUT cost | $100,000 | **$200** |

**500× fewer objects**, and the index returns to something a single
Postgres instance handles.

### What `large` costs

- **Integrity is per bundle.** A digest mismatch names the patient and
  type affected, not the individual resource.
- **Disposing one resource rewrites its bundle.** Disposal is normally
  per-patient or per-retention-period, where a bundle is exactly the
  right unit — but `admin-order` purge of a single record becomes
  read-modify-write.
- **Lookup by resource id reads the patient's bundle.** Every dominant
  read path here is already patient-scoped, so this is rarely needed.

Bundling is on `(patient, type)` rather than patient alone because
retention is configurable per resource type. A patient-only bundle would
mix a ten-year DocumentReference with a six-year Observation in one
object, and disposal could express neither.

**It is safe only because the source is retired.** Bundles assume the data
is static once migrated. Against a live, appending EMR this layout would
mean constant rewrites and `small` would be correct at any size.

### Why LIST partitioning on `resource_type`

The type set is small, known and stable, so the partition list is bounded
and readable. Retention and disposal are already expressed per type, so
partitions line up with the operations that scan ranges. Verified against
Postgres 16: a type-scoped query prunes to a single partition, and
uniqueness constraints hold within partitions.

The cost: restore-by-patient touches every partition rather than one. Each
is a fraction of the size with its own patient index. The alternative —
hashing on patient — would make `find_by_type` scan every bucket, and that
is the query disposal runs store-wide.

A `DEFAULT` partition catches unlisted types so an unexpected
`resourceType` is stored rather than rejected. Monitor it: rows landing
there mean a type is missing from the list.

**Bundles and the cold-tier floor.** A realistic FHIR Observation is
~1.2 KB, so ~114 resources clears the 128 KB minimum that cold tiers bill
per object. A patient with real clinical history has far more than that
per type — but this is a property of resource size, not of bundling, and
a bundle of very sparse resources would still sit under the floor.

---

## Storage and cost scale fine

AWS storage classes, named as AWS names them - these are the literal
transition targets a lifecycle rule takes:

| 100 TB | Monthly |
|---|---|
| S3 Standard | ~$2,355 |
| Standard-IA | ~$1,280 |
| Glacier Instant Retrieval | ~$410 |
| Glacier Deep Archive | ~$101 |

Ingest of 110 million document-sized objects takes under an hour at S3's
per-prefix rate. Object storage itself has no practical ceiling.

**The lifecycle default is wrong for large deployments, and correct for
small ones.** Transitions are off because Standard-IA and Glacier IR bill
a **128 KB minimum per object** — a 5 KB FHIR resource is billed at
128 KB, so transitioning costs *more*. That inverts once documents
dominate: a 1 MB object is far above the floor, and switching saves
roughly **$23,000/year at 100 TB**.

Use `enable_lifecycle_transitions = true` only if your objects are mostly
**above 128 KB**. Total size is not the deciding factor; object size is.

See `deploy/aws/terraform.tfvars.large-deployment.example`.

---

## What was rebuilt to make large deployments workable

Two operations previously loaded the entire object store into memory. Both
were fine below a few million objects and impossible above that — and
their failure mode was the bad kind: a store that verified in a minute
on day one takes an hour on day one thousand, and eventually stops
completing. **Verification that gets slower until it stops is
verification that quietly stops happening.**

### Audit verification is now incremental

`diagnose_chain()` needs every event's hash in memory to prove each
`prev_hash` resolves — that is inherent to the check. At ~110 million
events that is tens of gigabytes.

Routine verification now resumes from a **checkpoint** and reads only
what is new, so its cost tracks **what was written since the last run**
rather than what exists. Measured: a second run over a chain reads only
the new events, not the history.

The checkpoint lives in the **Postgres index, not the audit bucket** — an
attacker able to write to the audit bucket could otherwise forge a
checkpoint claiming the tampered range was already verified. Different
system, different credentials.

**A checkpoint is an optimisation, not evidence.** Incremental
verification says nothing new has been tampered with since the last full
check. Only `--deep` confirms the whole chain. Run one periodically and
after any incident.

Without an index configured, every run re-reads the whole chain and the
report says so.

### Reconciliation is now a merge join

`build_report()` held every storage key and every index key in Python
sets. `build_report_streaming()` walks both sides in sorted order — S3
lists lexicographically, Postgres uses keyset pagination — so memory
tracks **discrepancies**, not object-store size.

Measured, generating keys rather than storing them:

| Objects | Peak memory |
|---|---|
| 1,000 | 172 KB |
| 100,000 | 2,700 KB |
| 1,000,000 | 2,787 KB |

It rises to one pagination batch and then plateaus: 10× the objects costs
3% more memory.

Keyset pagination, not `OFFSET` — `OFFSET` makes the database scan and
discard every preceding row, so a full walk is quadratic, which is
exactly the shape that fails at scale.

Discrepancy **examples** are capped; **counts stay exact**. Ten million
orphaned keys in a report is not something a human can act on.

---

## Sizing the index

One row per ingested resource, ~400 bytes including indexes:

| Objects | Index | Instance |
|---|---|---|
| 1 million | ~0.4 GB | `db.t4g.micro` |
| 100 million | ~40 GB | `db.m6g.large`, 200 GB |
| 500 million | ~200 GB | `db.m6g.xlarge` + **partitioning** |
| 1 billion+ | ~400 GB+ | partitioning mandatory |

The schema ships **unpartitioned**. Above roughly 500 million rows, add
declarative partitioning on `resource_type` **before** ingesting —
converting a populated table is far more disruptive than starting
partitioned.

---

## What still has a ceiling

- **Full (`--deep`) audit verification remains O(chain length).** It is
  the authoritative check and cannot be made incremental without giving
  up what it proves. Budget for it as a periodic operation, not a routine
  one; it can be run against a date-prefixed window.
- **Deep object-integrity verification re-reads every object.** Sampling
  is the default for exactly this reason.
- **Bulk export iterates every key.** Bounded per resource type, but a
  long job at high object counts.
- **A single Postgres index past ~1 billion rows** is the point at which
  the index shape itself, not its hosting, is the question.

---

## Small deployments are unaffected

Everything above is opt-in or automatic-and-cheap. Under a terabyte:
leave the defaults alone. `db.t4g.micro` and no lifecycle transitions are
the right answers, the streaming paths cost nothing extra at small sizes,
and checkpointed verification simply verifies everything on the first run
and very little afterwards.
