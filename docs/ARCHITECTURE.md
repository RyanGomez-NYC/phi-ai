# Architecture

## DICOM imaging and AI (optional)

Off by default. When enabled, imaging is stored one encrypted object per
SOP instance under `dicom/{study}/{series}/{sop}.dcm`, in the same bucket
and under the same KMS key as everything else.

- **One object per instance, not per study.** A viewer opens one image at
  a time; storing a study as a single object would mean decrypting a
  whole 2 GB CT to display one slice. The cost is object count, and
  `docs/SCALING.md`'s sizing table counts DICOM per *study* - counted per
  instance, which is how it is stored, the same object store is roughly a
  thousand times more objects.
- **The imaging index holds identifying PHI**, unlike `stored_resources`.
  QIDO-RS is a search over patient names, dates and accession numbers;
  there is no de-identified worklist. It is a separate, opt-in set of
  tables behind its own database role - the same posture as the OMOP
  layer (`core/db/imaging_schema.sql`).
- **The DICOMweb API is read-only.** QIDO-RS and WADO-RS only; no STOW-RS.
  Imaging enters through `python -m core.dicom import`, never over HTTP.
- **Nothing is transcoded.** Studies are served in the transfer syntax
  they were ingested in and decoded in the browser - the only place that
  cannot silently re-encode data the platform exists to preserve.
- **The viewer is an unmodified upstream artifact.** The official OHIF
  container at a pinned tag, configured from outside, on its own origin so
  that `script-src 'none'` survives on every page that displays PHI.
- **Purpose of use comes from the session, not the request.** DICOMweb has
  nowhere to carry one, so the platform's own record page establishes it
  before handing off to the viewer, and every DICOMweb request inherits
  it. Auditing is per study rather than per request - one study view is
  thousands of HTTP calls and one disclosure.

See `runbooks/RUNBOOK_DICOM_IMAGING.md`.

## Goals driving the design

- **Data never transits vendor-controlled infrastructure.** Every component
  runs inside the customer's cloud account/VPC or on-prem network.
- **Encryption everywhere, keys never touch app servers.** All PHI is
  encrypted client-side (envelope encryption) before it reaches storage,
  using cloud-native KMS (AWS KMS, Google Cloud KMS, Azure Key Vault).
- **Every access is logged, and an edit to the log is evident.** Audit
  records are hash-chained, so removing or altering an entry breaks
  verification at that point. They are **not** written to write-once
  storage - there is no WORM on any cloud here, and on AWS the
  `DenyAuditLogDeletion` bucket policy has been removed, so the audit log
  is deletable by any principal an IAM policy allows. The property is
  detection, not prevention. See `docs/COMPLIANCE.md` → "Retention and
  integrity".
- **One ingestion model, six vendors.** Vendor-specific integration is
  isolated behind a FHIR R4 client and an `EMRProfile` per vendor (Epic,
  Oracle Health/Cerner, athenahealth, eClinicalWorks, MEDITECH, NextGen)
  describing that vendor's actual auth model, resource support and bulk
  export capability; the platform core only ever deals with FHIR
  resources and raw document attachments. The deployment picks its
  vendor with `PHI_AI_EMR_VENDOR`, and each connector is testable
  against a per-vendor emulator (`emulators/`). See
  `docs/EMR_CONNECTORS.md`.

## High-level flow

```
 Source EMR (FHIR R4 API - Epic, Cerner, athenahealth, eCW, MEDITECH, NextGen)
        │  SMART Backend Services (RS384 JWT client assertion) or, for
        │  athenahealth, OAuth2 client secret; TLS 1.2+
        ▼
 FHIR Ingestion Service ──► Envelope Encryption (KMS) ──► Object Storage
        │                                                     (S3 / GCS /
        │                                                      Azure Blob,
        │                                                      versioned,
        │                                                      NO WORM -
        │                                                      see below)
        ▼
 Audit Log (hash-chained, append-only) ──► SIEM / log sink (customer's own)
        ▲
        │
 Retrieval API ── role-based access control ── requesting user/system
```

## Components

### 1. FHIR Ingestion Service (`core/fhir`)
Connects to a customer's EMR FHIR R4 endpoint using the auth flow that
vendor actually implements - a signed JWT client assertion for the SMART
Backend Services vendors, a client secret for athenahealth, with the
choice data-driven from the vendor's `EMRProfile` (see
`docs/EMR_CONNECTORS.md`) - and pages through the requested resource types
(Patient, Encounter, Observation, DocumentReference, etc.), and hands
each resource to the storage layer for ingestion. Designed to be
paused/resumed and to support incremental (delta) ingestion via FHIR
`_lastUpdated` search parameters.

### 2. Storage abstraction (`core/storage`)
A single interface (`ObjectStore`) with three backends: `S3Storage`,
`GCSStorage`, `AzureBlobStorage`. All three:
- Require object versioning, so an overwrite leaves the prior version
  intact.
- Record a declared retain-until date as object metadata.
- Store only ciphertext — plaintext PHI never reaches disk unencrypted.

**They apply no immutability lock.** S3 Object Lock, GCS Bucket Lock and
Azure immutability policies were all removed deliberately. Retention is a
configuration value fixed at initial implementation and recorded per
object; it is not enforced by the storage layer. A principal holding
delete permission can remove stored records before that date, and
nothing removes them after it.

This makes the integrity property **detective** rather than
**preventive**. What remains: versioning, a SHA-256 per object verified
on retrieval, the hash-chained audit log, and cloud access logs. Those
make unauthorized modification or deletion *evident*, not *impossible*.
Enforcement is operational — IAM scoping, monitoring, alerting on delete
events, and a documented disposition procedure. What remains differs by
cloud and the difference is real: AWS has IAM deny-delete plus versioning
and no MFA delete, GCP has IAM plus versioning only, and Azure has only a
7-day blob soft-delete window. Reintroducing an unbypassable control
requires new buckets, since no provider can add WORM to existing storage.
See docs/COMPLIANCE.md → "Retention and integrity".

### 3. Envelope encryption (`core/crypto`)
Each stored object gets a unique data encryption key (DEK), generated
locally, used once, and immediately discarded after being wrapped by a
cloud KMS key encryption key (KEK). Only the wrapped DEK is stored
alongside the ciphertext. This means:
- Losing the storage bucket doesn't expose data (still encrypted).
- Revoking a KMS key can cryptographically "shred" data classes.
- No long-lived symmetric key sits on an application server.

### 4. Audit logging (`core/audit`)
Every read, write, export, and access-control decision is recorded as an
event containing actor, action, resource ID, timestamp, and a SHA-256 hash
of the previous event (hash chaining), making silent tampering or deletion
detectable. Logs are written to the same storage tier as the
clinical data, and can be forwarded to the customer's own SIEM.

### 5. Retrieval / access API
Role-based access control (minimum necessary standard from HIPAA
§164.502(b)); every retrieval requires a documented purpose-of-use and is
audit-logged before data is returned.

### 6. Installer & runbooks
`install/install.sh` and `install/installer_chatbot.py` walk an operator
through: choosing a cloud, provisioning KMS + storage with the correct
retention settings, configuring EMR connection details
(base URL, client ID, private key path), and standing up the service via
`docker-compose` or the per-cloud Terraform in `deploy/`.

### 7. AI assistant (`core/assistant`) — optional, off by default

An assistant that explains this system to the people running and using
it, grounded in the repository's own runbooks and in the deployment's own
configuration. Available as a terminal session (`python -m
core.assistant`, which works before any infrastructure exists) and as a
page in the web interface.

Architecturally it is a **read-only satellite**: nothing else imports it,
and it reaches the object store only through the same narrow
`RecordReader` seam `core/web` uses, for aggregates. Removing the
package would leave every other component unchanged.

It is also the only component with a network path off the deployment,
which shapes its whole design:

- **Provider is a compliance choice.** On AWS and GCP the model runs
  inside the operator's own account (Bedrock / Vertex AI) under the cloud
  BAA already covering storage and KMS. Azure has no in-cloud Claude
  offering and uses the Anthropic API directly.
- **The tool list is the security boundary.** Whatever the model is asked
  to do, it can only do what a tool permits, so "can it see PHI?" reduces
  to reading `core/assistant/tools.py`. By default there is no tool that
  decrypts an object, returns a patient reference, an object key or an
  audit entry, or writes anything. Clinical tools exist in that same file
  and are built only where the deploying organisation enabled a PHI tier
  (`PHI_AI_ASSISTANT_PHI_ACCESS`) - permission-gated, and audited as
  disclosures before the object is decrypted.
- **Object store facts are aggregates, never rows.** `posture.py` reads
  the same rows the retention page reads and returns only counts.
- **Egress is scanned and audited.** PHI-shaped outbound text is refused
  rather than stripped, and the audit entry is written before the request
  is sent — the same ordering `core/web/app.py` uses before decrypting.
- **Tools inherit the caller's permissions**, so the assistant cannot
  become a path to something a role does not already permit.
- **No JavaScript.** It is reachable from every page through a native
  `<details>` drawer and answers via ordinary form posts, so the
  interface keeps its `script-src 'none'` policy intact. Conversation
  state is bounded, in-memory, and expires with the session.

See `runbooks/RUNBOOK_AI_ASSISTANT.md` and the compliance discussion in
`docs/COMPLIANCE.md`.

## What's intentionally NOT in the core

- No multi-tenant SaaS mode. This is single-tenant, BYOI software.
- No EMRs beyond the six profiled FHIR R4 vendors, and no proprietary
  EMR protocol support (HL7v2 batch feeds, direct DB extracts) either.
  Both are possible future work or professional-services engagements
  layered on top of the FHIR-first core, not current scope - see
  `docs/EMR_CONNECTORS.md`.
- **No AI over clinical content by default** - and the default is the
  design position, not a missing feature. The optional assistant
  (component 7) ships with `PHI_AI_ASSISTANT_PHI_ACCESS` set to `none`,
  confined to this project's documentation and PHI-free aggregates, and a
  deployment that never changes that setting has no path from the model
  to a patient's record at all - not by policy, but because no tool
  exists that returns one. The deploying organisation may raise the tier:
  to the record the caller already has open, or to anything that caller's
  role permits. Summarising or answering questions about a patient's
  records genuinely does carry its own regulatory weight, which is
  exactly why that call belongs to the covered entity and requires its
  own acknowledgement that a BAA covers the configured model provider -
  rather than being made once, for everyone, by this repository. Three
  things hold at every tier, including the raised ones: every clinical
  read is audit-logged as a disclosure **before** the object is
  decrypted, through the same code path the web routes use, so a
  §164.528 accounting cannot distinguish an assistant read from a click;
  permission still gates access, so no role obtains by asking what it
  could not obtain by navigating; and psychotherapy notes
  (§164.508(a)(2)) are unreachable at every tier by itself - reaching
  them takes the deployment's own separately-acknowledged gate, a
  dedicated application role, and a database role the general research
  search never holds. See component 7 above and `docs/COMPLIANCE.md` →
  "The AI assistant and the outbound boundary".
- No built-in de-identification. Ingestion preserves PHI as-is under
  BAAs; de-identification is a separate, deliberate feature to design
  carefully (Safe Harbor vs. Expert Determination under §164.514) rather
  than bundle in by default.

## Document ingestion (OCR)

A record set fed only by an EMR's FHIR API is incomplete. Scanned paper
charts, faxed referrals, outside-hospital records and signed forms are
part of the record, and a scanned page nobody can search is barely
retained at all.

`core/ocr/` runs documents through [Tesseract](https://github.com/tesseract-ocr/tesseract)
locally — no document byte leaves the container, which avoids needing a
BAA with an OCR vendor (45 CFR 164.502(e)). `core/fhir/documents.py`
turns the result into a FHIR R4 `DocumentReference` and stores it
through the ordinary ingestion path, so it is encrypted, audited,
indexed and patient-linked identically to a resource pulled from Epic.

Three properties are load-bearing:

- **The source is the record; OCR text is derived.** Each ingestion
  stores the original scan alongside the resource. OCR is lossy, and CMS
  requires records "in their original or legally reproduced form"
  (42 CFR 482.24(b)(1)).
- **Patient linkage is supplied, never inferred.** Nothing reads the
  extracted text to decide whose document it is. A misread MRN digit
  would file a record under the wrong patient, silently — and OCR
  misreads digits routinely.
- **OCR text is PHI and never enters the index.** It lives in encrypted
  storage like any other clinical content; the Postgres index receives
  the same structural facts it receives for every resource and nothing
  more.

Low-confidence or empty extractions are retained with
`docStatus: "preliminary"` rather than rejected — a poor scan of a real
record is still that record, but a reader years later needs to know the
text was uncertain. See `runbooks/RUNBOOK_DOCUMENT_INGESTION.md`.
