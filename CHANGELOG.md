# Changelog

## 1.1.0 — 2026-09-08: the Components screen, the third System screen

- **Components** (`/system/components`, System group, `system:admin`). Every part of the platform that can go out of date, one row each —
  release and build stamps, the running image, dependency pins, runtimes, the vendored
  front-end, infrastructure pins, EMR vendor profiles, terminology releases, the model
  catalogue, the synthetic corpus, the migration ledger, key ages, operator configuration,
  the last green gates, backups and the releases kept — with what is running, what it was
  built from, the latest known, and the source of each fact. Five states — current, behind,
  drifted, unknown, updating — and a fact that was never checked is amber, never green.
  Three cards, one table per group, every total opening to its rows and every row to its
  evidence, its five-step procedure (plan, back up, apply, verify, recover) in its mode
  (direct, guided, record only), its recovery in one sentence, and its history. Reading is
  `system:admin`; applying is a second permission, `system:update`. Dark, in its own `.cx`
  scope. The screen states that it has no patient dimension.
- **One registry, declared once.** `core/components/registry.py` is the contract — key,
  group, kind, mode, backup unit, recovery, cadence, a reader and a procedure per component,
  the five steps, the five states, the cadences and retention counts — and the screen
  enumerates it; nothing about a component is typed into a template.
- **One release source.** `RELEASE` at the repository root names the release; `__version__`
  reads it, and the CHANGELOG's top heading, the tag, the build stamp and the colophon are
  held to it, so a page can no longer print a release the CHANGELOG has moved past.
- **The release unit is the image.** Every service built from the Dockerfile carries
  `image: ${PHI_AI_IMAGE:-phi-ai:dev}`: unset, compose builds and tags for development;
  pinned to a pulled digest, the updater's `up --no-build` runs it and the previous
  digest rolls all five back together. The release drop is `releases/`, not `release/`,
  which collides with the `RELEASE` file on a case-insensitive filesystem. The updater
  was rehearsed against real Docker: a local registry, three signed releases (a bad
  image whose healthcheck goes red and is rolled back, a good one that stays, a tampered
  BUILD.json that fails closed before anything is touched).
- **The migration ledger** (`schema_migrations`, `core/db/components_schema.sql`,
  `core/components/ledger.py`): every `core/db/*.sql` listed by glob with its checksum, and
  which of them ran, when and by whom — with a one-time backfill, after a `pg_dump` of the
  schemas it touches, for the files that ran by hand. The journal tables beside it
  (`platform_updates`, `platform_update_steps`, `platform_backups`, `platform_releases`,
  `platform_component_acks`) hold each job's five steps, the backups and rehearsals per
  store, the digests kept to roll back to, and the acknowledgements.

- **System recovery has a runbook.** `runbooks/RUNBOOK_SYSTEM_RECOVERY.md`: the
  operational-state dump under `system/backups/` and its restore, image rollback to the
  previous digest, the migration `down` and the dump restore, the vocabulary schema
  swap-back, config restore from `<name>.previous`, the JWKS re-publish, the restore
  rehearsal per store on its 180-day cadence, and the verification sweep — healthcheck,
  audit chain, routes, the component's own check — that alone earns "Recovered".
- **Thumbs on an answer.** Every assistant answer carries 👍 / 👎; a vote is recorded against
  the request it answers (`aiops.assistant_feedback`, granted in the three cloud bootstraps) and
  the assistant operations page counts votes per feature beside its other usage cards.

Tests: `tests/test_components.py`, `tests/test_components_web.py`, `tests/test_assistant.py`,
`tests/test_assistant_ops.py`.

## 1.1.0 — completed 2026-09-06: the Orchestration the 1.1.0 release named, in the platform

- **The EMR board** on the overview: one tile per vendor profile with the vendor's own posture —
  bulk or per-chart reads, what the certified surface accepts, whether a write connector is sold —
  and how this deployment has it wired. Derived from the profiles (`core/orchestration/board.py`).
- **One self-hosted script.** `static/app.js` gives the slow controls a busy state you can see from
  across the room, and lets lanes and scope selectors apply themselves. The CSP moves from
  `script-src 'none'` to `script-src 'self'`: no inline script, no eval, no third-party origin.

This is not a new release. v1.1.0 was titled *AI-Native Data Orchestration Designed for Compliance* and shipped the demonstration of it; this completes that release by putting the same Orchestration into the platform itself, so the product and its demonstration no longer diverge.

### Orchestration: wire an exchange, bound its scope, decide every delivery, run it

```text
 WIRE ──▶ SCOPE ──▶ PREFLIGHT ──▶ RUN ──▶ LEDGER + QA
 sources    chart /    every        decided   every step,
 & targets  population delivery     then      then audited
 from the   / segment  decided      performed by three
 profiles   + exclude  once, here   or named  independent
            heightened              as not    agents
```

- **The Orchestration screen** (`/orchestration`, Integration group,
  `integration:view`). Three steps, disclosed as they are earned: *Wire*
  never shuts; *Scope* opens when the exchange is saved and stays open
  through *Preflight*, so the reader decides whether to run with the whole
  scope on screen; a plain load opens *Wire* alone. Systems and their
  posture come from `core/fhir/emr_profiles.py` `PROFILES`, never typed.
  Named exchanges are kept whole (wiring, purpose, delivery, cadence,
  scope) and loaded back without running.
- **Scope**: one chart (a set of up to three), every patient, or a segment
  by condition, sex, age band, payer, state, medication, living/deceased
  and seen-since — and a run-wide switch, *leave heightened records out of
  this run*, that outranks a consent rather than restating it. A
  population scope under `patient_request` or `legal` collapses to the
  chart: neither purpose names a cohort.
- **One decision function.** `core/orchestration/decide.py`
  `decide_delivery()` is the only place a delivery is decided — target
  writability, purpose, and per heightened category: released only when
  the scope does not exclude it, the purpose permits it (`treatment`,
  `patient_request`) *and* the receiving system holds a disclosure consent
  for that chart in that category. The preflight, the run and the ledger
  consult it and nothing else, so they cannot disagree about a record.
- **Disclosure consents**, keyed receiving system × chart × category
  (`core/orchestration/consents.py`) — a release to one hospital earns
  nothing at another, and releasing a mental-health record does not release
  the HIV record beside it. Recorded and revoked on the preflight, each
  landing on the audit trail (`consent.disclosure_granted` / `_revoked`);
  every run decides again, so a revocation is honoured by the next one.
  Categories are the platform's own `SensitiveCategory`.
- **The way through.** "N heightened records stay behind" is true and, on
  a population scope, a dead end; *Work these charts per-chart* finds the
  charts in the store that actually carry a category and makes them the
  set. Gated like the roster: naming a chart is choosing a person
  (`patient:search`, `patient:read`).
- **Excluded means excluded.** With the switch on, no checklist item, no
  four-step panel, no per-chart offer; the summary reads "heightened,
  excluded", the tile "excluded by this scope", and the audit line names
  it. It is not silently dropped — the scope banner, the run and the trail
  all carry the choice.
- **The run** (`core/orchestration/run.py`, `integration:export`): one read
  step per source, one delivery per target per chart, each decided and then
  handed to a mover with only the records the decision allows. Tile for
  tile as the demonstration reports it — what the store holds for the
  scope, delivered, heightened left behind, released under consent — and
  every zero explains itself ("this run's scope excluded heightened
  records, so they stayed put"; "no disclosure consent covers them for
  these targets"). Reconciliation by resource type; a ledger with every
  step's reason; and **three QA agents** — Completeness, Integrity, Counts —
  that answer what the tiles cannot answer about themselves (a category
  released without a consent, a write to a read-only target, tiles that
  disagree with the ledger). Deciding and writing are separate on purpose:
  a deployment with no configured destination records every step as
  *decided, not written*, with the seam named — the platform's export
  manager takes the same line — and the writes remain the delivery
  service's (`core.fhir.delivery`) against a configured target URL with a
  verified identity map.
- **State survives a restart**: selection, scope, exchanges, consents and
  runs in `PlatformState` with SQL write-through; four tables in
  `core/db/platform_state_schema.sql`.
- **No scripts.** The platform serves `script-src 'none'`; the screen is
  server-rendered end to end — ticks apply on *Save this exchange*,
  selectors on *Confirm scope*.

Not yet in the platform, and visible as such: a drag canvas for the lanes
(checkbox lanes instead), per-source inventory and projection (needs
source and target calls), and the section's companion screens — patient
link set, practitioner crosswalk, permission lattice, holdings, connected-
systems catalogue.

Tests: `tests/test_orchestration_platform.py`, 23 through the FastAPI app;
1,830+ pass across the suite.

### The rest of the Integration group (2026-09-06, later the same day)

- **Patient link set** (`/orchestration/links`) — which chart in which system is this
  person. A link is an identifier on another system typed by one person and **verified by a
  second**; the person who entered it cannot vouch for it. Candidate → verified / rejected, and
  revoked. The verified links are the delivery writer's identity map (`core.fhir.delivery`),
  produced from the screen rather than a spreadsheet, with the verifier named.
- **The identity bound.** A per-chart delivery is now decided against the chart's verified link
  at the target — no verified identifier, no delivery — in the preflight, the lattice and the run
  alike. Identity refuses the *delivery*; the chart's heightened categories are still evaluated,
  so the checklist item and the consent matrix stay on screen and linking and consenting proceed
  in parallel.
- **Practitioner crosswalk** (`/orchestration/crosswalk`) — who each user is in each other
  system, by that system's Practitioner id; set and cleared by an administrator.
- **Permission lattice** (`/orchestration/lattice`) — the one decision, across every purpose
  this role may assert, for every system in the exchange: sources on whether a population read
  can be scheduled at all, targets through `decide_delivery()` with the chart, its categories,
  the consents and the link.
- **What each system holds** (`/orchestration/holdings`) — the PHI AI store by type and per
  chart, and each target as this deployment's runs actually wrote to it. A source's live count
  needs a connection; the screen says "not connected" rather than inventing one.
- **Connected systems** (`/orchestration/systems`) — every profiled vendor with its posture, and
  what this deployment has configured or wired it as.
- **42 CFR Part 2.** The category labels are derived from `SensitiveCategory` rather than listed
  by hand; a hand-written list of eight had left `part2_sud` out, so a Part 2 record labelled
  `sud_part2` (or `ETH`) classified as clean. Aliases normalise to the enum; a test pins coverage.

Tests: `tests/test_orchestration_platform.py` — 32, through the FastAPI app.

## 1.1.0-rc1 — 2026-09-02

### Nine more EMRs, one client, no new special cases

```text
 YOUR EMR ──▶ CLASSIFY ──▶ ENCRYPT ──▶ SYSTEM OF RECORD ──▶ GOVERNED AI
  FHIR R4     sensitive     per-object   your cloud, your     bounded by
  15 vendors  categories    data keys,   keys — storage       the asker's
              fail closed   your KMS     always wins          own role
```

- **Nine new EMR connectors** — ModMed, Altera Digital Health, Greenway
  Health, Veradigm, Practice Fusion, TruBridge, MEDHOST, Netsmart and
  Nextech join Epic, Oracle Health (Cerner), athenahealth,
  eClinicalWorks, MEDITECH and NextGen Healthcare: every entry in
  `core/fhir/emr_profiles.py` `PROFILES` is written from that vendor's
  own documentation — auth, keys, scopes, consent, bulk scope, writes,
  registration, limits — with a chapter in `docs/EMR_CONNECTORS.md`
  that separates what the vendor documents, what its own public
  endpoints returned, and what must be confirmed on the instance. Where
  a vendor documents nothing on a point the chapter says "not
  documented by the vendor" and the profile defaults conservatively;
  nothing is carried over from Epic.
- **Per-vendor assertion signing algorithm** —
  `EMRProfile.assertion_algorithm` (RS384 by default; ES384 where the
  vendor documents only that - the profile says which). The ingestion
  client signs with the profile's algorithm rather than a hard-coded
  RS384, so an EC P-384 key works where the vendor requires one;
  `Settings.from_env()` refuses a key of the wrong family at startup.
  The delivery CLI (`python -m core.fhir.delivery`) now builds its
  destination token request on the DESTINATION's profile too - its
  algorithm, its grant, its `kid`, and one `system/{Type}.write` scope
  per writable type where the profile requires explicit scopes - and
  refuses a `PHI_AI_DELIVERY_CLIENT_SECRET` set for a vendor whose
  profile takes none, instead of silently using it.
- **Emulators enforce the algorithm, and verify the assertion** —
  `EmulatorVendor.assertion_algorithms` lists what each emulator's token
  endpoint accepts; an assertion signed with anything else is refused as
  `invalid_client`, so a client that signs everything RS384 fails
  against the ModMed and Greenway emulators, not against a practice.
  Every assertion's audience, expiry, required claims and `iss == sub`
  are verified; its signature is verified when the client's public JWK
  Set is registered (`build_server(client_jwks=...)`, `python -m
  emulators --client-jwks PATH` - the integration tests and the e2e
  matrix both register one), and without one the emulator logs a
  WARNING that signatures are unverified. A client secret is checked
  against registered credentials the same way (`--client-secret
  ID:SECRET`). Wildcard-scope refusal is now its own per-vendor flag
  (`refuses_wildcard_scope`), true only for Oracle Health, which
  documents it. Malformed input (a non-integer Content-Length, a
  non-UTF-8 body, a non-string `id`, a negative `_offset`) is a 400
  with a body, never a dropped connection. Both-grant token endpoints
  (TruBridge, Netsmart, alongside Oracle Health), scope-required token
  requests and read-only write refusals are modelled the same way, each
  from the vendor's own documentation.
- **Ports 9107–9115** — one emulator per new vendor in
  `emulators/vendors.py` `DEFAULT_PORTS`; the earlier vendors keep the
  ports they had.
- **The end-to-end matrix** — `tests/test_e2e_matrix.py` and
  `scripts/e2e_matrix.py` drive every emulator as a source
  (authenticate with its real grant and algorithm, read its
  CapabilityStatement, ingest by paged search and by `$export` where
  supported, with the refusal asserted where not) and every vendor as a
  delivery target through `core/fhir/delivery/writer.py` (success where
  the CapabilityStatement advertises `create`, a structured refusal
  where it does not), across the full source-by-target matrix on
  synthetic, non-PHI data. Before each source's real grant the matrix
  sends that vendor's documented refusals (wrong algorithm, unsigned,
  unregistered key, missing scope, wrong grant) and asserts each 400,
  and the proof records what was refused per source. The proof table
  lands in `private-notes/e2e-proof.md` beside the checkout, never in
  the repository. The matrix delivers through `writer.py` with tokens
  it mints itself; the delivery CLI's own token request is covered by
  `tests/test_delivery.py`, parametrised over every profile.
- **Setup runbooks** — every vendor chapter ends with "Setting it up":
  register with the vendor, generate the key pair and JWKS (RSA or EC
  P-384 as the vendor documents), configure the environment, pre-flight
  the instance, first ingest, first delivery (and why it is refused on a
  read-only surface), local rehearsal against the emulator, and known
  limits with where to confirm them.
- **Enumerations derived, not maintained** — the README, ARCHITECTURE,
  the runbooks and the installer text now point at `PROFILES` and
  `DEFAULT_PORTS` instead of repeating a vendor count that had already
  drifted in places.

## 1.0.0-rc1 — 2026-08-29

```text
                         φ(ai)

    ____    __  __  ______      ______  ______
   /\  _`\ /\ \/\ \/\__  _\    /\  _  \/\__  _\
   \ \ \L\ \ \ \_\ \/_/\ \/    \ \ \L\ \/_/\ \/
    \ \ ,__/\ \  _  \ \ \ \     \ \  __ \ \ \ \
     \ \ \/  \ \ \ \ \ \_\ \__   \ \ \/\ \ \_\ \__
      \ \_\   \ \_\ \_\/\_____\   \ \_\ \_\/\_____\
       \/_/    \/_/\/_/\/_____/    \/_/\/_/\/_____/

                        1 . 0
   The AI-native platform for protected health data
```

**Health care runs on the most sensitive data there is — and the AI
revolution keeps happening somewhere else.** Locked demos, vendor
clouds, black boxes your compliance officer can't sign off on.

PHI AI 1.0 ends the standoff. It is a platform you deploy into your
own cloud, connect to your own EMR, and run under your own keys —
where a frontier model becomes a governed, first-class consumer of the
clinical record, and every safeguard the HIPAA Security Rule names is
enforced in code, not in a policy binder.

```text
┌────────────────┬────────────────┬────────────────┬────────────────┐
│  COMPLIANT BY  │   YOUR CLOUD   │    MINIMUM     │  HASH-CHAINED  │
│  CONSTRUCTION  │   YOUR KEYS    │   NECESSARY    │  AUDIT TRAIL   │
└────────────────┴────────────────┴────────────────┴────────────────┘
```

**Open Source. Free for All.** See it running on synthetic data right
now: <https://ryangomez.nyc/phi-ai/>

### One pipeline, from EMR to governed AI

```text
 YOUR EMR ──▶ CLASSIFY ──▶ ENCRYPT ──▶ SYSTEM OF RECORD ──▶ GOVERNED AI
  FHIR R4     sensitive     per-object   your cloud, your     bounded by
  6 vendors   categories    data keys,   keys — storage       the asker's
              fail closed   your KMS     always wins          own role
```

- **Six EMR connectors, one data-driven client** — Epic, Oracle Health
  (Cerner), athenahealth, eClinicalWorks, MEDITECH, NextGen — each
  speaking its vendor's real auth model, each testable end-to-end
  against a bundled emulator before you ever touch a live system.
- **An encrypted system of record** — one envelope-encrypted object
  per FHIR resource. Every index, every analytics layer, every answer
  traces back to those bytes; if a derived store ever disagrees, the
  object store wins.
- **Segmentation at the door** — 42 CFR Part 2, psychotherapy notes,
  state-law categories: classified on ingestion, withheld fail-closed,
  released only through their own consent lanes.

### An assistant that must show its work

Ask it about a patient and every claim comes back cited to stored
bytes. Nothing retrieved means nothing asserted — it abstains rather
than invents. Retrieval is bounded by *your* role grants, so the
assistant can never become a way to see what you couldn't open
yourself. And it is the only component in the platform with a network
path out of the deployment — a path that, on AWS and GCP, never leaves
your own account.

### AI that cannot touch the record on its own

```text
   model drafts ──▶ SIGNATURE QUEUE ──▶ human signs ──▶ the record

        Nothing an AI writes reaches the chart without a
              human signature. No exceptions.
```

The governance kernel treats every model as untrusted by default:
registry and execution gates, fairness screening, ambient consent
gating, patient-output release gates, a constrained action space.
Every gate refuses rather than degrades.

### And the rest of the platform

Population analytics that count patients, not rows. An optional OMOP
CDM layer for standard tooling. DICOM imaging with the upstream OHIF
viewer, pinned and unmodified. Release-of-information productions
where every withheld record is itemized, never silent. Complete
Terraform for AWS, GCP, and Azure. Twenty-five runbooks, an installer
chatbot, and a healthcheck that verifies compliance posture — not just
connectivity.

### What 1.0 is not

```text
   ╔═════════════════════[ THE HONESTY BOX ]═════════════════════╗
   ║  Software that manages PHI must not overstate itself.       ║
   ╚═════════════════════════════════════════════════════════════╝
```

- **No storage-level immutability.** Retention is recorded, not
  enforced; integrity is detective, not preventive.
- **No live EMR validation.** Everything is exercised against the
  emulators; Epic alone has also run against a live sandbox.
- **Not a compliance determination.** The software implements
  controls; operating on real PHI lawfully remains yours.
- **Not yet audited.** Run your own HIPAA security risk assessment —
  and get review from someone other than the code's own author —
  before any production workload.

### Try it in the next five minutes

```bash
git clone https://github.com/RyanGomez-NYC/phi-ai && cd phi-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q     # the whole platform proves itself, no cloud needed
```

The bundled emulators let you exercise every connector locally before
you provision a single cloud resource. The README's "Getting started,
in detail" takes it from there.

### Keep the code, keep the credit

```text
        ╔═══════════════════════════════════════════════════╗
        ║   APACHE-2.0  ·  KEEP THE CODE, KEEP THE CREDIT   ║
        ╠═══════════════════════════════════════════════════╣
        ║   PHI AI — Copyright 2026 Ryan Gomez & Co. Inc.   ║
        ║   Created by Ryan Gomez  ·  www.ryangomez.nyc     ║
        ╚═══════════════════════════════════════════════════╝
```

Apache 2.0, chosen for its explicit patent grant. Per Section 4(d),
the notices in `NOTICE` and the source-file headers travel with every
redistribution and derivative work.

---

Built by one person working with a frontier model. To err is human; to
completely blow stuff up is to AI. See something, say something —
report an issue. <https://www.ryangomez.nyc>
