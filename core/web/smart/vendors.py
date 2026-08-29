# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
EMR vendor profiles for SMART on FHIR EHR launch.

All five targets - Epic, Oracle Health (Cerner), athenahealth,
eClinicalWorks and NextGen - implement the SMART App Launch Framework,
which is the entire reason this is one implementation rather than five.
The launch sequence, the discovery document, PKCE, and the `patient`
context in the token response are the same everywhere.

What differs is registration, scope dialect, and client type. Those are
data, in the same spirit as core/fhir/emr_profiles.py: vendor quirks
belong in a table, not scattered through the flow.

THE MOST IMPORTANT FIELD HERE IS `record_source`, and it is not about
SSO at all. In-context launch works by taking the `patient` id the EMR
returns and looking it up in this platform. That only finds anything if
this deployment's records CAME FROM THAT SAME EMR INSTANCE - patient ids
are opaque and instance-specific, so an id from a Cerner tenant means
nothing in a deployment populated from Epic. A launch from an unexpected
issuer must land the user on a search page with an explanation, never on
a confidently-wrong empty record. See core/web/smart/launch.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class VendorProfile:
    key: str
    name: str

    # SMART v2 uses granular scopes (patient/*.rs); v1 uses patient/*.read.
    # Asking for a v2 scope from a v1-only server is rejected outright by
    # some servers rather than downgraded, so this is per-vendor.
    smart_version: str = "2"

    # Public clients cannot hold a secret (a browser app); confidential
    # clients authenticate at the token endpoint. PKCE is required either
    # way in SMART v2 and is used unconditionally here.
    confidential: bool = True

    # Extra scopes beyond the baseline. `launch` and `openid fhirUser`
    # are always requested - `launch` is what makes it an EHR launch
    # rather than a standalone one, and fhirUser is how the clinician is
    # identified.
    extra_scopes: tuple[str, ...] = ()

    registration_notes: str = ""
    docs_url: str = ""


VENDORS: dict[str, VendorProfile] = {
    "epic": VendorProfile(
        key="epic",
        name="Epic",
        smart_version="2",
        confidential=True,
        registration_notes=(
            "Register on the Epic developer portal (fhir.epic.com) as a SMART on FHIR app "
            "with an EHR launch redirect URI. Each health system then enables the "
            "app in their own environment and issues a client id - Epic is federated, so a "
            "client id is per-health-system, not global. The same registration discipline as the "
            "backend-services app this project already uses for ingestion."
        ),
        docs_url="https://fhir.epic.com/Documentation?docId=oauth2",
    ),
    "cerner": VendorProfile(
        key="cerner",
        name="Oracle Health (Cerner)",
        smart_version="2",
        confidential=True,
        registration_notes=(
            "Register in the Oracle Health code console (code.cerner.com) as a provider-"
            "facing SMART app. Cerner distinguishes provider from patient apps at "
            "registration; a clinical record viewer is a provider app."
        ),
        docs_url="https://fhir.cerner.com/smart/",
    ),
    "athenahealth": VendorProfile(
        key="athenahealth",
        name="athenahealth",
        smart_version="1",
        confidential=True,
        extra_scopes=("patient/Patient.read",),
        registration_notes=(
            "Register through the athenahealth Marketplace / developer portal. Practices "
            "enable the app per-practice, so expect a per-practice issuer."
        ),
        docs_url="https://docs.athenahealth.com/api/guides/smart-fhir",
    ),
    "eclinicalworks": VendorProfile(
        key="eclinicalworks",
        name="eClinicalWorks",
        smart_version="1",
        confidential=True,
        registration_notes=(
            "Register through eClinicalWorks' developer programme. Confirm with the "
            "specific deployment whether launch context includes `patient` in the token "
            "response - the flow here refuses to guess if it does not."
        ),
        docs_url="https://fhir.eclinicalworks.com/ecwopendev",
    ),
    "nextgen": VendorProfile(
        key="nextgen",
        name="NextGen Healthcare",
        smart_version="1",
        confidential=True,
        registration_notes=(
            "Register via the NextGen developer portal. As with the others, the issuer is "
            "per-practice and must be allow-listed explicitly."
        ),
        docs_url="https://www.nextgen.com/api-and-developer-portal",
    ),
    # Deliberately present: SMART is a standard, and an EMR outside this
    # list that implements it correctly will work. It gets the
    # conservative dialect rather than being refused.
    "generic": VendorProfile(
        key="generic",
        name="Other SMART-conformant EMR",
        smart_version="1",
        confidential=True,
        registration_notes=(
            "Any EMR implementing SMART App Launch with a "
            "/.well-known/smart-configuration document. Uses the v1 scope dialect, which "
            "v2 servers also accept."
        ),
    ),
}


def baseline_scopes(profile: VendorProfile) -> str:
    """Scopes requested at launch.

    `launch` is what makes this an EHR launch: it tells the authorization
    server to resolve the opaque launch token into patient context. Without
    it the user would be asked to pick a patient, which defeats the point.

    `openid fhirUser` identifies the clinician - needed because this
    application audits every PHI view against a named user, and "someone
    launched from Epic" is not a name.

    Patient READ ONLY, and nothing else. This platform never writes to the
    EMR and does not need to read clinical data from it - it only needs to
    know WHICH patient. Requesting broad clinical scopes would ask
    organisations to grant access this application has no use for, which
    is both a bad look in a security review and a real expansion of what a
    compromise of it would yield.
    """
    scopes = ["launch", "openid", "fhirUser"]
    if profile.smart_version == "2":
        scopes.append("patient/Patient.rs")
    else:
        scopes.append("patient/Patient.read")
    scopes.extend(profile.extra_scopes)
    # offline_access is deliberately NOT requested: a refresh token would
    # let this application reach into the EMR after the clinician has gone
    # home, and it has no background work to do there.
    seen, ordered = set(), []
    for scope in scopes:
        if scope not in seen:
            seen.add(scope)
            ordered.append(scope)
    return " ".join(ordered)
# Made by Ryan Gomez & Co. Inc.
