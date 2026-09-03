# PHI AI Platform — System Specification

**Status: ADOPTED — formal specification, effective 21 August 2026.** Canonical for scope, capabilities, invariants, and acceptance criteria. Changes to §3 (Invariants), §7 (Synthetic test data), or §10 (Validation) require explicit approval.

**Development repository: `RyanGomez-NYC/phi-ai`** — the only repo for this project.

---

## Allocation of responsibility — read before anything else

**HIPAA compliance is the responsibility of the organization that owns, manages, or is otherwise accountable for the protected health information. It is not a property of this software and cannot be delegated to it.**

This project supplies a technical platform. It does not manage, assess, certify, or assume any part of a deploying organization's regulatory obligations. The deploying organization is the covered entity or business associate; it owns the infrastructure, holds the cloud BAAs, controls the credentials, decides who gets access, and is accountable to regulators and to patients.

Because the platform is bring-your-own-infrastructure, the maintainers never receive, create, maintain, or transmit PHI on any organization's behalf, and are therefore neither a covered entity nor a business associate of any deployment. A professional services engagement that would touch PHI is a separate matter and may create a business associate relationship requiring its own BAA.

The platform implements technical safeguards under 45 CFR 164.312 and makes several structural. **It implements no part of 164.308 (administrative safeguards) or 164.310 (physical safeguards)** — risk analysis, policies, workforce training, sanctions, access decisions, contingency planning, and breach notification are organizational practices, not software behaviors.

"Designed to be compliant" means a compliant deployment is achievable and the technical controls will not have to be fought to get there. **It does not mean that deploying this platform makes an organization compliant, and no configuration of it can.** A deployment can satisfy every technical control here and still be out of compliance — because there was no risk analysis, because the BAA did not cover the service in use, because nobody read the audit logs, or because access was granted to people who should not have had it.

`docs/RESPONSIBILITY.md` is normative and carries the full allocation, including the operator obligation checklist. Software provided under Apache 2.0, without warranty. Nothing here is legal advice.

---

Single authoritative specification. Supersedes and absorbs `claude/ai-application-candidates.md` and `claude/product-description.md`; both are retired.

Scope: Epic FHIR first, AWS / GCP / Azure, bring-your-own-infrastructure. One release — every capability below is v1. Declined capabilities have been removed from the capability set; the boundaries that produced those declines survive as Invariants 16 and 19, because a boundary that exists only in a rationale document gets re-litigated by the next engineer to read a competitor's feature list.

> Implementation note (this repository): the enforcement kernel for Invariants 13–19 and subsystems §6.1–§6.6 lives in `core/governance/`; its operator documentation is `runbooks/RUNBOOK_MODEL_GOVERNANCE.md`. Each module cites the section it implements.

---

## 0. How to read this document

Every capability in §5 is in v1 scope. Every cross-cutting subsystem in §6 is a v1 dependency of at least one capability in §5, and several capabilities cannot ship without one. §7 specifies the synthetic test corpus — the platform is built with no access to real patient data, so §7 is the evidence base for every acceptance claim in §10. §11 lists what a specification cannot close — open dependencies requiring an authenticated vendor document, a counsel sign-off, or a pilot — and each is bounded, with the specific action that resolves it.

Regulatory statements carry an explicit **[VERIFIED]** or **[UNVERIFIED]** label. Verified means read from a primary source cited inline. Unverified means recalled, secondary-sourced, or inferred, and it is not to be repeated in customer-facing material until promoted.

---

## 1. Product definition

An open-source, bring-your-own-infrastructure AI layer over protected health information. It ingests EMR data into customer-controlled storage, indexes it, and provides grounded retrieval and Q&A, agent/MCP data access, de-identification and cohort building, ambient documentation, EMR write-back, and a model gateway with PHI guardrails.

**Thesis.** The market is not short of clinical AI features. It is short of clinical AI features an organization's privacy officer, general counsel, and CMIO can all sign. Every capability here is specified in terms of what makes it signable: where the PHI goes, who granted it, what the model saw, what it cited, and what happens when a gate fails.

**Corollary, and the strongest commercial position in this document:** for the highest-risk model classes the platform is the *governance substrate*, not the model vendor. It does not author predictive clinical models. It supplies the registry, egress preflight, attribution gate, fairness screen, hash-chained audit, action-space enforcement, and source-attribute artifacts that make a customer's own model operable and defensible (§5.15). This is a market position, not a scope retreat.

---

## 2. Scope

**In v1:** sixteen capabilities (§5), eight cross-cutting subsystems (§6).

**Non-goals, permanently:**
- The platform does not author predictive clinical models (Invariant 16).
- The platform does not generate or rank differential diagnoses, and does not produce a specific diagnostic or treatment directive (Invariant 19).
- The platform takes no autonomous clinical action, and nothing it generates enters the legal medical record without a human signature (Invariant 13).

These are not deferrals. §8.1 explains the regulatory mechanics that make them structural.

---

## 3. Invariants

Invariants 1–12 are canonical in the project instructions and are not restated here in full. In summary, and unchanged: storage is the system of record; PHI never reaches application logs; no silent fallback on security-relevant misconfiguration; ETL is idempotent with watermarks advancing only on clean runs; minimum necessary is structural and pre-filters retrieval; every derived AI artifact inherits its source's protection class; PHI egress to any model is declared and preflighted with machine-checkable evidence; entity attribution is a hard gate; de-identification is a one-way boundary with a determination record; every AI interaction is an audited event with purpose-of-use; no autonomous clinical action.

**Seven additions.** No capability ships before the invariant it depends on.

**13 — Human signature commits.** Nothing the platform generates enters the legal medical record without a human signature event. Write-back is a two-step protocol: stage a draft resource, a licensed human signs, the signature commits. AI-assisted provenance is recorded on the written resource. No auto-commit path exists, including for content classified low-risk.

**14 — Registered decision support.** Any model producing a patient-specific score, ranking, or classification that could influence care is registered as a patient care decision support tool with intended use, validated population, and a declared input-variable schema screened for protected-class variables and declared proxies, with a mitigation record attached. Unregistered models do not execute. This makes 45 CFR 92.210 structural rather than an attestation, the same way minimum necessary is already structural.

**15 — Ambient audio is a first-class PHI store.** Own CMK, own grant, own retention schedule distinct from the note it produces. Capture is gated on a per-encounter consent evaluation keyed to jurisdiction *and* modality, and an unresolved jurisdiction refuses capture.

**16 — The platform does not author predictive clinical models.** It hosts, gates, audits, monitors, and constrains the customer's models.

**17 — Human release gate on patient-directed output.** Drafts are never auto-sent. No configuration disables the gate.

**18 — Constrained action space.** Operational predictions may trigger supportive interventions. They may not trigger denial, deprioritization, or double-booking absent an explicit operator override recorded in the audit log with a stated basis.

**19 — No diagnostic or treatment directive.** The platform does not generate, rank, or order differential diagnoses, and does not output a specific preventive, diagnostic, or treatment recommendation. Hypothesis-directed evidence retrieval (§5.1) is the supported alternative and the refusal path names it.

---

## 4. Architecture

| Plane | Contents | Authority |
|---|---|---|
| Storage | S3 / GCS / Blob — raw FHIR resources | **System of record. Always wins.** |
| Index | Postgres index; vector index | Derived, rebuildable |
| Retrieval | Serialization, hybrid search, RAG cache | Derived, rebuildable, PHI-classed |
| De-identified | Separate bucket, key, role | One-way, determination record attached |
| Analytics | OMOP CDM | Derived, rebuildable |
| Gateway | Model registry, egress preflight, attribution gate | Enforcement point |
| Audit | Hash-chained event log with purpose-of-use | Append-only |
| **Ambient capture** | Encounter audio, consent records, STT egress | New — PHI store, §6.5 |
| **Write-back** | Staged drafts, signature events, provenance | New — §6.4 |
| **Streaming** | Real-time feeds for hosted models | New — §6.2 |
| **Model governance** | Registry extension, fairness screen, monitoring, source attributes | New — §6.2, §6.3, §6.6 |
| **Patient-directed output** | Release gate, labeling | New — §5.3, §5.6 |

---

## 5. Capabilities

### 5.1 Grounded clinical assistant — flagship

Natural-language questions scoped to a patient and optionally an encounter. Retrieves from the indexed FHIR corpus, generates an answer where every claim carries a citation resolving to a storage object key, and refuses rather than degrades when a gate fails.

**"Tuned on EMR data" means retrieval-side tuning, not fine-tuning a generative model on identified PHI.** Fine-tuned weights are a derived PHI artifact that is not rebuildable from the storage backend, which breaks the premise that lets storage be the system of record; and memorized content carries no chunk, no subject reference, and no storage key, so it passes the attribution gate invisibly and produces uncited assertions indistinguishable from grounded ones. Weights also cannot be amended per-patient, which collides with 45 CFR 164.526. Embedding, reranker, and extraction models may be fine-tuned on de-identified or public corpora only (MIMIC-IV, Synthea, published FHIR example sets, or our own de-identified derivative under its determination record), each with a registry entry recording the training corpus as basis.

**Retrieval design.**

*a. Structured-first serialization.* Chunk unit is a clinically coherent unit — one encounter's medication list, one lab panel at one effective time, one note section — rendered through a deterministic versioned template carrying subject reference, encounter reference, effective date/period, code system + code + display, and the storage object key. Where the deployment records it, the chunk also carries the provenance of the record: the system that disclosed it, the run that carried it, and when. The storage key answers where a fact is kept, which is not the question asked when a disclosure is challenged; under N-to-M exchange, many sources hydrating one store and that store feeding many targets, only provenance can name who handed a record over. Serialization is idempotent; template version is stored with the vector so a partial re-embed is detectable.

*b. Sensitive-category exclusion at serialization time*, never at retrieval. Delegated to §6.1.

*c. Hybrid retrieval.* Dense plus BM25/lexical, fused. Clinical queries carry exact tokens — drug names, LOINC codes, "A1c", "EF" — that dense embeddings blur. Query expansion through RxNorm (ingredient ↔ brand ↔ clinical drug), SNOMED CT subsumption, and LOINC value sets.

*d. Grant-bounded pre-filters inside the index scan.* Patient, encounter, date range, resource type, the caller's grant, and, where the deployment records origin, the permitted source systems are WHERE-clause predicates evaluated within the search. Origin is evaluated before the clinical predicates: a record that cannot be shown to have been lawfully ingested should not be reasoned about at all, and checking it last would make matching on everything else the cheapest way past it. A deployment that has not recorded origin is unaffected; requiring it is a declared posture, not a default. This is a relevance mechanism as much as an access-control one: a top-k drawn from the whole corpus and then filtered returns fewer usable results than a top-k drawn from the permitted scope.

*e. Temporal weighting.* Weight by effective date against the question's time anchor. A resolved 2019 problem must never outrank the active list.

*f. Status and negation survive into chunk text.* `Condition.clinicalStatus` / `verificationStatus`, `MedicationRequest.status`, `AllergyIntolerance.verificationStatus`, `DocumentReference.status`. A penicillin allergy marked `refuted` or `entered-in-error` that serializes as "penicillin allergy" is a data-integrity failure that looks exactly like a good retrieval. Dedicated test suite required.

*g. Deterministic structured spine.* For summarization-shaped questions, assemble the timeline — problems, medications, allergies, recent labs with trends, procedures, encounters — directly from structured resources, and use retrieval to fill narrative context around it. Highest-leverage decision in the capability: chunk-level recall silently drops the active medication that mattered while returning a fluent, well-cited, wrong answer.

*h. Answer contract.* Every claim carries ≥1 citation. Attribution gate runs before release. Empty retrieval produces abstention, never a parametric-knowledge answer; the abstention default is not operator-disableable, per the no-silent-fallback invariant. Purpose-of-use (Treatment / Payment / Operations / Research) is declared per session and bounds retrieval scope.

*i. Hypothesis-directed evidence retrieval.* The clinician states the hypothesis; the platform returns cited evidence consistent and inconsistent with it. "Is there anything in this chart relevant to amyloidosis?" is a retrieval question with a reviewable basis. "What is this patient's differential?" is refused under Invariant 19, and the refusal names this alternative.

**Acceptance:** §10.

### 5.2 Encounter and longitudinal summarization
Built on the 5.1(g) spine, not on top-k text. The failure mode instrumented is silent omission, not fabrication.

### 5.3 Patient-friendly instructions
Restates already-authored discharge instructions at a target reading level. **Does not generate clinical content.** Input is the clinician's instruction text plus the structured medication and follow-up list; output is a reading-level transformation with a no-new-assertions check that diffs asserted clinical facts against the source and fails on any addition. Clinician release gate required (Invariant 17).

### 5.4 Prior authorization and appeals
Given a payer criteria set, retrieve and cite chart evidence supporting each criterion, assemble the packet. Purpose tag Payment. Appeals are the same pipeline with a different output template. Highest ROI-to-friction ratio in the register and the clearest demonstration of the citation architecture.

### 5.5 Coding documentation-gap detection
**Gap detection with cited evidence. Not code recommendation.** Surfaces where documentation is insufficient to support what was clinically done, citing the thin resource. Does not rank candidate codes, does not order by reimbursement weight, and does not propose a code the existing documentation does not already support. An AI that suggests higher-weight codes is a false-claims exposure generator, and "supporting accurate coding" versus "optimizing revenue capture" is exactly the distinction an auditor probes. Writes are subject to Invariant 13.

### 5.6 Inbox and message triage
Routing plus draft, never auto-send (Invariant 17). Two constraints the market form omits:
- **Conservatively biased toward escalation.** A missed urgent message is a patient-safety event, not a precision metric. The operating point is chosen on recall of the urgent class, and the miss rate on that class is a published, monitored number rather than an implicit consequence of a threshold.
- Registered under §6.3 like any other decision support tool, because triage priority that varies in effect by protected class or a proxy is squarely within 45 CFR 92.210.

### 5.7 No-show risk and supportive intervention
Three structural constraints, in code rather than policy:
1. **Protected-class variables and declared proxies are excluded at the feature-schema level.** The schema is declared at registration and validated; a model whose schema contains a blocked variable does not register, and an unregistered model does not execute (Invariant 14). Proxy candidates — ZIP in isolation, payer class, primary language, interpreter need — require explicit operator justification rather than being silently permitted.
2. **A fairness report is a required registration artifact** (§6.3): performance disaggregated across the protected categories, with the mitigation record 92.210 requires.
3. **Constrained action space** (Invariant 18). Risk may drive an additional reminder, a transport or telehealth offer, an outreach call. It may not drive double-booking, deprioritization, or scheduling denial without a logged operator override. This is the difference between a tool that improves access for patients who struggle to attend and one that quietly penalizes them.

### 5.8 Scheduling optimization
Balances staff availability, bed counts, and cancellations, largely over FHIR Appointment/Schedule/Slot and operational data; much needs no model PHI egress at all. Where a decision attaches to an identified individual, Invariant 18 applies identically to 5.7.

### 5.9 Release-of-information triage
Which resources are responsive to a records request. A minimum-necessary problem, already structural. §6.1 segmentation does most of the work.

### 5.10 Registry and quality-measure abstraction
eCQM, cancer/trauma/STS/NCDR. Abstraction with a human confirming each element and a citation per element. Human sign-off is mandatory, not configurable. Writes subject to Invariant 13.

### 5.11 Cohort building / natural-language-to-SQL over OMOP
Runs entirely on the de-identified plane. Note the architectural reason this is a separate surface rather than a wider filter on 5.1: the attribution gate requires every cited chunk's subject to match the queried patient, so no generative answer over identified data can span patients. Cohort work routes through de-identification by construction, not by policy.

### 5.12 Trial pre-screening
Match against inclusion/exclusion criteria, output a coordinator worklist. Restricted role, distinct purpose tag, 45 CFR 164.512(i) basis.

### 5.13 Ingest data quality and mapping QA
Unmapped codes, terminology drift, duplicate patient records, external-record (Care Everywhere / C-CDA) reconciliation. Least regulated work in the register and it determines whether everything else is correct. Build it first within the release.

### 5.14 Ambient clinical documentation
Capture encounter audio, transcribe, structure, stage a note for signature. Depends on §6.5 (consent gate), §6.2 (STT egress preflight), §6.4 (write-back), and Invariants 13, 15, 17. Raw audio is retained under a schedule distinct from the note and is more sensitive than the note it produces, not less.

### 5.15 Hosted model governance
The substrate for a customer's own predictive models — including deterioration, readmission, and length-of-stay models the platform will not author. Provides: registry entry with intended use and validated population (§6.2); declared input-variable schema and 92.210 screen (§6.3); PHI egress preflight; hash-chained audit of every inference; drift and performance monitoring; constrained action space (Invariant 18); and the 31 source attributes plus IRM summary (§6.6). The customer supplies the model and its regulatory standing.

Readmission risk and length-of-stay estimation are frequently labeled operational, but they influence discharge planning and resource allocation, which makes them patient care decision support tools under 92.210 regardless of internal labeling. They route through this path, not around it.

### 5.16 EMR write-back
Staged-draft-plus-signature protocol, specified in §6.4 against Epic's verified write surface.

---

## 6. Cross-cutting subsystems

### 6.1 Sensitive-category segmentation engine

Determines what never enters the embedding corpus, and what is withheld from retrieval by jurisdiction.

**The governing finding, and it reshapes the design: FHIR security labels exist but are not populated in practice.** The mechanism is `Resource.meta.security`, bound extensibly to the `security-labels` value set, which imports Confidentiality codes (U/L/M/N/R/V) and the ActCode `InformationSensitivityPolicy` set (~49 concepts including ETH, SUD, HIV, PSY, MH, BH, GDIS, SEX, STD, PREGNANT, SDV, ADOL, MST, SICKLE, TBOO), with the HL7 Data Segmentation for Privacy IG as the implementation profile. **[VERIFIED]** But ONC's Interoperability Standards Platform rates the Security Label Sensitivity Tag at **Level 0/1 — not in a published USCDI version, "in limited use in production environments."** **[VERIFIED]**

Therefore: **an Epic corpus will arrive with `meta.security` overwhelmingly empty, and absence of a label must never be read as absence of sensitivity.** Exclusion is driven primarily by (i) resource type, (ii) code-system value-set membership — curated SNOMED / ICD-10 / LOINC / RxNorm sets per category, (iii) source department or compartment, with `meta.security` honored when present as an additional signal only. Fail-closed: an unclassifiable resource is excluded and counted, never included by default.

**Categories and basis:**

| Category | Basis | Status |
|---|---|---|
| Psychotherapy notes | HIPAA; existing invariant | Excluded at serialization |
| SUD records from Part 2 programs | 42 CFR Part 2 | Excluded; separate consent lineage |
| Reproductive / gender-affirming care | Cal. Civ. Code § 56.101(c) (AB 352) | Excluded + geo-gate |
| HIV/AIDS | N.Y. PHL § 2782; Tex. Health & Safety § 81.103 | Excluded |
| Genetic | Alaska Stat. § 18.13.010 | Excluded |
| Mental health | 740 ILCS 110; Tex. Health & Safety ch. 611; Cal. Civ. Code § 56.104 | Excluded |
| Minor-consented services | State consent-age matrix | Excluded; matrix unresearched |

**42 CFR Part 2, 2024 final rule — [VERIFIED]:** published 16 Feb 2024 (89 FR 12472), effective 16 Apr 2024, **compliance date 16 Feb 2026 — already in force.** A single consent now covers future TPO uses (§ 2.31); a HIPAA covered entity receiving Part 2 records under TPO consent may redisclose per HIPAA permissions (§ 2.33) **except for use in civil, criminal, administrative, or legislative proceedings against the patient**; the § 2.32 notice must accompany disclosure. HITECH penalties now attach. Platform consequence: the absolute redisclosure bar is gone, but consent lineage must still be tracked per record, and a TPO-consented corpus is not lawful for arbitrary secondary use — so Part 2 provenance is a retained attribute, not a one-time ingest decision.

**California AB 352 is a machine mandate, not a policy one — [VERIFIED].** Since 1 July 2024, systems holding medical information must be able to limit user access privileges to reproductive/gender-affirming/contraception data, prevent out-of-state disclosure, **segregate** that information from the rest of the record, and automatically disable out-of-state access. This maps directly onto serialization-time exclusion plus a requester-geography gate on retrieval, and it is the single most concrete state requirement in this document. The geo-gate is evaluated on requester location at query time and fails closed.

*[UNVERIFIED]* Illinois and Texas citations were located via secondary sources without primary text retrieval. The minor-consent matrix is unresearched. Both are §11 items.

### 6.2 Model registry and egress preflight

Registry entry per model target: intended use, validated population, declared input-variable schema, PHI-eligibility with operator-supplied basis, training-corpus provenance for any fine-tuned artifact, and the machine-checkable preflight evidence below. Registration failure stops the run; there is no degraded mode.

**Speech-to-text preflight matrix — the new egress class. [VERIFIED except as noted]**

| | AWS Transcribe / Transcribe Medical | Azure AI Speech (docs now under Microsoft Foundry) | GCP Speech-to-Text |
|---|---|---|---|
| BAA coverage | List entry reads "AWS Transcribe [Includes HealthScribe]"; **Transcribe Medical not separately named — [UNVERIFIED]**, requires AWS written confirmation | **Not publicly citable** — deferred to auth-gated Service Trust Portal appendices | Named Covered Product; docs state not to opt into data logging under BAA |
| Default retention | Omitting `OutputBucketName` → service-managed bucket, **90 days** | Real-time: no data at rest. Audio/transcription logging off by default, opt-in, 30 days | None by default; async transcripts ~5 days |
| Machine-checkable? | **Yes — strongest of the three** | **Split** | **No** |
| Preflight | `organizations:DescribeEffectivePolicy` with `PolicyType=AISERVICES_OPT_OUT_POLICY`, parse for Transcribe opt-out; assert `OutputBucketName` + `OutputEncryptionKMSKeyId` set on every job | Custom endpoints: `GET /speechtotext/v3.2/endpoints/{id}` → assert `properties.contentLoggingEnabled == false` (endpoint-level overrides session-level). **Base-model real-time: not server-queryable** — logging is a per-request client flag; gateway-side code assertion plus operator attestation | Enrollment is a **project-level console toggle with no API-queryable property**. Operator attestation required. Assert non-global regional endpoint (`us-speech` / `eu-speech`) as a machine-checkable client-config check |
| Constraints | en-US only | No PHI-specific SKU/region gate documented — **do not assume the Azure OpenAI non-Global deployment rule transfers** | Medical models en-US only; global endpoint gives no residency guarantee |

**Two permanent known asymmetries, recorded as asymmetries rather than gaps:**
- **GCP speech data-logging enrollment exposes no queryable property.** Structurally identical to the already-known GCP abuse-logging-exemption asymmetry: real, permanent, unverifiable in code, requiring operator attestation where AWS is verifiable. Billing SKU is a weak inferential signal only, not proof.
- **Microsoft does not publish a citable BAA scope list.** AWS and GCP do. This is a documentation asymmetry that shifts an evidence burden onto the operator on Azure alone.

**Streaming plane.** Real-time feeds for hosted models have no clean-run boundary against which to advance a watermark. Streaming ingestion therefore maintains a separate checkpoint discipline — per-partition offsets with explicit gap accounting — and never shares the batch ETL watermark. A gap is surfaced loudly, not interpolated.

### 6.3 Fairness screen — 45 CFR 92.210 made structural

At registration: parse the declared input-variable schema; reject any model declaring a protected-class variable (race, color, national origin, sex, age, disability); flag declared proxy candidates for explicit operator justification recorded with a basis; require a fairness report disaggregating performance across the protected categories; store the mitigation record. At execution: unregistered model does not run. At runtime: monitor for drift in disaggregated performance and alert on divergence.

**Honest limit.** Excluding declared protected-class variables is mechanical. Identifying undeclared proxies is not, and the platform must not claim to have solved it. The defensible claim is narrower and still substantial: the platform forces the question to be asked, recorded, and justified at registration time, and produces the disaggregated evidence a covered entity needs to discharge its own ongoing identification-and-mitigation duty. Stated as a limitation in the runbook, not left to be discovered.

### 6.4 Write-back protocol and Epic capability matrix

**Epic R4 write surface — [VERIFIED]** against the live CapabilityStatement (Epic software version "August 2026"):

| Resource | Interactions |
|---|---|
| AllergyIntolerance | create |
| BodyStructure | create, update |
| Communication | create |
| Condition | **create only — no update** |
| DiagnosticReport | **update only** |
| DocumentReference | create, update |
| Observation | create, update |
| ConceptMap | create |

**[VERIFIED negative]: MedicationRequest, ServiceRequest, Encounter, Immunization, and Goal expose read and search only. MedicationRequest has no REST create.** Order writes exist only as CDS Hooks "unsigned order" suggestions — a different transport, not a POST. Any design assuming a medication write via FHIR REST against Epic is wrong.

Named write interfaces: DocumentReference Create Clinical Notes STU3 `845` / R4 `1046`; Document Information Create `10050` / Update `10051`; Observation Create Vital Signs R4 `963`; Lines/Drains/Airways R4 `962`. Flowsheet writes require customer-side flowsheet row ID mapping; documented failure codes `59188` / `59189`. DSTU2 is read-only; STU3 writes are a small subset; R4 carries nearly all writes. **[VERIFIED]**

**Auth and enablement — [VERIFIED].** SMART configuration declares `permission-v1` and `permission-v2`, so granular `.c` / `.u` / `.d` scopes are available, alongside `client_credentials` for backend services. Enablement chain: register app → sandbox → mark Ready for Production → **each customer signs the open.epic API Subscription Agreement** → staff holding the "Purchase Apps" security point downloads the client record → per-customer, per-environment secrets provisioned. The app cannot run in any customer environment, production or not, until marked ready for production. Write availability is per-resource *and per-flavor*: "DocumentReference create" is several distinct APIs with separate enablement, so the install runbook enumerates them individually.

**The open dependency, stated precisely — [UNVERIFIED].** Whether a note created via DocumentReference lands *unsigned in a clinician's signing queue* or lands *committed* could not be confirmed from public Epic documentation; the per-API spec bodies are login-gated. What is verified: `docstatus` is a supported search parameter, so Epic persists a docStatus distinction; and DocumentReference supports update, which is mechanically compatible with a preliminary→final transition. That is capability, not proof of the signature workflow.

**Consequence for the design.** Invariant 13 is satisfiable today only on the one verified human-signature-gated path — the CDS Hooks unsigned-order pattern. Until the DocumentReference question is resolved by an authenticated read of api=1046/845 or written Epic confirmation, **the write-back plane ships with document writes staged into a platform-side signature queue and committed to Epic only after the human signature event is recorded on our side.** That is strictly more conservative than relying on an unverified Epic-side behavior, and it degrades gracefully to the native workflow once confirmed. §11 carries the resolution action.

Throughput and rate limits are undocumented publicly **[UNVERIFIED]**; the write path implements backoff and a bounded queue regardless.

### 6.5 Ambient consent gate

Per-encounter precondition, deny-by-default, keyed on **jurisdiction and modality** — the modality dependence is real and is missed by most implementations.

**All-party consent for in-person oral communications — [VERIFIED by two-source statutory citation, not primary-text read]:** California (Penal Code § 632), Delaware (11 Del. C. §§ 1335(a)(4), 2402(c)(4)), Florida (§ 934.03(2)(d), felony exposure), Illinois (720 ILCS 5/14-2(a)(1)), Maryland (Cts. & Jud. Proc. § 10-402(c)(3)), Massachusetts (ch. 272 § 99(C)(1), bars secret recording specifically), Montana (§ 45-8-213, notice-based), New Hampshire (RSA 570-A:2), Pennsylvania (18 Pa. C.S. §§ 5703, 5704), Washington (RCW 9.73.030).

**Split-rule states — where telehealth and in-office diverge:**
- **Oregon** — all-party *notice* in person (ORS 165.540(1)(c)); one-party for telecommunications (ORS 165.540(1)(a)). Upheld en banc, *Project Veritas v. Schmidt* (9th Cir. Jan. 2025), cert. denied Oct. 2025.
- **Connecticut** — one-party in person; all-party telephonic (Conn. Gen. Stat. § 52-570d).
- **Nevada** — one-party in person (NRS 200.650); all-party by telephone (*Lane v. Allstate*, 114 Nev. 1176 (1998)).

**Michigan is unsettled and treated as deny.** MCL 750.539c reads all-party but courts have applied a participant exception since *Sullivan v. Gray* (1982). Missouri appears on some secondary charts; § 542.402 is one-party and the all-party reading is not relied on.

**No state creates a healthcare exemption to its wiretap statute — [VERIFIED by targeted search].** The direction runs the other way: a clinical encounter is the paradigm "confidential communication," which strengthens a § 632-type claim. Overlays that add obligations: California CMIA, Washington My Health My Data Act. HIPAA does not authorize recording; it governs use and disclosure afterward. Live CIPA class actions against Sharp HealthCare, Sutter Health, and MemorialCare over ambient scribes are the current test and are tracked as a changing-law item.

**Consent implementation, three layers, all required:** (1) registration/annual packet — weakest, and the pending litigation theory is precisely that a buried checkbox is not consent for a specific recorded encounter; (2) **visit-level verbal attestation captured at the head of the recording** — strongest evidentiary posture, because consent and recording share one artifact and one timestamp; (3) structured consent state recorded discretely — status, timestamp, obtainer, revocation — never free-text. Revocation deletes the audio and the derived transcript under the retention schedule and is audited.

### 6.6 Source attributes and IRM artifact — HTI-1

**Who is obligated — [VERIFIED], and this closes the question.** The § 170.315(b)(11) obligations run **only to certified health IT developers, and only for predictive DSIs they themselves supply** in a certified Module. ONC/ASTP guidance states that predictive DSIs developed by a health system or a third-party technology company are not subject to (b)(11) unless a certified developer supplies them as part of a certified Module, and that developers need not review risk-management information from other parties. **A bring-your-own-infrastructure platform that is not itself certified health IT has no direct obligation here.**

**But there is a pass-through, and it is why we build it anyway.** § 170.315(b)(11)(v)(B) requires the certified Module to let users **record, change, and access source attributes for predictive DSIs developed by other parties or self-developed by the customer.** So a customer surfacing our output through a certified EHR has fields to populate and will demand that content from us contractually. Shipping it is not a legal requirement; it is the price of admission to certified-EHR deployments.

**What we ship:** the **31 predictive source attributes across 9 categories** — details/output, purpose, cautioned out-of-scope use, development data and input features, fairness in development, external validation, quantitative performance measures, ongoing maintenance of validity and fairness, and update/continued-validation schedule — plus an **IRM summary** covering risk analysis across the eight named characteristics (validity, reliability, robustness, fairness, intelligibility, safety, security, privacy), risk mitigation, and governance of how data are acquired, managed, and used. Published at a hyperlink accessible without preconditions, mirroring § 170.523(f)(1)(xxi). **[VERIFIED]**; per-category item counts are **[UNVERIFIED]** and must be confirmed against eCFR text before being coded to.

**Non-obvious consequence worth deciding deliberately.** § 170.102 defines a Predictive DSI as technology supporting decision-making based on algorithms or models that derive relationships **from training data** and produce a **prediction, classification, recommendation, evaluation, or analysis**. That definition is broad enough to capture a retrieval-grounded generative assistant producing "analysis" — it does not turn on risk level or on "AI" branding. **We therefore populate source attributes for the assistant itself (5.1), not only for hosted models (5.15).** Cheap to do, and the alternative is arguing the point under a customer's procurement review.

### 6.7 EMR conformance probe

Non-Epic EMRs are not assumed to expose comparable resources. At install, the platform reads the target's CapabilityStatement and produces a conformance matrix: which resources support read/search/create/update, which sensitive-category signals are populated, whether `meta.security` appears at all, and which serialization templates are therefore unsupported. Capabilities whose dependencies are unmet are **disabled explicitly with a named reason**, never silently degraded — this is the no-silent-fallback invariant applied to portability. The matrix is a runbook artifact and a support input.

### 6.8 Audit

Unchanged in principle, extended in coverage: every AI interaction, every registration decision, every preflight verdict, every consent evaluation, every signature event, every operator override of an action-space constraint, and every geo-gate refusal is an appended, hash-chained event with requester identity, purpose tag, object keys (keys only, never content), model target, token counts, response hash, and gate verdict.

---

## 7. Synthetic test data

**The platform is developed with no access to real patient data at any point.** That makes the test corpus not a convenience but the entire evidence base behind every acceptance claim in §10. Its provenance is therefore held to the same standard as regulatory claims: every calibration figure cites a primary source or is labeled unverified, and the method is published.

### 7.1 Rules

**R1.** No real patient data enters the repository or any development or test environment — including data described as de-identified. There is no exception path.

**R2.** Every generated resource carries an explicit synthetic marker: `meta.tag` = `HTEST` from `http://terminology.hl7.org/CodeSystem/v3-ActReason`. **Synthea does not emit `meta.tag` [VERIFIED]** — its only de facto markers are narrative text reading "Generated by Synthea…" and `Patient.identifier.system = https://github.com/synthetichealth/synthea` — so the tag is applied by our post-processing step. A fixture test fails the build if any fixture lacks it.

**R3.** Every fixture is reproducible from (generator, pinned version, seed). Synthea's own guidance is that populations generated with the same seed *and the same version* are identical, and its default seed is the wall clock if unset **[VERIFIED]** — so the release is pinned and `-s` is always passed explicitly. Never rely on the default.

**R4.** Each fixture set carries a provenance manifest: generator and version, seed, exact command line, calibration sources, and the invariant or acceptance criterion the fixture exercises.

**R5.** No GitHub Actions (project constraint). The synthetic-marker check, manifest validation, and fixture regeneration run as a local pre-push script and a test target, documented in the runbook.

### 7.2 The corpus is layered, because no single source covers it

**Layer 1 — wire-format realism: Epic's FHIR sandbox.**
Named test patients **[VERIFIED]**: Anna Cadence, Henry Clin Doc, John Grand Central, Omar Optime, Kyle Nelson; MyChart users Derrick Lin, Camilla Lopez, Desiree Powell, Olivia Roberts. Epic's published position is that the sandbox is for sample or synthetic data only and that Epic may wipe any individually identifiable data discovered there **[VERIFIED]** — note this is a policy prohibition plus a remediation right, **not a technical guarantee**, and our own writes could introduce PHI, so we enforce the non-PHI property on our side as well.
Per-patient volume is **[UNVERIFIED]** — Epic publishes no resource counts, encounter counts, or history spans — but it is demo-scale. **Use for conformance, auth, search-parameter, and citation-key plumbing. Do not use for longitudinal retrieval, cohort, or recall/precision testing**, and do not let any temporal-reasoning coverage depend on it.

**Layer 2 — longitudinal depth: Synthea.**
v4.0.0 (5 Mar 2026), Apache-2.0, FHIR R4 with the US Core IG applied via `exporter.fhir.use_us_core_ig`; supported US Core targets v3.1.1 through v7.0.0, default 6.1.0 **[VERIFIED]**. Conformance is asserted by writing `meta.profile` URLs plus a mapping table — **not validator-proven [VERIFIED]** — so our own conformance check runs independently rather than trusting the flag.
Seeding flags: `-s` (population), `-cs` (clinician), `-ps` (single person), `-r`/`-e` (reference/end date), `-p`, `-a`, `-g`.
Modules are state machines built from US Census demographics, CDC prevalence and incidence, NIH reports, and published care maps (Walonoski et al., JAMIA 2018, doi:10.1093/jamia/ocx079) **[VERIFIED]**.
**Calibration honesty, and this constrains what we may claim:** module sources are recorded as free-text remarks rather than machine-readable metadata, and their quality is uneven — verified examples cite CDC MMWR and AAAAI alongside consumer-health sites. The Synthea paper itself reports synthetic patients were roughly **4000× more likely to undergo diabetes-related amputation** than national rates. Synthea is therefore **directionally, not quantitatively, calibrated**. It supplies volume and structure. It does not support an epidemiological claim, and the documentation must not make one.

**Layer 3 — distribution calibration from free, redistribution-safe federal sources.**

| Source | Supplies | Access | Note |
|---|---|---|---|
| **NHANES** (CDC/NCHS) | Lab value distributions — CBC, CMP, HbA1c, lipids, creatinine/eGFR, urinalysis — plus biometrics | Free, click-through DUA, stable `.XPT` URLs, scriptable | Primary source for realistic lab values |
| **NAMCS / NHAMCS** (CDC/NCHS) | Diagnosis mix, medications per visit (up to 30), up to 5 ICD-10-CM dx, reason-for-visit, visit patterns | Free public-use files, no registration, open HTTPS directory | Primary source for encounter shape |
| **CDC WONDER** | Age/sex-conditioned prevalence priors | Free, XML POST API | **Constraint: no publishing statistics based on counts ≤ 9** — binds any derived table we commit |
| **CMS DE-SynPUF** | Claims *schema and format* fixtures | Free, click-through agreement, JSON API | CMS states its inferential value is very limited — co-occurrence was deliberately perturbed. **Format only, never calibration** |

**HCUP NIS is excluded** — purchase, signed DUA, and mandatory training make it incompatible with an open-source repository. If inpatient marginals are needed, use the free HCUPnet published aggregates only. **[VERIFIED]**

**Layer 4 — adversarial hand-authored fixtures.** This is the layer that actually tests the invariants, and **Synthea cannot produce any of it [all VERIFIED]**: it emits no `meta.security` at all; `Condition.verificationStatus` and `AllergyIntolerance.verificationStatus` are hardcoded to `confirmed`; there are no mental-health modules whatsoever, so no psychotherapy notes; opioid addiction is modeled as ordinary conditions and medications with no Part 2 semantics; all codes are standard, so there are no local or unmapped codes; and every clinical note renders from a single FreeMarker template, so there are no dictation artifacts, abbreviations, negation, copy-forward, or clinician voice variation.

| Fixture class | Exercises |
|---|---|
| `refuted` / `entered-in-error` Condition and AllergyIntolerance | Status-inversion rate (§10) |
| Resolved vs active conditions with overlapping dates | Temporal weighting, 5.1(e) |
| Every §6.1 sensitive category, in **two variants — `meta.security` populated, and stripped** | §6.1 recall. The stripped variant is the production condition |
| Psychotherapy notes | Serialization-time exclusion |
| Part 2-sourced records with consent lineage | §6.1 provenance retention |
| AB 352 categories with an out-of-state requester | Geo-gate refusal |
| Cross-patient near-duplicates (same name and DOB, different subject) | Attribution-gate false negatives |
| Encounter-reference mismatch | Attribution gate, encounter arm |
| Local/proprietary codes, terminology drift, duplicate patient records | 5.13. Synthea's `-f` fixed records and `exporter.split_records` partially cover this — its identity model produces nicknames, typos, stale addresses, and DOB discrepancies across providers **[VERIFIED]**; we author the input JSON |
| Narrative with embedded identifiers | De-identification pass |
| Multi-year spans exceeding any retrieval window | 5.1(g) structured spine |
| `medication[x]` as CodeableConcept **and** as Reference; Encounter reason coded **and** referenced | Consumer must-support — servers need support only one form, so we must handle both |
| "No known allergy" (SNOMED 716186003) vs "not asked" (1631000175102) | Semantically different; must not collapse |
| Resources with must-support elements absent | Must process without error and never render absence as a clinical negative |

### 7.3 US Core conformance target

Current US Core is **v9.0.0 (STU 9), targeting FHIR R4 4.0.1; every published US Core version targets R4 and there is no R5 US Core [VERIFIED]**. **We support v3.1.1 — the ONC certification baseline, still widely implemented — and current, simultaneously**, because real Epic endpoints will not all be on 9.0.0.

Consumer Must Support obligations, which become spec rules **[VERIFIED]**: process instances containing must-support elements without erroring; interpret a missing element as *data not present in the responder's system* — **not an error, and not a clinical negative**. US Core's "Suppressed Data" guidance further directs omitting optional elements rather than masking, and warns that `dataAbsentReason = masked` can itself leak beyond the recipient's access rights. **Consequence: absence is never reasoned over as a negative finding anywhere in the platform**, and that rule has its own fixture class above.

**Verified negative that confirms §6.1: US Core does not profile `meta.security`, does not require confidentiality labels, and does not reference DS4P.** Its security guidance only says implementers should be *aware of* FHIR core security labels, and points to Provenance instead. A design assuming sensitivity labels would therefore fail silently against fully *conformant* servers — §6.1's structural exclusion is required, not defense in depth.

### 7.4 Terminology: references, not content

Licensing dictates the architecture. **[VERIFIED unless noted]**

| Terminology | Commit to public repo? | Notes |
|---|---|---|
| **CPT** (AMA) | **Prohibited** | Separate paid distribution licence per product; UMLS Category 3. AMA also prohibits using the CPT data file to train or fine-tune AI models, while retrieval-based reference is covered under licence |
| **SNOMED CT US** (NLM/Affiliate) | **Prohibited** as raw content | Free in the US member territory, but a public repo is extraction of a substantial portion. **Each downstream deployment needs its own UMLS/Affiliate licence — the project cannot confer it** |
| **RxNorm full release** | **Prohibited** | Embeds restricted proprietary sources. RxNav REST API is public. Whether a restriction-free redistributable subset exists is **[UNVERIFIED]** |
| **VSAC expansions** | **Treat as prohibited** | Expansions carry CPT/SNOMED/LOINC/RxNorm member codes. Value-set OIDs, versions, and metadata are safe to commit |
| **LOINC** | Permitted | Copyright notice and licence must accompany each copy; not relicensed under our OSS licence; field names unchanged |
| **ICD-10-CM / PCS** | Permitted | Take from CMS/CDC, **never from the UMLS copy**, which carries Category 4 restrictions |
| **CVX** (CDC) | Permitted | Public domain, with attribution and a no-endorsement disclaimer |
| **NDC** (openFDA) | Permitted | CC0 1.0 |

**Design consequence.** A single install-time loader with a source manifest carrying a license class and fetch method per terminology; UMLS/UTS API key operator-supplied; CPT is a **disabled-by-default capability requiring an operator-attested licence ID**. Absent credentials the loader fails loud — no silent fallback, per invariant.

This reaches back into 5.1: the RxNorm / SNOMED / LOINC query expansion in 5.1(c) depends on the loader, so **expansion coverage is a deployment-time property**, not a platform constant. The conformance probe (§6.7) reports which expansions are available, and a deployment without a SNOMED licence has subsumption expansion **disabled explicitly with a named reason**, never silently degraded. Retrieval quality acceptance numbers are therefore reported per expansion configuration.

Two runbook "Known gaps" entries follow directly: the AMA prohibition on training or fine-tuning against the CPT data file, and the fact that SNOMED sub-licensing obligations pass to each deployment and cannot be satisfied on their behalf.

### 7.5 MIMIC-IV: excluded, with a precise reason

MIMIC-IV is **real de-identified patient data** under the PhysioNet Credentialed Health Data Use Agreement 1.5.0, not synthetic data. Two clauses independently bar committing derived fixtures **[VERIFIED]**: DUA §3, "I will not share access to PhysioNet restricted data with anyone else"; and PhysioNet's derived-works guidance, that any derived datasets or models "should be treated as containing sensitive information" and shared on PhysioNet under the same agreement. **That is our own invariant restated by a third party — every derived artifact inherits its source's protection class.**

Gray zone **[UNVERIFIED]**: whether a high-level marginal statistic constitutes a derived dataset. No numerical threshold is published, and DUA §9 obligates open-sourcing the code behind publicly disseminated MIMIC results, which would expose the derivation. **Any MIMIC-derived number in a public repository requires a written determination, not a judgment call.**

Operating rule: MIMIC-IV may be used only as a private sanity check by a credentialed team member, and never as the cited provenance for a committed fixture.

**One usable exception:** the **MIMIC-IV Demo** (100 patients) is released under **Open Data Commons ODbL v1.0**, open access, no credentialing, and is redistributable subject to attribution and share-alike **[VERIFIED]**. ODbL share-alike on a derived database is a real interaction with the project's own OSS licence and is an open licensing decision, not an assumption — §11.

### 7.6 Documentation deliverable

`docs/TESTDATA.md`, published alongside the runbooks, containing: the rules in 7.1; the layer map with per-layer source citations and access terms; the exact generator commands with pinned versions and seeds; the fixture-class table mapping each fixture to the invariant or acceptance criterion it exercises; the terminology licence matrix and loader behavior; the exclusions (HCUP NIS, MIMIC-IV full) with their reasons; and a "Known gaps" section per project documentation discipline. Every calibration figure carries a source citation or an unverified label.

### 7.7 What synthetic data cannot establish

Stated here so a green test suite is never mistaken for validation. Synthea narratives are materially cleaner than Epic's; module calibration is directional, not quantitative; and the Layer 4 fixtures test the gates rather than the clinical distribution. **Every acceptance number in §10 produced against this corpus is a lower bound on error.** Gate behavior — refusals, exclusions, attribution, preflight — *is* fully testable synthetically and is where synthetic data carries real weight. Retrieval quality on real clinical narrative is not, and does not become so by adding fixtures.

---

## 8. Regulatory basis

**8.1 FDA — Clinical Decision Support Software. [VERIFIED, read from the guidance document.]** Final guidance issued **January 2026**, superseding the 2022 version; FDA held a public town hall on it 11 March 2026. All four § 520(o)(1)(E) criteria must be met for exclusion from the device definition:

1. Not intended to acquire, process, or analyze a medical image, a signal from an in vitro diagnostic device, **or a pattern or signal from a signal acquisition system**.
2. Intended to display, analyze, or print medical information about a patient or other medical information.
3. Intended to support or provide recommendations to a health care professional about prevention, diagnosis, or treatment — and, per the guidance, **not to provide "a specific preventive, diagnostic or treatment output or directive."**
4. Intended to enable the professional to **independently review the basis** for the recommendation so as not to rely primarily on it.

The guidance notes that **automation bias intensifies in time-critical situations**, where insufficient time for adequate independent review may cause Criterion 4 to fail. It also indicates enforcement discretion where only one clinically appropriate option exists and the other criteria are met *(this last point is summarized rather than quoted — confirm before any customer-facing use)*.

**Why this produces Invariants 16 and 19, structurally.** A bedside deterioration model consuming continuous monitor vitals fails **Criterion 1** outright — it processes a pattern from a signal acquisition system — before the analysis even reaches reviewability; and it fails **Criterion 4** because a sepsis alert is definitionally time-critical. A differential-diagnosis generator fails **Criterion 3** by producing a specific diagnostic output. Meanwhile Criterion 4 is exactly the criterion the citation-to-storage-key design satisfies, and exactly the criterion a gradient-boosted probability cannot satisfy, because a risk score has no reviewable basis to display. This is the structural reason the platform's retrieval work is non-device and authored prediction would not be.

**8.2 Section 1557 — 45 CFR 92.210. [VERIFIED, eCFR current text.]** Covered entities must not discriminate on the basis of race, color, national origin, sex, age, or disability through patient care decision support tools, and have an **ongoing duty to make reasonable efforts to identify** uses of such tools employing variables measuring those categories and to **make reasonable efforts to mitigate** the resulting discrimination risk. Published 6 May 2024 (89 FR 37692), effective 5 July 2024. "Patient care decision support tool" is broader than "AI" and broader than "clinical," which is why 5.6, 5.7, and 5.8 are in scope. Implemented as §6.3 and Invariants 14 and 18.

**8.3 42 CFR Part 2. [VERIFIED.]** See §6.1. Compliance date 16 February 2026 — in force.

**8.4 State segmentation law.** California Civ. Code § 56.101(c) / AB 352 **[VERIFIED]**; N.Y. PHL § 2782 **[VERIFIED]**; Alaska Stat. § 18.13.010 **[VERIFIED]**; Cal. Civ. Code § 56.104 **[VERIFIED]**; Illinois 740 ILCS 110 and 410 ILCS 305, Texas Health & Safety §§ 81.103 and ch. 611 **[UNVERIFIED — secondary source, primary text not retrieved]**. Minor-consent matrix unresearched.

**8.5 Recording consent.** See §6.5. State list **[VERIFIED by two-source statutory citation]**, not primary-text read. No healthcare exemption found in any state **[VERIFIED by targeted search]**. Active litigation makes this a monitored item.

**8.6 ASTP/ONC HTI-1 — 45 CFR 170.315(b)(11), 170.102, 170.523(f)(1)(xxi). [VERIFIED.]** Applicability question closed in §6.6: no direct obligation on a non-certified platform; contractual pass-through via (b)(11)(v)(B). Certification required by 31 Dec 2024; (b)(11) entered the Base EHR definition 1 Jan 2025. Whether later HTI rules amended these dates is **[UNVERIFIED]**.

**8.7 45 CFR 164.512(i)** — review preparatory to research, basis for 5.12. **[VERIFIED citation; required representations not yet drafted.]**

**8.8 45 CFR 164.526** — right of amendment. One of the reasons fine-tuning a generative model on identified PHI is architecturally untenable (§5.1): weights cannot be amended per-patient and are not rebuildable from the storage backend.

**8.9 Coding exposure.** OIG and False Claims Act exposure profile for AI-assisted coding **[UNVERIFIED]**. §5.5 is written conservatively so that the answer does not change the design, only the marketing language.

---

## 9. Cost model

Parameterized rather than priced: unit prices change and a spec that hardcodes them goes stale silently. The operator supplies current unit costs and applies the formulas below. `docs/COST.md` carries the AWS line items with their rates and the date they were checked; no calculator ships with the platform.

**Corpus and embedding.** Chunks ≈ *P* patients × *R* resources/patient × *C* chunks/resource. One-time embedding cost = chunks × mean tokens/chunk (budget ~250) × embed unit price. **The dominant recurring term is re-embedding on serialization-template revision**, which is a full-corpus cost each time — so template version churn is a budget line, not a refactor detail, and batching template changes into scheduled revisions is a cost control worth designing for.

**Vector storage.** dimension × 4 bytes × chunks, plus index overhead (HNSW typically 1.5–2× raw vectors). Sizing is deterministic once *P·R·C* is known.

**Query.** (retrieval *k* × mean chunk tokens) + spine tokens + prompt overhead + output tokens, per query, × query volume. The 5.1(g) structured spine raises per-query input tokens and lowers error rate; that tradeoff is explicit and should be measured, not assumed.

**Ambient (5.14).** Audio minutes × STT unit price + audio storage GB-months at the retention schedule + note-generation tokens. Audio storage is the term most often underestimated because retention is set by policy rather than by cost.

**Governance (5.15).** Per-inference audit write, monitoring compute, and periodic fairness-report regeneration. Small per unit, non-zero at volume.

Sensitivity note: *C* (chunks per resource) is the parameter with the widest plausible range and the largest downstream effect on every other term. It should be measured against a real corpus during the pilot before any capacity commitment is made.

---

## 10. Validation and acceptance

All gold sets are built from the layered synthetic corpus specified in §7. No real patient data is used at any point, so eval artifacts, failure dumps, and regression fixtures are never PHI stores by construction — not by policy. Read §7.7 before quoting any number from this section: gate behavior is fully testable synthetically; retrieval quality on real clinical narrative is not.

**Retrieval and answer quality (5.1, 5.2):**
- Retrieval recall@k against known-answer resources.
- **Silent omission rate** — missed active medication, allergy, or abnormal result. This is the dangerous failure mode and it is invisible to hallucination metrics. Primary acceptance metric.
- **Status-inversion rate** — resolved / refuted / entered-in-error content presented as active. Target zero.
- **Attribution-gate false negatives** — a wrong-patient chunk that passed. The gate is deterministic, so any non-zero result is a bug, not a tuning parameter.
- Abstention correctness — abstained when it should, answered when it could.

**Segmentation (6.1):** recall of each sensitive category against a labeled corpus, measured with `meta.security` deliberately stripped, since that is the production condition. Fail-closed behavior verified by injecting unclassifiable resources.

**Gates:** consent gate refuses on every deny-list jurisdiction and on unresolved jurisdiction; egress preflight refuses on every negative evidence case per cloud; unregistered model refuses to execute; action-space constraint refuses without a logged override; geo-gate refuses out-of-state access to AB 352 categories.

**Triage (5.6):** recall on the urgent class is the published operating metric, with the miss rate monitored and reported rather than implicit.

**Real-data validation cannot close in this release, and no amount of specification changes that.** Synthetic scores are not predictive: Synthea renders every note from one template, so its narrative is materially cleaner than Epic's, and its module calibration is directional (§7.2). A supervised, audited evaluation inside a pilot site — silent-omission and status-inversion rates re-measured on real data, and *C* measured for §9 — remains required before general availability and requires a customer with data access. Everything else in this specification ships in the first release; this one item is bounded, named, and carried openly rather than papered over. The release is complete against the synthetic corpus; it is not validated against reality until that pilot runs.

---

## 11. Open dependencies

Everything below is bounded, with the action that resolves it. Nothing here blocks specification; several block shipping a specific capability, noted.

| # | Item | Resolution | Blocks |
|---|---|---|---|
| 1 | Epic DocumentReference draft-vs-committed behavior | Authenticated read of Epic API spec 1046/845, or written Epic confirmation | Nothing — §6.4 ships the conservative platform-side signature queue meanwhile |
| 2 | AWS BAA coverage of Transcribe **Medical** specifically | Written confirmation from AWS | 5.14 on AWS |
| 3 | Azure BAA scope for AI Speech | Service Trust Portal appendices under NDA | 5.14 on Azure |
| 4 | Illinois / Texas segmentation statutes | Primary-text retrieval | Nothing — treated as excluded meanwhile |
| 5 | Minor-consent state matrix | Research task | Nothing — treated as excluded meanwhile |
| 6 | HTI-1 per-category attribute counts | eCFR text confirmation before coding to them | §6.6 schema finalization |
| 7 | Counsel sign-off: coding exposure (5.5), recording consent (6.5), state segmentation (6.1) | Customer counsel; not closable by us | Customer-facing claims only |
| 8 | Epic write throughput limits | Undocumented publicly; measure in sandbox | Nothing — backoff and bounded queue regardless |
| 9 | Non-Epic FHIR conformance | §6.7 probe measures it per install | Capability availability per install, by design |
| 10 | Real-data validation (§10) | Requires a customer with data access — cannot close internally | General availability only |
| 11 | MIMIC-IV Demo ODbL v1.0 share-alike vs the project's OSS licence | Licensing decision before any Demo-derived fixture is committed | Use of that fixture set only |
| 12 | Whether a restriction-free redistributable RxNorm subset exists | NLM confirmation | Nothing — loader-only path assumed meanwhile |
| 13 | Whether Synthea v4.0.0 US Core output passes Inferno/HAPI validation | Run the validator against pinned output | Nothing — §7.2 runs an independent conformance check regardless |

**Known asymmetries** — permanent platform differences, recorded as asymmetries rather than gaps: GCP Cloud SQL IAM auth collapses index/OMOP/AI role separation into one identity; GCP zero-data-retention rests on a form-granted abuse-logging exemption exposing no queryable property; **GCP speech data-logging enrollment likewise exposes no queryable property**; **Microsoft publishes no citable BAA scope list where AWS and GCP do**. Each requires an operator attestation on that cloud alone and is documented as such in the runbook, not presented as a defect.

**Changing-law watch:** CIPA class actions over ambient scribes (Sharp, Sutter, MemorialCare); any HTI successor rule amending (b)(11); FDA CDS enforcement practice under the January 2026 guidance.
