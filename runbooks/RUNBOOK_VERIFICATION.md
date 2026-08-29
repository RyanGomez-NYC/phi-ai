# Runbook: verifying the object store

```bash
python -m core.verify              # everything runnable
python -m core.verify --deep       # identifier-level, definitive, slower
```

Exit codes: **0** clean · **1** warnings, or a flow that could not be
checked · **2** critical.

**A flow that could not be checked never returns 0.** "Sound" and
"unexamined" are different states, and a runbook step or CI job keying on
the exit code has to tell them apart. A system of record nobody could
verify must not report success.

---

> **`python -m core.verify` could not start before 2026-08-18.** It
> connected to the index as `settings.db_username`, which is not a field
> on the Settings dataclass, and raised AttributeError immediately.
>
> It now connects as **`db_ingest_username`**, not the reader, and the
> distinction matters: `verify_audit()` saves its checkpoint through
> `write_index_state()`, which INSERTs into `index_state`. Only the ingest
> role holds INSERT/UPDATE there. Connecting as the reader would verify
> correctly and then silently fail to save the checkpoint, quietly
> reverting every run to re-reading the entire audit chain - the exact
> cost the checkpoint exists to remove. So the scheduled verification job
> needs the ingest role's credentials, not the reader's.

## Read this first: one check expires

| Flow | Can be re-run later? |
|---|---|
| Object integrity | yes, any time |
| Index vs storage | yes |
| Audit chain | yes |
| Bulk export completeness | yes |
| Object store → destination EMR | yes |
| **EMR → object store** | **NO — only while the source EMR still exists** |
| **Object store freshness** | **NO — same reason** |

Comparing the object store against the source EMR requires the source EMR
to still be running. Once it is decommissioned — which is the entire point
of building this platform — there is nothing left to compare against. A
record that was never ingested becomes **permanently missing and
permanently undetectable**, because the only evidence it ever existed went
away with the server.

**Verify ingestion before the source system is switched off.** No software
recovers from getting that order wrong. If you do only one thing from this
runbook, do this one, and do it while you still can:

```bash
python -m core.verify --deep
```

---

## What each flow checks, and what a failure means

### EMR → object store (ingestion completeness)

Two depths:

- **Counts** (default) — one cheap `_summary=count` request per resource
  type. Catches an interrupted run, a whole missing page, a resource type
  nobody configured. Will *not* catch one specific record being absent
  while the totals happen to agree.
- **Identifiers** (`--deep`) — pages every id out of the source and
  compares the sets. Names exactly which records are missing. Costs
  roughly a second ingest pass.

Counts are evidence. Identifiers are proof. Use `--deep` before signing
anything off.

Records **in the source but not the object store** are `CRITICAL`. Records
**in the object store but no longer in the source** are `INFO` — expected,
because the object store is meant to outlive the source's own retention,
and treating it as a failure would train you to ignore the report.

This does not diff resource *content*. Clinical records legitimately
change after ingest — a corrected lab, an amended note — so a content
difference is not evidence of an ingestion failure. Presence is the
question.

### Object store freshness (superseded records)

Compares `meta.versionId` — falling back to `meta.lastUpdated` — between
the stored copy and the source's current version. Reports records the
source has moved on from.

**This is not a content diff, and the distinction is the point.** Diffing
bodies would flag every legitimate change — a corrected lab, an amended
note, a re-signed document — as a discrepancy, and a report full of
non-problems trains you to stop reading it. Version comparison is a fact
about sequence, not a judgement about content.

**Why it matters beyond tidiness.** 45 CFR 164.526 gives an individual the
right to amend their record. If an amendment lands in the source after
ingest and the object store keeps only the earlier version, the object
store — which is what survives the source — holds a version the patient
successfully had corrected. A records request served from it would
disclose the uncorrected text.

That is invisible to every other check here: the object's digest matches,
the index is in sync, the chain verifies. Everything looks sound because
everything *is* sound. The stored copy is simply out of date.

`WARNING`, not critical: the fix is to re-run ingestion over the named
ids, which is idempotent. Use `--deep` before a legal production, where
serving a superseded version has consequences beyond tidiness.

### Object integrity

Re-reads objects and compares against the SHA-256 recorded at ingest.
Sampled by default (50, evenly spaced — not the first 50, which are the
oldest and would hide a fault introduced by a recent change). `--deep`
checks everything.

A mismatch means stored ciphertext changed after it was written. Treat as
a suspected security incident — `RUNBOOK_INCIDENT_RESPONSE.md`.

### Index vs storage

Wraps `core/db/reconcile.py`. Drift is a `WARNING`, not critical: storage
is the system of record and the objects are safe. But records missing from
the index will not appear in search or restore-by-patient until it is
rebuilt, so a records request could come up empty on data that is present.

### Audit chain

Wraps `AuditLog.diagnose_chain`. Concurrent-writer forks are reported as
`INFO`, not tampering — two events sharing a predecessor is normal with
more than one writer.

Modified or removed entries are `CRITICAL`.

### Bulk export completeness

Compares ids in the exported NDJSON against ids in storage. An incomplete
export looks exactly like a complete one to whoever receives it, which is
why this exists.

```bash
python -m core.verify --export-dir ./export-output
```

Reads by identifier, never by decrypting and diffing bodies — so this
needs no clinical read access.

### Object store → destination EMR

A delivery reports what it *believes* it wrote. That is not what the
destination *holds*: a create can return 201 and still be rejected by a
downstream interface engine, land in a staging area nobody sees, or be
merged by the destination's own duplicate handling.

Confirmation searches the destination on `meta.source`, which
`core/fhir/delivery/writer.py` sets to the exact object key in the store.
That tag is what makes verification possible — without it you can only ask
"does something similar exist", which is guessing.

A record reported sent but absent downstream is a `WARNING`, not critical:
the object store still holds it and the delivery can be repeated. Severity
here tracks **recoverability**, not effort.

---

## When to run it

| When | Command |
|---|---|
| **Before decommissioning any source EMR** | `--deep` — this is the deadline |
| After a large ingest run | default |
| After a bulk export, before handing it over | `--export-dir` |
| After a delivery into a live EMR | delivery confirmation |
| Routinely | default, on a schedule |
| Investigating a suspected incident | `--deep` |

---

## Running it on a schedule

```bash
docker compose up -d verify
```

Or directly:

```bash
python -m core.verify.scheduled          # daily, deep weekly
python -m core.verify.scheduled --once   # a single pass, for cron
```

| Variable | Default | Meaning |
|---|---|---|
| `PHI_AI_VERIFY_INTERVAL_SECONDS` | `86400` | how often |
| `PHI_AI_VERIFY_DEEP_EVERY` | `7` | deep pass every Nth cycle; `0` never |
| `PHI_AI_VERIFY_EXPORT_DIR` | unset | also verify an export directory |
| `PHI_AI_VERIFY_SKIP_SOURCE` | unset | **leave unset while any source EMR exists** |

A misspelled `PHI_AI_VERIFY_SKIP_SOURCE` is simply not read, which in
that one case fails safe - the source check runs - but the same is not
true in the other direction for a variable you meant to set.

**Why bother scheduling it.** The hash chain makes tampering
*detectable*, not *detected*. Capability that is never exercised is
indistinguishable from its absence at the moment something actually goes
wrong. This is the thing that exercises it.

**Every run writes an audit entry.** `"we verify our stored records
regularly"` is a claim someone will eventually ask you to substantiate —
during an audit, a breach investigation, or a dispute about whether a
record was intact on a given date. A log line is not evidence; it can be
edited or rotated away. An entry in the hash-chained audit log is, and it
inherits every property that log already has.

The entry records the outcome and counts, never the findings' detail:
resource identifiers belong in the report, not in an append-only log that
is deliberately hard to prune.

**No alerting is built in, deliberately.** A critical finding exits `2`
and logs at `ERROR`, which every supervisor, cron wrapper and container
platform already knows how to act on. A second notification path would
mean credentials, delivery guarantees and an on-call model this project
has no business owning.

A crash in one cycle does not end the loop — a scheduler that dies on a
bad run stops verifying silently, which is the exact failure this is meant
to prevent.
