# Runbook: OMOP CDM analytics layer setup

Adds the OMOP CDM analytics layer on top of an existing PHI AI Platform
deployment, on any of the three supported clouds. **Assumes you have
already completed the base setup runbook for your cloud through its own
database step** (`RUNBOOK_AWS_SETUP.md` Step 6a, `RUNBOOK_GCP_SETUP.md`
Step 4a, or `RUNBOOK_AZURE_SETUP.md` Step 4a - the lightweight index
must already be running before this layer makes sense) - this runbook
does not repeat that setup.

> **This holds identified PHI.** Unlike the lightweight index
> (`stored_resources`), the tables this runbook creates hold real
> patient dates of birth, diagnoses, medication exposures, and lab
> values, tied to a persistent `person_id`. Treat access to the
> `omop_analyst` role (AWS/Azure) or to `omop_etl` (GCP - see that
> cloud's own section below for why it's the same identity that also
> writes the index there) with the same seriousness as any other PHI
> access path in this project.

Pick your cloud's section below - the schema and ETL pipeline
(`core/db/omop_schema.sql`, `core/db/omop_etl.py`) are identical across
all three; only the bootstrap mechanics differ, since each cloud's own
IAM-database-authentication model works differently. See
`core/db/connection.py`'s module docstring for the full account of why.

Every `phi_ai_*` name below (`phi_ai_ingest`, `phi_ai_index`,
`phi_ai_master`) is a literal SQL identifier. Type them exactly as
printed - a wrong identifier fails the connection outright rather than
degrading to anything usable, which is the intended behaviour and not
something to work around.

---

## AWS

### Step 1 - Apply the updated Terraform

This adds the `rds-db:connect` grant the ingest role needs to
authenticate as `omop_etl` - without this step, every OMOP write will
fail at the AWS IAM layer before reaching Postgres at all.

```bash
cd deploy/aws
terraform plan -out=tfplan
```

Confirm the plan shows exactly one change: a new
`ConnectToOmopDatabase` statement added to `aws_iam_role_policy.ingest`.
If the plan shows anything else changing, stop and investigate before
applying - it should be a single, additive IAM policy change. In
particular, a `destroy` line against any bucket, KMS key or database
instance means Terraform has lost track of a resource it believes it
manages. Find out why before applying; on a KMS key that plan is not
recoverable.

```bash
terraform apply tfplan
cd ../..
```

### Step 2 - Create the OMOP schema and vocabulary table

Connected as the same RDS master user used in the base setup's Step 6a:

```bash
psql "host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/omop_schema.sql -f core/db/omop_vocab_schema.sql
```

Expect a run of `CREATE SCHEMA`, `CREATE TABLE`, `CREATE INDEX`, and
`CREATE OR REPLACE VIEW` statements for `omop_schema.sql`, then the
same for the single `vocab.concept` table in `omop_vocab_schema.sql` -
that table is created **empty**; see "Load vocabulary" below for what
that means in practice.

### Step 3 - Bootstrap the OMOP roles

```bash
psql "host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/omop_bootstrap_aws.sql
```

Expect `CREATE ROLE`, `GRANT ROLE`, and a run of `GRANT` statements
ending in `REVOKE DELETE` - if it stops earlier, something failed
(`\set ON_ERROR_STOP on` is set specifically so a partial failure halts
loudly). This creates `omop_etl` (INSERT/UPDATE on every `cdm` table,
narrow column-scoped SELECT on `cdm.person`/`cdm.visit_occurrence` for
find-or-create lookups only) and `omop_analyst` (SELECT-only, full row
access) - see that file's own header for the complete access-model
reasoning.

### Step 4 - Add the OMOP env var

Only one new value is needed, and unlike the GCP equivalent (whose
username is derived from a service account email) it's a fixed string
on AWS - no Terraform output to look up:

```bash
echo "PHI_AI_OMOP_ETL_USERNAME=omop_etl" >> .env
```

The five existing `PHI_AI_DB_*` lines already in `.env` from the
base setup cover the rest - `core/config/settings.py`'s
`omop_target_configured()` only needs `db_target_configured()` (already
true) plus this one new value.

### Step 5 - Verify the connection works before running real ingestion

```bash
set -a; source .env; set +a
python3 -c "
from core.config.settings import Settings
from core.db import connection as db_connection

settings = Settings.from_env()
assert settings.omop_target_configured(), 'omop_target_configured() is False - check PHI_AI_OMOP_ETL_USERNAME is set'

conn = db_connection.connect(settings, username=settings.omop_etl_username)
with conn.cursor() as cur:
    cur.execute('SELECT current_user, person_id FROM cdm.person LIMIT 1')
    print('connected as:', cur.fetchone() or '(connected; cdm.person is empty, which is expected on a first run)')
conn.close()
print('OK - omop_etl authenticated and queried cdm.person successfully.')
"
```

Deliberately selects `person_id` specifically (one of the two columns
`omop_etl` actually holds SELECT on) rather than `count(*)` or `*` -
this exercises the exact narrow, column-scoped grant
`core/db/omop_bootstrap_aws.sql` creates, not a broader table-level
privilege that would mask a scoping mistake in that grant.

Expected output ends with the `OK` line (an empty `cdm.person` is
correct - nothing has been ingested through this path yet). If this
fails with an AWS `AccessDenied` on `rds-db:connect`, Step 1's
Terraform apply either didn't run or didn't complete - check
`terraform state show aws_iam_role_policy.ingest` for the
`ConnectToOmopDatabase` statement. If it fails with a Postgres
authentication error instead, Step 3 didn't complete - check
`\du omop_etl` in psql. If it fails with a Postgres permission error
on `person_id` specifically, the column-scoped GRANT in Step 3 didn't
apply correctly - re-run `core/db/omop_bootstrap_aws.sql`.

### Step 6 - Run ingestion and confirm OMOP rows land

```bash
python -m core.fhir.scheduler --once
```

Watch the log for `Connected to OMOP analytics layer` - if you instead
see `No PHI_AI_OMOP_ETL_USERNAME set`, re-check Step 4. Then see
"Confirm rows landed" below.

---

## GCP

### A real architectural difference, read before starting

Unlike AWS/Azure, GCP's Cloud SQL IAM database authentication ties a
Postgres role name directly to the authenticating identity's own email
- one service account maps to exactly one role name, never several.
This project's `ingest` service account is therefore used for BOTH the
lightweight index AND OMOP - `PHI_AI_DB_INGEST_USERNAME` and
`PHI_AI_OMOP_ETL_USERNAME` are set to the **same value**. There is
no separate `omop_etl` role on GCP, and no separate `omop_analyst` role
provisioned yet either (it would need its own dedicated service
account, not built in this installment). See
`deploy/gcp/database.tf`'s own header for the full reasoning.

### Step 1 - Create the OMOP schema and vocabulary table

Connected as the `postgres` admin user (see `RUNBOOK_GCP_SETUP.md` Step
4a for how that password was set):

```bash
gcloud sql connect phiai-index --user=postgres --database=phi_ai_index
```

(`phiai-index` is the Cloud SQL instance name Terraform builds from
`name_prefix`. Substitute your actual instance name if you changed that
variable, or extract it from
`terraform output instance_connection_name`'s `project:region:instance`
format.)

Then, in the psql session (or via `-f`, same as the AWS/Azure paths):
run `core/db/omop_schema.sql`, then `core/db/omop_vocab_schema.sql`.

### Step 2 - Bootstrap the OMOP grants

Run `core/db/omop_bootstrap_gcp.sql`, substituting its `{INGEST_IAM_USER}`
placeholder with the SAME value `core/db/bootstrap_gcp.sql` already
used - `terraform -chdir=deploy/gcp output ingest_db_iam_user` (already
double-quoted). That value is a service account email whose ID is
derived from `name_prefix`, so read it from the Terraform output rather
than typing it.

### Step 3 - Verify PHI_AI_OMOP_ETL_USERNAME is set

If you followed `RUNBOOK_GCP_SETUP.md` Step 4a's `.env` instructions,
this is already set to the same value as
`PHI_AI_DB_INGEST_USERNAME`. Confirm:

```bash
grep OMOP_ETL_USERNAME .env
grep DB_INGEST_USERNAME .env
```

Both lines should show the identical username.

### Step 4 - Run ingestion and confirm OMOP rows land

```bash
python -m core.fhir.scheduler --once
```

Watch the log for `Connected to OMOP analytics layer`. Then see
"Confirm rows landed" below.

---

## Azure

### A real architectural advantage, worth noting for contrast with GCP

Azure's `pgaadauth_create_principal_with_oid()` lets a Postgres role
name be chosen freely, independent of the authenticating identity's own
name. The SAME `ingest` managed identity is registered under a
**second, genuinely distinct** role - `omop_etl` - preserving real
role separation between the index writer and the OMOP writer, the same
way AWS does. See `deploy/azure/database.tf`'s own header for the full
contrast with GCP.

### Step 1 - Create the OMOP schema and vocabulary table

Connected as the Microsoft Entra administrator (same connection as
`RUNBOOK_AZURE_SETUP.md` Step 4a):

```bash
psql "host=$(terraform -chdir=deploy/azure output -raw db_host) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/omop_schema.sql -f core/db/omop_vocab_schema.sql
```

### Step 2 - Bootstrap the OMOP grants

```bash
psql "host=$(terraform -chdir=deploy/azure output -raw db_host) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require" \
  -f core/db/omop_bootstrap_azure.sql
```

Substitute `{INGEST_PRINCIPAL_ID}` first with
`terraform -chdir=deploy/azure output ingest_identity_principal_id` -
the SAME value `core/db/bootstrap_azure.sql` already used for that
file's `phi_ai_ingest` registration. That principal ID is a GUID,
independent of the Postgres role name it is registered under, which is
exactly the property that lets Azure keep `omop_etl` and
`phi_ai_ingest` genuinely separate where GCP cannot.

### Step 3 - Verify the env var

Already in `.env` from `RUNBOOK_AZURE_SETUP.md` Step 4's
`env_fragment`, if `enable_db` was true at apply time:

```bash
grep OMOP_ETL_USERNAME .env
```

Should show `PHI_AI_OMOP_ETL_USERNAME=omop_etl` - a fixed string,
genuinely distinct from `PHI_AI_DB_INGEST_USERNAME=phi_ai_ingest`.

### Step 4 - Run ingestion and confirm OMOP rows land

```bash
python -m core.fhir.scheduler --once
```

Watch the log for `Connected to OMOP analytics layer`. Then see
"Confirm rows landed" below.

---

## Confirm rows landed (all clouds)

Connected as your cloud's own database administrator (master user, AAD
admin, or `postgres`, per whichever section above you followed):

```sql
SELECT resourceType, count(*) FROM (
    SELECT 'person' AS resourceType FROM cdm.person
    UNION ALL SELECT 'visit_occurrence' FROM cdm.visit_occurrence
    UNION ALL SELECT 'condition_occurrence' FROM cdm.condition_occurrence
    UNION ALL SELECT 'procedure_occurrence' FROM cdm.procedure_occurrence
    UNION ALL SELECT 'drug_exposure' FROM cdm.drug_exposure
    UNION ALL SELECT 'measurement' FROM cdm.measurement
    UNION ALL SELECT 'observation' FROM cdm.observation
) t GROUP BY resourceType ORDER BY resourceType;
```

Every `condition_concept_id`/`procedure_concept_id`/`drug_concept_id`/
`measurement_concept_id`/`observation_concept_id` value will be `0`
right now (OMOP's own "unmapped" convention) - the vocabulary table is
still empty. `gender_concept_id` will be correctly populated (8507/
8532/8551), since that mapping is hardcoded and verified - see
`core/db/omop_concepts.py`. This is the expected, correct state of a
first run on any cloud: structurally complete rows, concept mapping
pending vocabulary load.

## Load vocabulary (optional, identical across all three clouds)

Without this, every non-gender concept_id stays 0 indefinitely - rows
are stored and queryable by source code/date, just not yet mapped to
OHDSI's standard terminology. To fix that:

1. Register at [athena.ohdsi.org](https://athena.ohdsi.org) and request
   a vocabulary download - at minimum SNOMED CT US Edition, RxNorm,
   ICD10CM, and LOINC (all free to use); accept each vocabulary's own
   license terms during the request.
2. Athena delivers a `CONCEPT.csv` (tab-delimited despite the
   extension) among other files. Load only what
   `core/db/omop_vocab_schema.sql`'s `vocab.concept` table needs, using
   whichever connection command your cloud's section above used:
   ```bash
   psql "..." -c "\copy vocab.concept FROM 'CONCEPT.csv' WITH (FORMAT csv, DELIMITER E'\t', HEADER true, NULL '')"
   ```
3. Re-run "Confirm rows landed" above, or re-run the scheduler against
   already-ingested resources - `core/db/omop_etl.py`'s UPSERT logic
   (INSERT, or UPDATE on the UNIQUE violation a re-ETL of the same
   resource produces) means existing rows get their concept_id fields
   correctly populated in place, not duplicated.

## Known gaps, stated plainly

- **Three resource types are not mapped, on any cloud**:
  `DocumentReference`, `AllergyIntolerance`, `ExplanationOfBenefit` -
  see `core/db/omop_schema.sql`'s header for why each is deliberately
  deferred rather than mechanically included.
- **`omop_analyst` (the read-only, human-facing role) is only
  provisioned on AWS** (`core/db/omop_bootstrap_aws.sql`). It doesn't
  exist as a distinct concept on GCP (where `ingest` already holds
  read access alongside its write access, by necessity - see that
  cloud's own section above) and hasn't been built for Azure yet
  (Azure genuinely could support a separate, dedicated
  `pgaadauth`-registered analyst role, the same way `omop_etl` is
  separate from `phi_ai_ingest` there - just not done in this
  installment). Wherever it does or could exist, it's real, broad
  access to identified PHI by construction. Treat granting it with the
  same rigor as granting the general `restore` role.
- **Type Concept provenance fields default to 0** (`VISIT_TYPE_CONCEPT_ID`
  and siblings in `core/db/omop_etl.py`) - not verified with the same
  confidence as gender/visit-class mapping, on any cloud. Fine
  structurally; not yet analytically precise about record provenance.
- **GCP's Cloud SQL has no private networking configured** - see
  `RUNBOOK_GCP_SETUP.md`'s own "Known gaps" section; affects this
  runbook's GCP section the same way it affects the base index.
