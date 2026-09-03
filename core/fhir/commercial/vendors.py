# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
One stub per vendor that sells a write path.

EVERY CLAIM HERE IS THE VENDOR'S OWN. The `sources` on each class name the
page it came from, and `docs/EXTENDING_CONNECTORS.md` walks through what
implementing one involves. Where a vendor's material does not say
something - a base URL, an auth scheme, a resource list - the stub says it
does not know rather than inventing a plausible value. An implementer with
a signed contract has documentation this repository does not.

None of these is implemented, and that is the honest state: each needs a
commercial agreement this project does not hold. What they give you is the
seam, the refusal text, and the paperwork trail.
"""

from __future__ import annotations

from core.fhir.commercial.base import CommercialConnector


class EClinicalWorksWriteAddOn(CommercialConnector):
    """eClinicalWorks FHIR Create/Update, sold as a contracted add-on.

    The nearest thing here to a drop-in: it is still FHIR, on the same
    endpoints, with create and update enabled per contract. An implementer
    with the contract may find that the standard EMRWriter simply works
    once the scopes are granted - try that before writing anything here.
    """

    vendor_key = "eclinicalworks"
    product = "FHIR Create/Update APIs (V12.0.2+)"
    is_fhir = True
    how_to_obtain = (
        "eClinicalWorks documents Create/Update APIs from V12.0.2 onward, but as a contracted "
        "add-on rather than a default capability: the practice must be on a supporting version and "
        "the interface has to be arranged with eClinicalWorks."
    )
    contact = "interop@eclinicalworks.com"
    sources = ("core/fhir/emr_profiles.py ECLINICALWORKS.write_notes",
               "docs/EMR_CONNECTORS.md, eClinicalWorks chapter")


class AlteraUnity(CommercialConnector):
    """Altera's Unity API - bidirectional, proprietary, not FHIR.

    Altera states its FHIR API is "limited to read-only access and not
    write-backs". Unity is the write path, sold on Integrator membership
    tiers, and it is a different protocol: implementing this is writing a
    Unity client, not adjusting a FHIR one.
    """

    vendor_key = "altera"
    product = "Unity API (proprietary, bidirectional)"
    is_fhir = False
    how_to_obtain = (
        "Unity is sold under Altera's Integrator membership tiers and sits outside FHIR entirely - "
        "demographics, appointments and financial data go through it. Membership and the Unity "
        "documentation come with the agreement."
    )
    contact = "ADP@alterahealth.com (Altera Developer Program)"
    sources = ("core/fhir/emr_profiles.py ALTERA.write_notes",
               "developer.adpstg.ahcentral.com ProcessOverview — 'limited to read-only access and not write-backs'")


class VeradigmUnity(CommercialConnector):
    """Veradigm's Unity API - the same product family as Altera's.

    Veradigm documents its FHIR API as read-only, and says developers
    "must utilize Unity to read or write patient demographic, appointment,
    or financial data". It is also the only route to Veradigm Practice
    Management.
    """

    vendor_key = "veradigm"
    product = "Unity API (proprietary, bidirectional)"
    is_fhir = False
    how_to_obtain = (
        "Unity is a proprietary API on Veradigm's Integrator tiers - a second client, not this "
        "profile. It is the only documented route to Veradigm Practice Management data."
    )
    contact = "Veradigm Connect — developer.veradigm.com"
    sources = ("core/fhir/emr_profiles.py VERADIGM.write_notes",
               "developer.veradigm.com ProcessOverview — 'limited to read-only access'")


class GreenwayGAPI(CommercialConnector):
    """Greenway's GAPI - proprietary, and different per product.

    Greenway's own description is the warning: "a Proprietary API with
    separate and distinct API calls and data structures for each of our
    EHR products". Intergy and Prime Suite differ, so this is realistically
    two implementations, and the subclass should not pretend otherwise.
    """

    vendor_key = "greenway"
    product = "GAPI (Greenway API, proprietary)"
    is_fhir = False
    how_to_obtain = (
        "GAPI 'supports reads and writes across a variety of clinical and financial data elements' "
        "and is reached through a different portal from the FHIR API. Its calls and data structures "
        "differ between Intergy and Prime Suite, so a deployment needs the one its practice runs."
    )
    contact = "developers.greenwayhealth.com"
    sources = ("core/fhir/emr_profiles.py GREENWAY.write_notes",
               "developers.greenwayhealth.com api-an-overview — 'read operations only at the present time'")


class ModMedProprietary(CommercialConnector):
    """ModMed's EMA Proprietary API - CREATE/READ/SEARCH/UPDATE, for a fee.

    Note the product boundary: EMA and Practice Management only. ModMed
    states it "will not be able to support gGastro customers", so a
    gGastro practice has no write path here at all and the stub should
    refuse rather than try.
    """

    vendor_key = "modmed"
    product = "EMA Proprietary API"
    is_fhir = False  # FHIR-shaped, but a separate product with its own auth and limits
    how_to_obtain = (
        "Offered 'for a fee' through the synapSYS Marketplace, with a technical review before a "
        "vendor is 'permitted to gain access to their first customer's production system'. Rate "
        "limited at 1,250 calls per minute per API key. EMA and Practice Management only - not gGastro."
    )
    contact = "synapsys@modmed.com"
    sources = ("core/fhir/emr_profiles.py MODMED.write_notes",
               "portal.api.modmed.com — Proprietary API; Mandatory Disclosures PDF ('for a fee')")


class MedhostInteroperabilityPackage(CommercialConnector):
    """MEDHOST - a licence gate rather than a different API.

    "Customers must purchase and activate the MEDHOST Interoperability
    package to use API access to patient health information." The facility
    buys it, not the integrator; without it there is no API access at all,
    read or write.
    """

    vendor_key = "medhost"
    product = "MEDHOST Interoperability package"
    is_fhir = True
    how_to_obtain = (
        "The FACILITY must purchase and activate the Interoperability package - it is a licence the "
        "customer holds, not something an integrator can buy on their behalf. Without it there is no "
        "API access to patient health information at all."
    )
    contact = "MEDHOST — via the facility's account team"
    sources = ("core/fhir/emr_profiles.py MEDHOST.write_notes",
               "MEDHOST costs-and-considerations workbook")


class NextGenEnterprise(CommercialConnector):
    """NextGen's proprietary Enterprise APIs, behind the developer portal.

    The public NextGen surface documents a patient-oauth token endpoint and
    no system/backend flow; the Enterprise APIs and full developer guides
    sit behind registration. An implementer needs that access before this
    can be written honestly.
    """

    vendor_key = "nextgen"
    product = "NextGen Enterprise APIs (proprietary)"
    is_fhir = False
    how_to_obtain = (
        "The full developer guides sit behind NextGen's developer portal registration; no "
        "system/backend flow or Enterprise-level $export is publicly documented, so the terms have "
        "to come from the agreement rather than from a public page."
    )
    contact = "NextGen developer portal"
    sources = ("core/fhir/emr_profiles.py NEXTGEN.write_notes",)


class NextechPracticePlus(CommercialConnector):
    """Nextech's writable surface - narrow, and per product.

    Select/NexCloud publishes exactly one create (DocumentReference, gated
    by a write scope); Practice+ on STU3 writes Patient, Appointment,
    DocumentReference and PaymentReconciliation. Different products, so a
    connector must know which one the practice runs.
    """

    vendor_key = "nextech"
    product = "Practice+ / Select write APIs"
    is_fhir = True
    how_to_obtain = (
        "Access is arranged through Nextech's connection request form and a client login. What can "
        "be written depends on the product: Select/NexCloud documents DocumentReference create only, "
        "gated by a write scope; Practice+ on STU3 adds Patient, Appointment and PaymentReconciliation."
    )
    contact = "nextech.com/developers-portal — connection request form"
    sources = ("core/fhir/emr_profiles.py NEXTECH.write_notes",
               "nextechsystems.github.io/practiceplusapidocspub (STU3 reference)")


class EpicLicensedWrite(CommercialConnector):
    """Epic - not a separate API, a per-health-system licence.

    Epic's write APIs are FHIR and the standard writer speaks them. What
    gates them is commercial: the specific write API must be licensed and
    enabled for that health system, per resource and per flavour. So this
    stub exists to carry the explanation, not a different protocol.
    """

    vendor_key = "epic"
    product = "Epic write APIs, licensed per health system"
    is_fhir = True
    how_to_obtain = (
        "Writing back is realistically limited to attaching documents to a chart, and even that "
        "requires the specific write API to be licensed and enabled for that health system - write "
        "APIs are enabled per resource and per flavour by each organisation, not once for Epic."
    )
    contact = "the health system's Epic team, via open.epic.com registration"
    sources = ("core/fhir/emr_profiles.py EPIC.write_notes",
               "open.epic.com/DeveloperResources")
