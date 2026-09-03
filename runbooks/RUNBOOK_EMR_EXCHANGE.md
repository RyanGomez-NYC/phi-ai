# Runbook: moving data between EMRs and this platform

## Source and target are different systems

| Term | Meaning | What this project does to it |
|---|---|---|
| **Source system** | The EMR where the data **originated** | **Reads only. Never writes.** |
| **Target system** | The EMR where the data **ends its workflow** | The only system written to |

**A delivery pointed at a source EMR is refused outright**, at construction,
before a token is requested or a capability statement fetched. There is no
override flag.

This platform exists because the source is being retired. Pushing stored
records back into it would re-populate a system someone is switching off,
using records that have been through an export/import round trip.

Configure the sources so the guard knows them:

```bash
PHI_AI_FHIR_BASE_URL=https://fhir.old-hospital.org/api/FHIR/R4
# any additional EMRs this deployment reads from
PHI_AI_SOURCE_EMR_URLS=https://fhir-ehr.cerner.com/r4/TENANT-A
```

`PHI_AI_SOURCE_EMR_URLS` matters more than it looks: it is what makes the
refuse-to-write-to-a-source guard know which URLs are sources, so a
misspelling does not fail loudly - it leaves those EMRs unprotected by
the guard.

An issuer registered in `config/smart_issuers.yaml` marks itself a source
with `record_source: true`. The loader matches that key literally and
treats an unrecognised key as forward-compatible rather than fatal, so a
misspelling has the same effect: the issuer is loaded as though the flag
were absent, stops being treated as a source, and falls outside this
guard. See `runbooks/RUNBOOK_EMULATORS.md`.

Comparison is on scheme, host, port **and path** — because multi-tenant
vendors put the tenant in the path, and two Cerner tenants sharing a host
are entirely different systems. Trailing slashes and case do not defeat it.

### Every request this project makes to a source system

| Request | Verb | Touches clinical data? |
|---|---|---|
| Paged resource search | `GET` | read |
| Single resource read | `GET` | read |
| `$export` kickoff / status / NDJSON | `GET` | read |
| OAuth token | `POST` | no — authentication |
| Bulk Data Delete | `DELETE` | **no** — frees an export *job* we asked it to create, never records |
| SMART in-context launch | `GET` | read — scopes are `patient/Patient.read` only, no `offline_access` |

That last `DELETE` is the only request that changes anything in a source
system, and what it changes is a job in that server's export queue. It is
the documented Bulk Data Delete Request, it is non-fatal if it fails, and
it cannot touch a clinical record. If your policy is that this platform
issues **no** state-changing request of any kind to a source, that call can
be disabled — the export works without it and Epic reaps jobs after
fourteen days regardless.

A test asserts the ingestion client contains exactly one write verb and
that it is the token endpoint, so a future change adding a write to the
read path fails the suite.

---

Four directions, and they are not equally safe.

| Direction | Scope | Tool | Risk |
|---|---|---|---|
| **Source** EMR → object store | per patient / bulk | `core.fhir.scheduler`, `core.fhir.bulk_scheduler` | Low — read-only against the source |
| Object store → file | bulk | `core.fhir.bulk_export` | Low — no destination system involved |
| Object store → file | per patient | `core.fhir.restore` | Low |
| **Object store → target EMR** | per patient / bulk | **`core.fhir.delivery`** | **High — see below** |

---

## Supported EMRs

| EMR | Auth | Bulk export | Writable (vendor-published) |
|---|---|:--:|---|
| Epic | SMART Backend Services (signed JWT) | yes | DocumentReference only |
| Oracle Health (Cerner) | SMART Backend Services (JWT **or** Basic secret; explicit system scopes required) | yes | DocumentReference, Condition, Observation |
| athenahealth | **OAuth client secret** | yes | DocumentReference only |
| eClinicalWorks | SMART Backend Services (private-key JWT) | yes | none by default (Create APIs are a contracted add-on) |
| MEDITECH Expanse | SMART Backend Services (g(10) baseline - confirm with MEDITECH) | yes | none published (view-only surface) |
| NextGen Healthcare | SMART Backend Services | **no** | DocumentReference only |
| ModMed | SMART Backend Services (private-key JWT, **ES384**; explicit `system/{Type}.rs` scopes required) | yes (system, Patient and Group level) | none — "Read, Search, and Bulk operations" only; writes are the separate Proprietary API |
| Altera Digital Health | SMART Backend Services (private-key JWT against a registered JWKS URL; explicit scopes required) | yes (Group level) | none — "read-only access and not write-backs"; writes are the Unity API |
| Greenway Health | SMART Backend Services (private-key JWT, **ES384**; explicit scopes required) | yes (Group level; `_since` defaults to the last 24 hours) | none — "read operations only"; writes are GAPI |
| Veradigm | SMART Backend Services (private-key JWT, RSA JWKS; documented scope `system/*.read`) | yes (Group level) | none — "limited to read-only access"; writes are Unity |
| Practice Fusion | SMART Backend Services (private-key JWT, RS384 or ES384, `kid` required; explicit scopes required) | yes (Patient and Group level; groups of at most 1,000) | none — "cannot change or write over EHR data" |
| TruBridge | SMART Backend Services (private-key JWT) **or** client secret — both documented; explicit scopes required | yes (system, Group and Patient level) | none documented ("read-only access") — the live CapabilityStatement over-advertises `create`; do not confirm a delivery |
| MEDHOST | SMART Backend Services (private-key JWT, RS384 or ES384) | yes (Group level only; 5,000 patients per Group, Group created by MEDHOST Support) | none — every documented operation is GET |
| Netsmart | SMART Backend Services (private-key JWT) **or** client secret — both documented; explicit scopes required | yes (Group level; `Retry-After: 120`) | DocumentReference, DiagnosticReport |
| Nextech | SMART Backend Services (private-key JWT, RS384 or ES384; explicit scopes required; 15-minute tokens) | yes (Patient, Group and system level; Select 16.9+) | DocumentReference only (Select/NexCloud); none (IntelleChartPRO) |

The rows follow `core/fhir/emr_profiles.py` `PROFILES`, which is the
source of truth - one row per profile, under the profile's display name,
and `tests/test_emr_profiles_coverage.py` fails when a profile has no
row here or a row names no profile; each vendor's chapter in
`docs/EMR_CONNECTORS.md` carries the citation for every cell.

Two things in that table change project plans, so read them before
committing to a timeline:

- **NextGen has no published Bulk Data Export** — the only profiled
  vendor recorded without one. A full history must be
  pulled per resource type through the paged search API. For a large
  practice that is materially longer and more rate-limited than a
  `$export`. Budget for it rather than discovering it mid-migration -
  `core.fhir.bulk_scheduler` refuses to run against a no-`$export`
  profile precisely so this becomes a planning decision instead of a
  silent fallback. (eClinicalWorks was in this position until the
  2026-08 review; their portal now documents bulk FHIR APIs, though
  availability for a specific practice may still require contracting.)
- **Writing into an EMR is far narrower than reading from one.** Every
  vendor exposes a broad read API. What each accepts as a *write* is
  gated per customer and, in several cases, is not a general FHIR create
  at all. The column above is a **starting point for a conversation with
  the destination's administrator**, never a guarantee.

The delivery tool does not trust that table. It reads the destination's
own `CapabilityStatement` at run time and refuses any resource type the
server does not advertise as creatable — because what a given customer's
build has enabled is theirs to configure, and asking the server is the
only honest way to know.

---

## Object store → live EMR

> **This writes into a chart clinicians read and act on.** Every other
> tool in this project fails safe: the worst outcome is missing data,
> fixed by re-running. This one can put a record in front of a clinician
> that was not there before. Three failure modes drive its design.

### 1. Wrong patient — no matching, ever

Stored records are keyed by the **source** EMR's patient id. Delivering
them needs the **destination's** id for the same person. The two are
unrelated opaque identifiers.

This tool does **no patient matching**. You supply a CSV:

```csv
source_patient_id,target_patient_id,verified_by,note
eAB12cd3,cerner-99871,j.okafor,matched on DOB + MRN by HIM 2026-08-18
```

`verified_by` is required. A patient mapping with nobody's name against
it is an assertion nobody made.

Matching on name and date of birth is an entire discipline with a real
error rate, and the two errors are not symmetric. A false negative
creates a duplicate chart — annoying, recoverable. A false positive
writes one person's medical history into **another person's** live chart:
a clinical safety incident and a HIPAA disclosure at once, and very hard
to unwind once other clinicians have read it.

The platform could not match even if it should: the index holds no names,
MRNs or dates of birth by design.

An unmapped patient is **skipped and reported**, never guessed at.

### 2. Duplicates — conditional create, or an explicit override

A delivery that runs twice writes everything twice unless the destination
can express *"create only if absent"*. Where the vendor supports FHIR
conditional create, `If-None-Exist` is sent keyed on the object store
key. Where it does not, **the delivery refuses to run** rather than risk
a silent second copy of a patient's entire history.

Override with `--allow-duplicates` only when you have confirmed
externally that the records are not already there.

### 3. Stale data read as current — provenance on every record

A 2019 observation appearing in a chart today, with no indication of
origin, looks like it was recorded today. Every delivered resource
carries:

- `meta.source` — the object store and the exact object it came from
- `meta.tag` — *"Historical record delivered from the PHI AI Platform,
  not captured in this system"*
- an extension preserving the **source patient reference**, so the record
  traces back to its origin

That `display` text is prose a clinician reads in their own chart. It is
the literal string `tag_as_prior_record()` writes in
`core/fhir/delivery/writer.py`; if it changes there, change it here.

> **Settle `PHI_AI_CANONICAL_BASE` before your first delivery, and do not
> change it afterwards.** The tag's system URL is not a fixed constant.
> `PRIOR_RECORD_TAG_SYSTEM` is resolved at call time from this
> deployment's own canonical namespace — `code_system("record-origin")`
> in `core/config/canonical.py` — deliberately, so the namespace is
> yours rather than this project's. The tag's code is the literal
> `prior-record`.
>
> The same system URL goes into the `If-None-Exist` header that makes a
> re-run safe, and **the destination matches that header against whatever
> was written the first time**. Change the canonical base after you have
> delivered anything and every previously delivered resource stops
> matching, so the next run re-creates a patient's entire delivered
> history as duplicates — the exact failure mode section 2 exists to
> prevent, arriving through a setting that looks unrelated to it. This is
> the delivery-side version of the warning `docs/RELEASE_CHECKLIST.md`
> §2 gives about setting the canonical base before ingesting.

This is the part most likely to be treated as optional. It is the part a
clinician is most likely to be harmed by its absence.

### Running it

> **`python -m core.fhir.delivery` could not start before 2026-08-18.**
> It connected to the index as `settings.db_username`, which is not a
> field on the Settings dataclass, so it raised AttributeError before
> reading anything. It now connects as `db_reader_username`.

```bash
# Dry run is the default and performs no write.
python -m core.fhir.delivery \
  --destination https://fhir.cerner.example/r4 \
  --vendor cerner \
  --identity-map ./patient-mapping.csv \
  --patient eAB12cd3 \
  --purpose-of-use "Continuity of care, patient transferred to Example Health"
```

Add `--confirm` to write. Use `--all-mapped` instead of `--patient` for
every mapped patient.

Credentials come from the environment, never arguments — a client secret
on a command line lands in shell history and in every process listing on
the host:

| Variable | For |
|---|---|
| `PHI_AI_DELIVERY_ACCESS_TOKEN` | a token you already hold |
| `PHI_AI_DELIVERY_CLIENT_ID` + `_TOKEN_URL` | either flow |
| `PHI_AI_DELIVERY_CLIENT_SECRET` | athenahealth (or any profile switched to `oauth2_client_credentials` — TruBridge and Netsmart document a secret as an alternative grant) |
| `PHI_AI_DELIVERY_PRIVATE_KEY_PATH` | SMART Backend Services vendors |

Every delivered record is audited as `record.deliver` before the write,
not after — so a failure leaves evidence of an attempted disclosure
rather than none.

### "Bulk" here means every *mapped* patient

Not everything stored. Delivery is bounded by the identity map, so the
upper limit on any run is exactly the set of patients a human verified.
There is no code path that resolves a patient without a mapping, so an
entire deployment's holdings cannot be pushed into a live clinical system
by accident.

To move everything this deployment holds somewhere that is **not** a live
EMR — a successor system, a warehouse, a vendor's own migration tooling —
use `core.fhir.bulk_export`. It emits standard FHIR Bulk Data NDJSON,
needs no destination credentials and no identity map, because it is not
writing into anyone's chart.

---

## What is not handled

- **Reference rewriting beyond the patient.** An Observation referencing
  `Encounter/enc1` still points at the *source* system's encounter, which
  does not exist in the destination. These are **reported, not silently
  stripped** — stripping discards clinical context, keeping them leaves a
  broken link, and choosing between those is a migration decision.
  Rewriting a whole reference graph across systems is a project, not a
  side effect of an export.
- **FHIR `$import`.** Draft, and not commercially supported by any
  profiled vendor (every profile records `supports_bulk_import=False`).
  Recorded so the answer is visible rather than rediscovered.

Delivery confirmation is now available — see `RUNBOOK_VERIFICATION.md`.
It searches the destination on `meta.source` to confirm each record it
claims to have sent is actually there.

**Ingestion verification has a deadline**, and it applies directly to
this runbook: once a source EMR is decommissioned, nothing can ever
confirm this deployment captured everything it held. Run
`python -m core.verify --deep` against every source **before** switching
it off.
