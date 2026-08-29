# Allocation of Responsibility

**Target:** `docs/RESPONSIBILITY.md`. Referenced from `README.md`, `docs/COMPLIANCE.md`, and every runbook's preamble.

---

## 1. The statement

**HIPAA compliance is the responsibility of the organization that owns, manages, or is otherwise accountable for the protected health information. It is not a property of this software and cannot be delegated to it.**

This project supplies a technical platform. It does not manage, assess, certify, or assume any part of a deploying organization's regulatory obligations. An organization deploying this platform remains solely responsible for its own compliance with HIPAA, HITECH, applicable state law, and every other legal requirement that attaches to its use of PHI.

No software can make an organization HIPAA compliant. Compliance is a property of an organization's practices — its risk analysis, its policies, its workforce, its contracts, and its ongoing operations. Software can implement technical safeguards and make certain controls structural rather than procedural. That is what this platform does, and it is all it does.

## 2. Who is who

**The deploying organization** is the covered entity or business associate. It owns the infrastructure, holds the cloud provider BAAs, controls the credentials, decides who gets access, and is accountable to regulators and to patients.

**This platform** is software the organization runs on its own infrastructure. It is bring-your-own-infrastructure by design: the project maintainers do not host it, do not operate it, and never receive, create, maintain, or transmit PHI on any organization's behalf. **The maintainers are therefore neither a covered entity nor a business associate of any deployment, and no BAA between the maintainers and a deploying organization is required or offered for use of the software itself.**

**Professional services are a separate matter.** If the maintainers or any third party are engaged in an arrangement where they would create, receive, maintain, or transmit PHI — implementation assistance touching a live environment, managed operation, support with production data access — that engagement may create a business associate relationship requiring its own BAA under 45 CFR 164.308(b) and 164.314(a). That is negotiated per engagement and is not conferred by using the software.

**The software is provided under Apache License 2.0, without warranty of any kind.** Nothing in this document, the specification, or any runbook constitutes a warranty, a certification, a regulatory opinion, or legal advice.

## 3. What the platform provides

The platform implements technical safeguards under 45 CFR 164.312 and makes several of them structural — enforced by code paths that fail closed rather than by policies that rely on correct behavior.

| Rule | Standard | What the platform contributes |
|---|---|---|
| 164.312(a)(1) | Access control | Separate IAM roles and grants per access path; caller's grant bounds retrieval as a pre-filter inside the index scan; per-bucket and per-secret binding rather than project-level |
| 164.312(a)(2)(i) | Unique user identification *(Required)* | Per-identity service accounts and per-requester audit attribution |
| 164.312(a)(2)(iv) | Encryption and decryption *(Addressable)* | Envelope encryption under customer-managed keys for every derived PHI artifact, including embeddings, RAG caches, and prompts |
| 164.312(b) | Audit controls | Hash-chained, tamper-evident audit log of every AI interaction, registration decision, preflight verdict, consent evaluation, signature event, override, and refusal |
| 164.312(c)(1) | Integrity | Object storage as system of record with versioning; derived indexes rebuildable and never authoritative; deterministic attribution gate on generated output |
| 164.312(e)(1) | Transmission security | Private networking, declared and preflighted model egress, endpoint restriction where the cloud exposes a queryable property |

It also implements controls that exceed the technical safeguards: purpose-of-use tagging, sensitive-category exclusion at serialization time, a de-identification boundary with a determination record, model registration gates, protected-class input screening, a constrained action space, and a human signature requirement before anything enters the legal medical record.

## 4. What the platform does not do

The platform implements no part of 45 CFR 164.308 (administrative safeguards) or 164.310 (physical safeguards). It cannot, because those are organizational and physical practices, not software behaviors.

Specifically, the platform does **not**:

- Conduct, maintain, or satisfy a Security Risk Analysis.
- Write, approve, or maintain policies and procedures.
- Train, supervise, clear, or sanction a workforce.
- Decide who should have access to what.
- Determine what "minimum necessary" means for a given use.
- Execute BAAs, or verify that any BAA exists or covers what you think it covers.
- Detect, investigate, report, or notify a breach.
- Provide legal, regulatory, or clinical advice.
- Validate any clinical or predictive model, or determine its FDA regulatory status.
- Certify, audit, or attest to any deployment's compliance.
- Constitute a defense, safe harbor, or mitigating factor in any enforcement action.

## 5. What the deploying organization must do

Not exhaustive. This is the floor, not the ceiling, and it does not replace counsel.

### 5.1 Legal and contractual

- Execute a BAA with each cloud provider **before any PHI enters the environment**, and confirm it covers every service actually in use. Service-level coverage lists are not sufficient — coverage varies below the service level, and Pre-GA offerings are commonly excluded from PHI use even when the parent service is covered.
- Execute BAAs with every downstream subcontractor, vendor, and professional services provider that will touch PHI (164.308(b), 164.314(a)).
- Hold the required licenses for any terminology content loaded at install time — CPT from the AMA, SNOMED CT and the full RxNorm release via NLM/UMLS, VSAC expansions. The platform ships a loader, never the content.
- Confirm the EMR vendor agreements permit the intended integration and data use.

### 5.2 Administrative safeguards — 45 CFR 164.308

- **Risk Analysis** *(Required)* — an accurate and thorough assessment of risks and vulnerabilities to ePHI across the whole environment, not just this platform. This is the obligation most often skipped and the one most often cited in enforcement.
- **Risk Management** *(Required)* — measures sufficient to reduce identified risks to a reasonable level.
- **Sanction Policy** *(Required)*.
- **Information System Activity Review** *(Required)* — regular review of audit logs and access reports. The platform produces the logs; someone has to read them on a defined cadence.
- **Assigned Security Responsibility** *(Required)* — a named security official. Designate a Privacy Officer as well under 164.530(a).
- **Workforce Security** — authorization and supervision, clearance, and termination procedures.
- **Information Access Management** — access authorization, establishment, and modification.
- **Security Awareness and Training** — including log-in monitoring and password management.
- **Security Incident Procedures** *(Required)* — identify, respond to, document, and mitigate.
- **Contingency Plan** — data backup *(Required)*, disaster recovery *(Required)*, emergency mode operation *(Required)*, plus testing and criticality analysis.
- **Evaluation** — periodic technical and nontechnical review against the rule.

### 5.3 Physical safeguards — 45 CFR 164.310

Facility access controls, workstation use and security, and device and media controls, including disposal and media re-use. Entirely the organization's, including for any workstation from which the platform is administered.

### 5.4 Privacy Rule obligations — 45 CFR Part 164 Subpart E

- Notice of Privacy Practices (164.520).
- Individual right of access (164.524) — including how a patient obtains records the platform helped assemble.
- Amendment (164.526) — note that amendment obligations are one reason the platform refuses to fine-tune generative models on identified PHI: weights cannot be amended per-patient.
- Accounting of disclosures (164.528).
- Requests for restriction and confidential communications (164.522).
- Minimum necessary policies (164.502(b)). The platform makes minimum necessary *structural* — it cannot decide what is minimum necessary for your uses.
- Documentation retained six years (164.316(b)(2)).

### 5.5 Breach notification — 45 CFR 164.400–414

Detection, risk assessment, individual notice, HHS notice, and media notice where applicable, within the required timeframes. The platform's audit log is evidence; it is not a breach detection or notification system.

### 5.6 Decisions only the operator can make

The platform requires these to exist and refuses to run without them. It does not supply their content.

- The purpose-of-use taxonomy, and which roles may assert which purpose.
- Every IAM grant — which identity gets which access path.
- The retention schedule for each store, including audio, which is retained separately from the note it produces. State medical-record retention law varies and governs.
- The de-identification determination — Expert Determination requires a qualified person's signed judgment, or Safe Harbor requires the operator's verification. The platform produces the artifact and enforces the boundary; a human signs the determination.
- Model registration content: intended use, validated population, declared input-variable schema, and the mitigation record. The platform enforces that a registration exists and screens the declared schema; the operator supplies the substance and owns its accuracy.
- The ongoing duty under 45 CFR 92.210 to identify patient care decision support tools using protected-class variables and to mitigate discrimination risk. The platform blocks declared protected-class variables and produces disaggregated performance evidence. **Identifying undeclared proxies is not solved, and the platform does not claim to solve it.** The duty remains the covered entity's.
- Jurisdictional determinations: which state's recording-consent rule applies to a given encounter, which segmentation categories apply to the corpus, and where requesters may be located.
- Clinical validation and regulatory status of any hosted predictive model. The platform hosts, gates, audits, and monitors. It does not author, validate, or clear models.
- Which licensed humans may sign staged drafts into the legal medical record, and their credentialing.

### 5.7 Verification the platform cannot perform

Some cloud properties expose nothing queryable. For these the platform requires a signed operator attestation with an evidence artifact, and it refuses to run without one. The attestation's truth is the operator's responsibility.

- GCP Gemini abuse-logging exemption — granted by form, with no API, config field, or console indicator exposing its state. Evidence is the approval email.
- GCP Speech-to-Text data-logging enrollment — console toggle only, no API to read it.
- Azure BAA scope for AI Speech — Microsoft publishes no citable covered-products list.
- AWS BAA coverage of Transcribe Medical specifically — the eligibility list names "AWS Transcribe [Includes HealthScribe]" without naming the Medical APIs.

### 5.8 Ongoing operations

Access reviews on a defined cadence. Log review (164.308(a)(1)(ii)(D)). Key rotation. Patch and dependency management. Drift and fairness monitoring review — the platform emits the signal; a human decides what it means. Incident response exercises. Re-evaluation whenever the deployment, the law, or the vendor's terms change.

## 6. What "designed to be compliant" means

It means the platform is built so that a compliant deployment is achievable, and so the technical controls do not have to be fought or worked around to get there. Concretely: the design refuses rather than degrades, treats every derived artifact as inheriting its source's protection class, requires declared and preflighted model egress, and gates output on deterministic checks rather than statistical ones.

**It does not mean that deploying this platform makes an organization compliant, and no configuration of it can.** A deployment can satisfy every technical control the platform provides and still be out of compliance — because there was no risk analysis, because the BAA did not cover the service in use, because nobody read the audit logs, or because access was granted to people who should not have had it.

Where the platform cannot fully deliver a control, it says so in that control's documentation and in the relevant runbook's "Known gaps" section. **A green test suite against the synthetic corpus is a lower bound on error, not validation** — and it is not evidence of compliance.
