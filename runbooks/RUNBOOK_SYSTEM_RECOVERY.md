# Runbook: system recovery

Audience: whoever operates this deployment technically. This is the
procedure for getting the SYSTEM back after an update that failed - an
image that will not come up, a migration that broke a screen, a
vocabulary load that lost rows, a config file that stopped a service, a
key rotation the vendor never accepted. It is not about records:
retrieving stored records for a request is `RUNBOOK_DATA_RESTORE.md`, and
it does not change here.

> **Read this first.** The records are already covered: the object store
> is the system of record, versioned under a KMS envelope, and the Postgres
> index is rebuildable from it (`python -m core.db.reconcile`,
> `RUNBOOK_INDEX_MAINTENANCE.md`). What had NO way back before this runbook
> was the *operational state* - the `platform_*` tables that hold what an
> administrator set (`core/db/platform_state_schema.sql`), the `config/`
> files, the vocabulary schema, the running image - and that is what every
> section below is about.
>
> Every recovery here is the fifth of the five steps the Components screen
> runs (`/system/components`; the proposal of 2026-09-07 §5). In **direct**
> mode the `updater` service performs it; in **guided** mode the screen
> prints the step and you run it. Either way the words are the same ones
> below: the guided checklist on the screen is generated from this runbook,
> so the two cannot disagree. Where the screen's engine has not landed yet,
> every command below still runs by hand exactly as written.
>
> Every `phi_ai_*` name here - the `phi_ai_index` database, the
> `phi_ai_master` user - is a literal identifier created by the bootstrap
> SQL (`core/db/bootstrap_aws.sql`) and granted in Terraform. The three
> application roles hold no DDL by design; every `psql`, `pg_dump` and
> `pg_restore` below connects as the master user, the same one used exactly
> once at setup (`RUNBOOK_AWS_SETUP.md` Step 6a). The master password comes
> from Terraform state (the `terraform state pull` snippet in
> `RUNBOOK_INDEX_MAINTENANCE.md` Step 4); it is entered at the prompt and
> stored nowhere.

The connection string used throughout, from the stack in front of you:

```bash
PGCONN="host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_index user=phi_ai_master sslmode=require"
```

`db_endpoint` is the hostname only. `terraform output -raw store_bucket`
and `terraform output -raw store_kms_key_arn` give the bucket and key the
backups below use; both are outputs of `deploy/aws/outputs.tf`.

---

## The five steps, and what "Recovered" means

Every update - direct or guided, any component - is the same procedure:

| Step | Direct mode (the `updater` service) | Guided mode (you, with the screen verifying) |
|---|---|---|
| 1. Plan | Shows what will change and the exact way back; nothing proceeds until the admin types the component and the target version | Same plan, same typed confirmation |
| 2. Back up | Takes the component's backup unit (table below) and records its checksum; **no verified backup, no apply** | You take it with the commands in this runbook; the screen verifies the checksum, or records your attestation as an attestation |
| 3. Apply | Performs it | The screen prints the command or file and waits |
| 4. Verify | The sweep (last section) plus the component's own check | The same sweep, run by the screen where it can reach, otherwise by you |
| 5. Recover | Automatic on any failed check: the way back, then step 4 again | The screen prints the way back; you run it; step 4 runs again |

**"Recovered" is displayed only after the second verification passes.**
A rollback that brought the previous image up is not a recovery until
`python -m core.healthcheck` is green, the audit chain verifies, and the
routes answer. The sweep is the last section of this runbook; nothing is
signed off before it.

Three things hold across every section:

- **One job at a time.** The open job row in `platform_updates`
  (`core/db/components_schema.sql`; a partial unique index lets the database
  refuse a second open job) is the lock across every service. While it is
  open the web app serves a maintenance banner on every page and refuses a
  second job. If the updater dies mid-job it reads the open job on restart
  and offers exactly one action: finish, or roll back.
- **Retention is three.** Three image digests and three backups per store
  are kept (`KEEP` in `core/components/registry.py`); an older one is
  removed only after a newer *verified* backup exists.
- **A PHI host never phones home.** Nothing here pulls from the internet.
  Release images come from the registry the operator configured, verified
  by digest and signature; "latest known" comes from the manifest the
  workstation CLI wrote.

## Backup unit and recovery, per component

The registry's own words (`core/components/members.py`; the demo mirror
of the demonstration is pinned to it by
`tests/test_components_coverage.py`). The section that carries each
recovery out is named on the right.

| Component | Mode | Backup unit | Recovery | Section |
|---|---|---|---|---|
| Release stamp, Build stamp | record | The previous release, kept | Roll the release back | C |
| Running image | **direct** | The previous image digest, kept (3 retained) | `compose up -d` the previous digest, then the sweep | C |
| Python pins, Vendored front-end, EMR vendor profiles | record / guided | The previous image | The previous image | C |
| Runtimes | record | n/a - facts only | A runtime changes through a release or a guided infra apply | C, or Terraform |
| Infra pins | guided | Terraform state, versioned in the state bucket | Guided: apply the previous plan | not this runbook - `deploy/aws/` |
| Terminology releases | **direct** | The previous vocabulary schema, kept on swap | Swap the previous schema back | E |
| Model catalogue | **direct** | The registry row's previous state | Restore the row | B (the row lives in `platform_models`) |
| Synthetic corpus | record | n/a - reproducible from generator, version and seed | Regenerate from the recorded seed | `scripts/generate_corpus.py` |
| Migration ledger | guided | `pg_dump` of the affected schemas, before the `up` | Run the `down`; restore the dump if the `down` cannot | D |
| Key and credential ages | guided | The previous key, retained until the vendor confirms the new one | Re-publish the previous JWKS | G |
| Operator config files | **direct** | The previous file, kept as `<name>.previous` | Restore it | F |
| Backups, Releases kept, Update journal, Last green gates | record | (these ARE the record) | - | B, H |

---

## A - Before any apply: the prechecks

Nothing is touched until all of these hold. The updater checks them
itself; in guided mode, check them yourself and do not skip one because
the change looks small - a rollback to bytes that were never the running
bytes is not a rollback, and the prechecks are what make sure the way
back exists before the way forward is taken.

```bash
docker compose ps                                        # every service you expect, Up
docker compose exec app python -m core.healthcheck       # exit 0 before you start, or you cannot tell what the update broke
docker compose exec app python -m core.audit.verify      # the chain is intact going in
aws s3 ls "s3://$(cd deploy/aws && terraform output -raw store_bucket)/system/backups/"   # the backup destination answers
```

And: no open job on the Components screen; the release you are moving to
is newer than the one running (or you are deliberately rolling forward to
an older one and have said so in the plan); the signature on the release
manifest verifies. Then take the backup - section B - before anything
else.

## B - The operational-state dump: take it, restore it

The unit behind every image change and every migration. The `platform_*`
tables hold what an administrator SET - configuration, the model
registry, exchanges, consents, links, the crosswalk, run history
(`core/db/platform_state_schema.sql`) - plus the update journal, the
backups and rehearsals per store, the digests kept and the
acknowledgements (`core/db/components_schema.sql`), and by design no PHI
(`core/web/platform_state.py`). The migration ledger, `schema_migrations`,
lives beside them under a name of its own, so the dump names it
explicitly. They go to the store bucket under a
`system/backups/` prefix, under the same KMS key the records use, so they
inherit the bucket's versioning, lifecycle and BAA. `config/` is copied
beside the dump. `.env` is **never copied**: its presence and its SHA-256
are recorded, and that is all.

### Take it

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STORE="$(cd deploy/aws && terraform output -raw store_bucket)"
KMS="$(cd deploy/aws && terraform output -raw store_kms_key_arn)"

# 1. The dump: every platform_* table and the migration ledger, custom format (restorable table by table).
pg_dump "$PGCONN" --format=custom --table='public.platform_*' --table='public.schema_migrations' --file="platform_state-$STAMP.dump"

# 2. Its checksum, recorded beside it. (macOS spells this `shasum -a 256`.)
sha256sum "platform_state-$STAMP.dump" > "platform_state-$STAMP.dump.sha256"
sha256sum .env >> "platform_state-$STAMP.dump.sha256"      # presence and checksum of .env; the file itself stays here

# 3. To the bucket, under the store's KMS key.
aws s3 cp "platform_state-$STAMP.dump"        "s3://$STORE/system/backups/$STAMP/platform_state.dump"        --sse aws:kms --sse-kms-key-id "$KMS"
aws s3 cp "platform_state-$STAMP.dump.sha256" "s3://$STORE/system/backups/$STAMP/platform_state.dump.sha256" --sse aws:kms --sse-kms-key-id "$KMS"
aws s3 cp config/ "s3://$STORE/system/backups/$STAMP/config/" --recursive --sse aws:kms --sse-kms-key-id "$KMS"
```

The step fails closed: if the upload did not land, or the checksum file
was not written, there is no backup and the apply does not proceed. Do not
"mark it taken" - an attestation is a different, visibly distinct thing
the screen records for an external backup (an RDS snapshot you took in
the console), not a substitute for a dump the updater verified.

### Restore it

Stop the writers first: the web app is the one service that writes these
tables.

```bash
docker compose stop web

# 1. Fetch the dump you are returning to, and prove it is the bytes you wrote.
aws s3 cp "s3://$STORE/system/backups/$STAMP/platform_state.dump"        ./
aws s3 cp "s3://$STORE/system/backups/$STAMP/platform_state.dump.sha256" ./
sha256sum --check --ignore-missing platform_state.dump.sha256     # must print: platform_state.dump: OK

# 2. Put the tables back exactly as dumped. --clean --if-exists drops each
#    platform_* table before recreating it; nothing outside the dump is touched.
pg_restore --clean --if-exists --no-owner --dbname="$PGCONN" platform_state.dump

docker compose start web
```

If `config/` was part of what changed, restore it beside the tables:
section F, or `aws s3 cp "s3://$STORE/system/backups/$STAMP/config/" config/ --recursive`
for the whole directory, then restart the services that mount it
(`app`, `web`, `scheduler`, `verify`, `bulk-scheduler` - all mount
`./config:/app/config:ro`).

Then the sweep (last section). Not before.

## C - Image rollback to the previous digest

The release unit is an image tagged with the `RELEASE` and pinned by
digest. Every release-borne row - the stamps, the Python pins, the
vendored front-end, the vendor profiles - rolls back with it, because
none of them changes on a running host: nothing is `pip install`ed into
a container, ever.

`docker-compose.yml` builds the application services (`app`, `web`,
`scheduler`, `verify`, `bulk-scheduler`) from the `Dockerfile`; `viewer`
is the one pre-built image, pinned by tag (`ohif/app:v3.13.4`). The
updater runs the previous digest through the `.env`-driven `image:` line
the release unit adds to those services (proposal §9); the guided path is
the same digest by hand. What is kept: three digests, listed on the
Components screen under *Releases kept* with their build stamps
(`platform_releases` in `core/db/components_schema.sql`).

```bash
# 1. What is running, and what is kept to return to.
docker compose images                      # the image and ID each service is running now
docker image ls --digests                  # every kept image, with its digest

# 2. Take the operational-state dump first (section B). Then bring the
#    previous digest up for the services built from the Dockerfile.
#    With the digest-pinned image: line in force, set it to the previous
#    digest in .env and recreate:
docker compose up -d --no-build app web scheduler bulk-scheduler
#    Without it (a deployment still building from the Dockerfile), point
#    the service's image name at the kept image and recreate the same way:
#      docker tag <previous image ID from step 1> <the image name `docker compose images` shows for the service>
#      docker compose up -d --no-build app web scheduler bulk-scheduler

# 3. The running stamp must be the previous release's, field for field.
docker compose exec web python -c "import core; print(core.__version__)"
```

`--no-build` is the point: a rollback recreates containers from an image
that already exists and is verified; it never builds one. A build would
be a new release, and a new release goes through the five steps from the
plan.

If the dump in section B was taken because a migration rode along with
the image (it does, when the release ships a schema change), restore it
now (section B, and section D for the `down`). Then the sweep.

## D - A migration: the `down`, and the dump restore

Schema files are `core/db/*.sql`. The migration ledger
(`schema_migrations` in `core/db/components_schema.sql`, kept by
`core/components/ledger.py`: name, checksum, applied_at, applied_by, note)
lists every one by glob and says which ran - the one-time backfill wrote a
row, noted `backfill`, for each file whose objects already existed, after
a `pg_dump` of the schemas it touches; a migration is direct only when a DDL
credential was entered at the moment of use - held for that job, never
stored - **and** the migration ships a `down`. Everything else is guided.
Today no file in `core/db/` ships a `down`, so today the way back is the
dump.

Before the `up`, the backup unit is a dump of the schemas the migration
touches - not only `platform_*`. For a change to `core/db/schema.sql`
that is the index itself:

```bash
pg_dump "$PGCONN" --format=custom --schema=public --file="index-$STAMP.dump"
sha256sum "index-$STAMP.dump" > "index-$STAMP.dump.sha256"
# to the bucket exactly as in section B, under system/backups/$STAMP/
```

Recovery, in this order:

1. **The `down`, when the migration ships one.** Run it with the same
   `psql "$PGCONN" -f <file>` the `up` used, then confirm the column or
   table is gone from `information_schema` - the screen's own check for a
   migration:
   ```bash
   psql "$PGCONN" -c "SELECT table_name, column_name FROM information_schema.columns WHERE table_name = '<table>' ORDER BY ordinal_position"
   ```
2. **The dump, when the `down` cannot** - or there is none. Stop the
   services that write the schema (`docker compose stop web scheduler
   bulk-scheduler` for the index; `web` alone for `platform_*`), then:
   ```bash
   sha256sum --check --ignore-missing index-$STAMP.dump.sha256
   pg_restore --clean --if-exists --no-owner --dbname="$PGCONN" index-$STAMP.dump
   docker compose start web scheduler bulk-scheduler
   ```
   `--clean --if-exists` drops and recreates what the dump holds and
   nothing else. On the index, records ingested between the dump and the
   restore are in the object store and not in the index: run
   `python -m core.db.reconcile` (it reports them as missing index rows)
   and backfill them with `python -m core.fhir.scheduler --once`, exactly
   as `RUNBOOK_INDEX_MAINTENANCE.md` Step 2 says.
3. Mark the ledger row: the migration is not applied. On a deployment
   with the ledger this is the updater's write; by hand it is a row the
   screen will show as *behind* until the migration is re-run correctly.

Then the sweep.

## E - Vocabulary schema swap-back

A terminology load goes into a staging schema, is verified by count, and
is swapped in; the previous schema is kept on the swap (one previous
schema, on disk). The schema is `vocab` and its table `vocab.concept`
(`core/db/omop_vocab_schema.sql`; the load itself is
`RUNBOOK_OMOP_SETUP.md`, "Load vocabulary"). This runbook fixes the
names: the kept schema is `vocab_previous`; a schema that failed
verification and was swapped out is `vocab_failed`.

Before the swap, the count that the swap will be verified against:

```bash
psql "$PGCONN" -c "SELECT count(*) FROM vocab.concept"
```

Swap back - one transaction, so there is never a moment with no `vocab`:

```bash
psql "$PGCONN" <<'SQL'
BEGIN;
ALTER SCHEMA vocab          RENAME TO vocab_failed;
ALTER SCHEMA vocab_previous RENAME TO vocab;
COMMIT;
SQL
psql "$PGCONN" -c "SELECT count(*) FROM vocab.concept"     # the count recorded before the swap, exactly
```

The ETL reads `vocab.concept` by name (`core/db/omop_etl.py`), so a swap
needs no restart. Drop `vocab_failed` only after the sweep passes and the
count matched; until then it is evidence. If a `pg_dump --schema=vocab`
was taken instead of keeping a schema (an older deployment), restore it
with `pg_restore --clean --if-exists --no-owner --dbname="$PGCONN" <file>`
as in section D.

Then the sweep, plus the vocabulary's own check above.

## F - Config file restore from `<name>.previous`

Operator config lives in `config/` on the host, bind-mounted read-only
into every application service (`./config:/app/config:ro`) and read at
startup; the shipped examples are `config/*.example.yaml`. A direct apply
of missing keys keeps the previous file beside the new one as
`<name>.previous` - `config/smart_issuers.yaml.previous` for
`config/smart_issuers.yaml`. The way back is that file:

```bash
cp -p config/<name>.previous config/<name>
sha256sum config/<name>          # equals the checksum the plan recorded for the previous file
docker compose restart app web scheduler bulk-scheduler    # they read config/ at startup; add verify if the profile is on
```

Do the same from `system/backups/$STAMP/config/` in the bucket (section
B) when the `.previous` file is not there. Never restore `.env` from a
backup: it was never copied. Its checksum in the `.sha256` file tells you
whether the one on disk is the one the backup saw.

Then the sweep. `python -m core.healthcheck` is the check that reads the
configuration the way the services do.

## G - JWKS re-publish: a key rotation the vendor did not accept

Key ages are shown, never values. Rotation is guided, by the vendor's
"Setting it up" chapter in `docs/EMR_CONNECTORS.md`, and the backup unit
is the previous key pair, **retained until the vendor confirms the new
one** - because the vendors that fetch a hosted JWK Set pick a change up
on their own schedule (Altera and Veradigm nightly, per
`RUNBOOK_INCIDENT_RESPONSE.md`), so the old key must keep working until
the new one is proven.

A rotation has failed when token requests fail signature verification:
`invalid_client` from the vendor, and the scheduler's audit entries show
no `record.write` after the rotation. The way back, in this order:

1. **Re-publish the previous JWK Set** at the JWKS URL registered on the
   client ID - the same file the vendor fetched before. For the Epic
   sandbox that is `deploy/aws/epic_jwks_nonprod.json` at the raw URL
   registered as the app's Non-Production JWK Set URL
   (`deploy/aws/README_EPIC_JWKS.md`); for a production connection, the
   URL you host. The `kid` in the set is an identifier the vendor looks
   up; it has to be the previous key's `kid`, exactly.
2. **Point the platform back at the previous private key**: in `.env`,
   `PHI_AI_FHIR_PRIVATE_KEY_PATH` to the previous `.pem` (mode 600) and
   `PHI_AI_FHIR_JWT_KID` to the previous `kid`. Then recreate the services
   that carry the key - the compose file mounts it at the same absolute
   path inside and out, and `env_file` changes need a recreate, not a
   restart:
   ```bash
   docker compose up -d app scheduler bulk-scheduler
   ```
3. **Verify the set the vendor will fetch**:
   ```bash
   curl -fsS "<the registered JWKS URL>" | python3 -c "import json,sys; print([k['kid'] for k in json.load(sys.stdin)['keys']])"
   ```
   must print the previous `kid`. Then one ingest cycle by hand,
   `docker compose exec app python -m core.fhir.scheduler --once`, and a
   `record.write` on the audit trail from it.

Only remove a previous key from the set, and from disk, after the vendor
has confirmed the new one and the sweep has passed with it. A compromised
key is the other way round - remove first - and that is
`RUNBOOK_INCIDENT_RESPONSE.md`, not this.

## H - The restore rehearsal, per store, every 180 days

A backup nobody has restored is a hope. Each store below has a rehearsal
on the cadence in `core/components/registry.py` (`CADENCE_DAYS["rehearsal"]`,
180 days); the Components screen shows "restore last rehearsed <date>",
or "never", per store, and a date past its cadence is amber. A rehearsal
restores into a scratch database or directory, verifies, and is
recorded; it never touches the live tables.

```bash
# The scratch database, created and dropped by the master user.
psql "$PGCONN" -c "CREATE DATABASE phi_ai_rehearsal"
PGREHEARSE="host=$(cd deploy/aws && terraform output -raw db_endpoint) port=5432 dbname=phi_ai_rehearsal user=phi_ai_master sslmode=require"
```

| Store | Rehearse by | Verified by |
|---|---|---|
| Operational state (`platform_*`) | Fetch the latest `system/backups/<stamp>/platform_state.dump`, check its `.sha256`, `pg_restore --no-owner --dbname="$PGREHEARSE" platform_state.dump` | `psql "$PGREHEARSE" -c "SELECT count(*) FROM platform_models"` equals the live count; a `platform_config` row reads back as set |
| Vocabulary schema | `pg_dump "$PGCONN" --format=custom --schema=vocab` and `pg_restore --no-owner --dbname="$PGREHEARSE"` it | `SELECT count(*) FROM vocab.concept` equals live |
| Index (Postgres) | The index is rebuildable, so the rehearsal is the rebuild path: `python -m core.db.reconcile` | Exit `0`, or `2` with only *missing* rows |
| Records (object store) | One synthetic chart through the real restore path: `docker compose exec app python -m core.fhir.restore --patient-id <synthetic id> --purpose-of-use "restore rehearsal" --role-arn "$(cd deploy/aws && terraform output -raw restore_role_arn)" --output ./restore-output/` | The tool's own ciphertext-digest check passes before it decrypts (`RUNBOOK_DATA_RESTORE.md` §3); delete `./restore-output/` after |
| `config/` | `aws s3 cp "s3://$STORE/system/backups/<stamp>/config/" ./config-rehearsal/ --recursive` | `sha256sum` of each file equals the live file, or differs only where the plan says it changed |

```bash
psql "$PGCONN" -c "DROP DATABASE phi_ai_rehearsal"
```

Record the date per store. On the platform the updater writes it into
the journal (`platform_backups`, kind `rehearsal`, in
`core/db/components_schema.sql`) and the screen reads it from there; where
the journal has not landed, record it where your organization records
changes - the same place `RUNBOOK_INDEX_MAINTENANCE.md` Step 4 sends you -
because a rehearsal that is not recorded is, to the screen, one that never
happened.

## I - The post-recovery verification sweep: what "Recovered" means

Run after every recovery above, in full, and again after any second
attempt. "Recovered" is shown only when all of it passes; a partial pass
is *unknown*, never green.

```bash
# 1. Compliance posture and configuration, the way the services read it. Exit 0.
docker compose exec app python -m core.healthcheck

# 2. The audit chain, end to end. Modified or removed entries are CRITICAL.
docker compose exec app python -m core.audit.verify

# 3. The routes answer. The web service is on the internal network only:
#    ask it from inside its own container, then walk the screens through
#    your authenticating proxy as an administrator.
docker compose exec web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read().decode())"   # prints: ok
#    Through the proxy: the Components screen, the Control panel, and every
#    screen the change touched - each answers 200 and reads its data, not an
#    error page. The updater's own sweep walks the same list.

# 4. The component's own check - the one the section you ran ends with:
#    the running stamp equals the manifest's (C); information_schema shows
#    or no longer shows the column (D); the vocabulary count (E); the config
#    checksum (F); the JWKS kid and an ingest cycle's record.write (G).
```

And when storage was touched at all - a migration on the index, a restore
of it - the six-flow verification as well, since it is the one that
checks the index against the store:

```bash
docker compose exec app python -m core.verify
```

Exit `0` is clean; `1` is a warning or a flow that could not be checked,
which is not a pass (`RUNBOOK_VERIFICATION.md`).

Any failure starts step 5 again - the same section, the same way back -
and the sweep runs again after it. Nothing is marked recovered by hand.

## Record

Every step transition is an audit event (`system.update_step`) with the
component, the step, the mode, the outcome and the actor; the journal
(`platform_updates`, `platform_update_steps` in
`core/db/components_schema.sql`) holds each job. Where a
recovery ran outside the engine - this runbook by hand, before the
journal existed on a deployment - record what you did, which backup you
restored and its checksum, and the sweep's results, in whatever your
organization already uses for changes. Master-user maintenance leaves no
trace inside the application; this record is the only one unless you
make it.
