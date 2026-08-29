#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Mock Epic FHIR server - stands in for Epic's sandbox so the rest of the
pipeline (auth handshake shape, pagination, S3 write, encryption, audit
chain, Postgres index) can be exercised and verified while the real
Epic invalid_client issue is unresolved.

WHAT THIS DOES NOT TEST: whether our real JWT/JWKS setup satisfies
Epic's actual authorization server. This mock's token endpoint accepts
any client assertion unconditionally - it exists to test everything
DOWNSTREAM of authentication, not authentication against Epic itself.
Nothing here should be read as evidence about the Epic auth investigation
either way.

ALL DATA BELOW IS SYNTHETIC. Every name, identifier, and clinical value
is fabricated for testing shape and structure - not sampled from or
resembling any real person. Resource IDs are deliberately prefixed
"eSyn" to make them impossible to mistake for a real Epic-issued ID or
to accidentally reuse as one, and every DocumentReference/note explicitly
labels itself as synthetic in its content.

RESOURCE TYPES: all 10 from EMRProfile.supported_resources now have
synthetic data - Encounter and Immunization were added once both were
registered as Incoming APIs on the real Epic app (confirmed via a live
fresh-reload check, not just trusted from the console's save
confirmation - see git history for that verification); ExplanationOfBenefit
(claims/billing) was added later, per docs/DATA_SCOPE_REVIEW.md, and
has NOT yet had that same live registration confirmation against a real
Epic sandbox - it's included in the mock so the pipeline can be exercised
now, but treat its real-Epic behavior as unverified until that check
happens. Earlier versions of this file modeled a registration gap as a
403, specifically to surface it during testing rather than let it stay
hidden until a real deployment tripped over it; the underlying
UNAUTHORIZED_TYPES mechanism is left in place, currently empty, in case
a future supported_resources addition outpaces what's actually
registered again - update that set, not the resource type list, if that
happens.

BULK DATA EXPORT: also simulates Epic's Group-level $export flow -
kickoff, async status polling, NDJSON file download, delete - matching
the documented behavior at fhir.epic.com/Documentation?docId=fhir_bulk_data
as closely as a local mock reasonably can. This exists because real
Epic's regular per-type search API rejects unscoped population queries
(confirmed live: "This resource requires demographics or _id parameter
for searching"), and Epic's own docs are explicit that bulk export
supports ONLY Group-level export with no `_since`/incremental capability
at all - every kickoff is a full re-extract. A real Group FHIR ID has to
come from Epic directly (email openepic@epic.com for a sandbox one, or
from the healthcare organization in a real deployment) - it is NOT
discoverable via any API, so this mock stands in with MOCK_GROUP_ID until
a real one is available. Swapping in a real Group ID later requires no
code change, only a different value in PHI_AI_FHIR_GROUP_ID.

USAGE:
    python3 mock_epic_server.py [--port 8899]

Then point .env at it (see README section at the bottom of this file for
the full switch-over / switch-back instructions):
    PHI_AI_FHIR_BASE_URL=http://localhost:8899/api/FHIR/R4
    PHI_AI_FHIR_TOKEN_URL=http://localhost:8899/oauth2/token

Then run the real pipeline against it, completely unmodified:
    python -m core.fhir.scheduler --once
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Synthetic patients. Three is enough to exercise Postgres's
# find_by_patient_reference() meaningfully (multiple resource types per
# patient) without the dataset being tedious to read in full.
# ---------------------------------------------------------------------------

PATIENTS = [
    {
        "resourceType": "Patient",
        "id": "eSyn0001Patient",
        "meta": {"lastUpdated": "2026-06-01T10:00:00Z"},
        "identifier": [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.0", "value": "SYN-MRN-00001"}],
        "name": [{"use": "official", "family": "Testworth", "given": ["Alexandra"]}],
        "gender": "female",
        "birthDate": "1985-03-14",
        "address": [{"use": "home", "city": "Springfield", "state": "IL", "postalCode": "62704", "country": "US"}],
    },
    {
        "resourceType": "Patient",
        "id": "eSyn0002Patient",
        "meta": {"lastUpdated": "2026-06-01T10:05:00Z"},
        "identifier": [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.0", "value": "SYN-MRN-00002"}],
        "name": [{"use": "official", "family": "Fixture-Jones", "given": ["Marcus"]}],
        "gender": "male",
        "birthDate": "1972-11-02",
        "address": [{"use": "home", "city": "Riverton", "state": "WY", "postalCode": "82501", "country": "US"}],
    },
    {
        "resourceType": "Patient",
        "id": "eSyn0003Patient",
        "meta": {"lastUpdated": "2026-06-01T10:10:00Z"},
        "identifier": [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.0", "value": "SYN-MRN-00003"}],
        "name": [{"use": "official", "family": "Sandbox-Chen", "given": ["Riley"]}],
        "gender": "female",
        "birthDate": "2001-07-22",
        "address": [{"use": "home", "city": "Ashland", "state": "OR", "postalCode": "97520", "country": "US"}],
    },
]

OBSERVATIONS = [
    # Labs
    {
        "resourceType": "Observation", "id": "eSynObs0001", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose [Mass/volume] in Serum or Plasma"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "effectiveDateTime": "2026-06-02T09:00:00Z",
        "valueQuantity": {"value": 98, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
    },
    {
        "resourceType": "Observation", "id": "eSynObs0002", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "718-7", "display": "Hemoglobin [Mass/volume] in Blood"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "effectiveDateTime": "2026-06-03T08:30:00Z",
        "valueQuantity": {"value": 14.2, "unit": "g/dL", "system": "http://unitsofmeasure.org", "code": "g/dL"},
    },
    {
        "resourceType": "Observation", "id": "eSynObs0003", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "2093-3", "display": "Cholesterol [Mass/volume] in Serum or Plasma"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "effectiveDateTime": "2026-06-04T07:45:00Z",
        "valueQuantity": {"value": 180, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
    },
    # Vital signs
    {
        "resourceType": "Observation", "id": "eSynObs0004", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "effectiveDateTime": "2026-06-02T09:05:00Z",
        "valueQuantity": {"value": 72, "unit": "beats/minute", "system": "http://unitsofmeasure.org", "code": "/min"},
    },
    {
        "resourceType": "Observation", "id": "eSynObs0005", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "effectiveDateTime": "2026-06-03T08:35:00Z",
        "valueQuantity": {"value": 128, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"},
    },
    {
        "resourceType": "Observation", "id": "eSynObs0006", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "8310-5", "display": "Body temperature"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "effectiveDateTime": "2026-06-04T07:50:00Z",
        "valueQuantity": {"value": 37.0, "unit": "Cel", "system": "http://unitsofmeasure.org", "code": "Cel"},
    },
]

CONDITIONS = [
    {
        "resourceType": "Condition", "id": "eSynCond0001",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9", "display": "Type 2 diabetes mellitus without complications"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "onsetDateTime": "2020-01-15",
    },
    {
        "resourceType": "Condition", "id": "eSynCond0002",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I10", "display": "Essential (primary) hypertension"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "onsetDateTime": "2018-06-01",
    },
    {
        "resourceType": "Condition", "id": "eSynCond0003",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "resolved"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "J06.9", "display": "Acute upper respiratory infection, unspecified"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "onsetDateTime": "2026-04-10",
    },
]

MEDICATION_REQUESTS = [
    {
        "resourceType": "MedicationRequest", "id": "eSynMed0001", "status": "active", "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975", "display": "Metformin 500 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "authoredOn": "2026-05-01",
    },
    {
        "resourceType": "MedicationRequest", "id": "eSynMed0002", "status": "active", "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "314076", "display": "Lisinopril 10 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "authoredOn": "2026-04-15",
    },
    {
        "resourceType": "MedicationRequest", "id": "eSynMed0003", "status": "completed", "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "308191", "display": "Amoxicillin 500 MG Oral Capsule"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "authoredOn": "2026-04-10",
    },
]

DOCUMENT_REFERENCES = [
    {
        "resourceType": "DocumentReference", "id": "eSynDoc0001", "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "11488-4", "display": "Consult note"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "date": "2026-05-20T14:00:00Z",
        "content": [{"attachment": {"contentType": "text/plain", "title": "SYNTHETIC TEST DATA - not a real clinical note"}}],
    },
    {
        "resourceType": "DocumentReference", "id": "eSynDoc0002", "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "date": "2026-05-18T11:30:00Z",
        "content": [{"attachment": {"contentType": "text/plain", "title": "SYNTHETIC TEST DATA - not a real clinical note"}}],
    },
    {
        "resourceType": "DocumentReference", "id": "eSynDoc0003", "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "34117-2", "display": "History and physical note"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "date": "2026-04-10T09:00:00Z",
        "content": [{"attachment": {"contentType": "text/plain", "title": "SYNTHETIC TEST DATA - not a real clinical note"}}],
    },
]

ALLERGY_INTOLERANCES = [
    {
        "resourceType": "AllergyIntolerance", "id": "eSynAllergy0001",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "7980", "display": "Penicillin"}]},
        # NOTE: AllergyIntolerance uses "patient", not "subject" - deliberately
        # kept faithful to the real FHIR spec so this exercises the "patient"
        # branch of extract_patient_reference() in core/db/index.py, not just
        # the "subject" branch every other resource type here uses.
        "patient": {"reference": "Patient/eSyn0001Patient"},
    },
    {
        "resourceType": "AllergyIntolerance", "id": "eSynAllergy0002",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "227493005", "display": "Shellfish allergy"}]},
        "patient": {"reference": "Patient/eSyn0003Patient"},
    },
]

PROCEDURES = [
    {
        "resourceType": "Procedure", "id": "eSynProc0001", "status": "completed",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "99213", "display": "Office visit"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "performedDateTime": "2026-05-01T10:00:00Z",
    },
    {
        "resourceType": "Procedure", "id": "eSynProc0002", "status": "completed",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "80053", "display": "Comprehensive metabolic panel"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "performedDateTime": "2026-06-03T08:30:00Z",
    },
    {
        "resourceType": "Procedure", "id": "eSynProc0003", "status": "completed",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "90715", "display": "Tdap vaccine administration"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "performedDateTime": "2026-04-10T09:15:00Z",
    },
]

ENCOUNTERS = [
    {
        "resourceType": "Encounter", "id": "eSynEnc0001", "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "period": {"start": "2026-05-01T09:00:00Z", "end": "2026-05-01T09:30:00Z"},
    },
    {
        "resourceType": "Encounter", "id": "eSynEnc0002", "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "period": {"start": "2026-06-03T08:15:00Z", "end": "2026-06-03T08:45:00Z"},
    },
    {
        "resourceType": "Encounter", "id": "eSynEnc0003", "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "period": {"start": "2026-04-10T08:50:00Z", "end": "2026-04-10T09:20:00Z"},
    },
]

IMMUNIZATIONS = [
    {
        "resourceType": "Immunization", "id": "eSynImm0001", "status": "completed",
        "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "141", "display": "Influenza, seasonal, injectable"}]},
        # NOTE: like AllergyIntolerance, Immunization uses "patient" per the
        # real FHIR R4 spec, not "subject" - a second, independent exercise
        # of that branch in extract_patient_reference(), on a resource type
        # that's otherwise unrelated to AllergyIntolerance.
        "patient": {"reference": "Patient/eSyn0001Patient"},
        "occurrenceDateTime": "2026-03-15",
    },
    {
        "resourceType": "Immunization", "id": "eSynImm0002", "status": "completed",
        "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "09", "display": "Td(adult), adsorbed"}]},
        "patient": {"reference": "Patient/eSyn0002Patient"},
        "occurrenceDateTime": "2026-02-20",
    },
    {
        "resourceType": "Immunization", "id": "eSynImm0003", "status": "completed",
        "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "213", "display": "SARS-COV-2 (COVID-19) vaccine, UNSPECIFIED"}]},
        "patient": {"reference": "Patient/eSyn0003Patient"},
        "occurrenceDateTime": "2026-01-10",
    },
]

# Claims/billing data - see docs/DATA_SCOPE_REVIEW.md and
# emr_profiles.py's comment on EPIC.supported_resources for why this was
# added. Deliberately uses ExplanationOfBenefit (not the lower-level
# Claim/ClaimResponse) per the FHIR spec's own guidance on which resource
# is appropriate here. Line items intentionally echo the same CPT codes
# already used in PROCEDURES for the same patient, so the synthetic
# dataset tells one coherent story per patient rather than unrelated
# fragments across resource types.
EXPLANATION_OF_BENEFITS = [
    {
        "resourceType": "ExplanationOfBenefit", "id": "eSynEob0001", "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional", "display": "Professional"}]},
        "use": "claim",
        "patient": {"reference": "Patient/eSyn0001Patient"},
        "created": "2026-05-02T00:00:00Z",
        "insurer": {"display": "SYNTHETIC TEST PAYER - not a real insurer"},
        "provider": {"display": "SYNTHETIC TEST PROVIDER - not a real provider"},
        "outcome": "complete",
        "item": [
            {
                "sequence": 1,
                "productOrService": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "99213", "display": "Office visit"}]},
                "adjudication": [
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "eligible"}]}, "amount": {"value": 150.00, "currency": "USD"}},
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "benefit"}]}, "amount": {"value": 120.00, "currency": "USD"}},
                ],
            }
        ],
    },
    {
        "resourceType": "ExplanationOfBenefit", "id": "eSynEob0002", "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional", "display": "Professional"}]},
        "use": "claim",
        "patient": {"reference": "Patient/eSyn0002Patient"},
        "created": "2026-06-04T00:00:00Z",
        "insurer": {"display": "SYNTHETIC TEST PAYER - not a real insurer"},
        "provider": {"display": "SYNTHETIC TEST PROVIDER - not a real provider"},
        "outcome": "complete",
        "item": [
            {
                "sequence": 1,
                "productOrService": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "80053", "display": "Comprehensive metabolic panel"}]},
                "adjudication": [
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "eligible"}]}, "amount": {"value": 65.00, "currency": "USD"}},
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "benefit"}]}, "amount": {"value": 52.00, "currency": "USD"}},
                ],
            }
        ],
    },
    {
        "resourceType": "ExplanationOfBenefit", "id": "eSynEob0003", "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional", "display": "Professional"}]},
        "use": "claim",
        "patient": {"reference": "Patient/eSyn0003Patient"},
        "created": "2026-04-11T00:00:00Z",
        "insurer": {"display": "SYNTHETIC TEST PAYER - not a real insurer"},
        "provider": {"display": "SYNTHETIC TEST PROVIDER - not a real provider"},
        "outcome": "complete",
        "item": [
            {
                "sequence": 1,
                "productOrService": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "90715", "display": "Tdap vaccine administration"}]},
                "adjudication": [
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "eligible"}]}, "amount": {"value": 85.00, "currency": "USD"}},
                    {"category": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adjudication", "code": "benefit"}]}, "amount": {"value": 85.00, "currency": "USD"}},
                ],
            }
        ],
    },
]

# Resource types Epic has actually granted - now all 10 in
# EMRProfile.supported_resources, confirmed via a live fresh-reload check
# of the app's Incoming APIs after registering Encounter and Immunization.
# ---------------------------------------------------------------------------
# The five CMS Conditions of Participation content types
# (42 CFR 482.24(c)(4)) - see docs/DATA_SCOPE_REVIEW.md and
# emr_profiles.py's comment on EPIC.supported_resources.
#
# Deliberately consistent with the rest of this dataset rather than
# free-standing: the same three synthetic patients, the same encounter and
# procedure dates, and the same CPT/RxNorm codes already used elsewhere in
# this file, so the stored dataset tells one coherent story per patient
# instead of unrelated fragments per resource type.
# ---------------------------------------------------------------------------

# (iv) complications, hospital-acquired infections, adverse drug/anesthesia
# reactions. Linked to the same medication already in MEDICATION_REQUESTS
# for this patient, so the adverse event has a traceable cause in-dataset.
ADVERSE_EVENTS = [
    {
        "resourceType": "AdverseEvent", "id": "eSynAdv0001",
        "actuality": "actual",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adverse-event-category", "code": "medication-mishap", "display": "Medication Mishap"}]}],
        "event": {"coding": [{"system": "http://snomed.info/sct", "code": "62014003", "display": "Adverse reaction to drug"}], "text": "SYNTHETIC - mild rash following medication administration"},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "date": "2026-05-03T14:00:00Z",
        "seriousness": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adverse-event-seriousness", "code": "Non-serious", "display": "Non-serious"}]},
    },
    {
        "resourceType": "AdverseEvent", "id": "eSynAdv0002",
        "actuality": "potential",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/adverse-event-category", "code": "incident", "display": "Incident"}]}],
        "event": {"text": "SYNTHETIC - near-miss, wrong-site verification caught pre-procedure"},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "date": "2026-05-04T08:15:00Z",
    },
]

# (v) properly executed informed consent forms. The structured counterpart
# to a scanned form arriving through DocumentReference.
CONSENTS = [
    {
        "resourceType": "Consent", "id": "eSynCons0001", "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "treatment", "display": "Treatment"}]},
        "category": [{"coding": [{"system": "http://loinc.org", "code": "59284-0", "display": "Consent Document"}]}],
        "patient": {"reference": "Patient/eSyn0001Patient"},
        "dateTime": "2026-05-01T08:45:00Z",
        "policyRule": {"text": "SYNTHETIC - general treatment consent, not a real executed form"},
    },
    {
        "resourceType": "Consent", "id": "eSynCons0002", "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "patient-privacy", "display": "Privacy Consent"}]},
        "category": [{"coding": [{"system": "http://loinc.org", "code": "57016-8", "display": "Privacy policy acknowledgment Document"}]}],
        "patient": {"reference": "Patient/eSyn0002Patient"},
        "dateTime": "2026-05-02T09:05:00Z",
        "policyRule": {"text": "SYNTHETIC - notice of privacy practices acknowledgment"},
    },
    {
        "resourceType": "Consent", "id": "eSynCons0003", "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "treatment", "display": "Treatment"}]},
        "category": [{"coding": [{"system": "http://loinc.org", "code": "59284-0", "display": "Consent Document"}]}],
        "patient": {"reference": "Patient/eSyn0003Patient"},
        "dateTime": "2026-05-03T10:20:00Z",
        "policyRule": {"text": "SYNTHETIC - procedural consent"},
    },
]

# (vi) practitioners' orders. MedicationRequest already covers the
# medication subset; these are the non-medication orders it cannot express.
SERVICE_REQUESTS = [
    {
        "resourceType": "ServiceRequest", "id": "eSynSvcReq0001",
        "status": "completed", "intent": "order",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24357-6", "display": "Urinalysis macro (dipstick) panel"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "authoredOn": "2026-05-01T09:15:00Z",
        "encounter": {"reference": "Encounter/eSynEnc0001"},
    },
    {
        "resourceType": "ServiceRequest", "id": "eSynSvcReq0002",
        "status": "completed", "intent": "order",
        "code": {"coding": [{"system": "http://loinc.org", "code": "30746-2", "display": "Chest X-ray"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "authoredOn": "2026-05-02T09:30:00Z",
    },
    {
        "resourceType": "ServiceRequest", "id": "eSynSvcReq0003",
        "status": "active", "intent": "order",
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "306098008", "display": "Referral to physiotherapy"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "authoredOn": "2026-05-03T11:00:00Z",
    },
]

# (vi) medication records - what was actually GIVEN, as distinct from what
# MEDICATION_REQUESTS shows was ordered. eSynAdv0001 above is the adverse
# reaction to this administration.
MEDICATION_ADMINISTRATIONS = [
    {
        "resourceType": "MedicationAdministration", "id": "eSynMedAdmin0001",
        "status": "completed",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1049502", "display": "Acetaminophen 325 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "effectiveDateTime": "2026-05-03T13:30:00Z",
        "request": {"reference": "MedicationRequest/eSynMed0001"},
    },
    {
        "resourceType": "MedicationAdministration", "id": "eSynMedAdmin0002",
        "status": "completed",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1049502", "display": "Acetaminophen 325 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "effectiveDateTime": "2026-05-02T10:00:00Z",
        "request": {"reference": "MedicationRequest/eSynMed0002"},
    },
    {
        "resourceType": "MedicationAdministration", "id": "eSynMedAdmin0003",
        "status": "not-done",
        "statusReason": [{"text": "SYNTHETIC - patient refused"}],
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1049502", "display": "Acetaminophen 325 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "effectiveDateTime": "2026-05-03T12:00:00Z",
    },
]

# (vi) radiology and laboratory reports. Carries the interpretation that a
# discrete Observation does not - each references the OBSERVATIONS already
# in this file for the same patient, which is the point of the distinction.
DIAGNOSTIC_REPORTS = [
    {
        "resourceType": "DiagnosticReport", "id": "eSynDiag0001", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "58410-2", "display": "CBC panel - Blood by Automated count"}]},
        "subject": {"reference": "Patient/eSyn0001Patient"},
        "effectiveDateTime": "2026-05-01T10:30:00Z",
        "issued": "2026-05-01T11:00:00Z",
        "result": [{"reference": "Observation/eSynObs0001"}, {"reference": "Observation/eSynObs0004"}],
        "conclusion": "SYNTHETIC - within normal limits. Not a real clinical interpretation.",
    },
    {
        "resourceType": "DiagnosticReport", "id": "eSynDiag0002", "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "RAD", "display": "Radiology"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "30746-2", "display": "Chest X-ray"}]},
        "subject": {"reference": "Patient/eSyn0002Patient"},
        "effectiveDateTime": "2026-05-02T10:15:00Z",
        "issued": "2026-05-02T12:00:00Z",
        "basedOn": [{"reference": "ServiceRequest/eSynSvcReq0002"}],
        "conclusion": "SYNTHETIC - no acute cardiopulmonary process. Not a real clinical interpretation.",
    },
    {
        "resourceType": "DiagnosticReport", "id": "eSynDiag0003", "status": "preliminary",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "24357-6", "display": "Urinalysis macro (dipstick) panel"}]},
        "subject": {"reference": "Patient/eSyn0003Patient"},
        "effectiveDateTime": "2026-05-03T11:30:00Z",
        "result": [{"reference": "Observation/eSynObs0003"}],
    },
]


# ExplanationOfBenefit.Search (Claim) still needs the same live
# registration + fresh-reload confirmation before it's genuinely working
# against real Epic, the same as every prior addition here - listed in
# RESOURCES_BY_TYPE now so the mock exercises it, not because that
# confirmation has already happened.
RESOURCES_BY_TYPE: dict[str, list[dict]] = {
    "Patient": PATIENTS,
    "Observation": OBSERVATIONS,
    "Condition": CONDITIONS,
    "MedicationRequest": MEDICATION_REQUESTS,
    "DocumentReference": DOCUMENT_REFERENCES,
    "AllergyIntolerance": ALLERGY_INTOLERANCES,
    "Procedure": PROCEDURES,
    "Encounter": ENCOUNTERS,
    "Immunization": IMMUNIZATIONS,
    "ExplanationOfBenefit": EXPLANATION_OF_BENEFITS,
    "AdverseEvent": ADVERSE_EVENTS,
    "Consent": CONSENTS,
    "ServiceRequest": SERVICE_REQUESTS,
    "MedicationAdministration": MEDICATION_ADMINISTRATIONS,
    "DiagnosticReport": DIAGNOSTIC_REPORTS,
}

# Mechanism kept in place (see module docstring) even though currently
# empty for the ORIGINAL 9 types - both Encounter and Immunization are
# resolved. ExplanationOfBenefit is newly added and has NOT had the same
# live fresh-reload confirmation on the real Epic app yet, so it's
# modeled here exactly the way Encounter/Immunization were modeled before
# that confirmation happened for them: a 403, not silent success. Move it
# out of this set once that confirmation actually happens - see git
# history for what that verification looked like for the prior two.
UNAUTHORIZED_TYPES: set[str] = {
    "ExplanationOfBenefit",
    # The five CMS CoP content types are in exactly the same position
    # ExplanationOfBenefit is: present in the profile and in this mock so the
    # pipeline can be exercised end to end, but with NO live registration
    # confirmation against a real Epic app. Modeled as 403 rather than silent
    # success so a deployment that hasn't registered them finds out during
    # testing instead of discovering an empty store later.
    "AdverseEvent",
    "Consent",
    "ServiceRequest",
    "MedicationAdministration",
    "DiagnosticReport",
}

# Force at least one real pagination round-trip even though the total
# dataset is small, so iter_resources()'s `next` link handling actually
# gets exercised rather than trivially returning everything in one page.
MAX_PAGE_SIZE = 2


def _bundle(resource_type: str, resources: list[dict], base_url: str, offset: int) -> dict:
    page = resources[offset : offset + MAX_PAGE_SIZE]
    entry = [{"resource": r, "fullUrl": f"{base_url}/{resource_type}/{r['id']}"} for r in page]

    links = [{"relation": "self", "url": f"{base_url}/{resource_type}?_offset={offset}"}]
    next_offset = offset + MAX_PAGE_SIZE
    if next_offset < len(resources):
        links.append({"relation": "next", "url": f"{base_url}/{resource_type}?_offset={next_offset}"})

    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "link": links,
        "entry": entry,
    }


def _operation_outcome(code: str, diagnostics: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}],
    }


# ---------------------------------------------------------------------------
# Bulk Data Export (Group-level) simulation - see module docstring.
# ---------------------------------------------------------------------------

# Deliberately fake and obviously so - matches every other "eSyn"-prefixed
# identifier in this file. Never mistakable for a real Epic-issued Group
# FHIR ID, which we don't have and can't generate (see module docstring).
MOCK_GROUP_ID = "eSynGroup0001"

# In-memory job state: job_id -> {"kicked_off_at": float, "poll_count": int,
# "resource_types": list[str] | None, "deleted": bool}. Resets whenever the
# server process restarts - there is deliberately no persistence here, this
# is a test double, not a system to model durability against.
_BULK_JOBS: dict[str, dict] = {}
_job_counter = 0

# Simulate genuine async behavior rather than completing instantly - forces
# the client's actual polling loop to run more than once, the same reason
# MAX_PAGE_SIZE forces a real pagination round-trip elsewhere in this file.
BULK_POLLS_BEFORE_COMPLETE = 2


def _resources_to_ndjson(resources: list[dict]) -> bytes:
    return "\n".join(json.dumps(r) for r in resources).encode("utf-8") + b"\n"


class MockEpicHandler(BaseHTTPRequestHandler):
    server_version = "MockEpicFHIR/1.0"

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/fhir+json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        print(f"[mock-epic] {self.address_string()} - {fmt % args}")

    def do_POST(self) -> None:
        if self.path.rstrip("/").endswith("/oauth2/token"):
            length = int(self.headers.get("Content-Length", 0))
            _ = self.rfile.read(length)  # client_assertion intentionally not validated - see module docstring
            self._send_json(
                200,
                {
                    "access_token": "mock-access-token-not-a-real-epic-token",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": "system/*.read",
                },
            )
            return

        self._send_json(404, _operation_outcome("not-found", f"No mock handler for POST {self.path}"))

    def _handle_bulk_kickoff(self, group_id: str, qs: dict) -> None:
        if group_id != MOCK_GROUP_ID:
            self._send_json(
                404,
                _operation_outcome(
                    "not-found",
                    f"Unknown group {group_id!r}. A real Group FHIR ID must come from Epic "
                    "directly (email openepic@epic.com for a sandbox one) - see module docstring.",
                ),
            )
            return

        # Epic's documented required headers for the kickoff request -
        # enforced here (not just documented) specifically so this mock
        # actually verifies the client sends them, not just that it works
        # when they happen to be present.
        if "application/fhir+json" not in self.headers.get("Accept", ""):
            self._send_json(400, _operation_outcome("invalid", "Kickoff request must include Accept: application/fhir+json"))
            return
        if "respond-async" not in self.headers.get("Prefer", ""):
            self._send_json(400, _operation_outcome("invalid", "Kickoff request must include Prefer: respond-async"))
            return

        global _job_counter
        _job_counter += 1
        job_id = f"eSynBulkJob{_job_counter:04d}"

        requested = qs.get("_type", [None])[0]
        _BULK_JOBS[job_id] = {
            "poll_count": 0,
            "resource_types": requested.split(",") if requested else None,
            "deleted": False,
        }

        status_url = f"http://{self.headers.get('Host', 'localhost')}/api/FHIR/BulkRequest/{job_id}"
        self.send_response(202)
        self.send_header("Content-Location", status_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_bulk_status(self, job_id: str, base_url: str) -> None:
        job = _BULK_JOBS.get(job_id)
        if job is None or job["deleted"]:
            self._send_json(404, _operation_outcome("not-found", f"Unknown or deleted bulk request {job_id!r}"))
            return

        job["poll_count"] += 1

        if job["poll_count"] <= BULK_POLLS_BEFORE_COMPLETE:
            searched = min(job["poll_count"], len(PATIENTS))
            self.send_response(202)
            self.send_header("X-Progress", f"Searched {searched} of {len(PATIENTS)} patients")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Simplification versus real Epic: an unrecognized _type value is
        # silently omitted from output rather than reported via the
        # documented error-file mechanism (an OperationOutcome URL in the
        # status response's "error" array). Exercising that specific path
        # isn't essential to proving the core kickoff/poll/download flow
        # works, so it's left out rather than half-implemented.
        resource_types = job["resource_types"] or list(RESOURCES_BY_TYPE.keys())
        output = [
            {"type": rtype, "url": f"{base_url}/BulkRequest/{job_id}/{rtype}.ndjson"}
            for rtype in resource_types
            if rtype in RESOURCES_BY_TYPE
        ]

        self._send_json(
            200,
            {
                "transactionTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "request": f"{base_url}/R4/Group/{MOCK_GROUP_ID}/$export",
                "requiresAccessToken": True,
                "output": output,
                "error": [],
            },
        )

    def _handle_bulk_file(self, job_id: str, filename: str) -> None:
        job = _BULK_JOBS.get(job_id)
        if job is None or job["deleted"]:
            self._send_json(404, _operation_outcome("not-found", f"Unknown or deleted bulk request {job_id!r}"))
            return

        # requiresAccessToken: true (set in the status response above) means
        # file downloads need the same bearer token as everything else -
        # confirmed here, not just assumed.
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send_json(401, _operation_outcome("login", "File download requires a Bearer token"))
            return

        rtype = filename[: -len(".ndjson")] if filename.endswith(".ndjson") else filename
        if rtype not in RESOURCES_BY_TYPE:
            self._send_json(404, _operation_outcome("not-found", f"No file {filename!r} for job {job_id!r}"))
            return

        body = _resources_to_ndjson(RESOURCES_BY_TYPE[rtype])
        self.send_response(200)
        self.send_header("Content-Type", "application/fhir+ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        match = re.match(r"^/api/FHIR/BulkRequest/([^/]+)$", urlparse(self.path).path)
        if match:
            job = _BULK_JOBS.get(match.group(1))
            if job is None:
                self._send_json(404, _operation_outcome("not-found", f"Unknown bulk request {match.group(1)!r}"))
                return
            job["deleted"] = True
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._send_json(404, _operation_outcome("not-found", f"No mock handler for DELETE {self.path}"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        host = self.headers.get("Host", "localhost")

        kickoff_match = re.match(r"^/api/FHIR/R4/Group/([^/]+)/\$export$", parsed.path)
        if kickoff_match:
            self._handle_bulk_kickoff(kickoff_match.group(1), qs)
            return

        file_match = re.match(r"^/api/FHIR/BulkRequest/([^/]+)/([^/]+)$", parsed.path)
        if file_match:
            self._handle_bulk_file(file_match.group(1), file_match.group(2))
            return

        status_match = re.match(r"^/api/FHIR/BulkRequest/([^/]+)$", parsed.path)
        if status_match:
            self._handle_bulk_status(status_match.group(1), f"http://{host}/api/FHIR")
            return

        match = re.search(r"/([A-Za-z]+)$", parsed.path)
        resource_type = match.group(1) if match else ""
        base_url = f"http://{host}" + parsed.path.rsplit("/" + resource_type, 1)[0]

        if resource_type in UNAUTHORIZED_TYPES:
            self._send_json(
                403,
                _operation_outcome(
                    "forbidden",
                    f"Client is not authorized for {resource_type}. This resource type is listed in "
                    "EMRProfile.supported_resources but was never registered as an Incoming API on the "
                    "Epic app - see mock_epic_server.py's module docstring.",
                ),
            )
            return

        if resource_type not in RESOURCES_BY_TYPE:
            self._send_json(404, _operation_outcome("not-found", f"Unknown resource type {resource_type!r}"))
            return

        offset = int(qs.get("_offset", ["0"])[0])
        self._send_json(200, _bundle(resource_type, RESOURCES_BY_TYPE[resource_type], base_url, offset))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    total = sum(len(v) for v in RESOURCES_BY_TYPE.values())
    print(f"Mock Epic FHIR server - {total} synthetic resources across {len(RESOURCES_BY_TYPE)} types")
    if UNAUTHORIZED_TYPES:
        print(f"Unauthorized (403) types simulated: {', '.join(sorted(UNAUTHORIZED_TYPES))}")
    else:
        print("No unauthorized types simulated - all supported_resources types are registered")
    print(f"Listening on http://localhost:{args.port}")
    print(f"  Token URL: http://localhost:{args.port}/oauth2/token")
    print(f"  Base URL:  http://localhost:{args.port}/api/FHIR/R4")
    print(f"  Bulk export Group ID: {MOCK_GROUP_ID}")
    print(f"  Bulk kickoff: http://localhost:{args.port}/api/FHIR/R4/Group/{MOCK_GROUP_ID}/$export")
    print("Ctrl+C to stop.\n")

    server = ThreadingHTTPServer(("localhost", args.port), MockEpicHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
# Made by Ryan Gomez & Co. Inc.
