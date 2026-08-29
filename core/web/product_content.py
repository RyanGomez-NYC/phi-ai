# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Copy for the v1 product screens (core/web/product_routes.py).

Transcribed from the adopted v1 product design (PHI AI v1.dc.html) and
kept as data rather than markup so one template renders every generic
screen - the same shape the design itself used. Everything here is
DESIGN-STATE content: where a screen has live platform data behind it
(ingest stats, cohort counts, the consent gate, the audit trail), the
route injects it and says so; where the platform capability is a decision
core without a workflow yet (inbox triage, no-show, ambient capture), the
screen presents the spec's worked example against the synthetic corpus
and labels itself accordingly.

Section kinds: stats | table | list | callout.
Cell values may be dicts {t, cls} to carry a verdict color class.
"""

from __future__ import annotations


def T(t: str, cls: str = "") -> dict:
    return {"t": t, "cls": cls}


def M(t: str, cls: str = "v-mute") -> dict:
    return {"t": t, "cls": ("mono " + cls).strip()}


def stats(head: str, items: list[dict]) -> dict:
    return {"kind": "stats", "head": head, "items": items}


def table(head: str, cols: list[str], rows: list[list[dict]]) -> dict:
    return {"kind": "table", "head": head, "cols": cols, "rows": rows}


def listing(head: str, items: list[dict]) -> dict:
    return {"kind": "list", "head": head, "items": items}


def callout(head: str, title: str, body: str, warn: bool = False) -> dict:
    return {"kind": "callout", "head": head, "title": title, "body": body, "warn": warn}


PAGES: dict[str, dict] = {
    "summary": {
        "title": "Encounter & longitudinal summarization",
        "lede": (
            "Built on the deterministic structured spine, not on top-k text. The "
            "failure mode instrumented is silent omission — a missed active "
            "medication, allergy or abnormal result — because that failure is "
            "invisible to hallucination metrics and is the one that hurts a patient."
        ),
        "invariants": ["5.1(g) spine", "silent-omission = primary metric", "absence ≠ negative"],
        "sections": [
            stats("Current window", [
                {"v": "0.4%", "k": "silent-omission rate, synthetic corpus — lower bound on error", "cls": "warn"},
                {"v": "0", "k": "status inversions (resolved/refuted presented as active)", "cls": "good"},
                {"v": "14", "k": "structured resources in the spine", "cls": ""},
                {"v": "6", "k": "narrative chunks filling context", "cls": ""},
            ]),
            listing("What the spine assembles first", [
                {"t": "Problems, medications, allergies, recent labs with trends, procedures, encounters",
                 "b": "Directly from structured resources through a deterministic versioned template. "
                      "Retrieval then fills narrative context around that skeleton rather than being "
                      "asked to reconstruct it."},
                {"t": "Status and negation survive into chunk text",
                 "b": "Condition.clinicalStatus and verificationStatus, MedicationRequest.status, "
                      "AllergyIntolerance.verificationStatus, DocumentReference.status. A penicillin "
                      "allergy marked refuted that serialises as 'penicillin allergy' is a "
                      "data-integrity failure that looks exactly like a good retrieval, so it has its "
                      "own test suite."},
                {"t": "Temporal weighting against the question's time anchor",
                 "b": "A resolved 2019 problem must never outrank the active list. Weighting is by "
                      "effective date, not by embedding similarity alone."},
            ]),
            callout("Honest limit", "Synthetic scores are not predictive.",
                    "Synthea renders every note from one template, so its narrative is materially "
                    "cleaner than Epic's, and module calibration is directional rather than "
                    "quantitative. Every acceptance number here is a lower bound on error until "
                    "re-measured inside a pilot on real data.", warn=True),
        ],
    },

    "inbox": {
        "title": "Inbox & message triage",
        "lede": (
            "Routing plus a draft, never an auto-send. The operating point is chosen on "
            "recall of the urgent class, because a missed urgent message is a "
            "patient-safety event rather than a precision metric — and the miss rate on "
            "that class is published and monitored rather than left implicit in a threshold."
        ),
        "invariants": ["the signature rule — human release gate", "registered under 6.3", "biased toward escalation"],
        "sections": [
            table("Queue — worked example, synthetic corpus", ["Message", "Class", "Draft reply", "State"], [
                [T("Chest tightness since last night, worse on stairs"), M("urgent", "v-warn"),
                 T("Escalation draft — advises same-day contact, no clinical advice added"), M("awaiting release", "v-amber")],
                [T("Refill request — lisinopril"), M("routine"),
                 T("Draft references the active prescription and last fill date, both cited"), M("awaiting release", "v-amber")],
                [T("Question about the A1c result from Tuesday"), M("routine"),
                 T("Draft restates the released result and the follow-up already documented"), M("released 14:02", "v-good")],
                [T("Insurance letter attached, unclear ask"), M("unclassified", "v-warn"),
                 T("No draft — routed to a human, not guessed"), M("routed", "v-mute")],
            ]),
            listing("Two constraints the market form omits", [
                {"t": "Conservatively biased toward escalation",
                 "b": "The threshold is set on urgent-class recall, and the resulting false-positive "
                      "load on staff is a stated cost rather than a hidden one. Publishing the miss "
                      "rate is what makes the tradeoff reviewable by the people who bear it."},
                {"t": "Registered like any other decision support tool",
                 "b": "Triage priority whose effect varies by protected class or a proxy is squarely "
                      "within 45 CFR 92.210, so this model carries a registry entry, a declared "
                      "input-variable schema and a fairness report — the same gate a clinical risk "
                      "model passes."},
            ]),
        ],
    },

    "ambient": {
        "title": "Ambient clinical documentation",
        "lede": (
            "Capture encounter audio, transcribe, structure, stage a note for signature. "
            "Raw audio is a first-class PHI store with its own CMK, its own grant and its "
            "own retention schedule — more sensitive than the note it produces, not less."
        ),
        "invariants": ["audio is PHI, stored as PHI", "signature commits the record", "6.5 consent gate"],
        "sections": [
            table("This encounter — worked example", ["Stage", "Detail", "State"], [
                [T("Consent evaluation"), T("California, in person, all-party — visit-level attestation captured at the head of the recording"), M("allow", "v-good")],
                [T("Capture"), T("18m 41s — own CMK alias/phi-ai-ambient — retention 90d, distinct from the note"), M("complete", "v-good")],
                [T("STT egress"), T("Preflight passed on this cloud; output bucket and KMS key asserted on the job"), M("complete", "v-good")],
                [T("Structuring"), T("Draft note plus 3 candidate structured findings, each carrying the transcript span it came from"), M("staged", "v-amber")],
                [T("Signature"), T("Waiting on the clinician. No auto-commit path exists."), M("blocked by design", "v-mute")],
            ]),
            callout("Revocation", "Revocation is a first-class operation.",
                    "It deletes the audio and the derived transcript under the retention schedule, "
                    "and the deletion is audited. Because every derived artifact inherits its "
                    "source's protection class, the transcript cannot outlive the consent that "
                    "produced it."),
        ],
    },

    "priorauth": {
        "title": "Prior authorization & appeals",
        "lede": (
            "Given a payer criteria set, retrieve and cite chart evidence for each "
            "criterion and assemble the packet. Highest ROI-to-friction ratio in the "
            "register, and the clearest demonstration of the citation architecture: a "
            "criterion with no supporting evidence is shown as unmet rather than argued around."
        ),
        "invariants": ["purpose — Payment", "every criterion cited or unmet", "signed-writes-only"],
        "sections": [
            table("Criteria — continuous glucose monitor, commercial payer",
                  ["Criterion", "Status", "Evidence"], [
                [T("Type 1 or type 2 diabetes diagnosis"), M("met", "v-good"), M("fhir/Condition/eC08a")],
                [T("Intensive insulin regimen or documented hypoglycaemia"), M("met", "v-good"), M("fhir/MedicationRequest/eM77a")],
                [T("Four or more daily glucose checks documented"), M("met", "v-good"), M("fhir/Observation/eO44a +11")],
                [T("Visit with the prescribing clinician in the last 6 months"), M("met", "v-good"), M("fhir/Encounter/eF41")],
                [T("Documented training on device use"), M("unmet", "v-warn"),
                 T("No resource supports this. Shown as a gap; nothing is inferred to fill it.")],
            ]),
            callout("Appeals", "The same pipeline, a different output template.",
                    "An appeal re-runs retrieval against the denial reason rather than the original "
                    "criteria set, and the packet carries the same citation keys — so the reviewer on "
                    "the payer side can check every assertion against a stable reference."),
        ],
    },

    "coding": {
        "title": "Documentation-gap detection",
        "lede": (
            "Gap detection with cited evidence. Not code recommendation. It surfaces where "
            "documentation is insufficient to support what was clinically done, and cites "
            "the thin resource — it does not rank candidate codes, does not order by "
            "reimbursement weight, and does not propose a code the existing documentation "
            "would not already support."
        ),
        "invariants": ["no code ranking", "no reimbursement ordering", "signed-writes-only"],
        "sections": [
            table("Open gaps — worked example", ["Encounter", "Gap", "Thin resource"], [
                [T("2026-06-14 — endocrinology"),
                 T("Diabetes documented without a complication status, while a nephropathy-consistent lab pattern is present in the same window"),
                 M("fhir/Condition/eC08a")],
                [T("2026-05-02 — inpatient"),
                 T("Procedure documented in narrative only; no coded Procedure resource exists"),
                 M("fhir/DocumentReference/eD90")],
                [T("2026-03-19 — office"),
                 T("Two problems addressed per the note; one has no corresponding assessment"),
                 M("fhir/Encounter/eF12")],
            ]),
            callout("Why the scope is drawn here",
                    "An AI that suggests higher-weight codes is a false-claims exposure generator.",
                    "'Supporting accurate coding' versus 'optimising revenue capture' is exactly the "
                    "distinction an auditor probes, so the capability is written conservatively "
                    "enough that the answer to the exposure question does not change the design — "
                    "only the marketing language.", warn=True),
        ],
    },

    "segmentation": {
        "title": "Sensitive-category segmentation",
        "lede": (
            "What never enters the embedding corpus, and what is withheld from retrieval "
            "by jurisdiction. The governing finding reshapes the design: FHIR security "
            "labels exist but are not populated in practice, so absence of a label is "
            "never read as absence of sensitivity."
        ),
        "invariants": ["excluded at serialization, not retrieval", "fail closed and count", "geo-gate fails closed"],
        "sections": [
            table("Categories in force", ["Category", "Basis", "Treatment"], [
                [T("Psychotherapy notes"), T("HIPAA — existing invariant"), M("excluded", "v-warn")],
                [T("SUD records, Part 2 programs"), T("42 CFR Part 2 — compliance date 16 Feb 2026, in force"), M("excluded", "v-warn")],
                [T("Reproductive / gender-affirming care"), T("Cal. Civ. Code § 56.101(c) (AB 352)"), M("excluded + geo-gate", "v-warn")],
                [T("HIV/AIDS"), T("N.Y. PHL § 2782 — Tex. Health & Safety § 81.103"), M("excluded", "v-warn")],
                [T("Genetic"), T("Alaska Stat. § 18.13.010"), M("excluded", "v-warn")],
                [T("Mental health"), T("740 ILCS 110 — Tex. Health & Safety ch. 611 — Cal. Civ. Code § 56.104"), M("excluded", "v-warn")],
                [T("Minor-consented services"), T("State consent-age matrix — unresearched, treated as excluded meanwhile"), M("excluded", "v-warn")],
            ]),
            stats("Exclusion signals, in priority order", [
                {"v": "resource type", "k": "first signal — structural, always available", "cls": "", "small": True},
                {"v": "value-set membership", "k": "curated SNOMED / ICD-10 / LOINC / RxNorm sets per category", "cls": "", "small": True},
                {"v": "department", "k": "source compartment where the EMR exposes it", "cls": "", "small": True},
                {"v": "meta.security", "k": "honoured when present — an additional signal only", "cls": "faint", "small": True},
            ]),
            callout("Why labels cannot be the mechanism",
                    "ONC rates the Security Label Sensitivity Tag at Level 0/1 — not in a published USCDI version, in limited use in production.",
                    "US Core does not profile meta.security, does not require confidentiality labels "
                    "and does not reference DS4P, so a design assuming sensitivity labels would fail "
                    "silently against fully conformant servers. Structural exclusion is required, not "
                    "defence in depth. An unclassifiable resource is excluded and counted.", warn=True),
            callout("AB 352 is a machine mandate",
                    "Segregation, and an out-of-state gate evaluated at query time.",
                    "Since 1 July 2024, systems holding this information must limit access "
                    "privileges, prevent out-of-state disclosure, segregate the information, and "
                    "automatically disable out-of-state access. That maps onto serialization-time "
                    "exclusion plus a requester-geography gate that fails closed — the single most "
                    "concrete state requirement in the specification."),
        ],
    },

    "noshow": {
        "title": "No-show risk & supportive intervention",
        "lede": (
            "Three structural constraints, in code rather than policy: protected-class "
            "variables and declared proxies excluded at the feature-schema level, a "
            "fairness report as a required registration artifact, and a constrained "
            "action space. Risk may buy a patient help. It may not cost them an appointment."
        ),
        "invariants": ["registered decision support only", "constrained actions only", "6.3 fairness screen"],
        "sections": [
            table("Declared input schema", ["Variable", "Verdict", "Basis"], [
                [T("Prior no-show count, 24 months"), M("permitted", "v-good"), T("Behavioural history, not a protected category")],
                [T("Appointment lead time"), M("permitted", "v-good"), T("Operational")],
                [T("Distance to clinic"), M("permitted", "v-good"), T("Operational — drives a transport offer")],
                [T("ZIP code in isolation"), M("proxy — justify", "v-amber"), T("Requires explicit operator justification recorded with a basis, not silent permission")],
                [T("Primary language / interpreter need"), M("proxy — justify", "v-amber"), T("Declared proxy candidate under 92.210")],
                [T("Race, sex, age, disability"), M("blocked", "v-warn"), T("A model declaring one does not register, and an unregistered model does not execute")],
            ]),
            table("Action space", ["Action", "Permitted", "Note"], [
                [T("Additional reminder"), M("yes", "v-good"), T("Supportive")],
                [T("Transport or telehealth offer"), M("yes", "v-good"), T("Supportive")],
                [T("Outreach call"), M("yes", "v-good"), T("Supportive")],
                [T("Double-booking"), M("override only", "v-warn"), T("Requires a logged operator override with a stated basis")],
                [T("Deprioritization or scheduling denial"), M("override only", "v-warn"),
                 T("Same. This is the difference between improving access and quietly penalising the patients who struggle to attend")],
            ]),
            callout("Honest limit",
                    "Excluding declared protected-class variables is mechanical. Identifying undeclared proxies is not.",
                    "The platform must not claim to have solved it. The defensible claim is narrower "
                    "and still substantial: the question is forced to be asked, recorded and "
                    "justified at registration, and the disaggregated evidence a covered entity "
                    "needs to discharge its own ongoing duty is produced. Stated as a limitation in "
                    "the runbook, not left to be discovered.", warn=True),
        ],
    },

    "ingest": {
        "title": "Ingest data quality & mapping QA",
        "lede": (
            "Unmapped codes, terminology drift, duplicate patient records, and "
            "external-record reconciliation. The least regulated work in the register, and "
            "it determines whether everything else is correct — so it is built first "
            "within the release."
        ),
        "invariants": ["idempotent ETL, watermark on clean runs", "storage always wins", "no silent degradation"],
        "sections": [
            # The route prepends a LIVE stats section from the reader when
            # the index is reachable; this static example follows it.
            table("Open findings — worked example", ["Finding", "Detail", "Effect"], [
                [T("Local procedure codes, cardiology"),
                 T("412 codes from one department map to no standard concept; OMOP rows land with a source value and no standard concept id"),
                 M("degraded", "v-amber")],
                [T("Terminology drift, RxNorm"),
                 T("Two ingredient concepts retired upstream since the pinned release; expansion coverage narrows until the loader is refreshed"),
                 M("degraded", "v-amber")],
                [T("Duplicate patients across sources"),
                 T("Candidates on name + DOB with differing addresses — the attribution gate treats each subject as distinct until reconciled"),
                 M("blocking", "v-warn")],
                [T("C-CDA external records"),
                 T("Care Everywhere documents reconciled; some carry no encounter linkage, so encounter-scoped retrieval will not surface them"),
                 M("named gap", "v-mute")],
            ]),
            callout("Streaming has no clean-run boundary",
                    "Real-time feeds keep their own checkpoint discipline.",
                    "Per-partition offsets with explicit gap accounting, never sharing the batch ETL "
                    "watermark. A gap is surfaced loudly rather than interpolated — an interpolated "
                    "gap is a silent data-integrity failure that looks like a healthy pipeline."),
        ],
    },

    "fairness": {
        "title": "Fairness screen — 45 CFR 92.210 made structural",
        "lede": (
            "At registration: parse the declared input-variable schema, reject any "
            "protected-class variable, flag declared proxy candidates for justification, "
            "require a disaggregated fairness report, store the mitigation record. At "
            "execution: an unregistered model does not run. At runtime: monitor "
            "disaggregated performance for drift."
        ),
        "invariants": ["the registered-decision-support rule", "unregistered → no execution", "ongoing duty is the deploying organization's"],
        "sections": [
            table("Disaggregated performance — no-show model v3",
                  ["Group", "AUC", "FPR", "Δ vs overall"], [
                [T("Overall"), M("0.74"), M("0.19"), M("—")],
                [T("Age 18–39"), M("0.72"), M("0.21"), M("+0.02", "v-amber")],
                [T("Age 65+"), M("0.76"), M("0.17"), M("−0.02", "v-good")],
                [T("Medicaid primary"), M("0.69"), M("0.27"), M("+0.08", "v-warn")],
                [T("Interpreter needed"), M("0.68"), M("0.29"), M("+0.10", "v-warn")],
            ]),
            listing("What the divergence triggers", [
                {"t": "A mitigation record, attached to the registry entry",
                 "b": "The two divergent strata are exactly where a proxy would show up first. "
                      "Because the action space is constrained, the consequence of a false positive "
                      "here is an extra reminder rather than a lost appointment — which is the "
                      "mitigation doing its work rather than a report saying it should."},
                {"t": "A monitored alert, not a one-time artifact",
                 "b": "Drift in disaggregated performance alerts on divergence; a fairness report "
                      "generated once at procurement and never regenerated is an attestation, not a "
                      "control."},
            ]),
            callout("Scope of the claim",
                    "The platform forces the question, produces the evidence, and does not claim to have solved proxy detection.",
                    "45 CFR 92.210 places an ongoing duty to identify and mitigate on the covered "
                    "entity. This screen is what makes that duty dischargeable — registration-time "
                    "refusal, recorded justification, disaggregated evidence — and the runbook says "
                    "plainly where it stops.", warn=True),
        ],
    },

    "attributes": {
        "title": "Source attributes & IRM artifact",
        "lede": (
            "Thirty-one predictive source attributes across nine categories, plus an "
            "intelligibility-and-risk-management summary, published at a hyperlink "
            "accessible without preconditions. A non-certified platform has no direct "
            "HTI-1 obligation here — this ships because it is the price of admission to "
            "certified-EHR deployments."
        ),
        "invariants": ["populated for the assistant too", "no preconditions on access", "counts unverified pending eCFR"],
        "sections": [
            table("Categories", ["Category", "What it records", "State"], [
                [T("Details & output"), T("What the DSI produces and in what form"), M("published", "v-good")],
                [T("Purpose"), T("Intended use, and the population it was validated on"), M("published", "v-good")],
                [T("Cautioned out-of-scope use"), T("Named uses the developer advises against — including any diagnostic directive"), M("published", "v-good")],
                [T("Development data & input features"), T("Corpus provenance and the declared input schema"), M("published", "v-good")],
                [T("Fairness in development"), T("The 92.210 screen and its mitigation record"), M("published", "v-good")],
                [T("External validation"), T("Sites and populations outside development"), M("pending pilot", "v-amber")],
                [T("Quantitative performance"), T("Measures, and the corpus they were measured on"), M("synthetic only", "v-amber")],
                [T("Ongoing maintenance of validity & fairness"), T("Monitoring cadence and alert thresholds"), M("published", "v-good")],
                [T("Update & continued-validation schedule"), T("When the model and its evidence are revisited"), M("published", "v-good")],
            ]),
            callout("Why the assistant is in scope for this",
                    "A Predictive DSI is technology deriving relationships from training data to produce a prediction, classification, recommendation, evaluation or analysis.",
                    "That definition is broad enough to capture a retrieval-grounded generative "
                    "assistant producing 'analysis', and it does not turn on risk level or on AI "
                    "branding. So source attributes are populated for the assistant itself, not only "
                    "for hosted models — cheap to do, and the alternative is arguing the point under "
                    "a health system's procurement review."),
            callout("Pass-through, stated precisely",
                    "The obligation runs to certified developers for DSIs they themselves supply.",
                    "A health system surfacing this platform's output through a certified EHR has fields "
                    "to populate under (b)(11)(v)(B) and will demand the content contractually. "
                    "Per-category item counts remain unverified and must be confirmed against eCFR "
                    "text before being coded to.", warn=True),
        ],
    },

    "conformance": {
        "title": "EMR conformance probe",
        "lede": (
            "At install, the platform reads the target's CapabilityStatement and produces "
            "a conformance matrix. Capabilities whose dependencies are unmet are disabled "
            "explicitly with a named reason, never silently degraded — the "
            "no-silent-fallback invariant applied to portability."
        ),
        "invariants": ["disabled with a named reason", "R4 carries nearly all writes", "expansion is deployment-time"],
        "sections": [
            table("Epic R4 write surface — read from the live CapabilityStatement",
                  ["Resource", "Interactions", "Consequence"], [
                [T("DocumentReference"), M("create, update"), T("The note path. Draft-vs-committed behaviour is the one open dependency")],
                [T("Observation"), M("create, update"), T("Vitals and flowsheet writes; needs site-side row ID mapping")],
                [T("Condition"), M("create only — no update", "v-amber"), T("A correction is a new resource, not an edit")],
                [T("DiagnosticReport"), M("update only", "v-amber"), T("No create path")],
                [T("AllergyIntolerance / Communication / ConceptMap"), M("create"), T("Available")],
                [T("MedicationRequest, ServiceRequest, Encounter, Immunization, Goal"), M("read, search only", "v-warn"),
                 T("Verified negative. Order writes exist only as CDS Hooks unsigned-order suggestions — a different transport, not a POST. Any design assuming a medication write via FHIR REST is wrong")],
            ]),
            table("Capability availability at this install", ["Capability", "State", "Named reason"], [
                [T("Ambient documentation"), M("enabled", "v-good"), T("Consent gate and STT preflight both satisfied on this cloud")],
                [T("SNOMED subsumption expansion"), M("disabled", "v-warn"), T("No UMLS/Affiliate licence supplied — sub-licensing obligations pass to each deployment and cannot be satisfied on its behalf")],
                [T("CPT-based retrieval"), M("disabled", "v-warn"), T("Requires an operator-attested licence ID. Disabled by default; the loader fails loud rather than degrading")],
                [T("Medication write-back"), M("unavailable", "v-warn"), T("The vendor exposes no create interaction for MedicationRequest")],
            ]),
            callout("Retrieval quality is a deployment property",
                    "Expansion coverage depends on the terminology loader.",
                    "RxNorm, SNOMED and LOINC query expansion is licence-gated, so acceptance "
                    "numbers are reported per expansion configuration rather than as a platform "
                    "constant. A deployment without a SNOMED licence has subsumption expansion "
                    "disabled explicitly, with the reason on this page."),
        ],
    },
}


# ---------------------------------------------------------------------------
# Bespoke-screen data that is copy rather than computation.
# ---------------------------------------------------------------------------

CONSENT_JURISDICTIONS: list[tuple[str, str]] = [
    # (display, code passed to the real gate; "" = unresolved)
    ("California", "CA"), ("Oregon", "OR"), ("Connecticut", "CT"),
    ("Nevada", "NV"), ("Michigan", "MI"), ("Washington", "WA"),
    ("Massachusetts", "MA"), ("Florida", "FL"), ("New York", "NY"),
    ("Texas", "TX"), ("Unresolved / multi-state", ""),
]

CONSENT_CITATIONS: dict[str, str] = {
    "CA": "Cal. Penal Code § 632 — overlay: CMIA",
    "DE": "11 Del. C. §§ 1335(a)(4), 2402(c)(4)",
    "FL": "Fla. Stat. § 934.03(2)(d) — felony exposure",
    "IL": "720 ILCS 5/14-2(a)(1)",
    "MD": "Md. Cts. & Jud. Proc. § 10-402(c)(3)",
    "MA": "Mass. ch. 272 § 99(C)(1) — bars secret recording specifically",
    "MT": "Mont. Code § 45-8-213 — notice-based",
    "NH": "N.H. RSA 570-A:2",
    "PA": "18 Pa. C.S. §§ 5703, 5704",
    "WA": "RCW 9.73.030 — overlay: My Health My Data Act",
    "OR": "ORS 165.540(1)(c) in person; (1)(a) telecommunications — Project Veritas v. Schmidt (9th Cir. 2025)",
    "CT": "Conn. Gen. Stat. § 52-570d — all-party telephonic",
    "NV": "NRS 200.650 in person; Lane v. Allstate, 114 Nev. 1176 (1998) by telephone",
    "MI": "MCL 750.539c — reads all-party; participant exception applied since Sullivan v. Gray (1982)",
    "NY": "N.Y. Penal Law § 250.00 — one-party",
    "TX": "Tex. Penal Code § 16.02 — one-party",
    "": "no jurisdiction resolved for this encounter",
}

CONSENT_LAYERS = [
    {"t": "Registration / annual packet", "strength": "weakest", "cls": "v-warn",
     "b": "The pending litigation theory is precisely that a buried checkbox is not consent for a specific recorded encounter."},
    {"t": "Visit-level verbal attestation", "strength": "strongest", "cls": "v-good",
     "b": "Captured at the head of the recording, so consent and recording share one artifact and one timestamp."},
    {"t": "Structured consent state", "strength": "required", "cls": "v-navy",
     "b": "Status, timestamp, obtainer, revocation — recorded discretely, never as free text."},
]

CONSENT_FOOTNOTE = (
    "No state creates a healthcare exemption to its wiretap statute; the direction "
    "runs the other way, since a clinical encounter is the paradigm confidential "
    "communication. HIPAA does not authorise recording — it governs use and "
    "disclosure afterwards. Live CIPA class actions over ambient scribes are "
    "tracked as a changing-law item."
)

PREFLIGHT = {
    "aws": {
        "label": "AWS Transcribe",
        "checks": [
            {"check": "Service opt-out policy",
             "probe": "organizations:DescribeEffectivePolicy — PolicyType=AISERVICES_OPT_OUT_POLICY",
             "finding": "Transcribe opt-out present and parsed. Strongest of the three clouds.",
             "verdict": "machine-checked", "cls": "good"},
            {"check": "Output bucket bound",
             "probe": "assert OutputBucketName + OutputEncryptionKMSKeyId on every job",
             "finding": "Set. Omitting the bucket sends transcripts to a service-managed bucket with a 90-day default retention.",
             "verdict": "machine-checked", "cls": "good"},
            {"check": "BAA coverage",
             "probe": "AWS BAA covered-services list",
             "finding": "The list entry reads 'AWS Transcribe [Includes HealthScribe]'. Transcribe Medical is not separately named — written AWS confirmation is required.",
             "verdict": "unverified", "cls": "amber"},
            {"check": "Language constraint", "probe": "job config", "finding": "en-US only.",
             "verdict": "machine-checked", "cls": "good"},
        ],
        "title": "Egress permitted, with one open dependency.",
        "body": "The preflight is machine-checkable end to end on AWS; only the Transcribe "
                "Medical BAA naming is unresolved, and that blocks ambient documentation on "
                "this cloud until AWS confirms in writing.",
        "tone": "amber",
    },
    "azure": {
        "label": "Azure AI Speech",
        "checks": [
            {"check": "Custom endpoint content logging",
             "probe": "GET /speechtotext/v3.2/endpoints/{id} — properties.contentLoggingEnabled",
             "finding": "False. Endpoint-level setting overrides session-level, so this is the authoritative check where a custom endpoint is used.",
             "verdict": "machine-checked", "cls": "good"},
            {"check": "Base-model real-time logging",
             "probe": "not server-queryable — per-request client flag",
             "finding": "Gateway-side code assertion plus operator attestation. There is no property to read.",
             "verdict": "attestation", "cls": "warn"},
            {"check": "BAA scope",
             "probe": "Service Trust Portal appendices — auth-gated",
             "finding": "Microsoft publishes no citable BAA scope list where AWS and GCP do. A documentation asymmetry that shifts the evidence burden onto the operator on Azure alone.",
             "verdict": "attestation", "cls": "warn"},
            {"check": "Region / SKU gate", "probe": "deployment config",
             "finding": "No PHI-specific SKU or region gate is documented. Do not assume the Azure OpenAI non-Global deployment rule transfers.",
             "verdict": "unverified", "cls": "amber"},
        ],
        "title": "Egress permitted only under operator attestation.",
        "body": "Two of four checks cannot be closed in code on this cloud. They are recorded "
                "as asymmetries rather than defects — real, permanent, and named in the runbook "
                "— because presenting an attestation as a check is the failure this design "
                "exists to prevent.",
        "tone": "warn",
    },
    "gcp": {
        "label": "GCP Speech-to-Text",
        "checks": [
            {"check": "Data-logging enrollment",
             "probe": "project-level console toggle — no API-queryable property",
             "finding": "Operator attestation required. Billing SKU is a weak inferential signal only, not proof.",
             "verdict": "attestation", "cls": "warn"},
            {"check": "Regional endpoint",
             "probe": "assert non-global endpoint (us-speech / eu-speech)",
             "finding": "Regional endpoint asserted client-side. The global endpoint gives no residency guarantee.",
             "verdict": "machine-checked", "cls": "good"},
            {"check": "BAA coverage",
             "probe": "GCP covered products list",
             "finding": "Named Covered Product; documentation states not to opt into data logging under the BAA.",
             "verdict": "machine-checked", "cls": "good"},
            {"check": "Model constraint", "probe": "model config",
             "finding": "Medical models are en-US only. Async transcripts retained ~5 days; nothing retained by default.",
             "verdict": "machine-checked", "cls": "good"},
        ],
        "title": "Egress permitted, one permanent asymmetry.",
        "body": "GCP speech data-logging enrollment exposes no queryable property — "
                "structurally identical to the known abuse-logging-exemption asymmetry. Where "
                "AWS is verifiable, GCP requires an attestation, and the runbook says so rather "
                "than implying parity.",
        "tone": "amber",
    },
}

REGISTRY_ROWS = [
    {"model": "claude — retrieval assistant", "use": "Grounded Q&A and summarization over indexed FHIR",
     "screen": ("n/a — no patient score", "v-mute"), "fair": ("source attrs", "v-good"), "exec": ("yes", "v-good")},
    {"model": "your organization — readmission-30d", "use": "Discharge planning support — patient care DSI under 92.210 regardless of label",
     "screen": ("passed", "v-good"), "fair": ("on file", "v-good"), "exec": ("yes", "v-good")},
    {"model": "your organization — no-show-v3", "use": "Supportive intervention targeting",
     "screen": ("2 proxies justified", "v-amber"), "fair": ("on file", "v-good"), "exec": ("yes", "v-good")},
    {"model": "your organization — deterioration-vitals", "use": "Bedside deterioration from continuous monitor signal",
     "screen": ("blocked", "v-warn"), "fair": ("—", "v-mute"), "exec": ("no", "v-warn")},
    {"model": "vendor — stt-medical", "use": "Speech-to-text for ambient documentation",
     "screen": ("n/a", "v-mute"), "fair": ("n/a", "v-mute"), "exec": ("preflight-gated", "v-amber")},
]

REGISTRY_FOOTNOTE = (
    "The platform does not author predictive clinical models. It hosts, gates, "
    "audits, monitors and constrains the deploying organization's — who supplies the model and "
    "its regulatory standing. Readmission and length-of-stay models route through "
    "this path rather than around it, because they influence discharge planning "
    "regardless of internal labelling."
)

# Every draft carries its FULL text: the review screen shows exactly
# what signing would commit, and a reviewer must be able to read all of
# it before the sign control means anything.
SIGNATURE_DRAFTS = [
    {"id": "d1", "what": "Ambient note — endocrinology follow-up",
     "detail": "Structured from an 18m 41s recording; 3 candidate findings each carry their transcript span",
     "api": "DocumentReference create — api=1046", "interaction": "create",
     "resource_type": "DocumentReference",
     "body": """ENDOCRINOLOGY FOLLOW-UP — AI-DRAFTED, UNSIGNED

Subjective: Patient returns for scheduled diabetes follow-up. Reports improved morning glucose readings since the last visit, occasional evening fatigue, no hypoglycemic episodes, no chest pain, no vision changes. Adherent to current regimen. [transcript 02:14-04:56]

Objective: See companion vitals draft (BP, weight, pulse captured this encounter). Most recent A1c and renal panel reviewed in chart. [transcript 06:02-06:40]

Assessment:
1. Type 2 diabetes mellitus — improving control by patient report; confirm with today's A1c when resulted. [transcript 08:11-09:03]
2. Diabetic nephropathy surveillance — prior eGFR trend warrants continued monitoring; see problem-list draft raised separately.
3. Hypertension — home readings at goal per patient log. [transcript 10:22-10:58]

Plan: Continue current regimen unchanged pending labs. Repeat A1c and renal panel today. Return visit 3 months, sooner for symptoms. Patient verbalized understanding. [transcript 15:40-17:22]

DRAFT NOTICE: Structured from an 18m 41s ambient recording under a captured visit-level consent. Three candidate findings carry their transcript spans for verification. Nothing in this draft enters the record until signed."""},
    {"id": "d2", "what": "Vital signs from the ambient encounter",
     "detail": "BP, weight, pulse — flowsheet row IDs mapped site-side",
     "api": "Observation create Vital Signs R4 — api=963", "interaction": "create",
     "resource_type": "Observation",
     "body": """VITAL SIGNS — AMBIENT ENCOUNTER CAPTURE, UNSIGNED

Blood pressure: 128/78 mmHg, seated, left arm, standard cuff [transcript 05:12]
Weight: 184 lb (83.5 kg), clothed, wall-mounted scale [transcript 05:31]
Pulse: 72 bpm, regular [transcript 05:40]

Mapping: three Observation resources staged against the Vital Signs R4 profile; flowsheet row IDs mapped site-side per the integration profile.

DRAFT NOTICE: Values were spoken during the encounter and structured by the platform. The signer attests the values match what was measured. Nothing writes to the flowsheet until signed."""},
    {"id": "d3", "what": "Problem-list addition — diabetic nephropathy",
     "detail": "Raised by documentation-gap detection; supported by the cited lab pattern",
     "api": "Condition create", "interaction": "create",
     "resource_type": "Condition",
     "body": """PROBLEM-LIST ADDITION — DIABETIC NEPHROPATHY, UNSIGNED

Proposed problem: Diabetic nephropathy (disorder)

Basis, from the record:
- eGFR declining across three consecutive results while A1c remained above target [chart · lab series]
- Persistent microalbuminuria on two urine albumin results [chart · lab series]
- Long-standing type 2 diabetes on the active problem list [chart · problems]

Raised by documentation-gap detection: the lab pattern supports a diagnosis that is absent from the problem list, and claims coding references it without chart support.

DRAFT NOTICE: This is a suggested addition with its evidence. The clinician confirms or rejects the diagnosis; the platform does not diagnose. Nothing changes the problem list until signed."""},
    {"id": "d4", "what": "Medication change — metformin 1000mg → 500mg BID",
     "detail": "Cannot be written. Epic exposes read and search only for MedicationRequest",
     "api": "no REST create — CDS Hooks unsigned order", "interaction": "create",
     "resource_type": "MedicationRequest",
     "body": """MEDICATION CHANGE — METFORMIN 1000MG → 500MG BID, UNSIGNED AND UNWRITABLE

Proposed change: reduce metformin from 1000 mg BID to 500 mg BID.
Context: renal function trend (see problem-list draft) warrants dose review per prescribing guidance.

WHY THIS CANNOT BE WRITTEN: Epic exposes MedicationRequest as read/search only over FHIR REST. Order-shaped writes exist only as CDS Hooks "unsigned order" suggestions inside Epic's own ordering workflow. The platform therefore refuses to fake a write path: this draft is presented for the clinician to act on inside the EMR's ordering flow, and this platform records that the suggestion was made — nothing more."""},
]

WRITEBACK_FACTS = [
    {"k": "Two-step protocol",
     "v": "Stage a draft resource, a licensed human signs, the signature commits. AI-assisted provenance is recorded on the written resource."},
    {"k": "Enablement is per-resource and per-flavor",
     "v": "'DocumentReference create' is several distinct APIs with separate enablement, so the install runbook enumerates them individually rather than as one capability."},
    {"k": "Throughput",
     "v": "Epic write rate limits are undocumented publicly. The write path implements backoff and a bounded queue regardless."},
]

SIGNATURE_DEPENDENCY = (
    "Whether a note created through DocumentReference lands unsigned in a "
    "clinician's queue or lands committed could not be confirmed from public Epic "
    "documentation. Until an authenticated read of api=1046/845 resolves it, "
    "documents commit to Epic only after the signature event is recorded here — "
    "strictly more conservative than relying on unverified Epic-side behaviour, "
    "and it degrades to the native workflow once confirmed."
)

COHORT_BADGES = ["de-identified plane", "determination record attached", "one-way boundary", "role: analyst"]
# Made by Ryan Gomez & Co. Inc.
