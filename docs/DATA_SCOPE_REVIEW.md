# Data scope review: what data this system should capture

A discussion draft for Health Information Manager review — not a
determination of what's legally required, and not something to treat
as final without that review. See "What this document is and isn't"
immediately below before reading further.

## What this document is and isn't

**This is not a comprehensive, authoritative statement of what HIPAA
and all 50 states require to be retained.** That request was raised
directly during this project's development, and building it in that
form was declined for the same reason a 50-state retention *rules
engine* was declined (see `docs/COMPLIANCE.md`) — it would mean this
project making legal conclusions across 50 independent bodies of law,
which isn't something either software or an AI assistant should present
as settled.  This requires a human to be in the loop to make authoritative decisions.

**What this actually is:** a systematic mapping of this system's
current data scope (the 9 FHIR resource types in
`core/fhir/emr_profiles.py`) against two specific, real, cited
regulatory frameworks that speak directly to health record *content* —
as opposed to record *retention duration*, which is a different
question this project already addressed (see
`runbooks/RUNBOOK_RETENTION_RULES.md`). Every claim below traces to a
specific citation, checked directly against the current regulatory
text, not reconstructed from memory. Where the analysis runs out —
and it does, well short of "all 50 states" — that's stated explicitly
rather than papered over.

The output is a set of **candidate FHIR resource types** for your HIM
manager and technical team to review, not a list to implement
automatically. Whether Epic's FHIR API actually exposes a given
resource type for any specific customer instance is also a separate,
real question — `emr_profiles.py`'s own existing guidance already
covers this: confirm against the target instance's own
`CapabilityStatement` before ingesting from it.

## Currently ingested (baseline)

Per `core/fhir/emr_profiles.py`, this system ingests 15 FHIR R4
resource types.

The original 9: `Patient`, `Encounter`, `Observation`, `Condition`,
`MedicationRequest`, `DocumentReference`, `AllergyIntolerance`,
`Immunization`, `Procedure`.

Added since this document's first pass, in response to it:
`ExplanationOfBenefit` (the billing/claims gap noted under Framework 2),
then `AdverseEvent`, `Consent`, `ServiceRequest`,
`MedicationAdministration` and `DiagnosticReport` (the five candidates
derived from Framework 1 below).

**Read the two caveats before treating those additions as finished
work.** First, this document produced *candidates for review*, and the
review it calls for is an organizational judgement about whether a given
deployment's compliance posture requires each type - not something the
codebase decides. Their presence in `emr_profiles.py` reflects that
decision having been made for this deployment; it is not a claim that
every deployment needs all six. Second, and more concretely: **none of
the six has been confirmed against a real Epic instance's
`CapabilityStatement` or registered as an Incoming API on a real Epic
app.** They are exercised against the mock server only. Until that
registration happens, a live instance will return 403 for each - which
`scripts/mock_epic_server.py` deliberately models via `UNAUTHORIZED_TYPES`
rather than returning an empty-but-successful response, so the gap
surfaces during testing instead of as a silently empty record set after
an EMR retirement.

## Framework 1: CMS Conditions of Participation for hospitals (42 CFR §482.24)

Primary source, read directly:
[ecfr.gov/current/title-42/.../section-482.24](https://www.ecfr.gov/current/title-42/chapter-IV/subchapter-G/part-482/subpart-C/section-482.24).
This is a federal regulation — a Condition of Participation a hospital
must meet to bill Medicare/Medicaid — administered by CMS, separate
from HIPAA itself. It applies specifically to **hospitals**; physician
offices and other ambulatory settings are governed by different,
generally state-level content standards not covered by this section
(see "What this analysis does not cover" below).

§482.24(c)(4) states: *"All records must document the following, as
appropriate"* — eight enumerated categories. Mapped against current
data scope:

| CMS content requirement (§482.24(c)(4)) | Current coverage |
|---|---|
| (i) History and physical examination | Covered — `DocumentReference`, `Observation`, `Condition` |
| (ii) Admitting diagnosis | Covered — `Condition` |
| (iii) Consultative evaluations | Covered — `DocumentReference` |
| (iv) Complications, hospital-acquired infections, adverse drug/anesthesia reactions | Covered — `AdverseEvent` (added in response to this review), plus `Condition` |
| (v) Properly executed informed consent forms | Covered — `Consent` (added in response to this review); scanned forms may also arrive via `DocumentReference` |
| (vi) Practitioners' orders | Covered — `ServiceRequest` for general orders (added in response to this review), `MedicationRequest` for medication orders |
| (vi) Nursing notes, reports of treatment | Covered — `DocumentReference` |
| (vi) Medication records | Covered — `MedicationRequest` for what was *ordered*, `MedicationAdministration` for what was actually *given* (added in response to this review) |
| (vi) Radiology and laboratory reports | Covered — `Observation` for discrete results, `DiagnosticReport` for the full report and its interpretation (added in response to this review) |
| (vi) Vital signs | Covered — `Observation` |
| (vii) Discharge summary | Covered — `DocumentReference` |
| (viii) Final diagnosis | Covered — `Condition` |

**Five specific candidate resource types fell out of this mapping:**
`AdverseEvent`, `Consent`, `ServiceRequest`, `MedicationAdministration`,
`DiagnosticReport`. All five are standard FHIR R4 resource types, not
something invented for this analysis.

**All five have since been added** to `EMRProfile.supported_resources`,
with synthetic data in `scripts/mock_epic_server.py` so the pipeline is
exercised end to end for each. Two regression tests in
`tests/test_client_store.py` keep the profile and the mock from
drifting apart, and a third asserts every synthetic resource resolves to
a real patient through `extract_patient_reference()` — that function
reads only `subject` and `patient`, so a type carrying its patient link
under some other field would index with a NULL reference and silently
drop out of restore-by-patient.

**What remains open, and it is the part that matters for a live
deployment:** none of the five has been confirmed against a real Epic
instance's `CapabilityStatement`, or registered as an Incoming API on a
real Epic app. Both remain required before any of them returns data from
a live instance, per the existing guidance in `emr_profiles.py`.

Separately, §482.24(b)(1) states hospital medical records *"must be
retained in their original or legally reproduced form for a period of
at least 5 years"* — an independent federal floor for CMS-participating
hospitals specifically, distinct from (not a substitute for) whatever
your state's own retention statute requires, and distinct from HIPAA
itself, which does not set a medical-record retention period. Worth
being precise about which of these three (CMS CoP, state statute,
HIPAA) is actually driving a given retention figure entered in
`config/retention_ruleset.yaml`.

## Framework 2: HIPAA's "designated record set" (45 CFR §164.501)

Primary source, corroborated across multiple independent legal/health
law summaries (a health law firm's client blog, AHIMA's own journal,
and others), all converging on the same regulatory text.

This is a genuinely different kind of framework than the CMS content
requirements above — it doesn't mandate what must be *created and
retained*, it defines what a patient has a **right to access a copy
of**, once created. It's still a useful signal for data-capture
completeness (if a patient could request it, losing it during an EMR
retirement is a real problem), but it isn't itself a retention mandate
the way the CMS content list is.

The designated record set comprises: medical and billing records
maintained by or for a covered provider; enrollment, payment, claims
adjudication, and case/medical management records maintained by or for
a health plan; and, more broadly, *any* record used to make decisions
about an individual.

Two things worth flagging specifically:

- **Billing and claims records** are explicitly part of the designated
  record set, and this system ingests the EMR's claims surface:
  `ExplanationOfBenefit` (adjudicated claims — services billed, amounts,
  payer adjudication) is a supported resource type, registered against
  Epic's real Incoming API (fhir.epic.com/Specifications?api=1073) and
  profiled for the other vendors where they expose it. EOB rather than
  the lower-level `Claim`/`ClaimResponse`, per the FHIR spec's own
  guidance. What remains outside scope is the *internals* of separate
  revenue-cycle systems (accounts-receivable ledgers, aging reports,
  collections workflows) that are not exposed as FHIR resources by the
  EMR — worth confirming with your HIM manager whether those systems
  need their own retention plan. (An earlier revision of this document
  placed all billing/claims data out of scope; that predated the
  `ExplanationOfBenefit` connector and was superseded by it.)
- **Psychotherapy notes are explicitly excluded** from the designated
  record set and get separate, stricter handling under HIPAA — they are
  defined narrowly (a mental health professional's private session
  notes, kept physically/logically separate from the rest of the
  record) and specifically exclude things like medication information,
  session times, and diagnosis summaries, which remain part of the
  regular record. If Epic surfaces anything meeting this narrow
  definition through `DocumentReference`, it may warrant separate
  handling rather than being stored identically to other clinical
  notes — a question for your HIM manager and counsel together, not
  something resolved here.

## What this analysis does not cover

Being explicit about the boundary, rather than letting "comprehensive"
imply more than what's actually been checked:

- **State-specific record-content-completeness requirements.** Every
  framework above is federal. Individual states may impose additional
  or different content requirements (often through medical board or
  hospital licensing regulations, separate from the retention statutes
  already handled in `docs/COMPLIANCE.md`). Genuine 50-state coverage of
  *this* question would be its own large research effort, not attempted
  here for the same reason the retention rules engine wasn't built as
  an AI-determined authority.
- **Non-hospital settings.** §482.24 is a hospital-specific CMS
  Condition of Participation. Physician offices, ambulatory surgery
  centers, and other settings are typically governed by different,
  often state-level standards not reviewed here.
- **42 CFR Part 2** (substance use disorder treatment records) — a
  separate, stricter federal confidentiality framework not researched
  for this document. If your deployment holds SUD treatment data, this
  needs its own review before assuming standard handling is sufficient.
- **Non-Epic data sources** generally, and **imaging/genetic/other
  specialized data categories** specifically — out of scope for this
  pass, which stayed within the FHIR resource types Epic's Backend
  Services API can plausibly expose.

## Recommended next steps

1. Share this draft with your Health Information Manager for review —
   same review discipline as any entry in
   `config/retention_ruleset.yaml`: confirm against current, primary
   sources, don't take this document's citations as a substitute for
   that confirmation.
2. For each of the five resource types now in
   `EMRProfile.supported_resources` (`AdverseEvent`, `Consent`,
   `ServiceRequest`, `MedicationAdministration`, `DiagnosticReport`),
   two separate confirmations are still outstanding: that your
   organization's compliance posture actually calls for ingesting it,
   *and* that your specific Epic instance's `CapabilityStatement`
   exposes it as a registered Incoming API (see
   `docs/EMR_CONNECTORS.md`). The code change did not and could not
   settle either one. Once an Incoming API is registered and confirmed
   live, remove that type from `UNAUTHORIZED_TYPES` in
   `scripts/mock_epic_server.py` — see the git history for what that
   verification looked like for `Encounter` and `Immunization`.
3. If your organization operates outside a hospital setting, or in a
   state whose specific content requirements haven't been checked here,
   treat this document as a starting structure to extend, not a
   finished answer for your situation.
