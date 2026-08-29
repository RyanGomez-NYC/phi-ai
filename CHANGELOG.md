# Changelog

## 1.0.0 — 2026-08-29

```text
                         φ(ai)

          PHI AI 1.0 — the first public release
   The AI-native platform for protected health data
```

PHI AI 1.0 is the first public release: an open-source,
bring-your-own-infrastructure AI platform for Protected Health
Information. You deploy it into your own cloud account, connect it to
your own EMR, and put a governed frontier model to work on your own
clinical data — under an encrypted, tamper-evident system of record,
with the HIPAA Security Rule's technical safeguards enforced
structurally rather than by policy document.

A live demonstration on fully synthetic data runs at
<https://ryangomez.nyc/phi-ai/>.

### What ships in 1.0

**The substrate**

- Encrypted system of record on S3, GCS, or Azure Blob — one
  envelope-encrypted object per FHIR resource, KMS-backed data keys,
  and the rule that gives every other structure its meaning: if a
  derived store ever disagrees with the object store, the object store
  wins.
- Hash-chained, tamper-evident audit logging; every read of PHI is
  recorded as a disclosure before the object is decrypted.
- Role-gated, minimum-necessary access enforced in code paths.
- Sensitive-category segmentation at the ingestion door — 42 CFR
  Part 2, psychotherapy notes (their own store and consent lane), and
  state-law categories — withheld fail-closed.

**The AI surface**

- A grounded assistant over a retrieval kernel with an attribution
  hard gate: every claim cited to stored bytes, abstention on empty
  retrieval, status and negation preserved through chunking,
  retrieval bounded by the asking user's own role grants. Off by
  default; three PHI-access tiers, each requiring its own BAA
  acknowledgement; the only component in the platform with a network
  path out of the deployment — and on AWS and GCP that path stays
  inside your own account via Bedrock or Vertex AI.
- A model-governance kernel: registry and execution gate, fairness
  screening, ambient consent gating, staged-draft write-back (AI
  output lands in a signature queue, never directly in the record),
  patient-output release gates, a constrained action space, and HTI-1
  source attributes. Every gate refuses rather than degrades.
- Capability cores: spine summarization, patient-instruction
  no-new-assertions checking, cited prior-auth packets, recall-biased
  triage, human-confirmed measure abstraction, trial pre-screening,
  ingest data-quality QA.

**Integration and delivery**

- Six EMR connectors through one data-driven FHIR R4 client — Epic,
  Oracle Health (Cerner), athenahealth, eClinicalWorks, MEDITECH,
  NextGen Healthcare — each using its vendor's real auth model, every
  quirk in a capability-profile table rather than in the client.
- Two ingestion paths feeding one pipeline: hourly per-type search and
  daily FHIR Bulk Data Export (the only way to ingest an entire
  population; vendors without `$export` are refused at startup, not
  silently downgraded).
- Per-vendor emulators (ports 9101–9106) reproducing each vendor's
  real seams, so every connector is testable end-to-end without a
  live EMR.
- Records delivery back to a live EMR, and release-of-information
  productions assembled for human review — withheld records itemized,
  never silent, Part 2 redisclosure refused without category-specific
  consent.

**Analytics and imaging (opt-in, off by default)**

- Population analytics that count patients rather than rows, name
  search in its own separately-enabled store, a guarded read-only SQL
  tool with every generated query audited verbatim, and an optional
  OHDSI OMOP CDM layer.
- DICOM imaging: PACS-export ingestion, one encrypted object per SOP
  instance, a read-only DICOMweb API serving an unmodified, pinned
  upstream OHIF viewer on a separate origin.

**Operations**

- Complete Terraform stacks for AWS, GCP, and Azure.
- Twenty-four operational runbooks, a non-interactive installer, a
  guided installer chatbot, a healthcheck that verifies compliance
  posture rather than mere connectivity, and a cross-flow verification
  suite with one answer and one exit code.

### The numbers behind this release

- 818 passing tests, runnable with no cloud at all.
- All four Terraform stacks validate.
- §10 evaluation metrics over the synthetic corpus, at their targets:
  zero silent omissions, zero status inversions, zero attribution
  false negatives, retrieval recall 1.0, zero abstention failures.
  These are lower bounds on error — synthetic only — and do not
  substitute for validation against your own data.

### What 1.0 is not

Software that manages PHI must not overstate itself, so the limits
ship in the same release notes as the features:

- **No storage-level immutability.** Retention is recorded, not
  enforced; integrity is detective, not preventive.
- **No live EMR validation.** Every integration is exercised against
  the emulators; Epic is the only vendor also run against a live
  sandbox. Registration with each vendor is still required.
- **Not a compliance determination.** The software implements
  controls; it does not certify anyone against HIPAA, and operating on
  real PHI lawfully remains the implementing organization's
  responsibility.
- **OCR is printed text only.**
- **Not yet audited.** Before any production or PHI workload this
  needs a formal HIPAA security risk assessment and, ideally,
  third-party security review by someone other than the code's own
  author.

### Known gaps

- Three ingested resource types are not yet mapped into OMOP
  (`DocumentReference`, `AllergyIntolerance`, `ExplanationOfBenefit`),
  and the OHDSI standardized vocabulary is a separately licensed
  download this project cannot bundle.
- The clouds are not identical: GCP's Cloud SQL IAM model constrains
  database role separation differently, Azure's only deletion
  protection is a 7-day soft-delete window, and the
  independent-audit-trail cross-check exists on AWS only. Each setup
  runbook's "Known gaps" section is the authority.
- The Azure Terraform stack validates with provider deprecation
  warnings that will need attention before the provider's v5.

### Getting started

```bash
git clone https://github.com/RyanGomez-NYC/phi-ai && cd phi-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q     # proves the platform on your machine, no cloud needed
```

Then follow the README's five-step "Getting started, in detail" — the
emulators let you exercise everything locally before you provision
anything.

### License and attribution

Apache License 2.0, chosen for its explicit patent grant. Attribution
is a condition of use: per Section 4(d), the notices in `NOTICE` and
the copyright headers in the source files travel with every
redistribution and derivative work. Keep the code, keep the credit.

---

PHI AI is created by **Ryan Gomez & Co. Inc.** — built by one person
working with a frontier model. To err is human; to completely blow
stuff up is to AI. We can make mistakes: see something, say something,
and report an issue. Feedback, questions and criticisms:
<https://www.ryangomez.nyc>.
