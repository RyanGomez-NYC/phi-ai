# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
EMR profiles: one entry per source/destination EMR this system connects to.

This table started life scoped to Epic alone - do one integration
correctly, including its actual auth model, before generalising. That
discipline paid off in the shape of this file: everything vendor-specific
(auth flow, resource support, bulk export, page size, rate limits, what a
write really means) lives HERE, in data, and core/fhir/client.py stayed a
plain FHIR R4 client. Adding the vendors below meant adding profiles and
one extra auth method (authenticate_client_secret, for the one vendor
that issues a secret instead of accepting a signed JWT) - not rewriting
the ingestion engine. Keep it that way: quirks belong in this table, not
scattered through the client.

The set of profiles is PROFILES at the bottom of this file - the one
place the vendors are enumerated, so nothing else here lists or counts
them. Each entry records the vendor's PUBLISHED capability surface with a
citation trail in
docs/EMR_CONNECTORS.md, and each is exercised against a per-vendor
emulator (emulators/) that reproduces its real seams. Where a vendor's
public documentation is ambiguous, gated behind a partner portal, or
per-health-system, the entry says so in `notes`/`write_notes` rather than
guessing - recorded uncertainty beats confident wrongness here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EMRProfile:
    name: str
    auth_flow: str
    supported_resources: tuple[str, ...]
    supports_bulk_export: bool
    # Whether the token request MUST carry explicit system/{Type}.read
    # scopes. Oracle Health was the first vendor here to require this ("we
    # do not support Wildcard scopes"); Epic documents the opposite - its
    # backend-services token request has no scope parameter at all, and
    # the granted scope comes from the app registration. Every other
    # profile answers for itself, citing that vendor's own token
    # documentation beside the field. The scope string itself is derived
    # from supported_resources at authentication time (see
    # FHIRIngestionClient.authenticate_from_settings), so the two can
    # never drift apart.
    requires_token_scopes: bool = False
    # Which JWS algorithm signs the client_assertion JWT when auth_flow is
    # smart_backend_services - and therefore which kind of key
    # PHI_AI_FHIR_PRIVATE_KEY_PATH must hold. RS384 is an RSA signature;
    # ES384 is ECDSA over the P-384 curve (secp384r1), which only an EC
    # P-384 key can produce. SMART App Launch's asymmetric client profile
    # obliges a server to accept at least one of the two, and each vendor
    # documents which: the value on a profile is copied from that vendor's
    # own registration or token page, never inferred from another vendor,
    # and where a vendor documents neither the field stays at this default
    # with the profile's comment saying so. build_client_assertion() in
    # core/fhir/client.py must sign with this value, and the per-vendor
    # emulators reject an assertion signed with any other algorithm as
    # invalid_client, so a wrong key type fails at rehearsal rather than
    # at the vendor.
    assertion_algorithm: str = "RS384"  # JWT client-assertion signing algorithm: RS384 unless the vendor documents ES384 (each profile says which)
    page_size: int = 50
    rate_limit_per_min: int = 60
    notes: str = ""

    # ---- writing INTO this EMR -------------------------------------
    #
    # Reading and writing are not symmetric, and the asymmetry is the
    # single most important thing this table records. Every vendor here
    # exposes a broad read API. What each accepts as a WRITE is far
    # narrower, is gated per health system, and in several cases is not a
    # general FHIR create at all.
    #
    # These fields are a STARTING POINT FOR A CONVERSATION with the
    # destination's administrator, never an assertion that a write will
    # succeed. core/fhir/delivery/writer.py checks the destination's own
    # CapabilityStatement at run time and refuses anything it does not
    # advertise, precisely because a table in this repository cannot know
    # what one health system's build has enabled.
    writable_resources: tuple[str, ...] = ()

    # Whether the vendor supports FHIR conditional create (If-None-Exist).
    # This is what makes a delivery re-runnable without duplicating every
    # record. Where it is false, delivery must be gated on an external
    # record of what was already sent - see writer.py.
    supports_conditional_create: bool = False

    # FHIR $import (Bulk Data Import). Draft, and essentially unsupported
    # commercially. Recorded so the answer to "can we bulk-load this?" is
    # visible rather than rediscovered per deployment.
    supports_bulk_import: bool = False

    write_notes: str = ""


# Confirm supported_resources against the target instance's own
# CapabilityStatement (GET {base_url}/metadata) before ingesting from it -
# Epic is federated, and what a given health system's instance actually exposes
# depends on their build and which APIs they've turned on, not just the
# platform version.
EPIC = EMRProfile(
    name="Epic",
    auth_flow="smart_backend_services",
    supported_resources=(
        "Patient",
        "Encounter",
        "Observation",
        "Condition",
        "MedicationRequest",
        "DocumentReference",
        "AllergyIntolerance",
        "Immunization",
        "Procedure",
        # Claims/billing data - see docs/DATA_SCOPE_REVIEW.md, which
        # flagged this as inside HIPAA's designated record set (45 CFR
        # 164.501) but outside this system's scope at the time. Epic
        # documents ExplanationOfBenefit.Search (Claim) as a real,
        # registerable Incoming API (fhir.epic.com/Specifications?api=1073)
        # - confirmed directly, not assumed. ExplanationOfBenefit, not the
        # lower-level Claim/ClaimResponse resources, per the FHIR spec's
        # own guidance: EOB is "for reporting out to patients... instead of
        # the Claim and ClaimResponse resources, as those resources may
        # contain provider and payer specific information which is not
        # appropriate for sharing." Whether a given Epic instance actually
        # has this module enabled and registered is, as with every entry
        # here, a separate question from whether it's listed in this tuple
        # - confirm against the instance's own CapabilityStatement, and
        # register the Incoming API on the Epic app console, before this
        # has any effect.
        "ExplanationOfBenefit",
        # The five candidate types docs/DATA_SCOPE_REVIEW.md derived
        # from CMS's hospital Conditions of Participation content list
        # (42 CFR 482.24(c)(4)), added after HIM review of that document.
        #
        # That document is explicit that these were CANDIDATES for review,
        # not a list to implement automatically, and it is worth preserving
        # why: whether a given organization's compliance posture calls for
        # retaining each of these is a determination for its HIM manager and
        # counsel, not one this codebase makes. Listing them here reflects
        # that review having happened for this deployment - it is not a
        # statement that every deployment needs all five.
        #
        # Each maps to a specific enumerated requirement:
        #   AdverseEvent            - (iv) complications, hospital-acquired
        #                             infections, adverse drug/anesthesia
        #                             reactions. Condition captures some of
        #                             this; AdverseEvent is the purpose-built
        #                             resource.
        #   Consent                 - (v) properly executed informed consent
        #                             forms. Scanned forms may also arrive via
        #                             DocumentReference; this is the
        #                             structured counterpart.
        #   ServiceRequest          - (vi) practitioners' orders. MedicationRequest
        #                             already covers medication orders
        #                             specifically; this covers the rest.
        #   MedicationAdministration- (vi) medication records. MedicationRequest
        #                             records what was ORDERED; this records
        #                             what was actually GIVEN, which is a
        #                             distinct fact and the one (iv) above
        #                             often turns on.
        #   DiagnosticReport        - (vi) radiology and laboratory reports.
        #                             Observation carries discrete results;
        #                             this carries the report and its
        #                             interpretation.
        #
        # SAME CAVEAT AS EVERY ENTRY ABOVE, and it is not a formality here:
        # none of these five has been confirmed against a real Epic
        # instance's CapabilityStatement or registered as an Incoming API on
        # a real Epic app. They are exercised against the mock server only.
        # Confirm registration before expecting any of them to return data
        # from a live instance - see UNAUTHORIZED_TYPES in
        # scripts/mock_epic_server.py, which models exactly that gap.
        "AdverseEvent",
        "Consent",
        "ServiceRequest",
        "MedicationAdministration",
        "DiagnosticReport",
    ),
    supports_bulk_export=True,
    writable_resources=("DocumentReference",),
    supports_conditional_create=False,
    supports_bulk_import=False,
    write_notes=(
        "Epic does not offer a general FHIR create for arbitrary historical "
        "clinical resources. Writing back is realistically limited to attaching "
        "documents to a chart, and even that requires the specific write API to be "
        "licensed and enabled for the health system. Treat anything beyond "
        "DocumentReference as needing confirmation from the health system's Epic "
        "analyst before it is designed around."
    ),
    notes=(
        "Backend Services auth uses an RS384-signed JWT client assertion, "
        "not a client secret. The public key half of the keypair is "
        "uploaded to the client ID's record on open.epic.com; the private "
        "key never leaves the deploying organization. Epic issues separate "
        "non-production and production client IDs per app - using the "
        "wrong one against a given base URL is the most common integration "
        "failure. See docs/EMR_CONNECTORS.md."
    ),
)


def get_profile(vendor_key: str = "epic") -> EMRProfile:
    """Look up a profile by vendor key, defaulting to Epic.

    A thin delegate to profile_for() below, kept because a default-
    argument call site reads better than repeating the default string.
    The vendor is selectable per deployment via PHI_AI_EMR_VENDOR - see
    core/config/settings.py, which validates it against PROFILES at
    startup."""
    return profile_for(vendor_key)

# ---------------------------------------------------------------------------
# The other target EMRs - every profile from here down to PROFILES, which is
# the one place the set is enumerated.
#
# All are FHIR R4 servers, so core/fhir/client.py's iter_resources() works
# against them unchanged - it does a paged GET per resource type and reads
# the `next` link, which is the specification, not an Epic behaviour. What
# differs is authentication, whether Bulk Data Export exists, and what the
# server will accept as a write.
#
# supported_resources here is the vendor's PUBLISHED surface. It is not a
# promise about any particular health system: every one of these vendors is
# federated or multi-tenant, and what a given practice has enabled is a
# separate question. Confirm against the instance's own
# CapabilityStatement (GET {base_url}/metadata) before ingesting from it -
# the same discipline the Epic profile above already documents.
# ---------------------------------------------------------------------------

CERNER = EMRProfile(
    name="Oracle Health (Cerner)",
    auth_flow="smart_backend_services",
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "MedicationAdministration", "ServiceRequest",
        "Consent", "AdverseEvent",
    ),
    supports_bulk_export=True,
    requires_token_scopes=True,
    page_size=50,
    writable_resources=("DocumentReference", "Condition", "Observation"),
    supports_conditional_create=True,
    supports_bulk_import=False,
    notes=(
        "Docs moved: fhir.cerner.com now 301-redirects to docs.oracle.com "
        "(millennium-platform-apis). Oracle documents TWO client auth modes for "
        "system apps: HTTP Basic client_id:secret per RFC 2617 (the primary "
        "documented mode), and a signed JWT with a JWKS preregistered in System "
        "Account Management - which their authorization framework calls 'the "
        "appropriate mode of authentication for Bulk Data Access'. This profile "
        "uses the JWT flow (auth_flow above) because population-scale ingestion "
        "wants $export. Either way the token request MUST carry explicit "
        "system/{Type}.read scopes - 'we do not support Wildcard scopes' - "
        "recorded as requires_token_scopes above, which makes "
        "authenticate_from_settings() derive and send them; Epic's omit-scope "
        "habit fails here. "
        "Implements SMART App Launch 1.0.0 / Backend Services 1.0.1. Tenant id "
        "forms part of the FHIR base URL (e.g. fhir-ehr.cerner.com/r4/{tenant-id})."
    ),
    write_notes=(
        "Oracle's relocated R4 docs still publish create endpoints for several "
        "types (Patient, Condition, DocumentReference POST operations). The "
        "If-None-Exist conditional-create support recorded here was taken from "
        "Cerner's pre-migration documentation and could not be re-verified on the "
        "public Oracle pages - confirm it against the tenant before designing a "
        "re-runnable delivery around it. Which types a given tenant permits is "
        "still tenant configuration."
    ),
)

ATHENAHEALTH = EMRProfile(
    name="athenahealth",
    auth_flow="oauth2_client_credentials",
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "MedicationAdministration", "ServiceRequest",
        "Consent", "AdverseEvent",
    ),
    supports_bulk_export=True,
    page_size=100,
    rate_limit_per_min=30,
    writable_resources=("DocumentReference",),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "Client-credentials OAuth with a client SECRET, not a signed JWT assertion - "
        "the one target that does not use the SMART Backend Services key flow, so it "
        "needs a different authenticate() path. Practice-scoped: the practice id is "
        "part of the FHIR base URL, and an app is enabled per practice through the "
        "Marketplace. Rate limits are tighter than the others; the lower default here "
        "is deliberate."
    ),
    write_notes=(
        "Document attachment is the realistic write path. Confirm per practice."
    ),
)

ECLINICALWORKS = EMRProfile(
    name="eClinicalWorks",
    auth_flow="smart_backend_services",
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "MedicationAdministration", "ServiceRequest",
        "Consent", "AdverseEvent",
    ),
    supports_bulk_export=True,
    page_size=50,
    # Vendor-documented ceiling, not a recommendation: "no more than 250
    # calls per minute, per base URL" applies to the FHIR resource, authorize
    # and token endpoints from Oct 2025 (fhir.eclinicalworks.com developer
    # portal). Recorded as documented; throttle well below it in practice.
    rate_limit_per_min=250,
    writable_resources=(),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "CORRECTED (2026-08 review against fhir.eclinicalworks.com): this entry "
        "previously said 'no bulk data export'. eClinicalWorks' own developer "
        "portal now documents backend (single patient) and bulk (multiple "
        "patient) FHIR APIs alongside the provider-centric ones, with Backend "
        "Services using 'Asymmetric (Private Key JWT) Authentication' against a "
        "registered JWKS - the same client-assertion flow authenticate() already "
        "speaks. Group-level $export availability for a specific practice is "
        "still a deployment question (onboarding runs through the eCW portal and "
        "may require contracting), so confirm with the practice before planning "
        "a migration around $export rather than paged search."
    ),
    write_notes=(
        "eClinicalWorks documents FHIR Create/Update APIs (Patient, Encounter, "
        "MedicationRequest, Immunization, DocumentReference variants, Coverage, "
        "ServiceRequest - V12.0.2+) but as a CONTRACTED add-on arranged through "
        "interop@eclinicalworks.com, not a default capability - which is why "
        "writable_resources above stays empty. Until a contract says otherwise, "
        "deliver to eClinicalWorks as files for their own migration tooling."
    ),
)

NEXTGEN = EMRProfile(
    name="NextGen Healthcare",
    auth_flow="smart_backend_services",
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "MedicationAdministration", "ServiceRequest",
        "Consent", "AdverseEvent",
    ),
    supports_bulk_export=False,
    page_size=50,
    writable_resources=("DocumentReference",),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "RE-VERIFIED 2026-08 against nextgen.com/api and the NGE regulatory page: "
        "NextGen Enterprise's PUBLIC documentation covers Patient Access FHIR R4 "
        "(authorization_code flow, e.g. fhir.nextgen.com/nge/prod/patient-oauth/"
        "token) and the proprietary Enterprise APIs; no system/backend flow or "
        "Enterprise-level $export is publicly documented - the full developer "
        "guides sit behind developer.nextgen.com onboarding. ONC g(10) obliges "
        "the certified stack to offer population services, so group-level export "
        "likely exists, but supports_bulk_export stays False until the gated docs "
        "or the instance's CapabilityStatement confirm it. Do not conflate with "
        "NextGen OFFICE, a separate small-practice product whose public Bulk FHIR "
        "API authenticates with Basic client_id:secret."
    ),
    write_notes="Confirm any write capability per practice before relying on it.",
)

MEDITECH = EMRProfile(
    name="MEDITECH Expanse",
    auth_flow="smart_backend_services",
    # Deliberately the NARROWEST resource list in this table: MEDITECH's
    # public documentation (the Greenfield explorer at
    # greenfield.meditech.com / fhir.meditech.com) covers the US Core R4 /
    # USCDI patient-access surface, so only the retention-relevant US Core
    # types are listed. The extended types the other profiles carry
    # (MedicationAdministration, ServiceRequest, Consent, AdverseEvent)
    # are NOT publicly documented for MEDITECH - absence here records
    # that, not a confirmed inability. Confirm against the instance's
    # CapabilityStatement, as with every entry in this file.
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport",
    ),
    supports_bulk_export=True,
    page_size=50,
    writable_resources=(),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "Register through the MEDITECH Greenfield Workspace "
        "(ehr.meditech.com/ehr-solutions/greenfield-workspace-resources); most "
        "technical detail sits behind the portal login, so this entry separates "
        "what was verified from what is certification baseline. VERIFIED from "
        "public pages: US Core FHIR R4 APIs for USCDI data; a documented Bulk "
        "Data topic in the API explorer; base URLs shaped .../v2/uscore/R4/ "
        "(the explorer documents 'v2/uscore/R4/{operation}/'), and MEDITECH "
        "appears in the SMART team's registry of bulk-data implementations. "
        "BASELINE, NOT VENDOR-CONFIRMED: the auth flow above is the ONC g(10) "
        "backend-services requirement (asymmetric JWT client assertion), which "
        "every certified population-services stack must implement - MEDITECH's "
        "public pages do not spell out their token request, so confirm the "
        "grant details, scopes and page size with MEDITECH before go-live."
    ),
    write_notes=(
        "Greenfield describes the patient-access APIs as view-only; no general "
        "FHIR create is publicly documented. Deliver to MEDITECH as files for "
        "their own conversion tooling unless the health system's MEDITECH contacts "
        "confirm a write path."
    ),
)

# ---------------------------------------------------------------------------
# ModMed - the Certified FHIR API shared by EMA, ModMed Practice Management,
# ModMed GI and gGastro.
#
# SOURCES, and only these: ModMed's "MMI Certified FHIR API Documentation"
# PDF of July 2024 (https://www.modmed.com/wp-content/uploads/2024/07/
# MMI-Certified-FHIR-API-Documentation-July-2024.pdf - "the PDF" below);
# the developer portal at https://portal.api.modmed.com ("the portal" -
# append .md to any page for its markdown, index at /llms.txt); ModMed's
# Certified API Terms of Use V2 (2022-12-23) and EMA Mandatory Disclosures
# (Feb 2025) PDFs on modmed.com; the EMA 7 compliance certificate PDF
# ModMed hosts; and ModMed's own production demonstration endpoint,
# https://fhirmp.mmi.prod.fhir.ema-api.com/fhir/r4 ("the demo endpoint"),
# whose CapabilityStatement, OperationDefinition and smart-configuration
# were read on 2026-09-01. Nothing below is carried over from the Epic
# profile: ModMed's access model, key algorithm, scope rule and bulk scope
# all differ, and where ModMed documents nothing the field keeps the
# conservative dataclass default and its comment says so. Citation trail
# and setup: the ModMed chapter of docs/EMR_CONNECTORS.md.
# ---------------------------------------------------------------------------
MODMED = EMRProfile(
    name="ModMed",
    # The portal's token-endpoint page (reference/post_auth-realms-fhir-
    # protocol-openid-connect-token): on the client_credentials grant
    # 'No client_secret - authentication is private_key_jwt instead',
    # with a 'client_assertion JWT, signed with the private key matching
    # the public JWKS your client is registered with'. That is the signed-
    # assertion flow this codebase calls smart_backend_services.
    auth_flow="smart_backend_services",
    # Same portal page: 'Signing algorithm confirmed working: ES384'. The
    # PDF's worked example (pp. 9-11) carries header alg ES384 and even an
    # extra form field encryption_method=ES384. RS384 is not documented by
    # ModMed as working, so this profile signs ES384 - an EC P-384 key,
    # not the RSA key every other profile in this file uses. The demo
    # endpoint's smart-configuration lists RS384 among a dozen algorithms,
    # but a discovery list is not a vendor statement; ES384 is.
    assertion_algorithm="ES384",
    # ModMed's PUBLISHED system-level $export type list, verbatim from the
    # portal's POST /$export page (reference/post_export, updated
    # 2026-08-28): twenty-five types. Every one of them is also a
    # read/search-type resource in the demo endpoint's CapabilityStatement
    # and has a system/{Type}.rs scope in its smart-configuration, so one
    # tuple serves the paged scheduler, the token scope string and
    # verify_export alike. Provenance is readable, searchable and
    # scope-able on ModMed but ABSENT from the published $export list, so
    # it is left out rather than have every export verification report it
    # missing. Trimming to the retention-relevant subset is a
    # per-deployment decision; the tuple records what ModMed publishes.
    supported_resources=(
        "AllergyIntolerance", "CarePlan", "CareTeam", "Condition", "Coverage",
        "Device", "DiagnosticReport", "DocumentReference", "Encounter", "Goal",
        "Immunization", "Location", "Medication", "MedicationDispense", "MedicationRequest",
        "Observation", "Organization", "Patient", "Practitioner", "PractitionerRole",
        "Procedure", "QuestionnaireResponse", "RelatedPerson", "ServiceRequest", "Specimen",
    ),
    # The PDF (p. 13): '{base_url}/$export', '{base_url}/Patient/$export'
    # and '{base_url}/Group/1.105681.22.0.1/$export'; the portal documents
    # POST /$export and POST /Patient/$export with _outputFormat, _since
    # and _type, async with a status URL. Vendor-documented, not inferred.
    supports_bulk_export=True,
    # The portal's token page: scope 'is required on this grant
    # (space-separated system/*.rs scopes) - it's not implied the way it
    # can be for the other two grants.' NOTE the syntax: ModMed documents,
    # and its registration screen lists, only .rs scopes; the string this
    # flag makes authenticate_from_settings() derive is system/{Type}.read.
    # See notes - this is an open client seam, not a ModMed ambiguity.
    requires_token_scopes=True,
    # Not documented by the vendor for the Certified FHIR API. The
    # portal's 'Count and Pagination' page (Patient max page 50, a `page`
    # parameter) describes the Proprietary /fhir/v2 API - a different
    # server. Dataclass default, recorded explicitly.
    page_size=50,
    # Not documented by the vendor for the Certified FHIR API. The
    # portal's 'Rate Limiting' page ('each API key is limited to 1250
    # calls per minute') is written for the Proprietary API's x-api-key
    # credentials, and the Certified API Terms of Use (V2, 3.4.7) only
    # oblige a client to 'not generate excessive load'. Dataclass default.
    rate_limit_per_min=60,
    # The PDF (p. 1): 'this Certified FHIR API supports only Read, Search,
    # and Bulk operations'; the portal's Getting Started: 'READ and SEARCH
    # only (no WRITE capabilities) + Bulk FHIR'. The demo endpoint's
    # CapabilityStatement advertises read and search-type for every
    # resource and create for none. Empty is the documented answer.
    writable_resources=(),
    # Nothing to conditionally create on a read-only surface; not
    # documented by the vendor.
    supports_conditional_create=False,
    # Not documented by the vendor; $import appears nowhere in ModMed's
    # documentation.
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09-01 from ModMed's own documentation (the July 2024 "
        "Certified FHIR API PDF, portal.api.modmed.com, the Certified API Terms "
        "of Use V2, the Feb 2025 Mandatory Disclosures, the EMA 7 certificate) "
        "and from ModMed's production demonstration endpoint; nothing here is "
        "an inference from another vendor's behaviour. "
        "ACCESS MODEL - one endpoint per practice, activated by the practice: "
        "'FHIR endpoints are customer-specific. Each practice has its own "
        "Certified FHIR endpoint' (portal). A practice base URL has the shape "
        "https://{firm}.mmi.prod.fhir.ema-api.com/fhir/r4 (the portal's OpenAPI "
        "server variable is 'Your firm subdomain (e.g. fhirmp, auraderm)'). "
        "ModMed publishes every practice in a directory app at "
        "mm-fhir-endpoint-display.prod.fhir.ema-api.com, which reads two public "
        "FHIR Endpoint bundles: public-api.mmi.prod.fhir.ema-api.com/fhir/r4/"
        "Endpoint (EMA/PM; filter connection-type=hl7-fhir-rest; addresses "
        "observed on 2026-09-01 were mostly https://{firm}.ef.prod.fhir.ema-api"
        ".com/fhir/r4/, some .mmi.prod.) and public-api.gastro.prod.fhir.ema-api"
        ".com/fhir/r4/Endpoint (gGastro; https://{uuid}.gastro.prod.fhir.ema-api"
        ".com/fhir/r4/). 'Vendors will first need to know the base url of the "
        "practice they want to integrate with' (PDF p. 13), so PHI_AI_FHIR_BASE_URL "
        "is per practice, and the access token ModMed mints carries an "
        "allowedFhirUrl claim naming ONE practice base URL (PDF p. 12): one "
        "practice, one token. A practice's own /metadata advertises its authorize "
        "and token URLs under {base}/auth/realms/fhir/protocol/openid-connect/ "
        "(demo endpoint); the PDF's worked example posts to "
        "https://sso.ema.md/auth/realms/fhir/protocol/openid-connect/token "
        "directly, and the assertion's aud must be 'this token endpoint URL' "
        "(portal) - use whichever URL you post to, and that is PHI_AI_FHIR_TOKEN_URL. "
        "REGISTRATION AND CONSENT: self-service at "
        "https://fhir-vendor-dashboard.kube.prod.mmicse.com/ (PDF p. 3; portal). "
        "Choose App Type 'Bulk' for unattended practice-level access - 'This "
        "allows an Admin at the practice to add your ClientId to their practice "
        "one time' (portal, register page); Access Type Client-Confidential; "
        "FHIR Version v4.0.1; the dashboard also takes Launch/Redirect/Logo/"
        "Policy/Terms of Service URLs and the scope list (PDF pp. 36-39). A new "
        "app 'will be created in a Disabled state' (PDF p. 5). Non-Bulk apps are "
        "enabled by ModMed - 'MMI will review new apps daily and Enable apps that "
        "are configured correctly' - but a Bulk app is enabled by the PRACTICE, "
        "not by ModMed: 'A Practice can provide your app consent by adding your "
        "app's ClientID to their Manage Bulk FHIR section in their Admin section' "
        "and 'Once a customer has added you, your app will become Enabled' (PDF "
        "pp. 3, 8). The Terms add that ModMed 'may request additional "
        "information' during registration review (2.3.1), that the customer must "
        "'activate use of such CAPIs' (2.3.3), and that 'Credentials may not be "
        "embedded in open source projects' (2.4). There is no separate sandbox "
        "for the Certified API: ModMed's 'production demonstration endpoint' "
        "fhirmp.mmi.prod.fhir.ema-api.com/fhir/r4 'behaves like any customer "
        "endpoint' (portal), and its /metadata answers without a token. Where in "
        "the dashboard the JWKS is entered (URL or inline) is not shown in the "
        "PDF's field list - the portal only says the client is 'registered with' "
        "a public JWKS; confirm on the dashboard. "
        "AUTH: grant_type=client_credentials with "
        "client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer "
        "and a client_assertion whose 'iss/sub = your client_id, aud = this token "
        "endpoint URL, plus jti/iat/exp (a short expiry - 5 minutes is typical)' "
        "(portal); the example header carries a kid, so PHI_AI_FHIR_JWT_KID must "
        "name the key in the registered JWKS. 'Signing algorithm confirmed "
        "working: ES384' - recorded as assertion_algorithm above; the key is EC "
        "P-384 (secp384r1). client_id is not a form parameter on this grant "
        "('client_credentials identifies the client via the client_assertion "
        "JWT's iss/sub claims instead') and client_secret is 'never sent on the "
        "client_credentials grant' (portal) - PHI_AI_FHIR_CLIENT_SECRET is inert "
        "here. The PDF's example also posts a form field encryption_method=ES384 "
        "that no standard defines; whether it is required is not documented by "
        "the vendor and this client does not send it. The example token has "
        "expires_in 1800 (PDF p. 12) - 30 minutes; this client never refreshes "
        "mid-run, so a bulk poll loop longer than the token lifetime will see 401 "
        "at the status URL (confirm expires_in on the instance). Observed on "
        "2026-09-01, not documented: sso.ema.md answers a missing or malformed "
        "client_assertion with HTTP 401 {error: invalid_client, error_description: "
        "'Invalid client or Invalid client credentials'}, and a practice endpoint "
        "answers an unauthenticated read or $export with HTTP 401, "
        "WWW-Authenticate: Bearer, empty body. "
        "SCOPES: 'scope is required on this grant (space-separated system/*.rs "
        "scopes)' (portal) - recorded as requires_token_scopes above. ModMed's "
        "registration screen (PDF pp. 37-39), its worked example (PDF p. 9) and "
        "the demo endpoint's scopes_supported list use ONLY the .rs form "
        "(system/Patient.rs); the portal calls this 'SMART v1 syntax' although "
        ".rs is the SMART App Launch 2 grammar. authenticate_from_settings() "
        "derives system/{Type}.read - the v1 form - from supported_resources. "
        "Whether ModMed accepts .read is not documented by the vendor and the demo "
        "endpoint advertises no .read scope, so until the client can emit .rs a "
        "live ModMed token request carries scopes ModMed does not list - an open "
        "client change, flagged in docs/EMR_CONNECTORS.md. The granted scopes "
        "also gate export: 'If a requested/available type isn't covered by the "
        "token's granted scopes, that type is silently skipped (not a failed "
        "request) and a note is written to the error file in the completion "
        "manifest instead' (portal, POST /$export). "
        "BULK: three kick-off shapes are documented - {base_url}/$export "
        "(system), {base_url}/Patient/$export ('returns FHIR resources in the "
        "USCDI data set') and {base_url}/Group/{id}/$export (PDF p. 13). Groups "
        "are not self-service: 'If you require a group or cohort of patients, "
        "the Patient IDs must be defined and then someone at MMI can create a "
        "group. Please reach out to synapsys@modmed.com' (PDF p. 13); Group "
        "supports READ only (PDF p. 25), so a Group id is never discoverable by "
        "search - it is PHI_AI_FHIR_GROUP_ID, obtained from ModMed. The portal "
        "documents the kick-off as POST with parameters 'sent as query params on "
        "the POST, not as a FHIR Parameters resource body'; only _outputFormat, "
        "_since and _type are accepted, and sending '_until, _elements, patient, "
        "includeAssociatedData, _typeFilter, organizeOutputBy, "
        "allowPartialManifests' 'fails the request'. _since IS supported ('Only "
        "include resources with meta.lastUpdated after this instant') - "
        "bulk_client.kickoff_export() does not yet pass it. 'Only ndjson format "
        "is supported for the Output' (PDF p. 13); a csv request gets 400 whose "
        "message 'would state Invalid Tenant' (PDF p. 14). The kick-off 'always "
        "runs asynchronously' with Content-Location = {base}/fhir-services/"
        "$export-status/{jobId}; polling 'returns 202 with a Status: In Progress "
        "header (note: not the Bulk Data IG's X-Progress header) and Retry-After: "
        "120 (a fixed value, not based on job size)', then '200 with the export "
        "manifest as the body (application/json, not application/fhir+json)' "
        "(portal, export status page) - so PHI_AI_BULK_POLL_INTERVAL_SECONDS=120 "
        "matches the vendor, and bulk_client.poll_status()'s X-Progress log "
        "detail will simply be empty. The manifest example carries "
        "expirationTime, requiresAccessToken false, output[], deleted[] and "
        "error[]; file URLs are Amazon S3 URLs (PDF p. 14) with 'a maximum of "
        "1000 resources' per file. bulk_client.iter_ndjson_resources() sends the "
        "bearer token to every file URL regardless of requiresAccessToken - "
        "honour the flag before the first live download. Status responses: 401 "
        "unauthorized, 404 'not found (includes a cancelled job)', 500 'the "
        "export job failed'; DELETE on the status URL 'cancels an in-progress "
        "export and deletes its output', 202, after which GET returns 404 "
        "(portal). ModMed documents no export throttle, no group-size guidance "
        "and no retention window (the manifest's expirationTime is the only "
        "signal) - none is assumed here. Kick-off method mismatch to confirm on "
        "the instance: this codebase issues GET Group/{id}/$export; ModMed "
        "documents POST, and the demo endpoint's OperationDefinition "
        "GroupPatient-it-export declares export for Group and Patient at type "
        "and instance level with system=false, while the portal documents "
        "system-level POST /$export - two ModMed sources that do not agree. "
        "VERSIONS AND CERTIFICATION: FHIR R4 4.0.1. The PDF says 'MMI has "
        "implemented the US Core Implementation Guide - 4.0.0 - STU4 Release' "
        "(its USCDI v1 mapping table); the demo endpoint instantiates the "
        "us-core-server CapabilityStatement and serves the later Coverage, "
        "MedicationDispense, Specimen and QuestionnaireResponse types - the "
        "instance's /metadata decides. 'For MMI EMA systems, the version of the "
        "software required is version 7.0 or higher'; the gGastro minimum is a "
        "placeholder ('version xxxxx') in ModMed's own PDF - not documented. TLS "
        "1.2 or higher required. 'ModMed utilizes SVAP Version Approved: SMART "
        "App Launch 2.0'; the demo endpoint's smart-configuration lists "
        "client-confidential-asymmetric, permission-v2 and S256. EMA 7 is "
        "certified by Drummond Group to 170.315 (g)(2-7, 9-10), certificate "
        "15.04.04.2002.EMA6.70.18.1.221129, date certified 11/29/2022 "
        "(ModMed-hosted certificate PDF, Jan 2026); (g)(10) obliges Backend "
        "Services authorization and group-level export, both of which ModMed "
        "documents directly rather than by implication. ModMed's ONC "
        "certification page lists EMA only - whether gGastro / ModMed GI hold "
        "their own (g)(10) certificate is not documented there; check CHPL. "
        "FEES: 'No fee charged for certified APIs' (Mandatory Disclosures, Feb "
        "2025, criteria (g)(7), (g)(9), (g)(10)); 'Currently, ModMed does not "
        "charge any fees specific to the CAPI' (Terms V2, 4.2). The Proprietary "
        "API is offered 'for a fee'. "
        "CONFIRM ON THE INSTANCE, as with every entry in this file: GET "
        "{base_url}/metadata and {base_url}/.well-known/smart-configuration of "
        "the practice's own endpoint - the resource list, the export operation "
        "on Group and Patient, the advertised token URL, ES384 in "
        "token_endpoint_auth_signing_alg_values_supported and the .rs scopes; "
        "then whether GET kick-off and Group-level export work on that practice, "
        "and the real expires_in. See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "The Certified FHIR API is read-only by ModMed's own statement - "
        "'supports only Read, Search, and Bulk operations' (PDF) and 'READ and "
        "SEARCH only (no WRITE capabilities) + Bulk FHIR' (portal) - which is why "
        "writable_resources above is empty. core/fhir/delivery/writer.py reads the "
        "destination's CapabilityStatement and will find create advertised for "
        "nothing, so every delivered type is skipped; that is correct behaviour, "
        "not a defect. The only write path ModMed documents is the EMA "
        "Proprietary API - a SECOND CLIENT, not this profile: FHIR R4-style "
        "resources under https://mmapi.ema-api.com/ema-prod/firm/{firm_url_prefix}/"
        "ema/fhir/v2/ with an x-api-key header plus an OAuth2 token (a new "
        "client_credentials flow and a legacy password grant 'being sunset'), a "
        "public sandbox at stage.ema-api.com, access through the synapSYS "
        "Marketplace with a 'technical review before they are permitted to gain "
        "access to their first customer's production system', '1250 calls per "
        "minute' per API key, EMA and Practice Management only ('It will not be "
        "able to support gGastro customers'), and offered 'for a fee'. Its "
        "documented creates/updates: Patient, Appointment, Task, Condition, "
        "AllergyIntolerance, MedicationStatement, Coverage, Composition, referring "
        "Practitioner and Organization, ChargeItem (into ModMed PM) and 'Upload "
        "document from S3 URL to EMA' (DocumentReference). Delivering a chart into "
        "ModMed therefore means the Proprietary API's DocumentReference upload - a "
        "per-practice, contracted, reviewed integration this profile cannot "
        "promise. core/fhir/delivery/__main__.py builds its destination token "
        "request on this profile (ES384, and - because requires_token_scopes is "
        "True - one system/{Type}.write scope per writable_resources entry); "
        "with writable_resources empty it sends no scope, ModMed refuses the "
        "scope-less request, and nothing reaches the FHIR server - the correct "
        "outcome for a read-only API."
    ),
)

# Altera Digital Health - Sunrise (acute), TouchWorks (ambulatory), Paragon
# and dbMotion, fronted by one "Altera FHIR" R4 server family and one
# developer portal. Every fact below is from Altera's own pages on
# developer.adp.ahcentral.com (the production portal host - Altera's sign-up
# instructions and its ONC compliance page both name it) or, where marked
# 'adpstg', from developer.adpstg.ahcentral.com (a STAGING host the research
# memo reached; its ProcessOverview wording differs from the production
# host's and is cited as such), from Altera's published Drummond
# certificates, or from the
# ADP sandboxes' own /metadata and /.well-known/smart-configuration, all
# fetched 2026-09-01. Where Altera documents nothing, the field stays at
# the dataclass default and the comment says so - nothing here is
# inherited from Epic or any other entry in this file.
ALTERA = EMRProfile(
    name="Altera Digital Health",
    # System apps 'make a direct call to the Token URL' whose body 'must
    # include' 'client_assertion: Indicates a token generated using a
    # private key', client_assertion_type jwt-bearer and grant_type
    # client_credentials (developer.adp.ahcentral.com/Fhir/FHIR_Sandboxes,
    # 'System Applications'; developer.adp.ahcentral.com/Fhir/SMARTonFHIR,
    # 'SMART on FHIR Backend Services (System Callers)').
    auth_flow="smart_backend_services",
    # assertion_algorithm is deliberately NOT set. Altera names no JWT
    # signing algorithm on any page, and the sandbox authorization
    # server's openid-configuration publishes no
    # token_endpoint_auth_signing_alg_values_supported (its
    # request_object_signing_alg_values_supported list is about request
    # objects, not client assertions, and is not evidence). The dataclass
    # default therefore stands as the value to CONFIRM on the sandbox with
    # a registered client, not as an Altera fact.
    #
    # The retention-relevant subset of Altera's PUBLISHED R4 (US) list
    # (developer.adp.ahcentral.com/Fhir/Resources, 'Summary of FHIR
    # Resources'; the Bulk Data page lists 28 extractable types). Altera
    # also publishes Binary, CarePlan, CareTeam, Coverage, Device, Goal,
    # Group, Location, Medication, MedicationDispense, MedicationStatement,
    # Organization, Practitioner, PractitionerRole, Provenance,
    # RelatedPerson and Specimen; they are omitted because this tuple is
    # load-bearing twice over - the schedulers ingest every type listed,
    # and with requires_token_scopes below every type becomes a requested
    # system/{Type}.read scope. Consent and AdverseEvent, which other
    # entries in this file carry, are NOT published by Altera and are
    # absent for that reason. Provenance is left out on purpose: Altera
    # 'does not currently support searching on the Provenance resource'
    # (Searching page), which the paged scheduler would need - see notes
    # for the bulk-export consequence.
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "ServiceRequest",
        # The Resources page badges MedicationAdministration 'R4 (UK)' only,
        # yet the Bulk Data page lists it among the extractable resources
        # and both US sandboxes advertise read/search-type for it and
        # publish system/MedicationAdministration.read. Two Altera pages
        # disagree; the instance's /metadata decides. Drop it from this
        # tuple if the instance does not advertise it, because a listed
        # type is also a requested scope.
        "MedicationAdministration",
    ),
    # '[FHIR path]/Group/INF-101/$export'; 'Only FHIR applications of the
    # type System can send bulk data requests'; 'Backend authentication
    # for access tokens via JWKS must be configured'
    # (developer.adp.ahcentral.com/Fhir/BulkData). Both ADP sandboxes
    # instantiate the HL7 bulk-data CapabilityStatement and advertise
    # $export on Group. Corroborated, not established, by 170.315 (g)(10)
    # on Altera's published Sunrise Acute Care 25.1 and TouchWorks EHR
    # 2026 certificates (alterahealth.com/legal/onc-reg-compliance/).
    supports_bulk_export=True,
    # Altera lists 'scope' among the parameters the token body 'must
    # include' - its own example is 'system/*.read (SMART v1) or
    # system/*.rs (SMART v2)' (FHIR_Sandboxes page). True here makes
    # authenticate_from_settings() send one explicit system/{Type}.read
    # per type above instead of no scope at all; every one of those
    # appears in the sandboxes' scopes_supported. Altera does NOT document
    # any refusal of wildcard scopes - this flag is set for the
    # scope-required half of its meaning only.
    requires_token_scopes=True,
    # Not documented by the vendor: _count is a supported common search
    # parameter (Searching page) but no page cap is published. File
    # default kept.
    page_size=50,
    # Not documented by the vendor: no request rate limit appears on any
    # Altera page (the only 429 documented is for polling a bulk status
    # URL before its Retry-After). File default kept as a client-side
    # courtesy throttle, not an Altera figure.
    rate_limit_per_min=60,
    # 'The Altera FHIR API is limited to read-only access and not
    # write-backs.' (developer.adp.ahcentral.com/Fhir/ProcessOverview)
    writable_resources=(),
    # Not documented by the vendor - a read-only API has nothing for
    # If-None-Exist to apply to.
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09-01 against Altera's own developer portal "
        "(developer.adp.ahcentral.com, the production host; where a quotation "
        "below comes from the developer.adpstg.ahcentral.com STAGING host, whose "
        "ProcessOverview wording differs, it says so), Altera's published "
        "Drummond certificates and "
        "the ADP sandboxes' own /metadata and /.well-known/smart-configuration. "
        "One portal and one 'Altera FHIR' R4 server family front Sunrise, "
        "TouchWorks, Paragon and dbMotion: 'supports FHIR release 4', 'new "
        "deployment of DSTU2 APIs are prohibited as of 6/1/2025', 'Any "
        "applications onboarding after 1/1/2026 should implement FHIR R4'. "
        "(Paragon also has a separate 'Paragon Open API'; this portal's FHIR "
        "documentation is what applies here.) US Core 6.1.0 needs Altera FHIR "
        "25.4+ (Sunrise 25.1 PR2+, TouchWorks 25.4.1+, Paragon Denali 25.1+, "
        "dbMotion 26.1+); older installs serve US Core 3.1.1. "
        "ACCESS MODEL - per client organisation: each Altera client's FHIR "
        "install registers its endpoints in Altera's 'downtown' environment, "
        "and the public Endpoint Directory (main.open.ahcentral.com/"
        "fhirendpoints) lists only production endpoints - provider/system ones "
        "'generally end in /fhir', patient ones in /open, some Altera-hosted "
        "(fhir.fhirpoint.ahcentral.com/fhirroute/...) and some client-hosted. "
        "PHI_AI_FHIR_BASE_URL is therefore per client, and the token URL comes "
        "from that instance's /metadata security extension. 'Versionless' "
        "endpoints serve DSTU2 and R4 on one URL with the default 'specified at "
        "the time FHIR is installed'; Altera's remedy is 'Accept: "
        "application/fhir+json; fhirVersion=4.0'. This client sends a plain "
        "application/fhir+json Accept, so use the R4-specific base URL and "
        "check fhirVersion in /metadata. "
        "REGISTRATION: self-service sign-up ('register as a corporate account'); "
        "register a FHIR app with App Type 'System' ('an external system, not a "
        "physician or provider'), a JWKS URL ('the URL for backend "
        "authentication access (JWKS) tokens'), Client Type Confidential, a "
        "Purpose of Use and the scopes it needs. The portal issues a Client ID, "
        "a Secret and a Secret Expiration Date. A new app 'is allowed for "
        "testing only' until the developer self-attests and clicks Request "
        "Production Access; the PRODUCTION host's ProcessOverview then says "
        "'Once production access is approved by Altera and licensed by the "
        "Altera client'. The STAGING host (developer.adpstg.ahcentral.com/Fhir/"
        "ProcessOverview - not the production page) adds that the app is "
        "'reviewed and, if appropriate, approved by Altera Connect' and 'Do "
        "not request production access for the application until the "
        "application name, type, and Purpose of Use are finalized ... These "
        "values cannot be changed once production access is granted' - "
        "staging-portal wording, confirm with ADP@alterahealth.com. Approval "
        "is not access: 'the clients must "
        "activate applications themselves through the License Management "
        "Portal (LMP)', where they 'can decide to grant all the requested "
        "authorization scopes or deny some scopes', and 'Some data may be "
        "restricted to access for the purpose of use by the client "
        "licensing.' Integrators cannot see the LMP documentation. "
        "AUTH (auth_flow above): the token body 'must include' "
        "client_assertion ('a token generated using a private key. The key "
        "must be signed by a certificate authority'), client_assertion_type "
        "jwt-bearer, grant_type client_credentials and scope. The assertion "
        "'includes an expiration time, generally two to 20 minutes, and can "
        "only be used once' - build_client_assertion()'s 4-minute exp and "
        "per-request jti fit inside that. Public keys are pulled from the "
        "registered JWKS URL by 'a nightly job ... that cycles through all "
        "registered FHIR system applications' and the result is 'downloaded to "
        "the client systems', so a new or rotated key is not live until that "
        "job has run - plan rotations a day ahead, and keep the JWKS URL "
        "reachable from Altera's side, not just yours. The Secret the portal "
        "issues belongs to the user-facing confidential-client flows; no "
        "Altera page documents a secret-only system grant and the sandboxes' "
        "discovery documents list only client_secret_basic/post and "
        "tls_client_auth as token_endpoint_auth_methods while also advertising "
        "the SMART client-confidential-asymmetric capability - so "
        "PHI_AI_FHIR_CLIENT_SECRET stays unset and the assertion is the "
        "documented credential. Signing algorithm, key size, kid handling and "
        "whether a self-signed key in a JWKS satisfies 'signed by a certificate "
        "authority': not documented by the vendor - confirm on the sandbox "
        "before go-live (see the assertion_algorithm comment above). "
        "SCOPES (requires_token_scopes above): the derived per-type "
        "system/{Type}.read string is SMART v1 grammar. 'Newly registered "
        "applications are assigned V1 scopes by default'; migration to V2 is "
        "one-way ('you won't be able to switch back'); V2 needs Altera FHIR "
        "25.4+ on the client's EHR; and the US Core 6.1 sandbox base "
        "advertises only v2 (.r/.rs) grammar in scopes_supported. Keep the "
        "registration on V1 until this client can emit .rs scopes, and test "
        "against the base whose scopes_supported lists the grammar you send. "
        "What Altera returns for a requested-but-denied scope is not "
        "documented. "
        "BULK (supports_bulk_export above): Group-level only - the sandboxes "
        "advertise $export on Group and nothing at system or Patient level, "
        "and no page documents either. Group IDs are created inside each EHR "
        "('Altera TouchWorks EHR uses the Patient List function') and are "
        "discoverable: 'To obtain a specific Group resource ID, you can query "
        "the Group resource' (GET {base}/Group). The run is asynchronous: 202 "
        "+ Content-Location; polls answer with X-Progress and 'Retry-After: "
        "... measured in seconds. If a status request is made prior to the "
        "retry-after date/time, the FHIR API responds with a HTTP 429'; "
        "completion carries an Expires header after which files 'are no "
        "longer available'; partial success returns both output and error "
        "file lists ('requiresAccessToken': true in Altera's sample); DELETE "
        "on the Content-Location cancels. bulk_client.poll_status() ignores "
        "Retry-After and raises on 429, so set "
        "PHI_AI_BULK_POLL_INTERVAL_SECONDS above any Retry-After you observe "
        "(typical values: not documented by the vendor). No _since, no "
        "export-frequency limit, no group-size guidance and no file-retention "
        "period are documented - recorded as absent, not as permission. "
        "Provenance rides along only when _type is omitted or names it; "
        "bulk_scheduler always passes _type = supported_resources, so this "
        "profile's exports carry no Provenance until Provenance can be "
        "requested for bulk without also being paged. bulk_scheduler's "
        "missing-Group-ID error text still names Epic's mailbox; read it as "
        "'query GET {base}/Group' here. "
        "CERTIFICATION: Altera publishes Drummond certificates for Sunrise "
        "Acute Care 25.1 (15.04.04.3123.Sunr.25.10.1.250905, certified "
        "09/05/2025) and TouchWorks EHR 2026 (15.04.04.3123.Touc.26.13.1.260316, "
        "03/16/2026) with 170.315 (g)(2-7, 9-10) among the criteria tested, "
        "and its ONC page names developer.adp.ahcentral.com/Fhir/"
        "ProcessOverview as the FHIR terms/specification link. (g)(10) "
        "corroborates the backend-services and group-export facts above; it "
        "is not their source. "
        "LIMITS: no request rate limit is documented (page_size and "
        "rate_limit_per_min above are this file's defaults). Search errors are "
        "'401: Unauthorized, 403: Forbidden, 404: Not found, 413: Request too "
        "large'; data may return marked 'redacted' when 'the Altera Clinical "
        "Authorization Service determines that the person requesting the data "
        "is not authorized to view it'. Altera describes the search API as "
        "for 'a single patient or small group of patients' and says bulk "
        "exists because paging a population 'is not technically feasible' - "
        "whether an unanchored GET {base}/Patient is refused is not "
        "documented; check it on the instance. "
        "FEES: 'Open accounts are free to build and deploy by anyone'; the "
        "FHIR API license is 'Variable rates, starting at $0'; Integrator "
        "tiers ($49-$2,499/month, May 2026 fee sheet) are for the proprietary "
        "Unity API; 'Certification required for Unity but optional for FHIR' "
        "($3,500 each beyond the tier allowance). See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "'The Altera FHIR API is limited to read-only access and not "
        "write-backs.' (developer.adp.ahcentral.com/Fhir/ProcessOverview) - "
        "which is why writable_resources above is empty and "
        "supports_conditional_create stays False. The documented write path "
        "is the proprietary, bidirectional Unity API ('enabling both reads and "
        "writes'; demographics, appointments and financial data go through "
        "Unity), sold under the Integrator membership tiers and outside FHIR "
        "entirely: a second client, not this profile. Two things to know "
        "before designing a delivery anyway. (1) The ADP sandboxes' own "
        "CapabilityStatements advertise create and update on Condition, "
        "AllergyIntolerance, MedicationRequest, Observation, Immunization, "
        "DocumentReference, MedicationStatement, MedicationAdministration, "
        "ServiceRequest, Questionnaire and QuestionnaireResponse, so "
        "core/fhir/delivery/writer.py - which trusts the live /metadata - "
        "would NOT refuse a POST there. Whether a System app's token may "
        "exercise those interactions is not documented by the vendor (the "
        "SMART on FHIR page's only 'create' sentence is about the EHR itself "
        "calling the API); treat a sandbox success as unconfirmed until "
        "ADP@alterahealth.com says otherwise. (2) core/fhir/delivery/"
        "__main__.py mints the destination token with the Epic profile, i.e. "
        "with no scope parameter, and Altera documents scope as required in "
        "the token body - expect that token request to fail before any write "
        "is attempted. Deliver to Altera sites as files for their own tooling "
        "unless a Unity integration is contracted."
    ),
)

# Greenway Health - Intergy and Prime Suite share one FHIR R4 API
# ('consistent for all of our EHR products'). Every value below is taken
# from developers.greenwayhealth.com (public - 'No registration login is
# required to view our documentation'), from Greenway's own certification
# documents, or from one production tenant's own conformance statement;
# the citation for each non-default field sits directly above it. Where
# Greenway documents nothing, the field stays at the dataclass default and
# the comment says so. Confirm against the tenant's own CapabilityStatement
# (GET {base_url}/metadata) before ingesting from it, as with every entry
# in this file.
GREENWAY = EMRProfile(
    name="Greenway Health",
    # 'backend services use client credentials which are comprised of a
    # public/private JWKS key pair' - a signed JWT client assertion. No
    # client secret is documented for backend services, so
    # PHI_AI_FHIR_CLIENT_SECRET is inert for this vendor.
    # https://developers.greenwayhealth.com/developer-platform/docs/how-to-create-a-backend-services-application
    auth_flow="smart_backend_services",
    # Greenway's registration form asks for 'a URL where your ES384 JWKS
    # Public Key resides' and instructs: 'Use the ES384 (ECDSA using P-384
    # and SHA-384) signature algorithm when generating your keys.' Not
    # RS384: core/fhir/client.py's hard-coded RS384/RSA assumption has to
    # yield to this field (and the key file must be EC P-384) before a
    # live token request can succeed here.
    # https://developers.greenwayhealth.com/developer-platform/docs/how-to-create-a-backend-services-application
    assertion_algorithm="ES384",
    # Greenway's PUBLISHED patient-record surface, restricted to this
    # project's retention scope. Sources: the resource list on
    # https://developers.greenwayhealth.com/developer-platform/docs/getting-started
    # ('a list of most of the resources available in our API'), the API
    # reference index at
    # https://developers.greenwayhealth.com/developer-platform/reference/getting-started-1
    # (which adds ServiceRequest and MedicationDispense endpoints), and one
    # production tenant's own /metadata (observed 2026-09-01), which
    # advertises read + search-type for every type below and nothing else.
    #
    # Load-bearing twice over: the schedulers iterate exactly this list,
    # and because requires_token_scopes is True each entry becomes a
    # system/{Type}.read scope in the token request - so every type here
    # must also be among the scopes selected on the app registration (see
    # notes; a scope beyond what the app was granted is refused).
    #
    # Deliberately ABSENT, with the reason:
    #   Binary            - published, but the observed conformance
    #                       statement advertises read only, no
    #                       search-type, and iter_resources() pages by
    #                       search. Document content arrives through
    #                       DocumentReference.
    #   Medication        - on the getting-started list, but there is no
    #                       reference endpoint for it and the observed
    #                       tenant does not advertise it; a referenced
    #                       resource, not a patient record.
    #   Location, Organization, Practitioner, PractitionerRole, Person,
    #   RelatedPerson, Specimen, Group
    #                     - published, but reference/administrative data
    #                       rather than patient records. Group is read at
    #                       run time to find the $export target, not
    #                       archived.
    #   Appointment, Consent
    #                     - 'not supported by our FHIR ecosystem'; a scope
    #                       for either is refused with invalid_scope
    #                       (docs/unsupported-scopes-removal). Consent
    #                       appears in other profiles in this file; it
    #                       must not appear here.
    #   AdverseEvent, MedicationAdministration, ExplanationOfBenefit
    #                     - not published by Greenway at all. Absence
    #                       records that, not a confirmed inability.
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "MedicationDispense", "DocumentReference", "AllergyIntolerance",
        "Immunization", "Procedure", "DiagnosticReport", "ServiceRequest",
        "CarePlan", "CareTeam", "Goal", "Device", "Coverage", "Provenance",
    ),
    # 'FHIR Bulk Export is authenticated using Backend Services
    # Authorization' - documented as GET {base}/Group/{id}/$export with
    # Prefer: respond-async, and the observed conformance statement
    # carries the export operation on Group. Group-level only. See notes
    # for the 24-hour _since default, which is the operational trap.
    # https://developers.greenwayhealth.com/developer-platform/docs/fhir-bulk-access
    supports_bulk_export=True,
    # Greenway's documented token payload is grant_type,
    # client_assertion_type, client_assertion AND 'scope: SMART-on-FHIR
    # scopes needed for the app' - the scope parameter is part of the
    # request. True here makes authenticate_from_settings() send one
    # explicit system/{Type}.read per supported resource. This is
    # Greenway's own documented shape, not Oracle Health's wildcard rule:
    # Greenway does not document wildcard handling at all, and the
    # explicit list never has to find out.
    # https://developers.greenwayhealth.com/developer-platform/docs/jwks
    requires_token_scopes=True,
    # page_size and rate_limit_per_min are deliberately NOT set: Greenway
    # documents neither a _count ceiling nor a rate limit, so both stay at
    # the dataclass defaults (50 / 60 per minute) rather than borrowing
    # another vendor's figure. Its Terms of Service reserve the right to
    # 'impose limits on certain features and services' and to act on 'an
    # unreasonable or disproportionately large load'; the default
    # throttle is the conservative reading of that.
    # https://developers.greenwayhealth.com/developer-platform/page/terms-of-service
    #
    # 'The current Greenway FHIR API supports read operations only at the
    # present time.' No FHIR write surface, so nothing is listed and
    # conditional create cannot exist. The proprietary GAPI is where
    # Greenway writes - see write_notes.
    # https://developers.greenwayhealth.com/developer-platform/docs/api-an-overview
    writable_resources=(),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09 against developers.greenwayhealth.com and against "
        "one production tenant's own /metadata and "
        "/.well-known/smart-configuration, reached through Greenway's public "
        "endpoint bundle. Nothing here comes from a third party. "
        "PRODUCTS AND TENANTS: one FHIR R4 API 'consistent for all of our "
        "EHR products', serving Intergy and Prime Suite (docs/api-an-overview). "
        "A tenant is a site: the base URL is "
        "https://fhir-api.fhirprod.aws.greenwayhealth.com/fhir/R4/{TENANT_ID}, "
        "where TENANT_ID is the site's OID, and Greenway publishes every "
        "customer endpoint as a FHIR Bundle of Endpoint+Organization pairs at "
        "fhir-servicebaseurl.fhirhlprod.greenwayhealth.com/servicebundle.json "
        "(1,333 pairs on 2026-09-01). The URL is therefore discoverable; the "
        "authorisation to use it is not. An Intergy tenant may hold several "
        "practices: 'you can designate which practice(s) the Back-end Service "
        "can access' (docs/how-to-create-a-backend-services-application). "
        "AUTH (auth_flow / assertion_algorithm above): the JWKS walkthrough "
        "(docs/jwks) fixes the token endpoint at "
        "https://auth-api.login.greenwayhealth.com/oauth2/as/token.oauth2 for "
        "EVERY tenant - only the FHIR base URL is per-tenant - and requires "
        "aud = that token endpoint, iss and sub = client ID, jti = a UUID, "
        "and 'The exp claim in the JWT assertion must not exceed 60 minutes "
        "from the time of issuance ... Assertions with longer expiration "
        "times will be rejected'; build_client_assertion()'s 4-minute exp is "
        "inside that. The same page lists the form-encoded token payload as "
        "grant_type, client_assertion_type, client_assertion and 'scope: "
        "SMART-on-FHIR scopes needed for the app' - recorded as "
        "requires_token_scopes above. The scopes are also chosen at "
        "registration ('select the desired scopes and click Save'), and "
        "Greenway's unsupported-scopes notice says a request for a scope that "
        "'exceeds that which the client is permitted to request' now gets "
        "400 invalid_scope rather than being silently dropped: the scopes "
        "selected on the app MUST cover every type in supported_resources, "
        "and Appointment and Consent, which Greenway names as unsupported, "
        "must never be requested. Whether a wildcard system/*.read is "
        "accepted is not documented by the vendor. The live discovery "
        "document advertises grant_types_supported client_credentials and "
        "capabilities client-confidential-asymmetric, permission-v1 and "
        "permission-v2, but no token_endpoint_auth_signing_alg_values_supported "
        "and no scopes_supported - ES384 rests on the registration page's "
        "instruction alone, and the walkthrough's looser 'RSA or ECDSA' "
        "phrasing does not override the instruction attached to the JWKS URL "
        "field. 'kid' handling is not documented by the vendor; the SMART "
        "asymmetric profile it implements requires one, so set "
        "PHI_AI_FHIR_JWT_KID to the kid in the hosted JWKS. Token lifetime "
        "(expires_in) is not documented by the vendor; the client reads it "
        "from the response. CLIENT GAP: core/fhir/client.py signs RS384 with "
        "an RSA key; until build_client_assertion() honours "
        "assertion_algorithm and PHI_AI_FHIR_PRIVATE_KEY_PATH holds an EC "
        "P-384 key, a live Greenway token request will be refused. "
        "REGISTRATION: self-service at "
        "devplatform.greenwayhealth.com/developer/registration; launch type "
        "'Back-end Service' ('No UI; runs independently; used for Bulk "
        "FHIR'); the JWKS URL and scopes go on the app; then 'Submit For "
        "Review' - 'reviewed by our Greenway team against relevant standards "
        "before getting published', and 'Once published, the system "
        "generates a client ID'. ONE client ID, generated on publication; "
        "Greenway documents no non-production/production split. The FAQ "
        "then describes per-site enablement - site identifiers (Intergy "
        "licence/GID or Prime Suite ID, practice IDs), WRITTEN site "
        "permission, provisioning by Greenway - which it says can take a "
        "week or longer. What cannot be changed after publication is not "
        "documented by the vendor. "
        "SANDBOX: none. The FAQ: 'At this time, we do not have a sandbox "
        "environment available for developers ... The sandbox environment is "
        "on our roadmap, but we do not have a specific ETA'; the "
        "getting-started page says one 'will be available soon'. A "
        "fhir-api.fhirstaging.aws.greenwayhealth.com host appears in one "
        "code sample and is described nowhere. Rehearse on emulators/ "
        "(port 9109). "
        "RESOURCES: see the per-field comment. FHIR 4.0.1; the observed "
        "conformance statement instantiates us-core-server and bulk-data; "
        "the reference site's regulatory page cites US Core STU6.1 and "
        "USCDI v3. Every type is read + search-type only. "
        "BULK (supports_bulk_export above): the documented operation is "
        "GET {base}/Group/{id}/$export with 'Prefer: respond-async' "
        "(required) - Group-level only; no system- or Patient-level export "
        "is documented, and the observed conformance statement carries the "
        "export operation on Group alone. Scope of a Group: 'bulk export "
        "operations via the Bulk Data API endpoint occur at the tenant "
        "(site) level by default, exporting data from all practices for the "
        "tenant. To export a subset of the data, filter parameters can be "
        "applied'. Groups ARE discoverable: GET /Group returns 'the "
        "metadata/attributes for tenant-wide patient groups', so "
        "PHI_AI_FHIR_GROUP_ID comes from that search; how a tenant's groups "
        "are created is not documented by the vendor. THE TRAP: '_since ... "
        "If this parameter is absent only resources created or updated in "
        "the past 24 hours will be exported.' A kickoff without _since is a "
        "24-hour delta, not a full extract; the first load must pass an "
        "early _since, and bulk_client.kickoff_export() has no such "
        "parameter today - a required change before this vendor's first "
        "run. Handshake as documented: 202 + Content-Location; polling 202 "
        "+ X-Progress until 200 with the manifest (transactionTime, request, "
        "requiresAccessToken true, output[], error[]); DELETE the status URL "
        "to cancel (202); files served as application/fhir+ndjson; errors as "
        "OperationOutcome. No kickoff throttle, Retry-After, group-size "
        "ceiling, file expiry or rate limit is documented by the vendor. "
        "bulk_scheduler.py's 24-hour default cadence happens to match the "
        "24-hour default window; that is coincidence, not a Greenway rule. "
        "LIMITS AND FEES: 'There are currently no fees for the use of the "
        "Greenway Health FHIR API, Developer Platform, and Application "
        "Gallery', changeable on 30 days' notice (page/fees); the Terms of "
        "Service reserve the right to charge and to 'impose limits'. Minimum "
        "product versions for the HTI-1 API are Intergy v22 and Prime Suite "
        "v22 (reference/minimum-product-requirements). PKCE S256 became "
        "mandatory for SMART app-launch clients on 2025-10-20 "
        "(docs/hti1-updates); it does not touch the backend flow. "
        "CERTIFICATION: Intergy v22 (15.04.04.2913.Inte.22.06.0.250814) and "
        "Prime Suite v22 (15.04.04.2913.Prim.22.04.1.250814) list "
        "170.315(g)(10) in Greenway's own certification-information PDF; the "
        "2026 real-world-testing plan counts 'Number of authorized Bulk "
        "Applications'. Every field above is vendor-documented or observed; "
        "g(10) is corroboration, not the source. "
        "Confirm the tenant's own /metadata before ingesting - one production "
        "tenant was observed, and per-practice enablement is configuration. "
        "See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "Greenway's FHIR API is read-only by the vendor's own statement: 'The "
        "current Greenway FHIR API supports read operations only at the "
        "present time' (docs/api-an-overview), and the observed conformance "
        "statement advertises no create, update or conditional interaction on "
        "any type - which is why writable_resources above is empty and "
        "supports_conditional_create is False. core/fhir/delivery/writer.py "
        "reads that CapabilityStatement first and will skip every resource "
        "type, so a delivery run against a Greenway tenant writes nothing "
        "and says so; that is the correct outcome, not a defect to work "
        "around. The vendor's write path is GAPI, 'a Proprietary API with "
        "separate and distinct API calls and data structures for each of our "
        "EHR products' (Intergy and Prime Suite differ) that 'supports reads "
        "and writes across a variety of clinical and financial data "
        "elements', reached through a different portal "
        "(developer.greenwayhealth.com, an API key plus an authorization "
        "token) and described on the platform overview as the dashboard for "
        "'Marketplace partners and clients'. That is a second client with its "
        "own onboarding, not this profile, and the GAPI element list is not "
        "on the public site. Until such a client exists, deliver to a "
        "Greenway practice as files for their own import tooling. A future "
        "FHIR write capability would surface in /metadata as a create "
        "interaction, and writer.py would honour it without a profile "
        "change - confirm on the tenant, not here."
    ),
)

# Veradigm EHR. Every fact below comes from Veradigm's own developer
# portal (developer.veradigm.com) or from the public Veradigm EHR sandbox
# that portal publishes, fetched 2026-09-01; nothing is inherited from the
# Epic entry above. Veradigm's FHIR API is READ-ONLY by its own statement,
# so this is an ingestion-only profile - see write_notes.
VERADIGM = EMRProfile(
    name="Veradigm",
    # developer.veradigm.com/Fhir/ProcessOverview, "System Applications":
    # the token request body 'must include' client_assertion ('Token
    # generated using a private key'), client_assertion_type
    # urn:ietf:params:oauth:client-assertion-type:jwt-bearer, and
    # grant_type client_credentials - the signed-assertion flow
    # authenticate() speaks. The JWKS is registered on the app as
    # 'JWKS URL' and must be 'hosted at a publicly accessible HTTPS
    # endpoint'. No client-secret grant is documented for System apps.
    auth_flow="smart_backend_services",
    # assertion_algorithm is left at the file default (RS384) rather than
    # set, because Veradigm documents the KEY TYPE, not the algorithm:
    # 'Each key must use the RSA key type (kty)' and 'Keys should be
    # 2048-bit RSA keys' (ProcessOverview, "JWKS Requirements"). Its
    # sample JWKS carries "alg": "RS256", its C# sample signs with
    # X509SigningCredentials, and the sandbox's smart-configuration
    # publishes no token_endpoint_auth_signing_alg_values_supported - so
    # which RS algorithms its validator accepts is not documented by the
    # vendor. RS384 is an RSA algorithm (satisfies the documented key
    # rule) and is what SMART App Launch 2.0.0 - which Veradigm's
    # Introduction page says it implements - requires servers to accept.
    # Confirm on the partner testing environment; see notes.
    #
    # developer.veradigm.com/Fhir/Resources, "Supported FHIR R4
    # Resources": 'Veradigm EHR version 26.0 supports these FHIR
    # resources. For versions earlier than 26.0, consult the Capability
    # Statement.' The page lists 27 R4 types; this tuple is the subset
    # that is a patient's clinical record, which is what this platform
    # retains. Left out although Veradigm publishes them: Coverage,
    # Device, Group, Location, Medication, Organization, Practitioner,
    # PractitionerRole, Questionnaire, QuestionnaireResponse,
    # RelatedPerson, Specimen - reference, administrative or form
    # definitions; a deployment can add any of them, every one is
    # vendor-published. NOT here because Veradigm does not publish them
    # for R4: MedicationAdministration and MedicationStatement (the
    # Resources catalog tags them 'R4 (UK)' / 'DSTU2 | R4 (UK)', and they
    # are absent from the R4 list - although both DO appear in the Bulk
    # Data resource list and in the sandbox CapabilityStatement, so a
    # $export may still deliver them), Consent and AdverseEvent (not
    # published at all), and Provenance (on the bulk list, but 'The
    # Veradigm FHIR API does not currently support searching on the
    # Provenance resource' per the Searching page - the paged scheduler
    # would fail on it; $export includes it by default instead).
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "ServiceRequest", "CarePlan", "CareTeam", "Goal",
        "MedicationDispense",
    ),
    # developer.veradigm.com/Fhir/BulkData: '[FHIR path]/Group/INF-101/
    # $export' - Group-level only, 'Only FHIR applications of the type
    # System can send bulk data requests', 'Backend authentication for
    # access tokens via JWKS must be configured'. The sandbox
    # CapabilityStatement instantiates
    # hl7.org/fhir/uv/bulkdata/CapabilityStatement/bulk-data and
    # advertises operation 'export' (OperationDefinition/group-export) on
    # Group and on no other resource. Also ONC 170.315(g)(10)-certified:
    # Veradigm EHR 26, certificate 15.04.04.2891.Vera.26.14.1.251231
    # (Drummond, 2025-12-31), criteria incl. (g)(10) -
    # veradigm.com/legal/onc-reg-compliance.
    supports_bulk_export=True,
    # Stays False DELIBERATELY, and not because Veradigm omits scope:
    # its documented System token body 'must include ... scope:
    # system/*.read' (ProcessOverview) - a WILDCARD. This field makes
    # authenticate_from_settings() send Oracle Health's per-type shape
    # (system/Patient.read system/Encounter.read ...), which Veradigm's
    # prose never shows, and it cannot express the wildcard Veradigm
    # does show. The sandbox's smart-configuration lists both forms in
    # scopes_supported (system/*.read, system/*.rs and per-type
    # system/{Type}.read / .rs), so neither is wrong on its face; what is
    # not documented by the vendor is whether a token request WITHOUT a
    # scope is refused. Consequence: today the client sends no scope to
    # Veradigm. If the partner testing environment refuses that, the fix
    # is to send the documented 'system/*.read' through
    # FHIRIngestionClient.authenticate(scope=...) - a client change, not
    # this flag. See notes.
    requires_token_scopes=False,
    # Dataclass default kept: the Searching page documents _count ('Do
    # not return more than this number of resources in each response')
    # but no default or ceiling. Not documented by the vendor.
    page_size=50,
    # rate_limit_per_min is left at the dataclass default: Veradigm
    # documents no request rate limit for the REST API. The one 429 it
    # documents is polling a bulk status URL before its Retry-After
    # (BulkData) - handled in notes, not by this throttle.
    #
    # 'The Veradigm FHIR API is limited to read-only access.'
    # (ProcessOverview, "Functionality Considerations"). Empty on the
    # vendor's own word - see write_notes for what the sandbox
    # CapabilityStatement advertises regardless, and for Unity.
    writable_resources=(),
    # Not documented by the vendor; the sandbox CapabilityStatement
    # carries no conditionalCreate element on any resource.
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09 against developer.veradigm.com (Introduction, "
        "ProcessOverview, SMARTonFHIR, BulkData, Searching, Resources, "
        "EndpointDirectory, FHIR_Sandboxes, and /Home/LearnMore for the tier "
        "and fee statements) and the public Veradigm EHR sandbox "
        "those pages name (fhir.fhirpoint.open.allscripts.com/fhirroute/fhir/"
        "CP00101 - its /metadata and /.well-known/smart-configuration). "
        "PRODUCT: this profile is Veradigm EHR ('the term product refers to "
        "Veradigm EHR'). Altera TouchWorks, Sunrise and Paragon belong to "
        "Altera's separate developer program (ADP@alterahealth.com), and "
        "Practice Fusion - same parent - has its own product and API; neither "
        "is this entry. Veradigm Practice Management is reachable only through "
        "the proprietary Unity API, not FHIR. "
        "VERSIONS: FHIR R4 (the sandbox reports 4.0.1, US Core 3.1.1 as its "
        "implementation guide, and US Core 3.1.1 / 6.1.0 profiles per "
        "resource on the Resources page). 'as of 6/1/2025, Veradigm will no "
        "longer be providing support for DSTU2' - 'The API is not turned off, "
        "but there is no longer technical support'. Some client sites run "
        "'Versionless' endpoints serving DSTU2 and R4 together; the default "
        "version there is whatever was set at install, so send "
        "'Accept: application/fhir+json; fhirVersion=4.0' (EndpointDirectory) "
        "and read fhirVersion from /metadata before trusting a response shape. "
        "ACCESS MODEL: per client organization. Each Veradigm client's FHIR "
        "installation registers its endpoints in Veradigm's 'downtown' "
        "environment and 'Only endpoints that are designated for production "
        "environments are listed in the Veradigm Endpoint Directory'; provider/"
        "system endpoints 'generally end in /fhir', patient endpoints in /open. "
        "So PHI_AI_FHIR_BASE_URL is per organization (sandbox shape "
        ".../fhirroute/fhir/{site}/), and the token URL is per organization "
        "too - take it from that base URL's own CapabilityStatement "
        "(oauth-uris extension; sandbox: .../fhirroute/authorizationV2/{site}/"
        "connect/token), never from another site. "
        "REGISTRATION AND REVIEW: sign up at developer.veradigm.com (the Open "
        "tier is free - 'Open accounts are free to build and deploy by anyone'), "
        "add a FHIR application with App Type 'System' ('an external system, "
        "not a patient or provider'), Client Type Confidential, and a JWKS "
        "URL. The portal issues a Client ID, a Secret and a Secret Expiration "
        "Date to every app; for System apps the documented token request uses "
        "only the JWT assertion, and the Secret's role is not documented by "
        "the vendor - PHI_AI_FHIR_CLIENT_SECRET stays unset. Then choose a "
        "Purpose of Use and the app's scopes ('The scopes must match the FHIR "
        "App Type'; SMART v1 scopes end in .read, v2 in .rs, and 'Do not "
        "request both SMART version 1 and 2 scopes for a single FHIR "
        "application. The app will not be approved'), test, and click Request "
        "Production Access: 'the application name, type, and Purpose of Use "
        "... cannot be changed once production access is granted'. 'The FHIR "
        "application is reviewed and, if appropriate, approved by Veradigm "
        "Connect' - a human review board - and only then 'clients can begin "
        "activating the FHIR application': developers 'cannot license their "
        "applications for clients; the clients must activate applications "
        "themselves through the client License Management Portal'. One "
        "registration, activated per organization. "
        "KEYS: RSA only, 2048-bit, one JWKS at a public HTTPS URL with a "
        "unique kid per key (set PHI_AI_FHIR_JWT_KID to it - Veradigm selects "
        "the key by kid). Veradigm's own SMARTonFHIR page: 'a nightly job is "
        "run downtown that cycles through all registered FHIR system "
        "applications. It downloads the JWKS information and updates the "
        "OAuth clients' - so a new or rotated key is not usable the moment it "
        "is published; allow a day, and keep the old key in the JWKS until the "
        "new one authenticates. The assertion (Veradigm calls it the 'system "
        "token') 'includes an expiration time, generally two to 20 minutes, "
        "and can only be used once'; its C# sample sets iss and sub to the "
        "client ID, aud to the token URL, a random jti and a 5-minute expiry - "
        "exactly what build_client_assertion() produces. ALGORITHM: not "
        "documented by the vendor beyond 'RSA key type'; the sample JWKS says "
        "RS256, the sandbox's smart-configuration lists no signing algorithms "
        "(and lists token_endpoint_auth_methods of client_secret_basic, "
        "client_secret_post, tls_client_auth and self_signed_tls_client_auth "
        "while its capabilities include client-confidential-asymmetric). The "
        "client signs RS384; if the partner testing environment rejects that, "
        "the change belongs in build_client_assertion(), not here. "
        "SCOPES: see requires_token_scopes above - Veradigm's documented "
        "System token body carries scope=system/*.read, which this client "
        "cannot send through that flag, so it currently sends no scope. "
        "Against the sandbox token endpoint (2026-09-01, without a registered "
        "client) a secret-only client_credentials request got 400 "
        "invalid_client and a malformed assertion got 400 invalid_request with "
        "or without scope - bare {'error': ...} bodies, no error_description - "
        "which is what to expect from a misconfiguration live. "
        "POPULATION READS: use $export. Every clinical resource's documented "
        "searches are patient-anchored (Resources page; the sandbox "
        "CapabilityStatement lists required search-parameter combinations such "
        "as patient+category), and Patient itself searches by name/birthdate/"
        "identifier, so scheduler.py's unanchored 'GET {Type}?_count=' is not "
        "a documented population query. The one unanchored example Veradigm "
        "documents is 'GET [FHIR path]/Observation?_lastUpdated=ge2022-03-01' "
        "(Searching), the shape iter_resources() sends on an incremental "
        "cycle - confirm on the sandbox before relying on it. Search errors "
        "are 401/403/404/413 ('Request too large'), and data can come back "
        "marked 'redacted' when 'the Veradigm Clinical Authorization Service "
        "determines that the person requesting the data is not authorized'. "
        "BULK: 'Group resources are created by organizations in Veradigm EHR' "
        "from 'segments in the Reporting module' - the Group ID for "
        "PHI_AI_FHIR_GROUP_ID comes from the organization, not an API (Group "
        "search by characteristic/type/member is documented, so a candidate "
        "list is at least enumerable). Kickoff returns 202 + Content-Location; "
        "status polls return Accepted with X-Progress and Retry-After in "
        "seconds, and 'If a status request is made prior to the retry-after "
        "date/time, the FHIR API responds with a HTTP 429 Too Many Requests "
        "error' - bulk_client.poll_status() does NOT read Retry-After and "
        "raises on 429, so set PHI_AI_BULK_POLL_INTERVAL_SECONDS at or above "
        "the Retry-After the instance actually returns (the default 600 s is a "
        "guess, not a vendor figure). Completion carries an Expires header: "
        "'once they expire, they are no longer available' - the expiry length "
        "is not documented by the vendor, so download promptly; the manifest "
        "example sets requiresAccessToken true and may list error files "
        "alongside output. Provenance 'is included by default for those "
        "requests that do not specify which resource to include', and when "
        "_type is given Provenance arrives only if listed. The BulkData page "
        "lists 25 exportable types (incl. Binary, MedicationAdministration, "
        "MedicationStatement, Provenance that are not on the R4 REST list). "
        "No _since, no export frequency limit, no group-size guidance and no "
        "file-retention period are documented by the vendor; bulk_scheduler's "
        "24-hour default interval is therefore not a Veradigm number. "
        "TIERS AND FEES: Open is free and covers the FHIR APIs; EHR-launch "
        "testing and Veradigm certification (App Expo listing) need Integrator "
        "tiers - neither is required to ingest. Sandbox credentials require a "
        "request form on FHIR_Sandboxes; the sandbox base URL and /metadata "
        "are public. Contact: VeradigmConnect@veradigm.com. "
        "See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "Veradigm's own statement: 'The Veradigm FHIR API is limited to "
        "read-only access.' - so writable_resources is empty and delivery to a "
        "Veradigm organization through THIS profile is not a path. Two things "
        "an operator will nonetheless see. (1) The sandbox CapabilityStatement "
        "advertises 'create' and 'update' on twelve types (Condition, "
        "AllergyIntolerance, MedicationRequest, Observation, Immunization, "
        "DocumentReference, MedicationStatement, MedicationAdministration, "
        "ServiceRequest, Questionnaire, QuestionnaireResponse, "
        "MedicationDispense; never Patient) while no Veradigm page documents a "
        "create request, its body, or any system write scope - the sandbox's "
        "scopes_supported has system/*.read and system/*.rs and no system "
        "write scope at all. core/fhir/delivery/writer.py trusts the live "
        "CapabilityStatement, so against such an instance it WILL attempt "
        "POSTs for those types; expect them to be refused by scope (403 is in "
        "Veradigm's documented error set) and treat any success as "
        "undocumented behaviour to raise with VeradigmConnect@veradigm.com, "
        "not a write path to design around. (2) The real write path is 'the "
        "bidirectional Unity API, enabling both reads and writes' - a "
        "proprietary API on Integrator tiers, a second client, not this "
        "profile, and the only route to Veradigm Practice Management "
        "('developers must utilize Unity to read or write patient demographic, "
        "appointment, or financial data'). Until a deployment builds that, "
        "deliver to a Veradigm organization as files for their own tooling. "
        "Conditional create (If-None-Exist) is not documented by the vendor. "
        "delivery/__main__.py builds its destination token request on this "
        "profile; with writable_resources empty the writer refuses every type "
        "against the CapabilityStatement, so nothing in this entry reaches a "
        "write."
    ),
)

# ---------------------------------------------------------------------------
# Practice Fusion. A Veradigm company, but a SEPARATE product with its own
# EHR, its own FHIR API and its own developer programme (the "PDS API
# Portal") - nothing in this entry comes from developer.veradigm.com, and
# nothing in it is carried over from Epic. Every non-default value cites one
# of Practice Fusion's own pages, all read 2026-09-01:
#
#   [PF-GS]   https://www.practicefusion.com/fhir/get-started/
#   [PF-API]  https://www.practicefusion.com/fhir/api-specifications/
#   [PF-SBX]  https://www.practicefusion.com/fhir/api-specifications/sandbox-documentation/
#   [PF-URL]  https://www.practicefusion.com/assets/static_files/ServiceBaseURLs.json
#   [PF-BLOG] https://www.practicefusion.com/blog/fhir-integration-guide/
#   [PF-TOS]  https://www.practicefusion.com/pds-api/termsofservice/
#   [PF-FEES] https://www.practicefusion.com/assets/misc/API-Fees-ONC-Cert-Criteria-for-Health-IT_May-2024.xlsx
#   [PF-ONC]  https://www.practicefusion.com/onc-certified-ehr/
#
# Where a field is left at the dataclass default, the comment beside it says
# that Practice Fusion documents nothing on the point. The gap is recorded,
# not filled from another vendor's answer. Confirm against the practice's
# own CapabilityStatement (GET {base_url}/metadata) before ingesting from it.
# ---------------------------------------------------------------------------
PRACTICEFUSION = EMRProfile(
    name="Practice Fusion",
    # [PF-API] 'System Apps': 'Practice Fusion supports the 2-legged O-auth
    # workflow for system clients to generate an access token using the
    # 'Client Credentials' grant_type', authenticated with a client_assertion
    # JWT; [PF-SBX]: 'no secret is required'. So the JWT-assertion branch of
    # authenticate_from_settings(), never a client secret.
    auth_flow="smart_backend_services",
    # assertion_algorithm is deliberately left at its RS384 default: [PF-API]
    # documents the assertion header as 'JWA algorithm (e.g., RS384, ES384)',
    # so RS384 - the one core/fhir/client.py signs with - is inside Practice
    # Fusion's own examples rather than an assumption imported from another
    # vendor. ES384 is named there too; nothing here forbids it.
    #
    # [PF-API] 'Supported scopes for system applications': the 22 types that
    # have a documented system/{Type}.read (SMART V1) or system/{Type}.rs
    # (SMART V2) scope, in Practice Fusion's own order. This tuple is
    # load-bearing twice: the schedulers iterate exactly these types, and -
    # because requires_token_scopes is True below - it is ALSO the scope
    # string sent to the token endpoint. Types the published
    # CapabilityStatement lists for read/search but that have NO system
    # scope (MedicationAdministration, ExplanationOfBenefit, Location, List,
    # Task, Questionnaire, Composition, ...) are deliberately absent: a
    # system app cannot request them, so listing them would put an
    # unauthorised scope into every token request. Consent and AdverseEvent,
    # which other profiles in this file carry, have neither a scope nor a
    # CapabilityStatement entry at Practice Fusion.
    supported_resources=(
        "AllergyIntolerance", "CarePlan", "CareTeam", "Condition", "Coverage",
        "Device", "DiagnosticReport", "DocumentReference", "Encounter", "Goal",
        "Immunization", "MedicationDispense", "MedicationRequest", "Observation",
        "Organization", "Patient", "Practitioner", "Procedure", "Provenance",
        "RelatedPerson", "ServiceRequest", "Specimen",
    ),
    # [PF-API] 'Bulk-Data Access for System Apps': GET {BaseURL}/Patient/$export
    # ('all patients in the practice represented in the base URL') and
    # GET {BaseURL}/Group/{GroupId}/$export ('a subset of patients';
    # 'Groups are limited to a maximum of 1,000 patients each'), answered
    # 202 + Content-Location {BaseURL}/Export/{guid}. [PF-GS]: 'Bulk Data
    # Access v1.0.1'. Practice-scoped: the base URL IS the practice.
    supports_bulk_export=True,
    # [PF-API]: the documented token request body is grant_type, scope,
    # client_assertion_type, client_assertion; the response's 'scopes' field
    # is 'Permissions Granted'; and 'System applications can only request
    # scopes that have been authorized by the EHR user'. So
    # authenticate_from_settings() derives system/{Type}.read - Practice
    # Fusion's own SMART V1 spelling - for every type above and sends it.
    requires_token_scopes=True,
    # Not documented by the vendor: [PF-API] gives no _count guidance and no
    # page-size ceiling anywhere. Dataclass default kept.
    page_size=50,
    # Not documented by the vendor for the FHIR API. (Practice Fusion's
    # separate, non-FHIR PDS/PHR guide says 'Rate limiting will be enforced
    # ... HTTP 429'; that is a different API and is not transferred here.)
    # Dataclass default kept as a client-side courtesy throttle only.
    rate_limit_per_min=60,
    # [PF-BLOG]: 'Although FHIR apps can read patient information, they
    # cannot change or write over EHR data.' See write_notes.
    writable_resources=(),
    # Not documented by the vendor.
    supports_conditional_create=False,
    # Not documented by the vendor.
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09 against Practice Fusion's own pages (the [PF-*] URLs "
        "in the comment block above). Practice Fusion is a Veradigm company but "
        "a separate product with a separate API - do not conflate with Veradigm "
        "EHR or the developer.veradigm.com programme. STANDARDS ([PF-GS]): 'HL7 "
        "FHIR R4 v4.0.1', 'US Core Profiles v6.1.0 / USCDI v3', 'Bulk Data "
        "Access v1.0.1', 'SMART App Launch v2.0.0'. The published "
        "CapabilityStatement ([PF-API], dated 2025-06-12) instantiates "
        "us-core-server and bulk-data and names its software 'NXT' from "
        "'MedicaSoft, LLC' - the FHIR facade is a third-party product in front "
        "of the EHR, worth knowing when a behaviour looks unlike the EHR's own "
        "screens. ACCESS MODEL: per practice - not federated per health system "
        "and not one tenant. Every practice has its own base URL, "
        "https://api.practicefusion.com/fhir/r4/v1/{practice-guid} for "
        "'Provider / System Access' ([PF-URL]: 3,422 practices at fetch time, "
        "each also with a separate Patient Access endpoint on "
        "api.patientfusion.com or .../fhir/fmh/... that is NOT this profile). "
        "The token endpoint is that base URL plus /token, published by "
        "{BaseURL}/.well-known/smart-configuration together with "
        "'client-confidential-asymmetric', 'permission-v1', 'permission-v2' and "
        "grant_types_supported including client_credentials. Two gates precede "
        "any token. (1) Registration ([PF-GS]): the PDS API Partner "
        "Registration Form (pfpds.practicefusion.com/s/Registration) -> PDS API "
        "Portal credentials by email -> the Partner Application form, choosing "
        "the 'System or Bulk export' app type ('third-party applications that "
        "may request large practice level data exports') and submitting the "
        "app's homepage, privacy policy, requested scopes and a 'JWKS (JSON Web "
        "Key Set) URL - Required if your application will use asymmetric key "
        "pair authentication' -> 'API credentials will be delivered to you via "
        "your PDS API Portal'. Under the developer agreement ([PF-TOS]) the "
        "developer tests the integration first and 'Practice Fusion may in its "
        "sole discretion enable the Integration'; review timelines are not "
        "documented by the vendor. (2) Practice authorisation ([PF-API], "
        "[PF-BLOG]): 'The practice needs to authorize system applications in "
        "the EHR before the applications can begin requesting access tokens'; "
        "a practice administrator first enables FHIR in the EHR, then approves "
        "apps individually, and for a system app selects 'Authorize App' on "
        "the application details view; 'System applications can only request "
        "scopes that have been authorized by the EHR user'. AUTH ([PF-API] "
        "'System Apps'): grant_type=client_credentials, client_assertion_type "
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer and a "
        "client_assertion JWT; 'no secret is required' ([PF-SBX]). Header: alg "
        "'JWA algorithm (e.g., RS384, ES384)'; kid Required and 'SHALL be "
        "unique within the client's JWK Set'; typ JWT; optional jku which, when "
        "present, 'SHALL match the JWKS URL value that the client supplied ... "
        "at client registration time'. Claims: iss and sub = client_id; aud = "
        "'FHIR Base URL's Token Endpoint' (per practice, so the assertion is "
        "per practice too); exp 'should not be > 300 seconds in the future'; "
        "jti 'A nonce string value that uniquely identifies this "
        "authentication JWT'. build_client_assertion() already emits this "
        "shape (RS384, exp now+240s, kid from PHI_AI_FHIR_JWT_KID) - set "
        "PHI_AI_FHIR_JWT_KID, because here kid is Required, not optional. "
        "Documented response: token_type 'Will always be Bearer', scope "
        "'Permissions Granted', expires_in (the worked example shows 3600), "
        "access_token. What the endpoint does when a request names a scope the "
        "practice did not authorise is not documented by the vendor - the "
        "response's own scope field is the only signal, so compare it with the "
        "request on the first live token. BULK ([PF-API]): kickoff GET "
        "{BaseURL}/Patient/$export or GET {BaseURL}/Group/{GroupId}/$export "
        "with Accept application/fhir+json -> 202 + Content-Location "
        "{BaseURL}/Export/{guid}; the practice defines the Group in the EHR "
        "(help.practicefusion.com, 'what-is-a-fhir-group-and-how-do-i-create-one') "
        "and hands over its id - set PHI_AI_FHIR_GROUP_ID; status GET "
        "{BaseURL}/Export/{guid} answers 202 with no body while in progress "
        "and a 200 manifest (transactionTime, request, requiresAccessToken "
        "true, output[], error[]) when complete; DELETE {BaseURL}/Export/{guid} "
        "-> 202; output files sit under {BaseURL}/Binary/export/{guid}/{type}/{n}. "
        "core/fhir/bulk_client.py speaks exactly this handshake but calls only "
        "the Group form, so the all-patients form is an integrator follow-up, "
        "not a profile switch; it also sends Prefer: respond-async (absent "
        "from Practice Fusion's header table, required by the Bulk Data IG the "
        "CapabilityStatement instantiates) and _type. NOT documented by the "
        "vendor, so confirm on the first live export: whether _type and _since "
        "are honoured or ignored; Retry-After and X-Progress on the 202; 429 "
        "behaviour; any limit on how often a kickoff may be repeated; how long "
        "output files persist; and the file format - the 'Retrieve Output' "
        "example shows one pretty-printed resource with Content-Type "
        "application/json, while iter_ndjson_resources() parses NDJSON line "
        "by line, so inspect the first file before trusting a full run. The "
        "Patient/$export manifest example lists Location, which has no system "
        "scope - expect manifest types you did not (and cannot) request. FEES "
        "([PF-FEES], May 2024): 'Practice Fusion does not charge API fees for "
        "development, deployment or upgrade at this time' and none 'for API "
        "usage at this time', with notice promised before that changes; "
        "[PF-TOS]: 'The Developer/API User does not pay fees for use of the "
        "Application Access APIs' while 'Practice Fusion customers shall pay "
        "to Practice Fusion the then standard Transaction Fee if any'; "
        "[PF-BLOG]: 'At present, FHIR is complimentary to all Practice Fusion "
        "clients.' SANDBOX ([PF-SBX]): shared, but 'Your application must be "
        "approved and available in the Practice Fusion App Marketplace' and "
        "'You must use your production credentials to access the sandbox "
        "environment' - there is no pre-approval test tenant, so emulators/ "
        "(port 9111) is the only rehearsal before approval. It publishes a "
        "system-app base URL and a 'Bulk Data Testing Group ID' covering nine "
        "test patients, and 'will be periodically reset'. CERTIFICATION "
        "([PF-ONC]): Practice Fusion EHR 3.7, CHPL 15.04.04.2924.Prac.37.01.1.240826, "
        "certified 2024-08-26 by Drummond Group with 170.315(g)(10) among the "
        "criteria - recorded for completeness; nothing above rests on the "
        "g(10) mandate alone. See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "Read-only for clinical data, by the vendor's own statement ([PF-BLOG], "
        "2024-02-29): 'FHIR apps are never bidirectional in their data access "
        "ability' and 'Although FHIR apps can read patient information, they "
        "cannot change or write over EHR data.' The published CapabilityStatement "
        "([PF-API]) advertises create and update for exactly one type, Group - "
        "the bulk-export cohort container, not clinical content - and the "
        "narrative documents Group definition only inside the EHR by the "
        "practice, so even that is not a write path this profile plans around; "
        "writable_resources above stays empty. core/fhir/delivery/writer.py "
        "reads the live CapabilityStatement, finds create advertised for Group "
        "alone, and skips every clinical type as 'the destination does not "
        "advertise create for {Type}'; with supports_conditional_create False "
        "it also refuses to run unattended unless --allow-duplicates is given. "
        "There is no FHIR write path into Practice Fusion: deliver as files for "
        "the practice's own import, or through Practice Fusion's separate "
        "partner Lab API (practicefusion.com/labs-documentation - HL7 v2 "
        "results and orders for laboratory and imaging partners, with its own "
        "onboarding) - a second client, not this profile, and one that carries "
        "lab results, not a patient's prior clinical history. Delivery's token "
        "client is built with the Epic profile and does not apply "
        "requires_token_scopes; moot here, since nothing is writable."
    ),
)

# ---------------------------------------------------------------------------
# TruBridge - the former CPSI / Evident "Thrive" hospital EHR. Every fact in
# this entry was read on 2026-09-01 from TruBridge's OWN material: the
# developer portal at fhir-developer.plt.trubridge.com (a JavaScript app;
# its pages are ?page=api/overview, ?page=api/endpoints,
# ?page=api/backend-services and ?page=supported-data), its FHIR API Terms
# and Conditions (assets/termsandconditions.pdf, revised 2025-11-20), its
# OpenAPI (openapi/openapi.yaml), its published sandbox and production
# endpoint directories, and the /metadata and
# /.well-known/smart-configuration its own servers return. Nothing here is
# carried over from another vendor's entry.
#
# TruBridge publishes ONE FHIR BASE URL PER FACILITY (583 in its production
# directory on the day of writing). Confirm against the facility's own
# CapabilityStatement (GET {base_url}/metadata) before ingesting from it,
# as with every entry in this file.
# ---------------------------------------------------------------------------
TRUBRIDGE = EMRProfile(
    name="TruBridge",
    # ?page=api/backend-services documents the client_credentials request
    # with EITHER a signed JWT client_assertion ("signed using a private
    # key corresponding to one of the public keys published in the backend
    # service's JWKS document ... obtained via the URL provided during the
    # backend service's registration") OR a client secret (Basic header or
    # POST body); smart-configuration lists client_secret_basic,
    # client_secret_post and private_key_jwt. This profile takes the
    # assertion path - the private key never leaves the deployment - and
    # TruBridge's token_endpoint_auth_signing_alg_values_supported is
    # [RS256, RS384, ES256, ES384], so the default RS384
    # assertion_algorithm applies (deliberately not overridden: the vendor
    # does not require ES384). "oauth2_client_credentials" with
    # PHI_AI_FHIR_CLIENT_SECRET is the equally documented alternative.
    auth_flow="smart_backend_services",
    # TruBridge's PUBLISHED surface (?page=supported-data) is US Core 6.1.0
    # / USCDI v3: AllergyIntolerance, CarePlan, CareTeam, Condition,
    # Coverage, Device, DiagnosticReport, DocumentReference, Encounter,
    # Goal, Group, Immunization, Location and Medication (contained only,
    # "scopes: []"), MedicationDispense, MedicationRequest, Observation,
    # Organization, Patient, Practitioner, PractitionerRole, Procedure,
    # Provenance, QuestionnaireResponse, RelatedPerson, ServiceRequest,
    # Specimen; the OpenAPI defines the same types as GET-only read and
    # search. Listed here: the retention-relevant clinical types among
    # them - the subset of TruBridge's published list that this project's
    # retention scope (docs/DATA_SCOPE_REVIEW.md) keeps, including
    # ServiceRequest (practitioners' orders), which TruBridge does
    # publish. MedicationAdministration,
    # Consent and AdverseEvent are NOT on TruBridge's published list (its
    # servers' CapabilityStatements advertise the first two without a US
    # Core profile, but the portal does not document them) and stay out -
    # recorded uncertainty, not confirmed inability. Because
    # requires_token_scopes is True below, this tuple is ALSO the scope
    # string sent at token time: every type here must have been granted
    # to the client at registration, or the whole token request fails.
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "ServiceRequest",
    ),
    # Vendor-documented, not just g(10)-mandated: ?page=supported-data lists
    # "Bulk Data Access v2.0.0" with [base]/$export, Group/[id]/$export and
    # Patient/$export, and the sandbox AND production CapabilityStatements
    # instantiate the bulk-data CapabilityStatement and advertise export,
    # group-export and patient-export in rest.operation. NOTE a second
    # vendor-internal contradiction: TruBridge's openapi.yaml defines no
    # $export path at any level and declares only an authorization_code
    # SMART security scheme (client_credentials commented out) - the
    # portal pages and both servers' CapabilityStatements are the sources
    # for bulk and for backend auth, not the OpenAPI document.
    supports_bulk_export=True,
    # ?page=api/backend-services marks `scope` REQUIRED: "Must be subset of
    # scopes that were granted to the backend service during registration
    # ... system/ scope prefix is appropriate for backend services". A
    # scope-less request is outside what TruBridge documents, so
    # authenticate_from_settings() must derive and send the per-type
    # scopes. TruBridge advertises permission-v1 AND permission-v2, so the
    # v1 system/{Type}.read strings it derives are within capability (the
    # vendor's own example is v2: "system/Observation.rs system/Patient.rs").
    requires_token_scopes=True,
    # Not documented by the vendor as a number. Terms s.4(b): TruBridge "may
    # restrict the amount of data returned by certain queries to a specific
    # page size and require you to implement logic to incrementally page
    # through the data set". _count is a documented search parameter on
    # nearly every published search; the default stays.
    page_size=50,
    # Not documented by the vendor. Terms s.4(c) reserve a discretionary
    # right to "suspend, throttle or otherwise limit" an app TruBridge
    # believes threatens its clients' systems; no ceiling is published, so
    # the default client-side throttle stays.
    rate_limit_per_min=60,
    # Empty on purpose: every TruBridge document says read-only and the
    # OpenAPI defines no POST/PUT/DELETE - see write_notes for why the
    # servers' CapabilityStatements say otherwise and what that does to
    # writer.py.
    writable_resources=(),
    # Not documented by the vendor.
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09-01 against TruBridge's own developer portal "
        "(fhir-developer.plt.trubridge.com), its FHIR API Terms and Conditions "
        "(revised 2025-11-20), its endpoint directories and its servers' own "
        "smart-configuration and CapabilityStatement - never against another "
        "vendor's behaviour. PRODUCT: 'The TruBridge FHIR R4 API delivers "
        "read-only access to USCDI v3 patient clinical data', for TruBridge EHR "
        "v22 (CHPL 15.04.04.3104.Thri.22.04.1.241210, Drummond Group, 2024-12-10, "
        "criteria include 170.315(g)(10); chpl.healthit.gov/#/listing/11541) and "
        "TruBridge Provider EHR v22 (15.04.04.3104.Thri.PR.04.1.241210). The "
        "server is the former CPSI/Evident Thrive stack and still identifies "
        "itself as 'cpsi-fhir-thrive', publisher 'Evident', on *.cpsi-cloud.com "
        "hosts - the same vendor, not a different one. ACCESS MODEL: hosted, one "
        "base URL per facility, published in a vendor endpoint directory (sandbox "
        "thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4/.well-known/"
        "endpoint, production thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/"
        "endpoint): Organization+Endpoint pairs where Endpoint.address is the "
        "base URL, shaped thrive-gw.cpsi-cloud.com/api/smart/{site}/"
        "id-osfac.{uuid}/fhir/r4 (583 pairs in production on the day of writing; "
        "the production document is a Bundle, the sandbox one a bare JSON array - "
        "parse both). The token endpoint is on ANOTHER host and must be read from "
        "{base}/.well-known/smart-configuration ('Every authorization workflow "
        "requires consultation of the SMART Configuration endpoint'), shaped "
        "thrive-oauth.cpsi-cloud.com/oauth/smart/{site}/id-osfac.{uuid}/token; "
        "PHI_AI_FHIR_BASE_URL and PHI_AI_FHIR_TOKEN_URL are therefore both "
        "per-facility. Only 'client facilities that participate in Promoting "
        "Interoperability and have a MyCareCorner instance configured' have an "
        "endpoint at all (TruBridge-FHIR-API.pdf). REGISTRATION: accept the "
        "Terms, read the portal, e-mail a registration request to "
        "info@trubridge.com; no console, no form, no published turnaround. "
        "TruBridge issues the client id and records the JWKS URL; then, per "
        "Terms s.4(b), the app 'must be approved in writing and registered for "
        "use by the applicable TruBridge client before TruBridge will enable' it "
        "in that client's environment - approval is per facility. AUTH "
        "(auth_flow above): TruBridge's asymmetric token request is "
        "grant_type=client_credentials + client_assertion_type jwt-bearer + "
        "client_assertion + scope, with client_id OMITTED ('Otherwise, this "
        "value is omitted') - exactly what FHIRIngestionClient.authenticate() "
        "sends. Its worked example signs RS256 with kid 'key-256', iss=sub= "
        "client id, exp one hour out, and aud = the FHIR BASE URL, whereas "
        "build_client_assertion() sets aud = the token endpoint per the SMART "
        "profile; TruBridge's prose does not say which it validates, so an "
        "invalid_client on the first sandbox token request points here first. "
        "Documented token response: {scope, expires_in: 3600, access_token}. "
        "SCOPES (requires_token_scopes above): required, granted at "
        "registration; wildcards system/*.* and system/*.cruds are advertised as "
        "supported (so this is not Oracle Health's no-wildcard rule - the client "
        "never sends one anyway); granular category scopes are documented for "
        "Condition and Observation. Request the read scope for exactly the types "
        "above. BULK (supports_bulk_export above): Bulk Data Access v2.0.0 at "
        "system, Group and Patient level is published; NOT documented: how a "
        "Group id is issued (the CapabilityStatement advertises Group "
        "read/search - try GET {base}/Group with a system token, else ask "
        "TruBridge), kickoff frequency, Retry-After, poll interval, output "
        "retention, whether a Group is facility-scoped. bulk_scheduler.py's "
        "24-hour interval and poll defaults are not TruBridge figures. "
        "FRESHNESS: the API is a repository fed 'in near real-time' by a queue "
        "from the EHR (TruBridge-FHIR-API.pdf), not a live chart query; "
        "_lastUpdated is the repository's clock. Whether an unanchored GET "
        "{base}/Patient returns a population to a system client is not "
        "documented (the sandbox is 401 without a token) - confirm before "
        "relying on scheduler.py for population reads. FEES (Terms s.3): "
        "sandbox and Patient Access free; 'Additional fees may apply to your "
        "TruBridge client for use of TruBridge FHIR APIs outside the Developer "
        "Sandbox for any use case other than Patient Access', under a separate "
        "agreement between the facility and TruBridge. Support: "
        "info@trubridge.com; API updates are announced only on the portal "
        "(Terms s.2(e)). See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "READ-ONLY by every document TruBridge publishes - the portal ('delivers "
        "read-only access'), trubridge.com/fhir-api, the FHIR API PDF, and the "
        "OpenAPI (fhir-developer.plt.trubridge.com/openapi/openapi.yaml), which "
        "defines GET operations only. BUT the sandbox AND production "
        "CapabilityStatements (cpsi-fhir-thrive 5.5.80, checked 2026-09-01) "
        "advertise create/update/delete on Patient, Condition, Observation, "
        "DocumentReference, Procedure, AllergyIntolerance, Coverage, Consent, "
        "Group and more. That matters because core/fhir/delivery/writer.py "
        "trusts the CapabilityStatement and refuses only what it does not "
        "advertise: against TruBridge the capability gate will NOT stop a "
        "DocumentReference, Observation or Condition create, so an undocumented "
        "POST would actually be attempted. That is why writable_resources above "
        "is empty and why no delivery to TruBridge may run with --confirm until "
        "TruBridge states in writing which resources a system client may write "
        "and under which scopes. A further reason a live write would fail "
        "today: core/fhir/delivery/__main__.py derives the destination token's "
        "scope from writable_resources (system/{Type}.write per entry), which is "
        "empty here, so the token request carries no scope and TruBridge - which "
        "documents scope as required - refuses it; and no system/*.c or .u grant "
        "is documented for a backend service anyway. Conditional create "
        "(If-None-Exist) is not "
        "documented, so supports_conditional_create stays False. The realistic "
        "write paths are a file handoff (core/fhir/bulk_export.py NDJSON) to the "
        "facility's own TruBridge tooling, or a patient-authorised app via "
        "MyCareCorner - a second client, not this profile."
    ),
)

# MEDHOST: every fact below is from MEDHOST's own developer portal
# (yourcareinteract.medhost.com - the "Developer Network" link on
# medhost.com/ehr/interoperability/), the OpenAPI file that portal serves
# (yourcareinteract.medhost.com/assets/doc/swagger.json), the portal's
# "Upcoming changes" document, the two public sandbox tenants' own
# /metadata and /.well-known/smart-configuration, and MEDHOST's own
# certification pages - all read 2026-09-01. The Epic entry above sets the
# DEPTH of this one; not one fact in it comes from Epic.
MEDHOST = EMRProfile(
    name="MEDHOST",
    # "Service client apps can obtain a token from the MEDHOST
    # authorization server using the 'client_credentials' workflow ...
    # When calling the token endpoint, the service client must
    # authenticate by sending a signed JSON Web Token and assertion
    # framework. This type of authentication is called 'private_key_jwt'
    # in OpenID terms." (Technical Guide for App Developers,
    # "Understanding Authentication Methods",
    # https://yourcareinteract.medhost.com/documentation). The secret-based
    # methods MEDHOST also documents (client_secret_basic, and
    # client_secret_post since July 2025) are for CONFIDENTIAL
    # authorization-code apps, not service clients - so this is the
    # assertion flow, not oauth2_client_credentials.
    auth_flow="smart_backend_services",
    # "MEDHOST supports RS384 and ES384 algorithms. Developers must
    # generate keys using either of these algorithms and register the
    # public key with the MEDHOST authorization server." (same page; the
    # US Core 6.1.0 sandbox tenant's smart-configuration publishes
    # token_endpoint_auth_signing_alg_values_supported = ["RS384","ES384"],
    # https://fhir.yourcareuniverse.net/tenant/7b158079-391d-484b-a078-bee596d2f165/.well-known/smart-configuration).
    # RS384 because it is what core/fhir/client.py signs today; ES384
    # (EC P-384) is equally documented and the emulator accepts both.
    assertion_algorithm="RS384",
    # The retention-relevant subset of MEDHOST's PUBLISHED read surface:
    # the 30 resource types in the portal's OpenAPI file
    # (https://yourcareinteract.medhost.com/assets/doc/swagger.json) and in
    # the 6.1.0 sandbox CapabilityStatement (.../metadata, version "6.1.0",
    # instantiates us-core-server and bulk-data). Absent on purpose because
    # MEDHOST does not publish them: Consent, AdverseEvent,
    # ExplanationOfBenefit. Published but not ingested here (a scope
    # choice, not a capability gap): CarePlan, CareTeam, Coverage, Device,
    # Flag, Goal, Medication, MedicationDispense, MedicationStatement,
    # Provenance, RelatedPerson, Specimen, and the non-clinical Endpoint,
    # Group, Location, Organization, Practitioner, PractitionerRole. Which
    # of these a facility actually exposes is that facility's
    # CapabilityStatement's to say - MEDHOST's own words: "For a List of
    # applicable FHIR Resources and SMART version supported at a facility,
    # please refer to the metadata".
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "MedicationAdministration", "ServiceRequest",
    ),
    # Vendor-documented: GET /Group/{id}/$export in the OpenAPI file, the
    # Group resource's `export` operation
    # (hl7.org/fhir/uv/bulkdata/OperationDefinition/group-export) in the
    # sandbox CapabilityStatement, and the Technical Guide's "Data Export"
    # step. Group level ONLY - see notes.
    supports_bulk_export=True,
    # Not documented by the vendor. MEDHOST documents scope selection at
    # app registration ("SMART v1 scopes will be configured based on the
    # selected SMART v2 scopes upon app registration or scope updates")
    # and never documents a mandatory scope parameter on the
    # service-client token request. Left at the conservative default, so
    # no scope is sent and the grant is whatever the facility approved.
    # If MEDHOST ever documents a required parameter, read the spelling
    # of their published scope list first - see notes.
    requires_token_scopes=False,
    # Vendor-documented: the "Upcoming changes" document
    # (https://api.mhdi10xasayd.com/medhost-developer-composition/v1/upcoming-changes,
    # US Core 3.1.1_v2, April 17 2024) sets the default page to 10 and the
    # maximum to 100, and says "Configure your app to pass a result count
    # of 20."
    page_size=20,
    # Not documented by the vendor as a number. What IS documented: the
    # OpenAPI file lists "429 Too Many Requests" as a response to
    # GET /Group/{id}/$export, and the Terms of Use
    # (https://yourcareinteract.medhost.com/terms) reserve the right to
    # "limit the use of the access to or use of API, including but not
    # limited to the volume of traffic or data flow permitted". The
    # default stays; treat a 429 as the vendor's answer, not a transient.
    rate_limit_per_min=60,
    # Not one FHIR write is published: all 61 operations in MEDHOST's
    # OpenAPI file are GET, the sandbox CapabilityStatement advertises only
    # `read` and `search-type` on every resource, and the "Upcoming
    # changes" document records "Removed support for system/Group.write"
    # (August 29 2024) - the one write scope MEDHOST ever listed. See
    # write_notes.
    writable_resources=(),
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09 against MEDHOST's developer portal "
        "(yourcareinteract.medhost.com - the 'Developer Network' link on "
        "medhost.com/ehr/interoperability/), its OpenAPI file "
        "(assets/doc/swagger.json), its 'Upcoming changes' document, its Terms "
        "of Use, and the two public sandbox tenants' own /metadata and "
        "/.well-known/smart-configuration. "
        "ACCESS MODEL - facility tenants, facility-gated apps: every MEDHOST "
        "facility is a tenant with its own FHIR base URL of the shape "
        "https://fhir.yourcareuniverse.net/tenant/{tenant-guid}; the "
        "production list is MEDHOST's published Endpoint/Organization bundle at "
        "api.mhdi10xasayd.com/medhost-developer-composition/v1/"
        "fhir-base-service-url-bundle (128 facilities when fetched). One "
        "authorization server serves them all - token endpoint "
        "https://api.mhdi10xasayd.com/smart/oauth2/token, from every tenant's "
        "smart-configuration and from idp.yourcareuniverse.net/.well-known/"
        "openid-configuration. "
        "REGISTRATION AND REVIEW: sign up on the portal, create a SANDBOX app of "
        "app type 'Service Client', then a separate PRODUCTION app. MEDHOST: "
        "'Production apps have access to real data, but only after MEDHOST and "
        "the facility have approved the app'; 'Provider-facing apps and service "
        "client apps also require approval from the appropriate facility'; 'New "
        "or updated apps must be submitted by Monday to be considered for review "
        "that week'; 'Some fields are not editable, and once an app is created, "
        "you may be required to create a new app altogether.' Contact: "
        "api-dev-admin@medhost.com. "
        "AUTH: private_key_jwt client assertion (auth_flow above), RS384 or "
        "ES384 (assertion_algorithm above). Keys are registered by JWK Set URL, "
        "never uploaded: 'App developers must provide the JWK Set URL that will "
        "return the client's JWKS key' (sandbox), and for production 'must work "
        "with individual facilities to set the JWKS keys ... during the approval "
        "process' - the production public key is a per-facility conversation, "
        "the mirror image of the base URL being per-facility. 'The app must use "
        "HTTPS with TLS 1.2'. Observed on the sandbox token endpoint 2026-09-01 "
        "(not in the docs): a client_secret on client_credentials gets HTTP 401 "
        "{'error':'invalid_client','error_description':'client authentication "
        "failed'}; a malformed assertion gets HTTP 400 invalid_client 'client "
        "authentication failed due to invalid client_assertion'. "
        "SCOPES: granted at registration and facility approval, SMART v2 with "
        "v1 back-fill ('For backward compatibility, SMART v1 scopes will be "
        "configured based on the selected SMART v2 scopes'); the Upcoming-"
        "changes scope matrix gives a System Client system/* scopes only - no "
        "patient/ or user/. MEDHOST asks for 'specific scopes whenever possible "
        "instead of wildcard scopes'. Two facts to know before ever sending "
        "scopes explicitly: the 6.1.0 tenant publishes no system/*.read wildcard "
        "at all, and MEDHOST's published scope list spells DiagnosticReport as "
        "'DiagnositcReport' in its v1 form (system/DiagnositcReport.read) - "
        "recorded as requires_token_scopes False above. "
        "VERSIONS: FHIR R4 4.0.1. Two sandbox tenants: US Core 3.1.1 (tenant "
        "174285d6-efb7-4560-a7ba-f3ae332b091f) and US Core 6.1.0 (tenant "
        "7b158079-391d-484b-a078-bee596d2f165); the 6.1.0 tenant advertises the "
        "SMART capabilities client-confidential-asymmetric and permission-v2, "
        "the 3.1.1 tenant neither, and MEDHOST notes its 3.1.1_v2 "
        "smart-configuration 'does not return value for attribute "
        "token_endpoint_auth_signing_alg_values_supported'. Which US Core a "
        "production facility runs is not stated in the base-URL bundle - read "
        "that facility's /metadata (CapabilityStatement.version is '6.1.0' on "
        "the newer sandbox). "
        "PAGING SEAMS (Upcoming changes, US Core 3.1.1_v2): default 10 per page, "
        "max 100, 'Configure your app to pass a result count of 20' (page_size "
        "above); 'Previous link ... no longer supported'; 'Self link ... no "
        "longer supported'; a Bundle with no results carries no 'entry' element "
        "at all and no 'meta'; the total is returned only with _total=accurate; "
        "'Saved FHIR IDs will become invalidated after the upgrade - Do not "
        "store FHIR IDs in your application'; 'Referenced resources are not "
        "guaranteed to be there'. iter_resources() in core/fhir/client.py "
        "tolerates every one of these - it reads only 'entry' and the 'next' "
        "link. Binary returns raw content (XML) unless Accept: "
        "application/fhir+json or _format is sent; the client already sends "
        "that Accept header. "
        "BULK: Group-level $export only - 'Currently the System and Patient "
        "Export are not supported through FHIR API'. There is no API to create a "
        "Group: 'The facility administrator will collaborate with MEDHOST "
        "Support to submit a support ticket containing the patient MRNs to be "
        "included in the Group ... the group ID will be provided to the "
        "facility administrator', and 'Group export is limited to 5000 patients "
        "per group ... If the request is for more than 5000 patients, MEDHOST "
        "will create multiple groups, and the customer will receive an ID for "
        "each group' - so PHI_AI_FHIR_GROUP_ID may need one run per group. The "
        "OpenAPI file documents 202/401/404/429/500 on the kickoff and NO _type, "
        "_since or _outputFormat parameters; bulk_client.kickoff_export() passes "
        "_type, so confirm on the facility whether it is honoured or ignored. No "
        "kickoff throttle interval, poll interval, file cap or manifest "
        "retention window is documented by the vendor; PHI_AI_BULK_POLL_"
        "INTERVAL_SECONDS keeps its default until MEDHOST or the facility says "
        "otherwise. "
        "TERMS: the portal's Terms of Use (Version 18.0) confine API use to "
        "'providing health care consumers who utilize MEDHOST Cloud Services' "
        "patient portal product (End Users) access to such End User's data' and "
        "reserve the right to revoke credentials - a retention/ingestion service "
        "client therefore rests on the facility's approval and agreement, not on "
        "the click-through terms alone. Resolve with the facility and "
        "api-dev-admin@medhost.com before go-live. "
        "CERTIFICATION: MEDHOST Enterprise - Clinicals 2024 R1, CHPL ID "
        "15.04.04.2788.MEDH.CL.10.1.250806 (certified 2025-08-06), criteria "
        "include 170.315 (g)(10), relying on 'MEDHOST Cloud Services' and the "
        "'MEDHOST Cures 2023 Interoperability Package' (medhost.com/about-us/"
        "regulatory-and-compliance/onc-certified-health-it/"
        "enterprise-onc-certified-health-it/). The facility must have licensed "
        "that package: 'Customers must purchase and activate the MEDHOST "
        "Interoperability package to use API access to patient health "
        "information' (MEDHOST's costs-and-considerations workbook, "
        "Ent.Clinicals 24R1). A facility without it has no FHIR door. Do not "
        "conflate the FHIR API with MEDHOST's (b)(10) EHI export, whose "
        "extension fields the interoperability page's spreadsheet documents - "
        "that is an administrator-run export, not this connector. See "
        "docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "MEDHOST publishes no FHIR write at all: every operation in its OpenAPI "
        "file is a GET, every resource in the sandbox CapabilityStatement "
        "advertises only read and search-type, the only write scope it ever "
        "published (system/Group.write) was removed in August 2024, and the "
        "g(10) criterion its API is certified to says such services "
        "'specifically exclude write capabilities'. The CapabilityStatement "
        "does list system-level batch and transaction interactions; with no "
        "resource advertising create, core/fhir/delivery/writer.py skips every "
        "record with 'does not advertise create for {Type}', which is the "
        "correct outcome - which is why writable_resources above is empty and "
        "supports_conditional_create is False. No alternative write path is "
        "documented on the developer portal; its only published contact is "
        "api-dev-admin@medhost.com. Until the facility and MEDHOST confirm a "
        "path, deliver to a MEDHOST facility as files."
    ),
)

# Confirm supported_resources against the target tenant's own
# CapabilityStatement (GET {base_url}/metadata - Netsmart serves it without
# authentication) before ingesting from it. Netsmart CareConnect fronts five
# separately certified CareRecords (GEHRIMED, myAvatar, myEvolv, myUnity,
# TheraOffice) through one FHIR connector, and its resource pages carry a
# per-CareRecord operations table ('support varies by the targeted
# CareRecord or solution'), so what a given tenant supports depends on which
# CareRecord sits behind it, not just on the connector version.
NETSMART = EMRProfile(
    name="Netsmart",
    # Netsmart documents BOTH grants for System Access and recommends this
    # one: 'Private Key JWT (Recommended)' / 'Client Secret' is the vendor's
    # own ranking on careconnect.netsmartcloud.com/docs/api/fhir/certified/
    # provider/system-access/. The client-secret alternative is fully
    # documented too and is one field flip away - see notes.
    auth_flow="smart_backend_services",
    # assertion_algorithm is deliberately left at the RS384 default:
    # Netsmart publishes no signing algorithm on its authorization page,
    # its System Access page or its Common Errors page, and the preview
    # tenant's .well-known/smart-configuration carries no
    # token_endpoint_auth_signing_alg_values_supported (observed
    # 2026-09-01). Not documented by the vendor - see notes for what the
    # first preview token request has to confirm.
    #
    # Netsmart's PUBLISHED System Access surface, restricted to the
    # clinical, retention-relevant types. The published list is the System
    # Access resource sidebar and the Provider APIs overview table
    # (careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/):
    # Base - Patient, Practitioner, Organization, Location, RelatedPerson;
    # Clinical - AllergyIntolerance, Condition, Procedure, Observation,
    # DiagnosticReport, Immunization, Specimen, MedicationRequest,
    # MedicationDispense; Workflow - Encounter, EpisodeOfCare,
    # ServiceRequest, CarePlan, CareTeam; Financial - Coverage;
    # Specialized - Device, DocumentReference, Binary, Group, Provenance;
    # plus Appointment, Goal, PractitionerRole, QuestionnaireResponse,
    # Schedule and Slot on the System Access sidebar. Every type below
    # except EpisodeOfCare also appears verbatim in the system_scope list
    # of Netsmart's own System Access tutorial
    # (.../docs/tutorials/testing-fhir-system-access-apis-with-postman/),
    # so the system/{Type}.read scope string derived from this tuple is
    # one Netsmart itself documents (in .rs form - see notes on syntax).
    #
    # Deliberately NOT listed, each for a stated reason:
    #   Practitioner, PractitionerRole, Organization, Location,
    #   RelatedPerson, Appointment, Schedule, Slot - reference and
    #     scheduling types; no retention rule attaches to them (the same
    #     line every other profile in this file draws).
    #   Group, Binary - export plumbing, not records (Binary's operations
    #     table is Read-only on every CareRecord).
    #   Provenance - the preview tenant advertises `read` only, no
    #     search-type (observed 2026-09-01), so the paged scheduler's
    #     GET /Provenance would fail; Netsmart's page was not checked for
    #     a search operation.
    #   Coverage, QuestionnaireResponse - published and plausibly
    #     retention-relevant (insurance coverage; behavioral-health
    #     assessment responses) but they need the HIM review that
    #     docs/DATA_SCOPE_REVIEW.md gave the claims types before they are
    #     ingested; listing them here would make that decision by default.
    #   MedicationAdministration, Consent, AdverseEvent,
    #   ExplanationOfBenefit - NOT on Netsmart's published System Access
    #     list. (Consent does appear on the preview tenant's live
    #     CapabilityStatement with create/update, observed 2026-09-01, but
    #     no Netsmart page documents it - absence here records that gap,
    #     not a confirmed inability.)
    supported_resources=(
        "Patient", "Encounter", "EpisodeOfCare", "Observation", "Condition",
        "MedicationRequest", "MedicationDispense", "DocumentReference",
        "AllergyIntolerance", "Immunization", "Procedure", "DiagnosticReport",
        "ServiceRequest", "CarePlan", "CareTeam", "Goal", "Device", "Specimen",
    ),
    # Vendor-documented: 'Bulk Data 2.0.0 - Asynchronous bulk data export'
    # (Provider APIs overview and System Access standards table), GET and
    # POST /Group/{id}/$export with _type, _since and _outputFormat
    # (.../system-access/resources/group/), and the CapabilityStatement
    # page shows the Group operation
    # 'http://hl7.org/fhir/uv/bulkdata/OperationDefinition/group-export|2.0.0'.
    # The preview tenant's live CapabilityStatement instantiates
    # bulk-data|2.0.0 and advertises that operation (observed 2026-09-01).
    supports_bulk_export=True,
    # Every client_credentials example Netsmart publishes carries an
    # explicit scope (authorization page, v2 migration guide, Common
    # Errors page, both Postman tutorials), the tutorials document an
    # 'Invalid scope' refusal, and the preview tenant's token endpoint
    # answered a scope-less request with 400 invalid_request 'scope is
    # required' (observed 2026-09-01). Netsmart DOES accept wildcards
    # ('system/*.rs' is the bulk tutorial's example and the preview
    # tenant's advertised scopes_supported); this flag only makes
    # authenticate_from_settings() send explicit per-type scopes, which
    # Netsmart documents just as well, so nothing is lost.
    requires_token_scopes=True,
    # Not documented by the vendor: Netsmart publishes no maximum _count;
    # its examples use 5, 10 and 20. Default kept.
    page_size=50,
    # Not documented by the vendor as a number. The Common Errors page
    # shows a 429 OperationOutcome (code 'throttled', 'Rate limit
    # exceeded. Please retry after 60 seconds.') with example headers
    # 'X-RateLimit-Limit: 1000 - Requests per time window' (window
    # unspecified) and 'Retry-After: 60'; the sandbox page says preview
    # limits 'may be more restrictive' than production. Default kept
    # until a tenant states a figure; the client does not yet honour
    # Retry-After on 429 (core/fhir/client.py has no 429 handling).
    rate_limit_per_min=60,
    # Vendor-documented: the DocumentReference and DiagnosticReport pages
    # (.../system-access/resources/documentreference/ and
    # .../resources/diagnosticreport/) each carry an operations table with
    # Create, Read, Update and Search = 'Yes' for all five CareRecords and
    # document POST /DocumentReference and POST /DiagnosticReport with a
    # request body. Every other clinical page checked (Condition,
    # Encounter, Binary) shows Create and Update as '-'. The preview
    # tenant's live CapabilityStatement advertises `create` and `update`
    # for exactly these two among the clinical types (observed
    # 2026-09-01).
    writable_resources=("DocumentReference", "DiagnosticReport"),
    # Not documented by the vendor: no Netsmart page mentions
    # If-None-Exist, and the preview tenant's CapabilityStatement declares
    # no conditionalCreate on any resource.
    supports_conditional_create=False,
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09 against careconnect.netsmartcloud.com (the CareConnect "
        "developer docs) and the fhirtest.netsmartcloud.com preview tenant. "
        "WHICH SURFACE: this profile targets the Certified v2 Provider System "
        "Access API - 'Automated system-to-system data exchange with bulk export "
        "capabilities' - FHIR R4 4.0.1, US Core 6.1.0, Bulk Data 2.0.0. It is the "
        "certified surface behind Netsmart's behavioral-health and post-acute "
        "CareRecords (myAvatar, myEvolv, myUnity, GEHRIMED, TheraOffice), NOT the "
        "'General Purpose' R4/STU3 APIs and NOT the v1 API (/uscore/v1, "
        "oauth.netsmartcloud.com, US Core 3.1.1, Bulk Data 1.0.0), which 'remain "
        "operational' with 'Deprecation timelines ... communicated with advance "
        "notice' and whose credentials 'will NOT work with v2 APIs'. "
        "TENANT MODEL: 'Each CareConnect FHIR tenant now has dedicated "
        "authorization and FHIR base URLs' - base "
        "https://fhir.netsmartcloud.com/provider/system-access/v2/{tenant-id}, "
        "token https://fhir.netsmartcloud.com/auth/{tenant-id}/oauth2/v1/token, "
        "preview on fhirtest.netsmartcloud.com with the same paths. The tenant "
        "id is a UUID shown in the developer portal once the tenant's owner "
        "approves the app's Tenant Authorization request, and 'each application "
        "can only be authorized for a single tenant' - one registration per "
        "customer tenant, so PHI_AI_FHIR_BASE_URL, PHI_AI_FHIR_TOKEN_URL and "
        "PHI_AI_FHIR_CLIENT_ID are all per-tenant. Production base URLs are also "
        "published as a SMART User-access Brands bundle at "
        "https://fhir.netsmartcloud.com/brand/brands.json. "
        "AUTH: two documented grants. (1) Private Key JWT, the one this profile "
        "uses because Netsmart marks it 'Recommended': 'To use this method you "
        "will need to include your JWK Set URI with the registration of your "
        "application' - a hosted JWKS is the only documented way to register "
        "the public key, so set PHI_AI_FHIR_JWT_KID to the kid in that JWKS. "
        "The documented token request is grant_type=client_credentials + "
        "client_assertion_type + client_assertion + scope; the Common Errors "
        "page lists the claims it checks ('iss, sub, aud, iat, exp, jti', "
        "'Verify audience matches token endpoint URL', 'Confirm private key "
        "matches registered public key') and the refusal as 400 "
        "{'error': 'invalid_client', 'error_description': 'Invalid client "
        "assertion JWT'}. What Netsmart does NOT document is the signing "
        "algorithm; the preview tenant's smart-configuration advertises the "
        "SMART 'client-confidential-asymmetric' capability, whose profile "
        "obliges the server to validate at least one of RS384 or ES384, so the "
        "first preview token request is what confirms RS384 - if it comes back "
        "'Invalid client assertion JWT' with a correct kid/aud/exp, try the "
        "secret grant before suspecting the key. (2) Client secret, equally "
        "documented: 'we recommend use of Basic Auth, however we do support their "
        "inclusion in the body as well', which is exactly the form-body shape "
        "authenticate_client_secret() sends, so switching this profile to "
        "auth_flow='oauth2_client_credentials' plus PHI_AI_FHIR_CLIENT_SECRET is "
        "a documented fallback. The preview tenant advertises "
        "token_endpoint_auth_methods_supported client_secret_basic, "
        "client_secret_post and private_key_jwt (observed 2026-09-01). Tokens: "
        "documented 'expires_in': 3600 with a 'tenant' claim in the response; "
        "the System Access page also promises 'Long-Lived Tokens' without a "
        "figure - not documented by the vendor beyond that. "
        "SCOPES: explicit system scopes on every token request (recorded as "
        "requires_token_scopes above). Netsmart documents SMART v2 syntax "
        "(system/Patient.rs) and states 'The v2 APIs support both v1 and v2 "
        "scope syntax for backwards compatibility', which is why the v1 "
        "system/{Type}.read strings authenticate_from_settings() derives are "
        "documented-valid; the preview tenant advertises permission-v1 and "
        "permission-v2. Which scopes the tenant owner actually GRANTED is "
        "tenant configuration: the tutorials' read-time refusal is 'HAPI-0333: "
        "Access denied by rule: Request is not authorized for this Tenant' and "
        "the token-time one is 'Invalid scope' ('verify resource names'); "
        "whether the authorization server narrows or refuses a request naming "
        "an ungranted type is not documented by the vendor - so every type in "
        "supported_resources must be one the app registration was granted. "
        "Netsmart's own standards table says 'SMART Backend Services 1.0' while "
        "its overview and migration guide say 'SMART App Launch 2.0.0'; nothing "
        "in this profile depends on the difference. "
        "BULK EXPORT (supports_bulk_export above): Group-level only is "
        "documented ('Group-Level Export - Export data for specific patient "
        "populations'); the System Access page's headline example shows a "
        "system-level /$export URL but no page documents that operation and "
        "the preview tenant advertises export on Group only. Group ids ARE "
        "discoverable through the API - the bulk tutorial's own step is GET "
        "{base}/Group?_count=10 and 'Copy a Group ID from "
        "entry[].resource.id'; set it as PHI_AI_FHIR_GROUP_ID. Required "
        "headers 'Accept: application/fhir+json' and 'Prefer: respond-async' "
        "(bulk_client.py sends both). Parameters _type, _since, _outputFormat "
        "(application/fhir+ndjson, ndjson, application/ndjson) and "
        "_typeFilter are documented; bulk_client.py sends _type but has no "
        "since parameter at all, so every run is a full re-extract of the "
        "Group even though Netsmart supports incremental export - a client "
        "gap, not a Netsmart one. Kickoff 202 + Content-Location "
        "(.../provider/export-status/v2/{tenant-id}/jobs/{id}); polling 202 "
        "with 'X-Progress: Running (45% complete)' and 'Retry-After: 120', "
        "'Use the Retry-After header value to determine when to poll again', "
        "'If no Retry-After header is present, wait 1-2 minutes', 'Large "
        "exports may take 10-30 minutes or longer'; bulk_client.py ignores "
        "Retry-After and polls at PHI_AI_BULK_POLL_INTERVAL_SECONDS, so set "
        "that to 120-180 for this vendor rather than the 600 default. The "
        "manifest's output URLs are Binary/{id}/$export-download with "
        "'requiresAccessToken': true (bulk_client.py sends the bearer token) "
        "and Netsmart's example adds 'Accept: application/fhir+ndjson', which "
        "bulk_client.py does not send - whether it is required is not "
        "documented by the vendor. DELETE on the status URL returns 202. "
        "Limits: '429 Too Many Requests - Export Limit' for 'Too many "
        "concurrent bulk export requests' and 'Avoid concurrent export "
        "requests'; the sandbox page adds 'Limited concurrent export jobs'. No "
        "per-day throttle is documented, so bulk_scheduler.py's 24-hour "
        "default interval is this codebase's default, not a Netsmart limit. "
        "'Your CareConnect app registration must include bulk data export "
        "permissions to access the $export operations' - ask for them at "
        "registration. "
        "PAGED SEARCH: _lastUpdated is a documented search parameter on most "
        "types (Encounter, DocumentReference, DiagnosticReport pages, and the "
        "preview tenant's CapabilityStatement) but NOT on the Condition page "
        "nor on the preview tenant's Condition searchParam list, so "
        "scheduler.py's incremental cycles (which send _lastUpdated=gt...) "
        "may draw the documented 400 'Invalid search parameter' on Condition "
        "after the first run - confirm on the tenant. 'Not all Netsmart "
        "solutions support Condition search.' POST /{Type}/_search is "
        "documented and recommended 'when searching with patient identifiers "
        "or other sensitive data'; iter_resources() uses GET. "
        "CERTIFICATION AND FEES: myAvatar Certified Edition holds "
        "170.315(g)(10) 'by leveraging Netsmart's CareConnect FHIR Interface' "
        "and 'A CareConnect FHIR subscription is required' (Netsmart's own "
        "Drummond disclosure, CHPL 15.04.04.2816.myAv.05.08.1.241227, certified "
        "2024-12-27); the Certified CareRecords page shows Bulk Data Export for "
        "all five CareRecords. The API Terms of Service add that 'Connection "
        "services may be needed ... any associated fees, would be set forth in "
        "a separate written agreement', that Netsmart may 'suspend, throttle or "
        "otherwise limit' an application, and that 'Client is responsible for "
        "managing and capturing any applicable patient level consents' - there "
        "is no consent screen in System Access; the tenant owner's "
        "authorization is the consent. 'Business Associate Agreements - "
        "Required for all system integrations'. "
        "NETWORK: outbound 443 to fhir.netsmartcloud.com (preview "
        "fhirtest.netsmartcloud.com) and the "
        "careconnect-prod-fhir-user-pool.auth.us-east-2.amazoncognito.com user "
        "pool host; the System Access page lists 'IP whitelisting' and the 403 "
        "causes include 'IP address not whitelisted (if IP restrictions "
        "apply)' - ask the tenant owner whether they apply. "
        "PREVIEW: the myAvatar sandbox tenant is "
        "d6c40265-c5c6-494f-b1aa-a27bf9a8c3f1 ('Internal CGI Avatar', "
        "CareFabric scope CGIAV_KS!UAT:PROD), synthetic data only, 'Allow 3-5 "
        "business days for tenant authorization approval'; myUnity, myEvolv, "
        "GEHRIMED and TheraOffice sandboxes are 'TBD'. See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "Netsmart's FHIR surface IS writable for two clinical types and no "
        "others: the DocumentReference and DiagnosticReport pages document POST "
        "(create) and PUT (update) with Create/Read/Update/Search = 'Yes' for all "
        "five CareRecords, which is what writable_resources above records. "
        "Netsmart's create example is a DocumentReference whose content is an "
        "attachment URL ('contentType': 'application/pdf', 'url': "
        "'https://example.com/document.pdf'); what a tenant does with an "
        "external URL versus inline data is not documented by the vendor - "
        "confirm on the preview tenant before relying on either. Condition, "
        "Encounter, Observation, MedicationRequest and the other clinical pages "
        "show Create and Update as '-', so a delivery of those types has no "
        "FHIR path at Netsmart at all; the preview tenant additionally "
        "advertises create/update for a set of Da Vinci PAS/DTR, scheduling and "
        "Consent/Subscription types (observed 2026-09-01) that no Netsmart page "
        "documents and that this system does not deliver. No conditional create "
        "(If-None-Exist) is documented, so a re-run will duplicate unless "
        "delivery is gated on the prior-record tag writer.py already applies. "
        "core/fhir/delivery/writer.py reads the destination tenant's own "
        "CapabilityStatement and refuses anything it does not advertise - the "
        "preview tenant advertises create for DocumentReference and "
        "DiagnosticReport, so that check passes there. TWO THINGS TO CONFIRM "
        "FIRST: (a) core/fhir/delivery/__main__.py now builds its destination "
        "token request on this profile and, because requires_token_scopes is "
        "True, sends system/DocumentReference.write system/DiagnosticReport.write "
        "(SMART v1 grammar, derived from writable_resources). Netsmart documents "
        "no system-level write scope string - its SMART 2.0 example syntax is "
        "patient/Observation.cruds - so whether the preview tenant honours the "
        ".write form is not documented by the vendor; if it refuses, mint the "
        "token out-of-band (PHI_AI_DELIVERY_ACCESS_TOKEN) with "
        "system/DocumentReference.cruds, the shape to try there; (b) the app "
        "registration must have been granted write "
        "permission by the tenant owner, which is tenant configuration. "
        "Netsmart's terms also make the Client responsible for testing any "
        "Developer Application 'before ... a live production environment'. "
        "Confirm all of this per tenant and per CareRecord before designing a "
        "delivery around it."
    ),
)

# Nextech: specialty ambulatory (ophthalmology, dermatology, plastics, med
# spa, orthopedics). Nextech publishes ONE SMART on FHIR model across two
# FHIR R4 servers - Select/NexCloud (select.nextech-api.com/api/r4) and
# IntelleChartPRO (api.intellechart.net/icp-fhir-api/) - and a separate,
# older "partner authorization" door that is NOT this profile (see notes).
# Every value below is taken from Nextech's own documentation, cited
# inline; where Nextech documents nothing the field stays at the
# conservative dataclass default and the comment says so. Nothing here is
# carried over from Epic or any other entry in this file.
NEXTECH = EMRProfile(
    name="Nextech",
    # VERIFIED (nextechsystems.github.io/selectapidocspub/r4.html, "System
    # apps"): "System apps must follow the SMART Backend Services
    # Authorization (STU 1.0.1) specification ... the client credentials
    # flow, with JWT credentials" - a signed JWT presented "instead of a
    # client secret". The live /.well-known/smart-configuration of BOTH R4
    # servers (fetched 2026-09-01, no credentials needed) advertises
    # token_endpoint_auth_methods_supported ["private_key_jwt"] and nothing
    # else, and grant_types_supported includes client_credentials.
    auth_flow="smart_backend_services",
    # assertion_algorithm is deliberately left at the RS384 default. Nextech
    # accepts "either an RS384 or ES384 signature" (same page) and the live
    # smart-configuration lists token_endpoint_auth_signing_alg_values_supported
    # ["RS384", "ES384"]; RS384 is what core/fhir/client.py signs today, so
    # an RSA key is sufficient and no EC P-384 material is required. This
    # is a codebase choice between two vendor-documented options, not a
    # vendor preference.
    #
    # Nextech's PUBLISHED R4 read surface, retention-relevant subset. The R4
    # reference and the live CapabilityStatement (which instantiates
    # http://hl7.org/fhir/us/core/CapabilityStatement/us-core-server)
    # document, for Select/NexCloud: Patient, AllergyIntolerance, CarePlan,
    # CareTeam, Condition, Device, DiagnosticReport, DocumentReference,
    # Encounter, Goal, Immunization, MedicationDispense, MedicationRequest,
    # Observation, Procedure, RelatedPerson, ServiceRequest (the R4 OpenAPI
    # tags it "v19.2+"), Specimen, Binary, Account, Coverage, Location,
    # Organization, Practitioner, Provenance - each with a matching
    # system/{Type}.read scope in Nextech's scope list. As with MEDITECH,
    # only the types this system retains are listed, which also keeps the
    # scope string derived below to the "bare minimum scopes" Nextech
    # insists on. NOT documented by Nextech, therefore absent:
    # MedicationAdministration, Consent, AdverseEvent, ExplanationOfBenefit.
    # MedicationDispense IS documented by Nextech but is not a type
    # core/fhir/clinical_dates.py or encounter_context.py map, so it is left
    # out rather than ingested half-understood - add it with those maps, not
    # before. IntelleChartPRO's own reference lists a narrower set (no
    # ServiceRequest, no Specimen, but Medication and PractitionerRole in
    # its live CapabilityStatement); confirm against the instance's
    # CapabilityStatement, as with every entry in this file.
    supported_resources=(
        "Patient", "Encounter", "Observation", "Condition", "MedicationRequest",
        "DocumentReference", "AllergyIntolerance", "Immunization", "Procedure",
        "DiagnosticReport", "ServiceRequest",
    ),
    # VERIFIED: the "Bulk FHIR Export" chapter of the R4 reference documents
    # GET /r4/Patient/$export, /r4/Group/{GroupID}/$export and /r4/$export
    # (every parameter marked "Initial Version 16.9"), and the live Select
    # CapabilityStatement advertises rest.operation "export" and
    # instantiates http://hl7.org/fhir/uv/bulkdata/CapabilityStatement/bulk-data.
    # IntelleChartPRO's reference documents the same three kick-offs under
    # its own base; its live CapabilityStatement carries the export
    # operation on the Group resource.
    supports_bulk_export=True,
    # VERIFIED: Nextech's documented system-app token request is
    # "grant_type=client_credentials &client_assertion_type=...
    # &client_assertion=[signed JWT] &scope=[spaceDelimitedDesiredScopes]"
    # and the response echoes the granted scope; the SMART Backend Services
    # STU 1.0.1 guide Nextech tells system apps to follow marks scope as
    # required. Nextech ALSO documents the wildcard system/*.read, but
    # "SMART apps must only request the bare minimum scopes that are
    # required for their app to function", so the per-type
    # system/{Type}.read string authenticate_from_settings() derives from
    # supported_resources is the right shape (Nextech: scopes must follow
    # "the format defined in version 1.0.0 of the SMART app specification").
    # What Nextech does with a scope-less request is not documented by the
    # vendor; sending the documented shape is the conservative choice.
    requires_token_scopes=True,
    # VERIFIED: "Search results are limited to 50 matches per page"; the
    # default is "the first ten matches ordered by entered date",
    # overridable "up to fifty" with _count.
    page_size=50,
    # VERIFIED ceiling: "a rate limit of 20 requests per second per
    # endpoint", HTTP 429 "Too Many Requests", "We advise to design to
    # handle these requests with Exponential backoff", and "The API is
    # intended for on-demand requests for user interaction in real-time,
    # try to avoid synchronizing data ... Requests should be staggered".
    # 20/s/endpoint is 1,200/min PER ENDPOINT; this throttle is global to
    # the client, so it is set to half that ceiling. The older partner-API
    # "Getting Started" PDF the developers portal still links states
    # "1,000 API calls per day (12AM - 12AM UTC) combined across all
    # applications for a single client" - confirm with Nextech which limit
    # binds your registration before sizing a run.
    rate_limit_per_min=600,
    # VERIFIED (Select/NexCloud R4 reference, "Writing"): "Currently, only
    # creating a document via a POST
    # https://select.nextech-api.com/api/r4/DocumentReference call is
    # supported." IntelleChartPRO R4 reference: "Currently, no write calls
    # are supported." See write_notes.
    writable_resources=("DocumentReference",),
    # If-None-Exist / conditional create is not documented by the vendor.
    supports_conditional_create=False,
    # FHIR $import is not documented by the vendor.
    supports_bulk_import=False,
    notes=(
        "VERIFIED 2026-09-01 against Nextech's own documentation: the developers "
        "portal (nextech.com/developers-portal), the Select/NexCloud R4 reference "
        "(nextechsystems.github.io/selectapidocspub/r4.html), the IntelleChartPRO "
        "R4 reference (nextechsystems.github.io/intellechartapidocspub/r4.html), "
        "Nextech's API Terms of Use (effective 10/1/2022) and the live, "
        "unauthenticated /metadata and /.well-known/smart-configuration of both "
        "R4 servers. "
        "TWO DOORS, ONE PROFILE. Nextech documents 'two different authorization "
        "models': SMART App authorization and partner authorization. This profile "
        "is the SMART door, specifically 'A secure SMART Backend Service with no "
        "user interaction' ('System apps'). The partner door is the older "
        "per-practice integration API: an OAuth 2.0 password grant ('Use password "
        "(Resource owner credentials grant)') at "
        "login.microsoftonline.com/nextech-api.com/oauth2/token, an nx-practice-id "
        "header on every call, credentials that 'expire on your first login and "
        "must be reset through Microsoft', an STU3-first surface, and Nextech's "
        "own statement that 'Partner integrations use resource owner credentials, "
        "not SMART'. That is a second client, not this profile. "
        "PRODUCTS. The same SMART model is published for Select/NexCloud (base "
        "https://select.nextech-api.com/api/r4) and IntelleChartPRO (base "
        "https://api.intellechart.net/icp-fhir-api/); both use the authorization "
        "server https://sts.mypatientvisit.com. Practice+ PM documents only an "
        "STU3 partner API (client_credentials with a Partner Secret and an Azure "
        "'resource' parameter) and SRSPro's documentation is hosted on MeldRx "
        "(app.meldrx.com) - neither is this profile. Nextech: 'We will continue to "
        "support our STU3 APIs, however we recommend that all new projects "
        "utilize the FHIR r4 APIs'. Certification: Nextech Select and NexCloud "
        "v20 is ONC-certified including 170.315(g)(10) (nextech.com/compliance/"
        "onc-health-it/nextech; ONC-ACB Certification ID "
        "15.04.04.2051.Ntec.20.12.1.251202, Drummond, certified 12/02/2025; CHPL "
        "listing 11722; the v20 Mandatory Disclosures PDF lists (g)(10) by name). "
        "Nextech EHR (ICP) 9 is certified separately (.../intellechart; "
        "15.04.04.2051.Inte.09.02.0.251202; CHPL 11724). Nothing in this entry "
        "rests on the mandate alone - every capability is vendor-documented. "
        "MULTI-TENANT, NOT FEDERATED. There is ONE base URL per product for every "
        "practice: Nextech's machine-readable endpoint bundles "
        "(nextech.com/hubfs/NexCloud_R4.json, 1,772 entries; hubfs/ICP_R4.json, "
        "1,499 entries) each carry a single Endpoint resource. Which practice a "
        "system app reaches is fixed at registration - 'The Portal Practice ID of "
        "the practice the client will communicate with'; 'The practice that the "
        "system app wishes to access must be setup with myPatientVisit, and "
        "established at app registration time'. So PHI_AI_FHIR_BASE_URL is the "
        "product base, PHI_AI_FHIR_TOKEN_URL is "
        "https://sts.mypatientvisit.com/connect/token, and 'which practice' lives "
        "in the client ID, not in the URL: one registration per practice. Whether "
        "the nx-practice-id header is also required on the SMART door is not "
        "documented by the vendor (the R4 OpenAPI requires it on 'partner-facing "
        "endpoints'); confirm on the instance. "
        "REGISTRATION. Through Nextech's app registration form (the developers "
        "portal's 'connection request form'; client-side requests go through "
        "nextech.my.site.com, 'Client login is required'). A backend service "
        "'must be confidential' and must supply 'A TLS 1.2 protected URL to the "
        "public JWK Set utilized by the app for JWT (JSON Web Key) credential "
        "signing' plus the Portal Practice ID; redirect URIs are 'not required "
        "for SMART Backend Services'. Confidential clients are also issued a "
        "client secret that 'will only be received once' - it serves the "
        "authorization-code (user-facing) flow, not this one, and "
        "PHI_AI_FHIR_CLIENT_SECRET is ignored for this vendor. No static "
        "public-key upload is documented: the JWK Set URL is the registration "
        "artefact. No review timeline, sandbox or non-production environment is "
        "documented by the vendor. "
        "TOKEN REQUEST. POST https://sts.mypatientvisit.com/connect/token with "
        "grant_type=client_credentials, "
        "client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer, "
        "client_assertion=[signed JWT], scope=[space-delimited scopes]. JWT "
        "claims: 'The sub and iss claims within the JWT must be the "
        "Nextech-issued client ID of the application, and must be exactly the "
        "same'; 'The aud claim is the token endpoint of the Nextech authorization "
        "server'; jti, iat, exp; signed with the app's private key, 'either an "
        "RS384 or ES384 signature'. Nextech's sample prints kid among the "
        "claims; the Backend Services guide it points to defines kid as the JWT "
        "HEADER parameter naming the key in your JWK Set, which is where "
        "client.py puts it when PHI_AI_FHIR_JWT_KID is set - set it, because "
        "Nextech resolves keys through the registered JWK Set URL. Whether "
        "Nextech also reads a payload kid is not documented by the vendor. The "
        "documented response is 'expires_in': 900 - FIFTEEN-MINUTE access tokens "
        "- and 'system apps are not issued refresh tokens, and so must always "
        "request a new access token upon previous access token expiration'. "
        "client.py authenticates once per run and tracks no expiry, so a paged "
        "run longer than about fifteen minutes will start receiving 401 "
        "('Unauthorized - The request lacks valid authentication credentials') "
        "until it re-authenticates; close that gap before a production run. "
        "SCOPES. Nextech 'currently only supports scopes that adhere to the "
        "format defined in version 1.0.0 of the SMART app specification' - "
        "system/{Type}.read, exactly what requires_token_scopes makes "
        "authenticate_from_settings() send - while also listing permission-v2 "
        ".rs forms, category sub-scopes and the wildcard system/*.read. 'With the "
        "exception of the DocumentReference.write or DocumentReference.cud "
        "scopes, writing-related scopes are currently not supported. "
        "Additionally, SMART apps must only request the bare minimum scopes that "
        "are required for their app to function.' When sub-scopes are requested "
        "'the parent resource-level scope ... will not be granted'. Behaviour on "
        "an unknown or ungranted scope is not documented by the vendor, so keep "
        "supported_resources to types the practice's CapabilityStatement lists. "
        "READ SURFACE AND PAGING. Both servers instantiate us-core-server (live "
        "CapabilityStatement). Unanchored searches are documented "
        "(GET /r4/Patient?_count=25), so core/fhir/scheduler.py's paged path can "
        "walk a whole practice; bundles carry first/next/previous/last links; "
        "_lastUpdated is documented (yyyy-MM-dd, and 'As of version 14.3' a full "
        "dateTime) and recommended 'to avoid re-querying unmodified data'. TLS "
        "1.2 is mandatory ('must use TLS 1.2') and only JSON is served: 'any type "
        "explicitly defined in the request's Accept header will be ignored'. "
        "BULK DATA EXPORT. Documented for Select/NexCloud from release 16.9 and "
        "for IntelleChartPRO: GET {base}/Patient/$export (all patients), "
        "{base}/Group/{GroupID}/$export, {base}/$export (system level). The "
        "kick-off REQUIRES 'Accept: application/fhir+json' and 'Prefer: "
        "respond-async' (bulk_client.py sends both). _outputFormat (default "
        "application/fhir+ndjson), _type and _since ('Resources will be included "
        "in the response if their state has changed after the supplied time') "
        "are all documented - Nextech has an incremental path even though "
        "bulk_client.py exposes no since parameter. 202 + Content-Location -> "
        "GET {base}/Export/{ExportJobID}: while In-Progress, 202 with X-Progress "
        "and 'The Retry-After HTTP response header gives a delay time in seconds' "
        "(their example: 120; poll_status() does not read it, so set "
        "PHI_AI_BULK_POLL_INTERVAL_SECONDS accordingly); on Error, 500 with an "
        "OperationOutcome ('An internal timeout has occurred' is their example); "
        "on Complete, 200 with the manifest, whose Expires header 'indicates when "
        "the files in the response will no longer be available for access'. "
        "Nextech's sample manifest has 'requiresAccessToken': false and file "
        "URLs on Azure blob storage (storagesample.blob.core.windows.net); "
        "bulk_client.iter_ndjson_resources() unconditionally sends the bearer "
        "token to the file URL, which against Nextech would send a live token to "
        "a non-Nextech host - honour requiresAccessToken before production. "
        "DELETE {base}/Export/{ExportJobID} cancels. Groups are the practice's "
        "'letter writing' groups (Patient search: 'group-id - The letter writing "
        "group of the patient', and GET /r4/Patient/ID?group-id=20 lists a "
        "group's patient ids); the Group resource itself is not searchable on "
        "Select (live CapabilityStatement: Group carries only the export "
        "operation). bulk_scheduler.py performs Group-level export only and "
        "insists on PHI_AI_FHIR_GROUP_ID, so with Nextech either obtain a "
        "letter-writing-group id from the practice or extend the scheduler to "
        "Nextech's documented Patient/$export. No export frequency limit, group "
        "size guidance or file retention period is documented by the vendor; "
        "bulk_scheduler's 24-hour default interval is not a Nextech fact, and "
        "Nextech's only guidance is 'If you need to synchronize data, it is best "
        "to do so during non-peak business hours. Which vary on a per practice "
        "basis.' "
        "LIMITS, FEES, TERMS. 20 requests/second/endpoint, 429, exponential "
        "backoff (above); the older partner 'Getting Started' PDF says 1,000 "
        "calls/day per client with 429 when reached. API Terms: 'At this time "
        "Nextech does not charge API connection fees for APIs required under the "
        "2015 or 2015 Cures Act Update CEHRT Regulations', with the right to do "
        "so in future; 'Either you, Nextech, or a provider engaging your "
        "application services may terminate your right to use the Nextech API at "
        "any time, with or without cause or notice.' The v20 disclosures list "
        "per-doctor licences, conversions, implementation and training, annual "
        "support and third-party interfaces as practice-side costs and state the "
        "practice contract 'does not contain limitations for the certified "
        "capabilities'. Rate limits are 'subjet to change' (sic). "
        "CONFIRM ON THE INSTANCE: GET {base}/metadata (which types, which of them "
        "show create, that rest.operation lists export) and "
        "{base}/.well-known/smart-configuration (token endpoint, private_key_jwt, "
        "RS384/ES384) - both are served without credentials; the practice's "
        "release (bulk needs Select 16.9+, ServiceRequest 19.2+); that the "
        "practice has 'activated their FHIR services'; and the letter-writing "
        "group id if Group export is used. See docs/EMR_CONNECTORS.md."
    ),
    write_notes=(
        "Select/NexCloud R4: 'Currently, only creating a document via a POST "
        "https://select.nextech-api.com/api/r4/DocumentReference call is "
        "supported', gated by the system/DocumentReference.write (v1) or "
        "system/DocumentReference.cud (v2) scope - the only 'writing-related "
        "scopes' Nextech supports. The live Select CapabilityStatement also "
        "advertises create on DiagnosticReport, which Nextech's prose does not "
        "mention; it is not listed above, and core/fhir/delivery/writer.py's "
        "CapabilityStatement check decides at run time either way. "
        "IntelleChartPRO R4: 'Currently, no write calls are supported.' "
        "Conditional create (If-None-Exist) is not documented by the vendor, so "
        "every delivery must be gated on an external record of what was already "
        "sent (see writer.py). Operationally: core/fhir/delivery/__main__.py "
        "builds the destination token request on this profile and sends "
        "system/DocumentReference.write, derived from writable_resources - the "
        "scope Nextech documents for the create; and Nextech's fifteen-minute, "
        "non-refreshable system-app tokens apply to delivery runs too. The "
        "broader writes Nextech advertises - Patient "
        "create/update, Appointment book/confirm, PaymentReconciliation, "
        "Composition ('Create non-clinical patient notes') - exist on the "
        "Select/NexCloud and Practice+ PARTNER APIs (STU3, password grant or "
        "client_secret, nx-practice-id header). They are a second client, not "
        "this profile, and not FHIR R4. Confirm the practice has activated FHIR "
        "services and that the registration was granted the write scope before "
        "designing a delivery around it."
    ),
)


PROFILES: dict[str, EMRProfile] = {
    "epic": EPIC,
    "cerner": CERNER,
    "athenahealth": ATHENAHEALTH,
    "eclinicalworks": ECLINICALWORKS,
    "meditech": MEDITECH,
    "nextgen": NEXTGEN,
    "modmed": MODMED,
    "altera": ALTERA,
    "greenway": GREENWAY,
    "veradigm": VERADIGM,
    "practicefusion": PRACTICEFUSION,
    "trubridge": TRUBRIDGE,
    "medhost": MEDHOST,
    "netsmart": NETSMART,
    "nextech": NEXTECH,
}


def profile_for(vendor_key: str) -> EMRProfile:
    """Look up a profile, or fail with the list of what exists.

    Deliberately no silent fallback to Epic: an unrecognised vendor key
    quietly getting Epic's auth flow and rate limits would produce a
    confusing authentication failure far from its cause.
    """
    key = (vendor_key or "").strip().lower()
    if key not in PROFILES:
        raise KeyError(
            f"unknown EMR vendor {vendor_key!r}. Known: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[key]
# Made by Ryan Gomez & Co. Inc.
