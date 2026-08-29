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

Current profiles: Epic, Oracle Health (Cerner), athenahealth,
eClinicalWorks, MEDITECH, NextGen Healthcare. Each entry records the
vendor's PUBLISHED capability surface with a citation trail in
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
    # scopes. Oracle Health is the one vendor that requires this ("we do
    # not support Wildcard scopes"); Epic documents the opposite - its
    # backend-services token request has no scope parameter at all, and
    # the granted scope comes from the app registration. The scope string
    # itself is derived from supported_resources at authentication time
    # (see FHIRIngestionClient.authenticate_from_settings), so the two
    # can never drift apart.
    requires_token_scopes: bool = False
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
# The other four target EMRs.
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


PROFILES: dict[str, EMRProfile] = {
    "epic": EPIC,
    "cerner": CERNER,
    "athenahealth": ATHENAHEALTH,
    "eclinicalworks": ECLINICALWORKS,
    "meditech": MEDITECH,
    "nextgen": NEXTGEN,
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
