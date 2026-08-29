```text
══════════════════════════════════════════════════════════════════════════

       ▄███████▄    ▄█    █▄     ▄█          ▄████████  ▄█
      ███    ███   ███    ███   ███         ███    ███ ███
      ███    ███   ███    ███   ███▌        ███    ███ ███▌
      ███    ███  ▄███▄▄▄▄███▄▄ ███▌        ███    ███ ███▌
    ▀█████████▀  ▀▀███▀▀▀▀███▀  ███▌      ▀███████████ ███▌
      ███          ███    ███   ███         ███    ███ ███
      ███          ███    ███   ███         ███    ███ ███
     ▄████▀        ███    █▀    █▀          ███    █▀  █▀

                      █
                ▄▄▄███████▄▄▄
            ▄██▀▀     █     ▀▀██▄           THE AI-NATIVE PLATFORM
          ██          █          ██         FOR PROTECTED HEALTH DATA
          ██          █          ██
          ██          █          ██         True AI, securely and compliantly,
            ▀██▄▄     █     ▄▄██▀           running in your own cloud.
                ▀▀▀███████▀▀▀
                      █                     Open Source, Free for All.
                  φ ( a i )

            ╱╲                                ╱╲
───────────╱  ╲─────╲  ╱────────────────────╱  ╲─────╲  ╱─────────────────
                     ╲╱                            ╲╱
══════════════════════════════════════════════════════════════════════════
```

# PHI AI

An open-source, bring-your-own-infrastructure AI platform for Protected
Health Information (PHI). It connects to your EMR — Epic, Oracle Health
(Cerner), athenahealth, eClinicalWorks, MEDITECH, or NextGen Healthcare —
and makes that clinical data usable on infrastructure you already own and
control: a grounded AI assistant, population analytics and cohort
counting, imaging with an embedded viewer, records delivery back to a
live EMR, and a queryable OMOP CDM layer. Everything runs under an
encrypted, tamper-evident system of record, with the HIPAA Security
Rule's technical safeguards — access control, audit, integrity
verification, encryption — enforced structurally rather than by policy
document.

**Live demonstration** (synthetic data, no real patients):
<https://ryangomez.nyc/phi-ai/>

> **Compliance responsibility:** HIPAA compliance belongs to the
> organization that owns or manages the PHI, not to this software. See
> [docs/RESPONSIBILITY.md](docs/RESPONSIBILITY.md) — and the
> [Responsibility boundary](#responsibility-boundary) below, which
> nothing else in this repository overrides.

## Mission

Give healthcare organizations a trustworthy, vendor-neutral,
regulation-compliant way to put AI and analytics to work on their own
clinical data, on infrastructure they already control, and make it easy
enough to install that it drives real adoption.

The compliance substrate is not a feature alongside the AI — it is the
reason the AI is deployable at all. An AI product over PHI without
envelope encryption, a tamper-evident audit log, structural
minimum-necessary access control and a verifiable system of record is one
whose risk assessment has no good answers. Note *verifiable*, not
*immutable*: this platform applies no storage-level WORM on any cloud,
and the difference matters enough that `docs/COMPLIANCE.md` →
"Retention and integrity" is worth reading before the rest of this file.

```text
┌────────────────┬────────────────┬────────────────┬────────────────┐
│  COMPLIANT BY  │   YOUR CLOUD   │    MINIMUM     │  HASH-CHAINED  │
│  CONSTRUCTION  │   YOUR KEYS    │   NECESSARY    │  AUDIT TRAIL   │
└────────────────┴────────────────┴────────────────┴────────────────┘
```

## Features

Every feature is AI-native: each one is either a governed surface a
frontier model operates through, or the substrate that makes doing so
defensible.

```text
  ╔════════════════════ T H E   S U B S T R A T E ═════════════════════╗
  ║                                                                    ║
  ║  Encrypted system of record  One encrypted object per FHIR         ║
  ║                              resource; storage always wins         ║
  ║                                                                    ║
  ║  Envelope encryption         KMS-backed, per-object data keys      ║
  ║                                                                    ║
  ║  Tamper-evident audit        Hash-chained log; every read of       ║
  ║                              PHI recorded before decryption        ║
  ║                                                                    ║
  ║  Role-gated access           Minimum necessary enforced in         ║
  ║                              code paths, not policy documents      ║
  ║                                                                    ║
  ║  Sensitivity segmentation    42 CFR Part 2, psychotherapy notes,   ║
  ║                              state-law categories — withheld       ║
  ║                              fail-closed                           ║
  ║                                                                    ║
  ╚════════════════════════════════════════════════════════════════════╝
```

- **Grounded AI assistant** (`core/assistant/`, `core/rag/`) — a
  retrieval kernel with an attribution hard gate: every claim cited to
  stored bytes, abstention on empty retrieval, status and negation
  preserved through chunking, temporal weighting, grant-bounded
  retrieval so the assistant can never see what the asking user's role
  does not permit. Off by default; three PHI-access tiers, each needing
  its own BAA acknowledgement. The only component with a network path
  out of the deployment.
- **Model governance kernel** (`core/governance/`) — a model registry
  and execution gate enforcing SPEC Invariants 13–19: fairness
  screening, sensitive-category segmentation, ambient consent gating,
  staged-draft write-back (AI output lands in a signature queue, never
  directly in the record), patient-output release gates, a constrained
  action space, HTI-1 source attributes. Every module refuses rather
  than degrades.
- **Six EMR connectors, one data-driven client** (`core/fhir/`) — Epic,
  Oracle Health (Cerner), athenahealth, eClinicalWorks, MEDITECH,
  NextGen Healthcare. Each vendor's real auth model (RS384 JWT client
  assertions for the SMART Backend Services vendors, client secret for
  athenahealth, explicit system scopes for Oracle Health); every quirk
  in a capability profile table, not in the client. Per-type search
  hourly and FHIR Bulk Data Export daily, feeding one pipeline.
- **Per-vendor EMR emulators** (`emulators/`, ports 9101–9106) — every
  connector exercised end-to-end against an emulator reproducing that
  vendor's real seams (auth accepted, `$export` present or absent, what
  is creatable), so integration is testable without a live EMR.
- **Records delivery & release of information** (`core/fhir/delivery/`,
  ROI queue in the web UI) — stored records delivered back to a live
  EMR, identity-mapped; ROI productions assembled for human review with
  withheld records itemized, never silent, and Part 2 redisclosure
  refused without category-specific consent.
- **Population analytics & OMOP CDM** (`core/analytics/`, `core/db/`) —
  cohort counting that counts patients rather than rows, facility
  breakdowns, name search in its own separately-enabled store, a
  guarded read-only SQL tool with every generated query audited
  verbatim, and an optional OHDSI OMOP CDM layer for standard
  analytics tooling. Identified PHI, opt-in, own database roles.
- **DICOM imaging with an embedded viewer** (`core/dicom/`) — PACS
  export ingestion, one encrypted object per SOP instance, a read-only
  DICOMweb API (QIDO-RS/WADO-RS) serving an unmodified, pinned upstream
  OHIF viewer on a separate origin so `script-src 'none'` stays intact
  on every PHI page.
- **OCR ingestion** (`core/ocr/`) — Tesseract over scanned documents.
  Printed text only; Tesseract is not built for handwriting.
- **Capability cores** (`core/capabilities/`) — spine summarization,
  patient-instruction no-new-assertions checking, cited prior-auth
  packets, recall-biased triage, human-confirmed measure abstraction,
  trial pre-screening worklists, ingest data-quality QA.
- **Multi-cloud Terraform** (`deploy/aws|gcp|azure/`) — complete stacks
  for all three clouds: versioned storage, KMS-backed envelope
  encryption, role-separated IAM/RBAC, optional Postgres index + OMOP.
- **Operable by hospital IT** — install scripts, fourteen runbooks, a
  guided installer chatbot, a healthcheck that verifies compliance
  posture rather than mere connectivity, and a verification suite with
  one answer and one exit code.

## Architecture

Storage is the unconditional system of record. Every other data
structure — the Postgres index, the OMOP layer, the imaging index, the
name-search store — is explicitly derived and rebuildable; if any of
them ever disagree with the encrypted object store, the object store
wins. That is what makes every AI answer traceable to bytes rather than
to a cache.

```text
 ┌───────────────────────────────────────────────────────────────────────┐
 │                        YOUR EMR  (FHIR R4)                            │
 │   Epic · Oracle Health · athenahealth · eCW · MEDITECH · NextGen      │
 └──────────────────────────────────┬────────────────────────────────────┘
        per-type search (hourly)    │    Bulk Data $export (daily)
                                    ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  INTEGRATION LAYER          core/fhir/                                │
 │  vendor capability profiles · real auth models · emulator-tested      │
 └──────────────────────────────────┬────────────────────────────────────┘
                                    ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  INGESTION & CLASSIFICATION                                           │
 │  validate → classify sensitivity (Part 2 / psych / state-law) →       │
 │  envelope-encrypt (KMS data key per object) → write → audit           │
 └──────────────────────────────────┬────────────────────────────────────┘
                                    ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  ███ ENCRYPTED SYSTEM OF RECORD ███         core/storage/ + crypto/   │
 │  S3 / GCS / Azure Blob — one encrypted object per resource            │
 │  psychotherapy notes: separate store, separate consent lane           │
 │        o═o═o═o  hash-chained audit trail  o═o═o═o                     │
 └───────┬───────────────┬───────────────┬───────────────┬───────────────┘
         ▼               ▼               ▼               ▼
  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
  │  Postgres  │  │  OMOP CDM  │  │  imaging   │  │    name    │
  │   index    │  │   layer    │  │   index    │  │   search   │
  │  derived   │  │   opt-in   │  │   opt-in   │  │   opt-in   │
  └──────┬─────┘  └──────┬─────┘  └──────┬─────┘  └──────┬─────┘
         ▼               ▼               ▼               ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  GOVERNED SURFACES                          all role-gated, audited   │
 │  web UI & JSON API · grounded assistant (RAG) · analytics & cohorts   │
 │  DICOM viewer (OHIF) · records delivery → EMR · ROI productions       │
 └───────────────────────────────────────────────────────────────────────┘
        ▲
        │  the ONLY outbound network path in the platform:
        │  the assistant's model call — Bedrock / Vertex in YOUR account
        │  under YOUR cloud BAA (Azure: Anthropic API), off by default
```

Deeper treatment with the full module map: `docs/ARCHITECTURE.md`, and
the repository layout below.

## Data flows

**1 · Ingestion — EMR to system of record**

```text
  EMR ──FHIR R4──▶ scheduler ──▶ validate ──▶ classify sensitivity
                                                    │
                              ┌─────────────────────┤
                              ▼                     ▼
                       ordinary record       Part 2 / psych / state-law
                              │                     │ (flagged, withheld
                              ▼                     ▼  fail-closed)
                     KMS data key ──▶ encrypt ──▶ object store
                              │
                              ▼
                     audit append (hash-chained) ──▶ derived indexes
```

**2 · A grounded question — the RAG loop**

```text
  clinician asks ──▶ audit the question ──▶ retrieval, bounded by the
        │               (before anything      asker's own role grants
        │                is sent anywhere)          │
        │                                           ▼
        │                              chunks w/ status & negation kept,
        │                              sensitive categories withheld
        │                                           │
        ▼                                           ▼
  answer ◀── attribution hard gate ◀── model (in YOUR cloud account)
             every claim cited to stored bytes;
             empty retrieval => abstention, never invention
```

**3 · AI output back to the record — staged drafts only**

```text
  model draft ──▶ signature queue ──▶ human reviews, edits, signs ──▶
  record write (audited) ──▶ optional delivery to the live EMR
                 ▲
                 └── nothing an AI writes ever lands in the record
                     without a human signature. No exceptions.
```

**4 · Release of information — disclosure with a conscience**

```text
  request ──▶ jurisdiction requirements validated (can block,
        │      never auto-approve) ──▶ production assembled
        ▼
  included records itemized ── withheld records itemized, with the rule
        │                      (psych notes: never via ROI;
        ▼                       Part 2: category-specific consent only)
  human decision ──▶ fulfil / deny ──▶ audited either way
```

## Getting started, in detail

### 0. Prerequisites

- Python 3.11+, `git`, and for cloud deployment Terraform ≥ 1.5 and an
  AWS / GCP / Azure account you control.
- No GitHub Actions, no hosted CI, no telemetry: everything runs on
  your machines. Local pre-push gates: `scripts/pre_push_gates.sh`
  (install once per clone: `ln -s ../../scripts/pre_push_gates.sh
  .git/hooks/pre-push`).

### 1. Clone, install, prove it works — no cloud needed

```bash
git clone https://github.com/RyanGomez-NYC/phi-ai && cd phi-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q          # the full suite runs without any cloud
```

Every EMR connector can be exercised locally against the bundled
emulators (`emulators/`, ports 9101–9106) — see
`runbooks/RUNBOOK_EMULATORS.md`. The installer chatbot and the
assistant's documentation tier also work before any infrastructure
exists:

```bash
python3 install/installer_chatbot.py   # guided setup Q&A
python -m core.assistant               # answers from the runbooks
```

### 2. Provision a cloud (AWS shown; GCP/Azure runbooks are parallel)

```bash
cd deploy/aws/bootstrap && terraform init && terraform apply   # state backend
cd ..
cp backend.hcl.example backend.hcl          # edit — keeps your account ID out of git
cp terraform.tfvars.example terraform.tfvars # edit
terraform init -backend-config=backend.hcl && terraform apply
terraform output -raw env_fragment > ../../.env
```

Read `runbooks/RUNBOOK_AWS_SETUP.md` **before** running this —
particularly the retention warning at the top: this stack applies **no
storage-level immutability**, so retention is recorded rather than
enforced (see `docs/COMPLIANCE.md` → "Retention and integrity"), and
Steps 6a/6b cover the optional Postgres index and Bulk Data Export.
For GCP or Azure start at `RUNBOOK_GCP_SETUP.md` /
`RUNBOOK_AZURE_SETUP.md` — same shape, genuinely different mechanics in
places, each runbook's "Known gaps" section says where.

### 3. Connect your EMR

```bash
./scripts/generate_epic_keypair.sh     # one-time, SMART Backend Services vendors
python3 install/installer_chatbot.py   # walks you through registration details
```

Set `PHI_AI_EMR_VENDOR` to one of `epic`, `oracle_health`,
`athenahealth`, `eclinicalworks`, `meditech`, `nextgen`. Per-vendor
registration, auth, scopes, bulk-export behavior and write surfaces:
`docs/EMR_CONNECTORS.md`. No live EMR yet? Point it at the matching
emulator and everything downstream behaves identically.

### 4. Verify posture, then ingest

```bash
python -m core.healthcheck          # compliance posture, not just connectivity
python scripts/smoke_test_aws.py    # end-to-end, synthetic data only
python -m core.verify               # cross-flow verification: one exit code
```

The schedulers (`core/fhir/scheduler.py` hourly per-type search,
`core/fhir/bulk_scheduler.py` daily Bulk Data Export) feed the same
pipeline; the bulk path is the only way to ingest an entire population
and refuses vendors whose profile records no `$export` support.

### 5. Turn on what you need — everything optional is off by default

| Surface | Where to read first | Why it is opt-in |
|---|---|---|
| Web UI & API | `core/web/` (auth via reverse proxy) | your front door |
| AI assistant | `runbooks/RUNBOOK_AI_ASSISTANT.md` | only outbound network path; PHI tiers need BAA acknowledgements |
| Postgres index | `core/db/schema.sql` header | derived, rebuildable |
| OMOP CDM layer | `runbooks/RUNBOOK_OMOP_SETUP.md` | identified PHI, broader surface |
| Analytics & name search | `runbooks/RUNBOOK_ANALYTICS.md` | identified PHI; names live nowhere else outside encrypted objects |
| DICOM imaging | `runbooks/RUNBOOK_DICOM_IMAGING.md` | imaging index holds identifying PHI |

Set the deployment shape before ingesting: `PHI_AI_PROFILE` decides the
storage layout and cannot be changed on a populated object store
(`docs/SCALING.md`); `PHI_AI_CANONICAL_BASE` is minted into stored FHIR
extensions and must be namespaced to **your** organization from day one.

## Read this before believing any of the above

```text
╔══════════════════════[ THE HONESTY BOX ]═══════════════════════════╗
║                                                                    ║
║     Software that manages PHI must not overstate itself. So:       ║
║                                                                    ║
╚════════════════════════════════════════════════════════════════════╝
```

- **No storage-level immutability.** No WORM, no Object Lock, no Bucket
  Lock on any cloud. Retention is recorded, not enforced; integrity is
  detective, not preventive. `docs/COMPLIANCE.md` sets out what remains
  per cloud once that is true.
- **No live EMR validation.** Every integration is exercised against
  the emulators, not a real Epic, Cerner, athenahealth, eCW, MEDITECH
  or NextGen instance (Epic is the only vendor also run against a live
  sandbox). Registration is per customer and still required.
- **Not a compliance determination.** The software implements controls;
  it does not certify anyone against HIPAA. Neither this code nor its
  documentation is legal advice.
- **OCR is printed text only.** Tesseract is not built for handwriting.
- **Not a finished, audited product.** Core abstractions are
  unit-tested; before any production or PHI workload it needs a formal
  HIPAA security risk assessment and, ideally, third-party security
  review by someone other than the code's own author. The commit
  history and `docs/COMPLIANCE.md` record — deliberately, in public —
  the genuine bugs internal audit passes have found and fixed,
  including ones affecting the integrity-verification control and
  three entry points that could not start; "unit-tested" is easy to
  read as a stronger claim than it is.

## Responsibility boundary

This is open-source software under Apache 2.0, provided **AS IS, with
no warranty of any kind** — including no warranty of regulatory
compliance. The project builds structural controls, gates, and evidence
artifacts, and takes them as far as engineering can; **operating on
real PHI lawfully is the implementing organization's responsibility**,
and cannot be delegated to a codebase. Concretely, the implementing
organization owns: its BAAs with cloud and model vendors; its own HIPAA
Security Risk Assessment; counsel review of every jurisdiction-
dependent table this software applies (recording consent, sensitive-
category segmentation, retention); its terminology licences (SNOMED,
CPT, UMLS — this project cannot confer them); every operator
attestation the preflight and governance gates record; and validation
against its own real data before clinical use. `docs/COMPLIANCE.md` →
"Responsibility boundary" states this in full; nothing anywhere else in
this repository overrides it.

## Repository layout

```
phi-ai/
├── core/               Python package: the platform engine
│   ├── config/         Environment & deployment configuration loading,
│   │                    plus the retention ruleset engine (applies
│   │                    rules, does not determine them - see
│   │                    retention_rules.py and docs/COMPLIANCE.md)
│   ├── storage/        Cloud storage abstraction (AWS/GCP/Azure) - the
│   │                    system of record for everything else here
│   ├── crypto/         Envelope encryption (KMS-backed)
│   ├── audit/          Hash-chained, tamper-evident audit logging
│   ├── governance/     Enforcement kernel for docs/SPEC.md Invariants
│   │                    13-19: model registry & execution gate, 92.210
│   │                    fairness screen, sensitive-category segmentation,
│   │                    ambient consent gate, staged-draft write-back,
│   │                    patient-output release gate, constrained action
│   │                    space, HTI-1 source attributes. Every module is
│   │                    a gate that refuses rather than degrades; see
│   │                    runbooks/RUNBOOK_MODEL_GOVERNANCE.md
│   ├── rag/            Grounded-assistant retrieval kernel (SPEC §5.1):
│   │                    versioned chunk serialization with status and
│   │                    negation preserved, attribution hard gate,
│   │                    temporal weighting, deterministic structured
│   │                    spine, hybrid grant-bounded retrieval, answer
│   │                    contract (every claim cited, abstention on
│   │                    empty retrieval), §10 metrics (eval.py)
│   ├── capabilities/   Capability cores over the kernel (SPEC §5)
│   ├── terminology/    Licence-classed vocabulary loader (SPEC §7.4)
│   ├── ocr/            Tesseract OCR for scanned-document ingestion
│   ├── fhir/delivery/  Stored record → live EMR delivery (identity-mapped)
│   ├── verify/         Cross-flow verification (one answer, one exit code)
│   ├── web/            Web UI + JSON API (auth via reverse proxy)
│   ├── dicom/          OPTIONAL DICOM imaging, off by default
│   ├── analytics/      OPTIONAL population queries, off by default
│   ├── assistant/      OPTIONAL AI assistant, off by default - the only
│   │                    component that talks outside the deployment
│   ├── db/             Optional Postgres queryable index + optional
│   │                    OMOP CDM analytics layer
│   ├── fhir/           EMR FHIR R4 clients + schedulers + per-vendor
│   │                    capability profiles (six vendors)
│   └── healthcheck.py  Verifies compliance posture, not just connectivity
├── config/             Deployer-owned templates (retention ruleset, ...)
├── deploy/             Terraform per cloud: aws/ gcp/ azure/
├── docs/               ARCHITECTURE · COMPLIANCE · EMR_CONNECTORS ·
│                        SPEC · SCALING · COST · TESTDATA · more
├── emulators/          One emulator per EMR vendor (ports 9101-9106)
├── install/            install.sh + guided installer chatbot
├── runbooks/           Fourteen operational runbooks (setup per cloud,
│                        incident response, HIM verification, ...)
├── scripts/            Keypair generation, mock Epic server, corpus
│                        generation, eval metrics, smoke tests
├── tests/              The full suite - runs with no cloud at all
├── docker-compose.yml
├── LICENSE             Apache License 2.0
└── NOTICE              Attribution — travels with every redistribution
```

## License & attribution

```text
        ╔═══════════════════════════════════════════════════╗
        ║   APACHE-2.0  ·  KEEP THE CODE, KEEP THE CREDIT   ║
        ╠═══════════════════════════════════════════════════╣
        ║   PHI AI — Copyright 2026 Ryan Gomez & Co. Inc.   ║
        ║   Created by Ryan Gomez  ·  www.ryangomez.nyc     ║
        ╚═══════════════════════════════════════════════════╝
```

Apache 2.0 (see `LICENSE`). Chosen for its explicit patent grant, which
matters for a project institutions will run against regulated data.

Attribution is a condition of use: per Section 4(d) of the License, the
attribution notices in `NOTICE` — and the copyright headers carried in the
source files — must be retained in any redistribution or derivative work.
Keep the code, keep the credit.

Feedback, questions and criticisms: <https://www.ryangomez.nyc>.
