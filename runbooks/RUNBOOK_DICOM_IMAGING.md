# Runbook: DICOM imaging and the embedded viewer

Ingesting medical images out of a retiring PACS, and viewing them through
an embedded [OHIF Viewer](https://github.com/OHIF/Viewers).

**Optional and off by default.** A deployment that ingests no imaging is
unaffected by everything here: the `/dicomweb` routes are not mounted at
all, no imaging tables are created, and the viewer container is not run.

> **Read this before enabling it.** Imaging changes two things this
> platform is otherwise careful about. The index that makes studies
> findable holds **identifying PHI** — patient names, birth dates,
> accession numbers — because a DICOM worklist is made of exactly those
> fields. And the viewer **requires JavaScript**, which the platform's own
> interface deliberately does not. Both are handled below, neither is
> hidden.

---

## What this gives you

| | |
|---|---|
| **Ingest** | `python -m core.dicom import <directory>` — walks a PACS export, encrypts every instance, stores and indexes it |
| **Storage** | One encrypted object per SOP instance, under `dicom/{study}/{series}/{sop}.dcm`, same bucket and KMS key as the rest of the object store |
| **Serving** | A DICOMweb API (QIDO-RS + WADO-RS) at `/dicomweb`, permission-gated and audited |
| **Viewing** | The official OHIF Viewer, unmodified, as a separate container |

---

## How the viewer is embedded, and how you update it

**There is no OHIF source in this repository.** No fork, no patches, no
vendored bundle. The viewer is the published `ohif/app` image at a pinned
tag, and the whole integration is one configuration file
(`config/ohif-app-config.js`) mounted over the image's own.

To update the viewer:

1. Pick a new tag from [Docker Hub](https://hub.docker.com/r/ohif/app/tags).
   Prefer a stable release; the `latest-beta` and `v3.x.0-beta.N` tags are
   pre-release.
2. Change the tag in `docker-compose.yml`'s `viewer` service.
3. Read OHIF's release notes for changes to the `dataSources` keys in
   `config/ohif-app-config.js`.
4. `docker compose up -d viewer`, then confirm the config actually took:

```bash
curl -s https://viewer.records.example.org/app-config.js | grep qidoRoot
```

That last step is not optional — see the read-only warning below.

### Three things verified against the image, not assumed

- **It listens on port 80**, although the image `EXPOSE`s 8080. Publishing
  8080 gets a connection refused that looks like a crashed container.
- **The config path is** `/usr/share/nginx/html/app-config.js`.
- **Do not add `read_only: true`.** With a read-only root filesystem the
  config bind mount does not appear in the container at all, nginx serves
  OHIF's built-in default, and **that default points the viewer at a
  public demo DICOMweb server instead of your own deployment**. The
  container returns HTTP 200 throughout, so nothing looks wrong. This is
  why step 4 above checks what is actually served.

---

## Why the viewer runs on its own origin

The platform's interface runs under `script-src 'none'` — it ships no
JavaScript at all, so that no cross-site scripting bug on a page
displaying PHI can execute anything (`core/web/security.py`). A medical
image viewer cannot run that way: OHIF needs scripts, web workers and WASM
codecs.

Serving it from a **separate origin** keeps that guarantee intact for
every page of the platform and confines the scripting to an origin that
holds no session and renders no record page. The cost is CORS, which the
platform handles with an exact-origin allowlist scoped to `/dicomweb` —
the viewer origin gets no cross-origin access to anything else.

**Put the viewer behind the same authenticating proxy as the web service.**
It is the surface a browser loads before any request to the platform is
made.

---

## Setup

### 1. Create the imaging index

```bash
psql "$CONN" -f core/db/imaging_schema.sql -f core/db/bootstrap_aws.sql
```

Use your own cloud's bootstrap script. Then set the role:

```
PHI_AI_IMAGING_DB_USERNAME=phi_ai_imaging
```

The two files do different halves of the job, which matters when
something is missing: `core/db/imaging_schema.sql` creates the
`dicom_studies` / `dicom_series` / `dicom_instances` tables, and the
bootstrap script creates the `phi_ai_imaging` role and grants it
SELECT/INSERT/UPDATE on those tables. The grants are conditional on the
tables existing, which is why the schema file runs first.

`phi_ai_imaging` is a literal identifier - set the variable to exactly
that. On GCP the username is the service account's email instead, as it
is for every other role there — see `deploy/gcp/database.tf`.

**A wrong value here does not fail the way you would expect.** The
`/dicomweb` routes only mount when this variable resolves, so an unset or
misspelled value shows up as a `404` on every imaging route rather than
as an authentication error against the database.

### 2. Point the platform at the viewer

```
PHI_AI_IMAGING_VIEWER_URL=https://viewer.records.example.org
PHI_AI_IMAGING_VIEWER_ORIGIN=https://viewer.records.example.org
```

### 3. Point the viewer at the platform

Edit the three roots in `config/ohif-app-config.js` to the platform's
externally reachable URL — **the one the browser can reach**, not a
container name. These are fetched by the user's browser, not by the viewer
container.

```js
wadoUriRoot: 'https://records.example.org/dicomweb',
qidoRoot:    'https://records.example.org/dicomweb',
wadoRoot:    'https://records.example.org/dicomweb',
```

Getting one side wrong shows up as a CORS error in the browser console and
an empty study list — not as a server error.

### 4. Import

```bash
python -m core.dicom count /mnt/pacs-export      # how much is there
python -m core.dicom import /mnt/pacs-export --dry-run
python -m core.dicom import /mnt/pacs-export
```

A bulk import is long-running and **resumable**: an instance already in
storage is skipped by key, so re-running the same command is the supported
way to finish an interrupted run, not a repair mode.

---

## Access control

`imaging:read` is granted to **viewer** and **HIM** only. Auditor,
disposition and admin hold no clinical read anywhere else and hold none
here — a DICOM header carries a name and a birth date, and the pixels can
too.

**A purpose of use is required before anything is served.** DICOMweb has
nowhere to carry one — PS3.18 defines no such parameter and OHIF would not
send it. So the platform establishes it: a user opens a study from the
patient record page, which already asked for a purpose, and that choice is
stored in the signed session. Every DICOMweb request inherits it. A
request arriving with no purpose is **refused, never defaulted** — a
default would be a disclosure recorded under a reason nobody chose.

This is also what stops someone navigating to the viewer directly and
reading imaging under no stated reason.

### Auditing is per study

Opening one CT is thousands of HTTP requests. `core/audit/sink.py` writes
one object per event, so auditing each would turn a single study view into
thousands of audit objects, slow chain verification measurably, and bury
the entries that matter.

What is recorded is what a human did: **this user opened this study, at
this time, for this stated purpose** — as `record.read.imaging.study`
against `dicom/{study}/`. The frame fetches that follow are the mechanics
of that one disclosure, and the study entry accounts for it under
45 CFR §164.528.

That single entry per study is therefore what an accounting of
disclosures has to be built from. Over an exported log:

```bash
grep -E '"action": *"record\.read\.imaging\.study"' exported-audit.jsonl
```

The frame-level requests are deliberately absent from the log, so do not
read their absence as an incomplete accounting — see
`RUNBOOK_INCIDENT_RESPONSE.md` Step 2 for the same audit trail used for
breach scoping rather than for a §164.528 request.

---

## What this does not do

- **No de-identification.** Headers are stored verbatim, which is
  correct for a system of record and means every identifier in them is
  retained.
- **Burned-in annotation is not addressed.** Pixel data can itself carry a
  name, an accession number or a date, rendered in by the acquiring
  modality — ultrasound and secondary capture especially. No header
  inspection finds it, DICOM's own `BurnedInAnnotation` attribute is
  optional and frequently absent or wrong, and this project does not
  attempt to strip it. Anything relying on de-identification needs a
  dedicated tool and a human review pass.
- **No transcoding.** Studies are served in whatever transfer syntax they
  were ingested in; the viewer decodes in the browser. Transcoding
  server-side would need codec libraries this project does not ship and
  would silently alter data the object store exists to preserve.
- **No STOW-RS.** The DICOMweb API is read-only. Imaging enters through
  `python -m core.dicom import`, never over HTTP.
- **No DICOM networking.** No C-STORE, C-MOVE or C-FIND. Everything
  downstream of ingest is source-agnostic, so adding them later changes
  `core/dicom/ingest.py` and nothing else.

---

## Performance, honestly

**Metadata is decrypted on demand.** `/studies/{uid}/metadata` reads every
instance in the study — for a 2,000-slice CT that is 2,000 object reads
and 2,000 AES-GCM decryptions before the viewer draws anything.

The alternative — a metadata sidecar object per instance, written at
ingest — would **double the object store's object count**, and
`docs/SCALING.md` is explicit that object count is the number this system
scales on. So the cost is paid at read time by the person who opened the
study, rather than permanently by every deployment.

Two consequences:

- `enableStudyLazyLoad: true` is set in the viewer config. Leave it on.
- `PHI_AI_IMAGING_MAX_STUDY_METADATA` (default 500) caps how many
  instances one study-level request will decrypt. Past it the viewer is
  told to fetch series by series, which is the cheaper path anyway.

**Object count is the sizing number, and instances are the unit.**
`docs/SCALING.md`'s table counts DICOM at 100 MB per *study*. Counted per
*instance* — which is how this platform stores them, so that a viewer can
fetch one slice without decrypting a whole CT — the same holdings are
roughly a thousand times more objects. Estimate instances before choosing
a storage profile.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Empty study list, CORS error in the browser console | `PHI_AI_IMAGING_VIEWER_ORIGIN` does not exactly match the origin the browser sees, or the roots in `ohif-app-config.js` are wrong |
| `403` with "No purpose of use is recorded" | The viewer was opened directly. Open a study from the patient record page instead |
| `404` on every `/dicomweb` route | `PHI_AI_IMAGING_DB_USERNAME` is unset, or set to a role the database does not have; see Setup step 1 |
| `413` opening a large study | Above `PHI_AI_IMAGING_MAX_STUDY_METADATA` — the viewer should fetch series metadata |
| Viewer loads but shows OHIF's demo studies | The config mount is not taking effect. Check `read_only` is not set, then `curl <viewer>/app-config.js` |
| Connection refused to the viewer | Published port 8080 instead of 80 |
| Imported files reported as already stored and skipped | Expected on a re-run. Use `--reimport` to overwrite |
