# Compliance mapping

This document maps product features to specific regulatory requirements.
It is engineering guidance, not legal advice — every deploying
organization needs its own counsel and its own HIPAA Security Risk
Assessment before running this against real PHI.

**`docs/RESPONSIBILITY.md` is normative and governs this document.**
HIPAA compliance belongs to the organization that owns or manages the
PHI, not to this software, and no configuration of it can confer
compliance. The "Responsibility boundary" section immediately below
summarizes the allocation; `docs/RESPONSIBILITY.md` carries it in full,
including the operator obligation checklist.

## Responsibility boundary

Read this section as governing everything below it.

This is open-source software, licensed Apache 2.0, provided **AS IS
and without warranties or conditions of any kind** — expressly
including any warranty of regulatory compliance, fitness for clinical
use, or accuracy of the regulatory analysis in this repository's
documentation. The project's position, stated once and relied on
everywhere:

**The project builds what engineering can build.** Structural
safeguards, fail-closed gates, hash-chained audit, machine-checkable
preflight evidence where a cloud exposes it, attestation records where
one does not, and documentation that labels every regulatory claim
[VERIFIED] or [UNVERIFIED] with its source. The software is taken as
far as it can be taken with what the project has — which is no access
to real PHI, no BAAs of its own, and no attorney-client relationship
with anyone.

**The implementing organization owns operating on real PHI.** That
ownership is not transferable to this codebase and includes, at
minimum:

- **BAAs** with its cloud providers, model vendors, and speech-to-text
  services. The project documents what is publicly citable about BAA
  scope per vendor (docs/SPEC.md §6.2) and where an operator
  attestation must stand in; obtaining and verifying the agreements is
  the organization's work.
- **Counsel review** of every jurisdiction-dependent table this
  software *applies* but does not *determine*: the recording-consent
  table (core/governance/consent_gate.py), the sensitive-category
  bases (docs/SPEC.md §6.1), the retention ruleset
  (core/config/retention_rules.py), and the coding-exposure posture
  (§5.5). Open dependency #7 in docs/SPEC.md §11 is deliberately
  marked "not closable by us."
- **Its own HIPAA Security Risk Assessment**, state-law analysis, and
  any FDA device determination for models it brings to the hosted-
  model-governance path (§5.15 — the customer supplies the model *and
  its regulatory standing*).
- **Terminology licences** — SNOMED CT, CPT, UMLS. Sub-licensing
  obligations pass to each deployment; the project cannot confer them
  (docs/SPEC.md §7.4).
- **Every operator attestation** the gates record where no machine
  check exists (GCP speech logging, Azure base-model logging). An
  attestation is the organization asserting a fact about its own
  cloud account; the software only records that it was made.
- **Validation on its own real data** before clinical use. Every
  acceptance number in this repository was produced against synthetic
  data and is a lower bound on error (docs/SPEC.md §7.7, §10).

None of this is a hedge bolted onto the software; it is the same
boundary the architecture draws. Bring-your-own-infrastructure means
the organization's accounts, the organization's keys, the
organization's agreements — and the organization's accountability.

## Federal: HIPAA Security Rule (45 CFR §164.308–.312)

| Requirement | Citation | How the PHI AI Platform addresses it |
|---|---|---|
| Access control, unique user ID | §164.312(a)(1)/(a)(2)(i) | RBAC with per-identity auth; no shared service accounts for human access |
| Automatic logoff | §164.312(a)(2)(iii) | Session timeout enforced at retrieval API |
| Encryption/decryption | §164.312(a)(2)(iv) | Envelope encryption, AES-256, cloud KMS-backed keys |
| Audit controls | §164.312(b) | Hash-chained, append-only audit log for every access/action |
| Integrity controls | §164.312(c)(1) | Object versioning; SHA-256 verification on retrieval; hash-chained audit log. **Detective, not preventive** — see "Retention and integrity" below |
| Transmission security | §164.312(e)(1) | TLS 1.2+ enforced for all network paths (EMR↔ingestion, service↔storage, service↔KMS) |
| Minimum necessary | §164.502(b) | RBAC scoped to resource type/purpose; no blanket "export everything" role by default |
| Business Associate obligations | §164.308(b), §164.502(e) | Because this is BYOI, the deploying org's existing BAAs with AWS/GCP/Azure cover infrastructure; the PHI AI Platform itself never processes data as a hosted service, so it does not itself need to be a Business Associate. **One component changes the shape of this question when enabled** — see "The AI assistant and the outbound boundary" below |
| Breach notification readiness | §164.400–414 | Audit log + storage access logs give the forensic trail needed to scope a breach within required notification windows |
| Retention | §164.316(b)(2)(i) and §164.530(j)(2) govern HIPAA **documentation**; the records themselves are governed by **state** medical record retention law | Retention is configurable both platform-wide and per resource type - either via flat env vars, or via a structured, cited ruleset file owned by a Health Information Manager (`PHI_AI_RETENTION_RULESET_PATH` - see `core/config/retention_rules.py`). **Recorded as metadata, NOT enforced by storage** - see "Retention and integrity" below. HIPAA's six-year rule is **not** a floor for medical records; the binding floor is the maximum applicable state minimum. No hardcoded minimum at any layer - see the note below the state law table |

## State law (non-exhaustive — varies significantly by state)

| State | Law | Relevant consideration |
|---|---|---|
| California | CMIA (Civil Code §56 et seq.), CCPA/CPRA (health data carve-outs) | Stricter consent/authorization norms than HIPAA in places; medical record retention minimums |
| Texas | HB 300 (Tex. Health & Safety Code §181) | Broader covered-entity definition than HIPAA; shorter breach notification deadlines |
| New York | SHIELD Act | Reasonable safeguards requirement applies regardless of HIPAA-covered-entity status |
| Illinois | Genetic Information Privacy Act, biometric law (BIPA) if applicable | Relevant if stored data includes genetic or biometric identifiers |
| Massachusetts | 243 CMR 2.07(13); M.G.L. c.111 §70 | Record retention of **20 years** — more than three times HIPAA's documentation period, and the clearest single reason HIPAA's six years cannot be treated as the floor |
| North Carolina | 10A NCAC 13B .3903 | A minor's record must be retained until the patient reaches **age 30** — a period that depends on date of birth, which the ingest/index layer deliberately does not hold (see "Open compliance work") |
| All states | Medical record retention statutes (vary 5–20+ years, longer for minors) | Retention config must be settable per state/jurisdiction, not hardcoded |

**Design implication:** retention periods, minimum-necessary role
definitions, and breach-notification log exports must all be
*configurable*, because the strictest applicable law (federal or state)
governs, and that answer changes by state and by data type.

**Note the word "configurable" is now doing all the work here — nothing
enforces these values. See "Retention and integrity" immediately below
before reading the rest of this section.** Earlier versions of
`deploy/aws/variables.tf` had a Terraform precondition requiring the
retention variable to be at least 2192 days (6 years) and COMPLIANCE-mode
Object Lock for any non-dev environment - both hardcoded, neither
configurable. Florida's physician-record retention minimum is
specifically 5 years, which the 6-year floor would have blocked a Florida
deployer from setting correctly - a direct contradiction of the "must be
settable per state/jurisdiction, not hardcoded" row in the state-law
table above, caught during a later review and fixed: both are now
genuinely deployer-configurable in every environment, with the only
remaining Terraform-level check being a sanity guard against the dev
default being carried unedited into a real deployment. (The variable is
`phi_retention_days`.) Retention "by data type," specifically, is
handled at the application layer (`PHI_AI_RETENTION_YEARS_OVERRIDES` -
see `core/fhir/client.py`, and the ruleset mechanism in
`core/config/retention_rules.py`), which is where a per-resource-type
figure can actually be expressed. There is no longer a bucket-level
floor for it to interact with: see "Retention and integrity" below.

## Retention and integrity: what this system does and does not enforce

Stated plainly, because two rows in the table above and the whole
retention section depend on it.

**This platform applies no storage-level immutability, on any cloud.**
No S3 Object Lock, no GCS Bucket Lock or bucket retention policy, no
Azure immutability policy, and no bucket policy denying deletes. That is
a deliberate design decision, not an oversight or a dev-only shortcut.
The three clouds are not equivalent in what stands in its place, and the
differences are real rather than cosmetic:

- **AWS.** IAM deny-delete statements on the ingest, restore and auditor
  roles, plus bucket versioning. A plain delete leaves a delete marker
  and the object remains recoverable; a versioned delete
  (`DeleteObjectVersion`) destroys the content outright. **MFA delete is
  not enabled.** The `DenyAuditLogDeletion` bucket policy has been
  removed, so the audit log itself is deletable by any principal an IAM
  policy allows — the hash chain and CloudTrail make such a deletion
  *evident*, they do not make it *impossible*.
- **GCP.** No retention policy and no Bucket Lock. IAM scoping and object
  versioning only. This is the least-protected of the three and should be
  read that way.
- **Azure.** No immutability policy. The 7-day blob soft-delete window is
  the only deletion protection present, and it is a recovery window, not
  a bar — past seven days a deleted blob is simply gone.

**Retention is configuration, not enforcement.** The period is chosen at
initial implementation — `PHI_AI_RETENTION_YEARS`, the per-type
overrides, or a reviewed ruleset file (`core/config/retention_rules.py`,
`runbooks/RUNBOOK_RETENTION_RULES.md`) — and recorded as object metadata
plus a row in the Postgres index. It documents the intended disposition
date and drives `runbooks/RUNBOOK_DISPOSITION.md`. It does not prevent an
early delete, and nothing deletes anything when it expires.

**What this means for §164.312(c)(1).** The integrity control is
detective. Unauthorized modification or deletion is *evident*: versioning
preserves superseded objects, every object carries a SHA-256 verified on
retrieval, the audit log is hash-chained so a removed entry breaks
verification at that point, and CloudTrail records object-level access
independently of the application. It is not *prevented* — any principal
granted `s3:DeleteObject` can destroy stored PHI or audit records.
Nothing in this system structurally prevents the early deletion of PHI.

**What this means for state retention law, which is the harder half.**
Retention is not one number, and HIPAA does not set the floor. HIPAA's
six-year rule (45 CFR §164.316(b)(2)(i), §164.530(j)(2)) governs *HIPAA
documentation* — policies and procedures, risk analyses, authorizations,
accountings of disclosure. HIPAA assigns no retention period to medical
records at all. The binding rule for the records this platform stores is
the **maximum of every applicable state minimum**, and those exceed six
years routinely: Massachusetts requires 20 years, and North Carolina
requires a minor's record be kept until the patient reaches age 30. A
deployer treating patients in more than one state must configure to the
longest applicable period, not to six. Because nothing enforces whatever
number gets configured, satisfying a 20-year or age-30 obligation is, in
this platform, entirely a matter of the deployer not deleting the object.
The system will record the intended date. It will not defend it.

**What changed in the disposal tooling.** `core/fhir/purge.py`'s
`expired` mode previously could not delete an unexpired object even if
its own logic were buggy, because S3 refused the call. That backstop is
gone, so the retention check now lives in application code and is
re-verified immediately before each delete
(`_dispose_one(require_expired=True)`). That is a genuinely weaker
guarantee than storage-layer refusal, and it is worth knowing which one
you have. `admin-order` mode's COMPLIANCE-mode refusal is gone entirely,
because there is no longer a deployment posture in which early removal is
impossible.

**The compensating controls are operational, and they are yours to run.**
Every one of them is **detection or recovery, not prevention** — none of
them stops a delete; they tell you it happened, or give you a window to
undo it. At minimum: scope `s3:DeleteObject` to as few principals as
possible (the one genuinely preventive item on this list, and it narrows
who *can*, not who *may*), keep `enable_admin_order_purge` false unless
genuinely needed, alert on delete and delete-marker events in CloudTrail
(detection), verify the audit chain on a schedule rather than only during
an incident (detection), rely on object versioning and, on Azure, the
7-day soft-delete window (recovery — and on Azure that window is short),
and keep the disposition procedure documented and actually followed.

**If your risk analysis requires unbypassable retention**, that is a
different deployment shape and it must be decided before you ingest
anything: none of the three cloud providers can add WORM to an existing
bucket or container. Reintroducing it means creating new storage with the
lock enabled at creation and migrating every object across.

Whether the posture described here is defensible for your organization is
a determination for your Privacy/Security Officer and counsel, informed
by your §164.308(a)(1)(ii)(A) risk analysis. This document describes what
the software does; it is not a compliance opinion.

## The AI assistant and the outbound boundary

`core/assistant/` is optional, off by default, and is the only component
of this system that sends anything outside the deployment. Because the
Business Associate row above rests on "the PHI AI Platform never
processes data as a hosted service", it is worth being precise about
what enabling the assistant does and does not change.

**What is sent.** The user's typed question, this project's own committed
documentation, and PHI-free aggregates about the deployment: counts by
resource type, a count of distinct patients, configuration booleans,
retention totals, and the audit-chain verification verdict. Nothing else.

**Population analytics (optional, off by default).** The assistant can
answer questions about the population rather than about one record -
cohort counts, facility breakdowns, name search - over the optional OMOP
layer. Three things are worth a reviewer's attention:

- **Aggregate counts are reported exactly, with no small-cell
  suppression.** This was a deliberate decision rather than an oversight.
  A count of 2 for a rare condition is disclosive, and many research data
  warehouses suppress below a threshold (CMS uses 11). The reasoning for
  not doing so here: every count is permission-gated and audited, and the
  same user could query the OMOP layer directly, so the judgment sits
  with access control rather than output filtering. An organisation that
  disagrees should implement suppression in `core/analytics/cohort.py`,
  and should treat `analytics:query` as a grant that can identify people
  indirectly.
- **A new `analyst` role separates analytics from disclosure.** It holds
  `analytics:query` and neither `patient:read` nor `identity:search`, so
  it can count a cohort and cannot open a chart or resolve the cohort to
  names. `analytics:query` and `identity:search` are granted separately
  and neither implies the other.
- **`identity.patient_identity` is the only place a patient's name is
  stored outside an encrypted object.** It holds names, birth date,
  administrative gender and the opaque EMR id - no MRN, no SSN, no
  address, no clinical content - behind its own database role, and is
  removed by the same disposal path that removes the record. Read that
  file's own header before enabling it.

Generated SQL is bounded by the database role rather than by string
inspection: the analytics connection holds SELECT on seven `cdm` tables
and `vocab.concept` and no write grant of any kind. Every generated query
is recorded in the audit trail verbatim.

**Whether PHI may be sent at all is the deploying organisation's
decision.** `PHI_AI_ASSISTANT_PHI_ACCESS` defaults to `none` and can
be raised to let the assistant read the record a user already has open,
or anything that user's role permits. Each tier above the default
requires a separate acknowledgement that a BAA covers the configured
model provider and that the organisation's risk assessment covers the
flow. What follows describes the default; where a tier is enabled, two
things hold unchanged: **every clinical read is audit-logged as a
disclosure before the object is decrypted**, through the same code path
the web routes use, so reads made through the assistant appear in an
accounting of disclosures under §164.528 and are indistinguishable from
reads made by clicking; and **permission still gates access**, so a role
without `patient:read` cannot obtain a record by asking. Psychotherapy
notes (§164.508(a)(2)) are unreachable at every tier by itself: they
become reachable only through their own deployment gate
(`PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS` plus its own acknowledgement,
refused below the lookup tier), their own database role over their own
separately-stored search table, and a dedicated `psychotherapy`
application role - and every search and read is audit-logged as a
disclosure before anything is decrypted. The general research search
(`research:search`, the `researcher` role) can never return them: its
database role holds no grant on the psychotherapy table, so the
separation is enforced by Postgres, not by prompt.

**What is not sent at the default tier, and why that is structural.** No clinical content, no
patient reference, no object key, no audit entry, no decrypted object.
Not because the model is instructed to avoid them, but because no tool
exists that returns one — `core/assistant/tools.py` is the complete,
readable list, and every object-store-reading tool returns aggregates
computed inside `core/assistant/posture.py` from rows that never leave
that module. Outbound text is additionally scanned and refused if it
matches PHI-shaped patterns, and every question is written to the
hash-chained audit log **before** it is sent, so a failed audit ends the
request having transmitted nothing.

**Where the traffic goes depends on the provider, and that is the
compliance decision.** On AWS (`bedrock`) and GCP (`vertex`) the model
runs inside the deploying organization's own cloud account, under the
same BAA already covering S3/GCS and KMS, reached with the same workload
identity — no new vendor relationship and no traffic crossing the account
boundary, so the row above is unchanged. On Azure, which has no in-cloud
Claude offering, the only option is Anthropic's API directly, which is a
genuine third-party path. Anthropic offers a BAA; whether one is required
here is a question for the deploying organization's privacy officer, and
the honest input is that no PHI travels this path by construction, so the
decision is about whether a PHI-holding system may reach an external API
at all rather than about what it would disclose if it did.

**The limit, stated rather than left to be discovered.** The outbound
scan is pattern-based. It catches structured material — FHIR resources,
object keys, identifiers, contact details — and it cannot catch a
patient's name typed as narrative free text, because a name has no
structure to match. What protects against that case is that the assistant
has no capability that could act on a name: no patient search, no record
read, no lookup of any kind. Staff training should reflect that the box
is not a safe place to paste a chart, and
`runbooks/RUNBOOK_AI_ASSISTANT.md` says so in those words.

**Conversation state.** The web interface keeps a multi-turn transcript
so follow-up questions work. It is held in worker memory, never written
to disk, the database or object storage, and it expires on the same clock
as the session cookie that references it. Every question in it has
already passed the outbound scan and is already in the audit log, so it
holds nothing that was not sent and recorded anyway. It is therefore not
a new disclosure surface, but it is a new place text lives, which is why
it is named here rather than left to be found in the code.

**Access follows the existing role model.** Each assistant tool declares
the same permission the equivalent page in the web interface requires, so
the assistant cannot become a route to something a user's role does not
already permit. A viewer gets documentation only.

## DICOM imaging

Optional and off by default. Enabling it has three compliance-relevant
consequences, none of which are hidden by the implementation.

**The imaging index holds identifying PHI.** Patient names, birth dates,
sex, accession numbers and referring physicians, in queryable columns.
This is unavoidable: QIDO-RS (DICOM PS3.18 §10.6) defines a search over
exactly those attributes, and a de-identified worklist returns studies
nobody can identify - which is a broken feature, not a privacy control. So
the control is access rather than omission: a separate set of tables, a
separate database role, opt-in, and every read audited. The lightweight
`stored_resources` index is unchanged and still holds no clinical
content.

**Every image read is a disclosure, audited per study.** A user states a
purpose of use on the platform's own record page before the viewer opens;
that purpose is recorded with the study in the audit trail as
`record.read.imaging.study`, before anything is decrypted. That string is
the literal audit event type the code emits - `_audit_study()` in
`core/web/dicomweb_routes.py`, from both the study-level and series-level
metadata routes - so it is what an investigator searches for, and naming
a different one here would send them looking for events that do not
exist. The QIDO-RS study search writes a second action,
`record.search.imaging`; that route requires `patient:search` and filters
on PatientName and AccessionNumber, so it is a read of identifying PHI in
its own right and belongs in the same review. Auditing is at study
granularity rather than per HTTP request - opening one CT is thousands of
requests and one disclosure, and per-request entries would bury the trail
rather than enrich it. The study entry is what appears in an accounting
of disclosures under §164.528.

**Nothing is de-identified, and burned-in annotation is not addressed.**
DICOM headers are stored verbatim, so every identifier in them is
retained - correct for a system of record, and worth stating plainly. Pixel data
can additionally carry a patient's name or an accession number rendered in
by the acquiring modality; no header inspection detects this, DICOM's own
`BurnedInAnnotation` attribute is optional and frequently absent or wrong,
and this project does not attempt to strip it. Any use requiring
de-identification needs a dedicated tool and a human review pass. See
`runbooks/RUNBOOK_DICOM_IMAGING.md`.

## OCR'd documents

Scanned documents ingested via `core/ocr/` are stored under exactly the
same controls as FHIR resources pulled from the EMR: envelope encryption
with a customer-managed key, the hash-chained audit log, the same
retention configuration, and the same index that holds no clinical
content.

Two points specific to OCR are worth stating for a compliance record:

- **No third party sees the documents.** Tesseract runs locally in the
  platform's own container. No hosted OCR service is used, so no Business
  Associate Agreement under 45 CFR 164.502(e) is required for document
  text extraction, and no PHI is transmitted for that purpose.
- **Extracted text is explicitly derived, and marked when uncertain.**
  The source scan is retained as the record of truth, satisfying CMS's
  "original or legally reproduced form" requirement (42 CFR
  482.24(b)(1)). Text extracted at low confidence, or not extracted at
  all, is recorded with FHIR `docStatus: "preliminary"` rather than
  `"final"`, so an uncertain transcription is never indistinguishable
  from a verified one.

Accuracy is not claimed and should not be assumed. OCR misreads
characters — a verification run misread a clean printed date by two
years at 82% confidence. Nothing in this system makes a clinical or
identity decision from OCR output; patient linkage is always asserted by
the operator ingesting the document. See
`runbooks/RUNBOOK_DOCUMENT_INGESTION.md`.

## Why this project does not include a 50-state retention rules engine

This was requested directly during development, and it's worth
documenting why the answer was no, in this specific form.

A rules engine that *determines* what HIPAA and all 50 states require
would mean this codebase making legal conclusions - not configuring
them, actually deciding them - across 50 independent, changing bodies
of law. That's a materially different thing from everything else in
this document, all of which is about making the *deployer's own*
determination configurable rather than hardcoding one number.

Concrete evidence this isn't hypothetical caution: even resources built
specifically to track this professionally have gotten individual states
wrong and needed correction. One retention-law reference table had six
states' worth of figures re-checked and corrected within the same
month they were published. The underlying law changes with real
consequences, too - Washington's hospital record retention period
changed from 10 years post-discharge to 26 years from creation in
2025; Texas added a new electronic-records retention rule effective
January 2026. A hardcoded table would drift out of date the same way
the removed 6-year Terraform floor did, at a much larger scale and with
a much larger blast radius when it happened.

**What exists instead:** `core/config/retention_rules.py` and
`runbooks/RUNBOOK_RETENTION_RULES.md` - a rules engine in the sense of
software that reliably *applies* a structured ruleset, not one that
determines what belongs in it. Every rule requires a citation, a
reviewer, and a review date; the tool warns (not blocks) when a rule
hasn't been looked at in a while, given the Washington/Texas examples
above. The human in the loop is deliberately scoped as a Health
Information Manager - the role that operationally owns retention
schedules in most healthcare organizations - not a requirement for
outside counsel, though nothing stops an organization from routing
review through counsel as part of their own process if that's how they
already work. AHIMA (the American Health Information Management
Association) maintains ongoing professional guidance in this space and
is a reasonable starting point for determining actual figures - a real,
periodically-updated authority, rather than something reconstructed
from a single point-in-time reference.

## Open compliance work (tracked, not yet solved)

- [ ] Formal third-party HIPAA Security Risk Assessment of the reference
      deployment.
- [ ] Pre-populated per-state retention data. `core/config/retention_rules.py`
      provides the mechanism (structured, cited, reviewable rules) but
      deliberately ships with no actual state data - see "Why this
      project does not include a 50-state retention rules engine" above
      for why that's a hard line, not a to-do item to eventually close
      by populating one.
- [ ] Minor-specific retention (a longer period for minors' records,
      which several states require) - would need the ingestion pipeline
      to know patient date of birth, which conflicts with never handling
      identifiable demographics at the ingest/index layer. Deliberately
      out of scope for `retention_rules.py` as it exists today - see
      that module's own docstring. North Carolina's age-30 rule (10A
      NCAC 13B .3903, in the state table above) is the concrete shape of
      this gap: the platform cannot currently compute that date.
- [ ] Independent security review of `core/assistant/`'s outbound boundary — it is the one component with a network path off the deployment, and its guarantees are currently asserted by this project's own tests rather than by anyone else's
- [ ] Business Associate Agreement template for the professional-services
      arm (only needed when Anthropic-style "we touch your data" services
      are involved, e.g. migration assistance).
- [ ] SOC 2 Type II style control mapping for the reference Terraform in
      `deploy/`.
- [ ] Formal de-identification module design (Safe Harbor / Expert
      Determination) — deliberately deferred, see ARCHITECTURE.md.
