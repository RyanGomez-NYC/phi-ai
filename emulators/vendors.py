# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Per-vendor emulator behaviour.

WHY EMULATORS AND NOT A SINGLE MOCK. Testing against one generic FHIR
server proves the happy path and hides everything that actually breaks an
EMR integration. The differences that matter are not in the FHIR
resources - those are standardised - they are in the seams: how a client
authenticates, whether $export exists, what the server will accept as a
write, and what it does when asked for something it does not support.

Each emulator therefore reproduces the SEAMS of its vendor, honestly,
including the unhelpful behaviours:

  - athenahealth takes a client SECRET and rejects a JWT assertion.
  - Oracle Health (Cerner) takes EITHER a Basic-auth secret or a JWT
    assertion (both are documented), but refuses any token request that
    does not spell out explicit system scopes - no wildcards.
  - NextGen has no $export and returns an OperationOutcome saying so,
    rather than an empty result that a caller might mistake for "no
    data". (eClinicalWorks used to be modelled that way too; their portal
    now documents bulk FHIR APIs, so its emulator gained $export - see
    core/fhir/emr_profiles.py for the citation.)
  - MEDITECH serves only the US Core read surface and advertises create
    for nothing at all.
  - Epic advertises create for almost nothing.
  - Cerner supports conditional create; the others 412 or duplicate.

WHAT THESE ARE NOT. They are not certification. A green run here means the
client handles the shapes these emulators produce - which is the majority
of integration defects, but not proof that a real customer's build behaves
identically. Every profile in core/fhir/emr_profiles.py still says to
confirm against the instance's own CapabilityStatement, and that still
applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmulatorVendor:
    key: str
    name: str
    fhir_path: str                 # path prefix under which FHIR lives

    # Which token grants the emulator will honour. A client using the
    # wrong one gets the error a real server would give, not a helpful
    # fallback - the point is to catch that in a test.
    accepts_jwt_assertion: bool = True
    accepts_client_secret: bool = False

    supports_bulk_export: bool = True
    supports_conditional_create: bool = True

    # Refuse client_credentials token requests that carry no explicit
    # scope (and any wildcard scope). Oracle Health documents exactly
    # this: "Applications must explicitly request each scope" and
    # wildcards are unsupported - a client tuned on Epic, whose backend
    # token request takes no scope parameter at all, should fail HERE.
    requires_token_scope: bool = False

    # Resource types the CapabilityStatement advertises `create` for.
    # core/fhir/delivery/writer.py reads this and refuses anything absent,
    # so this is what makes the capability check testable.
    creatable: tuple[str, ...] = ()

    # Forces a real pagination round-trip regardless of _count. Small on
    # purpose: a client that mishandles the `next` link should fail here,
    # not in production against a large practice.
    page_size: int = 2

    smart_version: str = "2"
    notes: str = ""


VENDORS: dict[str, EmulatorVendor] = {
    "epic": EmulatorVendor(
        key="epic",
        name="Epic",
        fhir_path="/api/FHIR/R4",
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        supports_bulk_export=True,
        supports_conditional_create=False,
        creatable=("DocumentReference",),
        smart_version="2",
        notes="Backend Services JWT assertion. Advertises create for almost nothing.",
    ),
    "cerner": EmulatorVendor(
        key="cerner",
        name="Oracle Health (Cerner)",
        fhir_path="/r4/EMULATOR-TENANT",
        accepts_jwt_assertion=True,
        # CORRECTED against docs.oracle.com's authorization framework:
        # Basic client_id:secret (RFC 2617) is Oracle Health's PRIMARY
        # documented system-account mode, with the JWT assertion as the
        # bulk-data mode - this previously modelled JWT-only, which would
        # have failed a perfectly valid Basic-auth client in tests.
        accepts_client_secret=True,
        supports_bulk_export=True,
        supports_conditional_create=True,
        requires_token_scope=True,
        creatable=("DocumentReference", "Condition", "Observation"),
        smart_version="2",
        notes="Honours If-None-Exist; demands explicit system scopes at the token endpoint.",
    ),
    "athenahealth": EmulatorVendor(
        key="athenahealth",
        name="athenahealth",
        fhir_path="/fhir/r4",
        accepts_jwt_assertion=False,      # rejects the assertion flow outright
        accepts_client_secret=True,
        supports_bulk_export=True,
        supports_conditional_create=False,
        creatable=("DocumentReference",),
        smart_version="1",
        notes="Client secret only. A JWT assertion gets invalid_client, as it would live.",
    ),
    "eclinicalworks": EmulatorVendor(
        key="eclinicalworks",
        name="eClinicalWorks",
        fhir_path="/fhir/r4",
        accepts_jwt_assertion=True,       # asymmetric private-key JWT, per their portal
        accepts_client_secret=False,
        # CORRECTED with the profile (2026-08): eCW's portal documents
        # backend and bulk FHIR APIs, so the emulator grew $export too.
        supports_bulk_export=True,
        supports_conditional_create=False,
        creatable=(),
        smart_version="1",
        notes="Bulk export present; create advertised for nothing (their Create APIs are a "
              "contracted add-on, and this emulator models the uncontracted default).",
    ),
    "meditech": EmulatorVendor(
        key="meditech",
        name="MEDITECH Expanse",
        # The one URL fact MEDITECH's public explorer does give away:
        # operations live under v2/uscore/R4 (greenfield.meditech.com).
        fhir_path="/v2/uscore/R4",
        accepts_jwt_assertion=True,       # g(10) backend-services baseline
        accepts_client_secret=False,
        supports_bulk_export=True,        # Bulk Data is a documented explorer topic
        supports_conditional_create=False,
        creatable=(),                     # Greenfield calls the APIs view-only
        smart_version="2",
        notes="US Core read surface only; advertises create for nothing. Auth modelled on "
              "the g(10) baseline - see the profile's notes for what is vendor-confirmed.",
    ),
    "nextgen": EmulatorVendor(
        key="nextgen",
        name="NextGen Healthcare",
        fhir_path="/nge/prod/fhir-api-r4",
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        supports_bulk_export=False,
        supports_conditional_create=False,
        creatable=("DocumentReference",),
        smart_version="1",
        notes="No $export either.",
    ),
}

# Ports, so all six can run at once and a test can talk to any of them.
DEFAULT_PORTS: dict[str, int] = {
    "epic": 9101,
    "cerner": 9102,
    "athenahealth": 9103,
    "eclinicalworks": 9104,
    "nextgen": 9105,
    "meditech": 9106,
}
# Made by Ryan Gomez & Co. Inc.
