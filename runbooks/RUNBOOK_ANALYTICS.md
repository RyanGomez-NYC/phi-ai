# Runbook: population analytics and name search

Answering questions **about the population**, not about one record — "how
many patients have diabetes", "how many patients were seen at this
facility", "find patients named Mary Smith".

> **This layer reads identified PHI.** It sits on the optional OMOP
> analytics layer (`runbooks/RUNBOOK_OMOP_SETUP.md`), which holds real
> birth dates, diagnoses and medication exposures against a persistent
> `person_id`. Name search additionally stores patient **names** — the
> only place in this system they live outside an encrypted object. Both
> are off by default and each needs its own database role.

---

## What this gives you

| Question | Answered by | Needs |
|---|---|---|
| What is in this deployment? | `platform_population` | OMOP + analyst role |
| How many patients have diabetes? | `count_patients_with_condition` | OMOP + analyst role |
| How many patients went to this facility? | `count_patients_by_facility` | OMOP + analyst role |
| Which facilities are represented? | `list_facilities` | as above |
| Anything else about the population | `run_analytics_query` (guarded SQL) | OMOP + analyst role |
| Find patients named Mary Smith | `find_patients_by_name` | identity index |

`platform_population` is the tool's real name in `core/analytics/` — it
reports this deployment's population.

---

## Two things the OMOP ETL populates

Neither is a setting. Both are filled in by the ETL as resources are
processed, so both answer nothing until it has run over the resources
they come from.

**Facility comes from the Encounter, through the ETL.** `cdm.person` and
`cdm.visit_occurrence` carry a `care_site_id` column because OMOP
defines one, and `cdm.care_site` is populated from
`Encounter.serviceProvider` as Encounters are processed. Facility counts
return nothing until Encounters have been through the ETL.

**Names are deliberately absent from the index.** `core/db/schema.sql`
holds no names and OMOP's `person` has no name column, by design. Name
search therefore needs `identity.patient_identity`, a separate,
separately-enabled store populated from Patient resources by the ETL.

Running the OMOP ETL is safe and idempotent: every write is an upsert
keyed on a deterministic id, so processing the same resource again
produces the same rows rather than duplicates. See
`runbooks/RUNBOOK_OMOP_SETUP.md`.

---

## Setup

### 1. Apply the schema changes

```bash
psql "$CONN" -f core/db/omop_schema.sql
```

`CREATE TABLE IF NOT EXISTS` throughout, so this is safe to run against a
database that already has the OMOP schema — it adds `cdm.care_site` and
the cohort indexes without touching any other table or its data.

### 2. Grant the read-only analyst role

```bash
psql "$CONN" -f core/db/omop_bootstrap_aws.sql   # or _gcp.sql / _azure.sql
```

This grants `omop_analyst` SELECT on `cdm.care_site`, alongside the
grants it already held. `omop_analyst` is the role every analytics query
runs as, and it holds **no** INSERT, UPDATE, DELETE or DDL — which is
what makes generated SQL safe to run at all.

```bash
PHI_AI_OMOP_ANALYST_USERNAME=omop_analyst
```

Its value is the literal Postgres role name - set it to what the database
actually has, and type both sides exactly as printed; a wrong role name
fails the connection rather than degrading to anything usable.

### 3. Name search — only if you want it

Read the header of `core/db/identity_schema.sql` first. It is the longest
warning in this codebase and it is there for a reason: a copy of that
table is a patient list.

```bash
psql "$CONN" -f core/db/identity_schema.sql
PHI_AI_IDENTITY_READER_USERNAME=phi_ai_identity
```

`phi_ai_identity` is the Postgres role name created by
`core/db/identity_schema.sql` and granted in Terraform — set the value
exactly as shown above or the reader will fail to connect.

`pg_trgm` is used for fuzzy matching. If your managed Postgres forbids
extensions, comment out the two GIN indexes at the foot of that file —
search detects the absence and falls back to exact and prefix matching
rather than failing.

### 4. Run the ETL

Facility and identity rows appear as Encounters and Patients are
processed. Verify:

```sql
SELECT count(*) FROM cdm.care_site;
SELECT count(*) FROM cdm.visit_occurrence WHERE care_site_id IS NOT NULL;
SELECT count(*) FROM identity.patient_identity;
```

A zero in the second row with a non-zero first row means no Encounter
processed so far carried a `serviceProvider` reference the ETL could map
to a care site.

---

## Who can do what

| Role | Cohort counts | Name search | Open a record |
|---|---|---|---|
| `viewer` | no | **yes** | yes |
| `him` | **yes** | **yes** | yes |
| `analyst` | **yes** | no | **no** |
| `auditor` | no | no | no |
| `disposition` | no | no | no |
| `admin` | **yes** | no | no |

`analyst` is a new role and the reason it exists is the row above it:
someone answering "how many patients have diabetes" for a service-line
review needs the population, not the people in it. Without
`patient:read` they cannot open a chart; without `identity:search` they
cannot turn a cohort into a list of names. That separation is the whole
distinction between analytics and disclosure, and it is enforced by the
permission set, not by convention.

Note that `analytics:query` and `identity:search` are granted
**separately** and neither implies the other. An analyst role that could
join a cohort result to names would make every aggregate query a patient
list.

---

## Generated SQL, and what actually makes it safe

The assistant can write its own SELECT for questions the curated tools do
not cover. Four things constrain it, in descending order of how much
weight each carries:

1. **The database role.** `omop_analyst` holds SELECT on seven `cdm`
   tables and `vocab.concept`. Not `stored_resources`, not
   `roi_requests`, not `index_state`, not any imaging table, and no write
   grant of any kind. A query that got past every other check still
   cannot write anything or read outside that grant. **This is the
   boundary.**
2. **Read-only transaction.** Every query runs inside
   `SET TRANSACTION READ ONLY`, so a grant widened by mistake later
   cannot be exercised from here.
3. **Statement timeout and a wrapped row limit.** A cross join is not a
   security problem but it is an outage.
4. **Lexical checks** in `core/analytics/sql_guard.py` — writes,
   multiple statements, filesystem and catalogue access. These fail fast
   with a message the model can correct. They are a usability feature
   with a security benefit, not the reverse.

The table names in point 1 are literal: `stored_resources` is the real
index table in `core/db/schema.sql`, and the grant list must be read
exactly as written.

The limit is applied by **wrapping** (`SELECT * FROM (…) LIMIT n`), never
by editing the query — appending `LIMIT 500` to a query ending in
`LIMIT 1000000` does nothing.

**Every generated query is recorded in the audit trail verbatim.** That
is what makes generated SQL acceptable at all: a reviewer reads the exact
statement, not a tool name and some arguments.

---

## Counts are exact, and that was a decision

Aggregate counts over a patient population can re-identify people when
the number is small — "how many patients have this rare condition"
returning 2, in a facility with a known catchment, is disclosive. Many
research data warehouses suppress counts below a threshold (i2b2 does;
CMS uses 11 for public cell suppression).

**This deployment reports exact counts, with no suppression.** That was
chosen deliberately over threshold suppression, on the reasoning that
every count here is already permission-gated and audited, and the same
user could run the query against the OMOP layer directly. It puts the
re-identification judgment on access control rather than on output
filtering.

What that means in practice, and what to watch:

- A small count is a real disclosure risk. Treat `analytics:query` as a
  grant that can identify people indirectly, not as a reporting
  convenience.
- The audit trail is the control. Every cohort query is recorded against
  a username with the query itself; review it the way you would review
  chart access.
- If your organisation decides otherwise later, suppression belongs in
  `core/analytics/cohort.py` at the point each count is returned, not in
  the assistant's prompt.

---

## Counting patients is not counting events

The single most likely wrong answer this layer can produce, and it looks
entirely plausible.

`cdm.condition_occurrence` holds one row per diagnosis occurrence. A
patient diagnosed with diabetes at four visits is **one patient and four
rows**. `SELECT count(*)` answers "how many diagnoses"; the question was
almost always "how many patients".

Every curated tool uses `COUNT(DISTINCT person_id)`. The assistant is
told to do the same in generated SQL and to say which one it counted. If
a number looks about three times larger than you expected, this is the
first thing to check.

The same applies to the facility breakdown: a patient seen at two
facilities appears once in the headline figure and once per facility in
the rows, so **the rows do not sum to the total**. That is correct, and
it is stated in every result.

---

## What a count does and does not include

Stated in the caveats of every result, and worth understanding before an
operational decision rests on one:

- **Only what this deployment holds.** A patient diagnosed before the
  period this deployment retains, or treated at an organisation whose
  records are not here, is not counted.
- **Only what the source EMR recorded**, under the code it used.
- **Without the OHDSI Athena vocabulary**, matching uses the raw source
  codes (ICD-10, SNOMED) rather than standard concepts. That vocabulary
  is a separate licensed download this project cannot bundle. Condition
  shortcuts cover common code ranges; a condition recorded under a code
  outside them will not be counted. The result says so when the
  vocabulary is absent — do not report the number without that caveat.
- **Facility counts exclude visits with no facility recorded**, and the
  result reports how many were excluded.

Every result carries this deployment's total patient count as a
denominator, because 12 out of 40 and 12 out of 400,000 are different
answers to the same question.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "no such tool" for cohort questions | `PHI_AI_OMOP_ANALYST_USERNAME` unset, or the role lacks `analytics:query` |
| Facility counts return zero | No Encounter has been through the OMOP ETL carrying a `serviceProvider` reference — see above |
| Name search returns nothing | `identity.patient_identity` not populated; run the ETL over Patient resources |
| Name search never matches misspellings | `pg_trgm` absent; search fell back to exact and prefix matching |
| `permission denied for table …` | The query named a table outside the analyst grant. Working as intended. |
| `role "phi_ai_identity" does not exist` | `core/db/identity_schema.sql` has not been run against this database — see Setup step 3 |
| Counts look ~3x too high | `COUNT(*)` instead of `COUNT(DISTINCT person_id)` |
| Every string filter returns zero rows | A bug now fixed — the guard used to execute a copy with string literals stripped. |

---

## Known gaps

- **No de-identification.** Cohort results are counts, but
  `run_analytics_query` can select any column the analyst role can read,
  including birth years and source values. It is a read of identified
  PHI and is audited as one.
- **Minimum necessary is a prompt, not a control.** Nothing stops a
  generated query selecting more columns than a question needed. The
  audit trail records it.
- **Condition shortcuts are a convenience, not a terminology.** Seventeen
  common families as ICD-10 prefixes. They are not a substitute for the
  Athena vocabulary and are not clinically validated for research use.
- **Facility names come from `reference.display`** on the Encounter,
  because this project does not ingest `Organization`. If the source EMR
  populated that text inconsistently, one facility can appear under more
  than one name. The id is authoritative; the name is a label.
- **No cross-schema joins between analytics and identity.** Deliberate —
  `omop_analyst` has no grant on `identity.patient_identity`, so a cohort
  query cannot resolve its members to names in one step.
