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
  - The client_assertion's signing algorithm is a per-vendor fact, held
    in each entry's `assertion_algorithms` and taken only from that
    vendor's own documentation. The token endpoint verifies against that
    tuple and never lets the JWT header pick the verifier, so an
    ES384-only vendor refuses the RS384 assertion the shipped client
    signs, an RSA-only vendor refuses ES384, and a vendor documenting
    both takes either - the refusal is invalid_client, the code RFC 7523
    s3.2 assigns to an invalid client JWT. A non-JWT string is refused
    the same way. Which vendor is which is read from VENDORS below.
  - A client secret on the client_credentials grant is honoured only
    where the vendor documents it; everywhere else it is invalid_client.
  - A POST to any type the CapabilityStatement does not list as creatable
    is a 422 OperationOutcome, and a $export on a vendor without bulk is
    an OperationOutcome saying so - never a 500, never an empty 2xx that
    a caller could mistake for success or for "no data".
  - Where a vendor documents nothing on a point, its entry says "not
    documented by the vendor" and the emulator defaults conservatively;
    nothing is borrowed from another vendor's answer.

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

    # The JWT client-assertion signing algorithms this vendor's token
    # endpoint verifies. A per-vendor fact taken from that vendor's own
    # documentation; where a vendor documents none, its entry says so and
    # this default stands as the conservative choice, not as a statement
    # about the vendor. server.py pins its verifier to this tuple - the
    # JWT header is compared against it, never allowed to choose it - so
    # an assertion signed with anything else (or a non-JWT string) gets
    # the invalid_client that RFC 7523 s3.2 assigns to an invalid client
    # JWT, in a test rather than against a real tenant.
    assertion_algorithms: tuple[str, ...] = ("RS384",)  # algs the emulator token endpoint accepts for client_assertion; an assertion signed with any other alg is rejected as invalid_client

    supports_bulk_export: bool = True
    supports_conditional_create: bool = True

    # Refuse client_credentials token requests that carry no explicit
    # scope. Oracle Health documents exactly this: "Applications must
    # explicitly request each scope" - a client tuned on Epic, whose
    # backend token request takes no scope parameter at all, should fail
    # HERE. Whether a WILDCARD scope is refused is a separate per-vendor
    # fact (refuses_wildcard_scope below); the two used to ride on one
    # flag, which made every scope-requiring emulator stricter than its
    # vendor on a point most of them leave undocumented.
    requires_token_scope: bool = False

    # Refuse a wildcard scope ("system/*.read") on the token request.
    # True only where the vendor documents the refusal (Oracle Health:
    # "we do not support Wildcard scopes"); "not documented by the
    # vendor" -> False, the conservative default, because an emulator
    # that refuses what the vendor may accept proves nothing either way.
    refuses_wildcard_scope: bool = False

    # True when the vendor's own discovery document publishes
    # token_endpoint_auth_signing_alg_values_supported and
    # assertion_algorithms above is that published list; the emulator's
    # /.well-known/smart-configuration then publishes it too. False where
    # the vendor publishes no list, so the emulator's discovery document
    # stays as silent as the vendor's rather than inventing one.
    signing_algs_published: bool = False

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
        # "we do not support Wildcard scopes" (the authorization framework
        # on docs.oracle.com) - the one vendor in this file that documents
        # the refusal, so the only one whose emulator refuses "*".
        refuses_wildcard_scope=True,
        creatable=("DocumentReference", "Condition", "Observation"),
        smart_version="2",
        notes="Honours If-None-Exist; demands explicit system scopes at the token endpoint "
              "and refuses wildcard scopes, both as documented.",
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

    "modmed": EmulatorVendor(
        key="modmed",
        name="ModMed",
        # A practice endpoint is https://{firm}.mmi.prod.fhir.ema-api.com/fhir/r4
        # (portal.api.modmed.com, OpenAPI server variable 'Your firm
        # subdomain'); the path under the host is what the emulator keeps.
        fhir_path="/fhir/r4",
        # The portal's token page: client_credentials is private_key_jwt -
        # 'No client_secret', and client_secret is 'never sent on the
        # client_credentials grant'. A secret gets invalid_client here.
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        # 'Signing algorithm confirmed working: ES384' (portal); the PDF's
        # worked example assertion is ES384. An RS384 assertion - the alg
        # every other profile signs - is rejected as invalid_client, the
        # error code RFC 7523 s3.2 assigns to an invalid client JWT.
        assertion_algorithms=("ES384",),
        # {base}/$export, /Patient/$export and /Group/{id}/$export are
        # documented (PDF p. 13; portal POST /$export): async, 202 first,
        # ndjson only.
        supports_bulk_export=True,
        # Read-only surface: nothing to create, so nothing to
        # conditionally create. Not documented; If-None-Exist gets 412.
        supports_conditional_create=False,
        # 'scope is required on this grant (space-separated system/*.rs
        # scopes)' (portal). A scope-less request - Epic's shape - fails
        # here with invalid_scope. Wildcards are NOT refused: the portal's
        # own wording is 'system/*.rs', so refuses_wildcard_scope stays
        # False.
        requires_token_scope=True,
        # The demonstration endpoint's smart-configuration publishes
        # token_endpoint_auth_signing_alg_values_supported containing
        # ES384 (see the ModMed chapter's pre-flight step), so this
        # emulator's discovery document publishes the tuple above too.
        signing_algs_published=True,
        # 'supports only Read, Search, and Bulk operations' (PDF); the
        # demo endpoint's CapabilityStatement advertises create for
        # nothing. Every write is a 422 'does not accept create'.
        creatable=(),
        page_size=2,
        # 'SVAP Version Approved: SMART App Launch 2.0' (PDF p. 35).
        smart_version="2",
        notes=(
            "Read/search/bulk only, ES384-only assertion, explicit scopes required. "
            "Reproduced: client_secret -> invalid_client; RS384 or non-JWT assertion "
            "-> invalid_client; no scope -> invalid_scope; $export served "
            "async (202, then manifest); any create -> 422; If-None-Exist -> 412. NOT "
            "reproduced (the shared server has no knob for them): live ModMed returns "
            "HTTP 401 for invalid_client, not 400; lists only system/{Type}.rs scopes, "
            "so a .read scope that passes here is unlisted there; polls with 'Status: "
            "In Progress' + Retry-After 120 rather than X-Progress; publishes "
            "requiresAccessToken false with S3 file URLs; documents POST kick-off; "
            "mints 30-minute tokens. Confirm those on the practice's own endpoint."
        ),
    ),

    "altera": EmulatorVendor(
        key="altera",
        name="Altera Digital Health",
        # The Sunrise ADP sandbox's documented R4 provider/system base is
        # https://sunrise-fhir-r4.adpsandbox.ahcentral.com/R4/fhir-Prod
        # (developer.adp.ahcentral.com/Fhir/FHIR_Sandboxes); TouchWorks
        # is /R4/fhir-R4. One documented shape is enough to make the
        # prefix real.
        fhir_path="/R4/fhir-Prod",
        # System apps authenticate with a private-key JWT client_assertion
        # verified against the JWKS URL registered on the app
        # (developer.adp.ahcentral.com/Fhir/SMARTonFHIR, 'SMART on FHIR
        # Backend Services (System Callers)').
        accepts_jwt_assertion=True,
        # The portal issues a Secret, but no Altera page documents a
        # secret-only system grant: the token body 'must include'
        # client_assertion. The sandboxes' smart-configuration lists
        # client_secret_basic/post for the user-facing confidential
        # clients. A secret sent as the system credential is undocumented
        # and is refused here; the error text is the emulator's own -
        # Altera documents none.
        accepts_client_secret=False,
        # Altera names no signing algorithm and its openid-configuration
        # publishes no token_endpoint_auth_signing_alg_values_supported.
        # This is the module default, not a vendor fact; it is written out
        # so the emulator's refusal of anything else is visible, and so it
        # can be corrected the day Altera documents one.
        assertion_algorithms=("RS384",),
        # '[FHIR path]/Group/INF-101/$export'; 202 + Content-Location,
        # then X-Progress / Retry-After polling and 'HTTP 429 Too Many
        # Requests' if polled before Retry-After
        # (developer.adp.ahcentral.com/Fhir/BulkData). Group-level only:
        # the sandboxes advertise $export on Group and nothing at system
        # or Patient level. The shared server does not reproduce the
        # 429-on-early-poll; bulk_client.poll_status() would raise on it.
        supports_bulk_export=True,
        # Not documented by the vendor - the API is read-only, so there is
        # nothing for If-None-Exist to apply to. 412, as the shared server
        # does for every vendor that does not document it.
        supports_conditional_create=False,
        # The token body 'must include' scope; Altera's own example is the
        # wildcard system/*.read (developer.adp.ahcentral.com/Fhir/
        # FHIR_Sandboxes). This flag makes a scope-less request fail with
        # invalid_scope - the seam a client tuned on a scope-less vendor
        # must hit here. Wildcards are NOT refused (refuses_wildcard_scope
        # stays False): Altera's own example IS the wildcard, and the
        # refusal is Oracle Health's documented rule, not Altera's. The
        # error_description the shared server emits ('requires explicit
        # ... scopes') is Oracle wording; Altera documents no error text
        # for a missing scope.
        requires_token_scope=True,
        # 'The Altera FHIR API is limited to read-only access and not
        # write-backs' (developer.adp.ahcentral.com/Fhir/ProcessOverview).
        # Advertising nothing makes writer.py refuse, which is the seam a
        # delivery must hit. The real sandboxes DO advertise create on
        # eleven types despite that sentence - see the profile's
        # write_notes; the emulator follows the documentation, and a
        # 422 'does not accept create' here is the emulator's own text.
        creatable=(),
        page_size=2,
        # SMART App Launch 1.0.0 and 2.0.0 are both documented
        # (developer.adp.ahcentral.com/Fhir/Introduction) and the
        # sandboxes advertise permission-v1 and permission-v2. New apps
        # get v1 scopes by default and the profile's scope string is v1
        # grammar; the shared server does not check scope grammar.
        smart_version="2",
        notes="Read-only: advertises create for nothing. Group $export only, async. Token "
              "request must carry a scope (Altera's example is the wildcard system/*.read, "
              "which passes: wildcards are refused only where a vendor documents the "
              "refusal). Secret-only and non-RS384 assertions get invalid_client; Altera "
              "documents no error text for either.",
    ),

    "greenway": EmulatorVendor(
        key="greenway",
        name="Greenway Health",
        # Greenway's base URL is /fhir/R4/{tenant OID} on one shared host;
        # the tenant id is the only per-customer part of the URL.
        fhir_path="/fhir/R4/EMULATOR-TENANT-OID",
        accepts_jwt_assertion=True,
        # No client-secret flow is documented for Greenway backend
        # services ('client credentials which are comprised of a
        # public/private JWKS key pair'); the live discovery document's
        # client-confidential-symmetric is for app-launch confidential
        # clients, not this flow. A secret gets invalid_client here.
        accepts_client_secret=False,
        # 'Use the ES384 (ECDSA using P-384 and SHA-384) signature
        # algorithm when generating your keys.' An RS384 assertion - what
        # core/fhir/client.py signs today - is refused as invalid_client,
        # which is the failure a client tuned on RS384 vendors must meet
        # here, in a test, not against a real tenant.
        assertion_algorithms=("ES384",),
        supports_bulk_export=True,
        supports_conditional_create=False,     # read-only API; nothing to dedupe
        # Greenway's documented token payload carries 'scope: SMART-on-FHIR
        # scopes needed for the app', so a scope-less request is refused.
        # Greenway does not document wildcard handling, so
        # refuses_wildcard_scope stays False (not documented by the vendor).
        requires_token_scope=True,
        creatable=(),                          # 'supports read operations only'
        smart_version="2",
        notes="ES384-only assertion (RS384 and secrets get invalid_client); scope-less token "
              "requests get invalid_scope; $export served; advertises create for nothing so "
              "the delivery writer skips every type. NOT modelled: Group-only export (this "
              "server answers $export at any level) and Greenway's 24-hour default _since "
              "window - see the profile's notes for both.",
    ),

    "veradigm": EmulatorVendor(
        key="veradigm",
        name="Veradigm",
        # Shaped like the sandbox Veradigm publishes on its FHIR Partner
        # Testing Environments page (.../fhirroute/fhir/CP00101/): the
        # site code is part of the path, one base URL per organization.
        fhir_path="/fhirroute/fhir/CP-EMULATOR",
        # developer.veradigm.com/Fhir/ProcessOverview, "System
        # Applications": the token body carries client_assertion +
        # jwt-bearer + client_credentials. That is the only System-app
        # grant Veradigm documents, so a client_secret is refused - and
        # the real sandbox token endpoint answered a secret-only
        # client_credentials request with 400 invalid_client (2026-09-01).
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        # 'Each key must use the RSA key type (kty)' (ProcessOverview,
        # "JWKS Requirements"): an EC/ES384 assertion has no key Veradigm
        # could verify it with, so it is rejected. Which RSA algorithms
        # the validator accepts is not documented by the vendor - its
        # sample JWKS says RS256, SMART App Launch 2.0.0 (which Veradigm
        # says it implements) requires RS384 - so both RSA algorithms
        # pass here; the profile signs RS384.
        assertion_algorithms=("RS384", "RS256"),
        # developer.veradigm.com/Fhir/BulkData: Group-level $export,
        # async 202 + Content-Location, then Accepted + X-Progress +
        # Retry-After until the manifest.
        supports_bulk_export=True,
        supports_conditional_create=False,  # not documented by the vendor
        # False on purpose: Veradigm's documented token body 'must
        # include ... scope: system/*.read' - a WILDCARD - and this flag
        # would reject exactly that. Whether a scope-less request is
        # refused is not documented by the vendor, so the emulator
        # accepts the wildcard, a per-type list, or no scope at all.
        requires_token_scope=False,
        # 'The Veradigm FHIR API is limited to read-only access.'
        # (ProcessOverview). Advertises create for nothing, so a POST
        # gets 422 not-supported and writer.py skips every type. (The
        # real sandbox's CapabilityStatement advertises create/update on
        # twelve types with no documented write scope - the emulator
        # models the documented API, not that contradiction; see the
        # profile's write_notes.)
        creatable=(),
        smart_version="2",                # SMART App Launch 2.0.0 (Introduction)
        notes="JWT assertion only (RSA: RS384 or RS256; ES384 is invalid_client); no scope "
              "enforcement because the documented scope is the wildcard system/*.read; "
              "Group $export with the async handshake; read-only, advertises create for "
              "nothing. Not reproduced: the 429 for polling before Retry-After, the Expires "
              "deadline on export files, and Provenance-by-default in $export output.",
    ),

    "practicefusion": EmulatorVendor(
        key="practicefusion",
        name="Practice Fusion",
        # Per-practice base URLs: api.practicefusion.com/fhir/r4/v1/{practice-guid}
        # is the 'Provider / System Access' endpoint in the vendor's own
        # ServiceBaseURLs.json (practicefusion.com/assets/static_files/).
        fhir_path="/fhir/r4/v1/EMULATOR-PRACTICE",
        # practicefusion.com/fhir/api-specifications 'System Apps':
        # client_credentials with a client_assertion JWT; the sandbox page
        # says 'no secret is required'. A secret gets invalid_client, as it
        # would live.
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        # The assertion header's alg is documented as 'JWA algorithm (e.g.,
        # RS384, ES384)' - both named by the vendor, so both are honoured
        # and anything else is refused as invalid_client (RFC 7523 s3.2).
        assertion_algorithms=("RS384", "ES384"),
        # Patient/$export and Group/{id}/$export are documented, with the
        # 202 + Content-Location / 202-in-progress / 200-manifest / DELETE
        # 202 handshake this emulator already speaks.
        supports_bulk_export=True,
        # Not documented by the vendor: an If-None-Exist create gets 412.
        supports_conditional_create=False,
        # The documented token request body carries `scope`, and 'System
        # applications can only request scopes that have been authorized
        # by the EHR user' - a scope-less request is refused. The vendor's
        # scope list is per-type system/{Type}.read|.rs only; no wildcard
        # form appears in it, but what the live server does with one is
        # not documented by the vendor, so refuses_wildcard_scope stays
        # False rather than borrowing Oracle Health's refusal.
        requires_token_scope=True,
        # 'they cannot change or write over EHR data' (vendor blog). The
        # real CapabilityStatement advertises create for Group alone - the
        # bulk cohort container, not clinical data - which this emulator
        # omits so that it and the profile's empty writable_resources
        # agree; the delivery outcome is identical either way: every
        # clinical type is skipped as not creatable.
        creatable=(),
        smart_version="2",                # 'SMART App Launch v2.0.0' (get-started page)
        notes="JWT assertion only (RS384 or ES384), explicit per-type scopes required, "
              "$export present, creates nothing. Not reproduced: the real token endpoint "
              "sits under the practice base URL ({BaseURL}/token) while this emulator "
              "keeps /oauth2/token; the practice's in-EHR 'Authorize App' gate; the "
              "1,000-patient Group cap; and the live sandbox's need for an approved app "
              "and production credentials.",
    ),

    "trubridge": EmulatorVendor(
        key="trubridge",
        name="TruBridge",
        # Production base URLs are per facility, shaped
        # thrive-gw.cpsi-cloud.com/api/smart/{site}/id-osfac.{uuid}/fhir/r4
        # (TruBridge's production endpoint directory, 2026-09-01); the
        # emulator keeps that shape so a client assuming a short,
        # tenant-less path fails here. The real token endpoint lives on a
        # DIFFERENT host (thrive-oauth.cpsi-cloud.com/oauth/smart/{site}/
        # id-osfac.{uuid}/token, read from {base}/.well-known/
        # smart-configuration) - this emulator cannot reproduce that; its
        # token endpoint is {base}/oauth2/token like every other emulator.
        fhir_path="/api/smart/emulator/id-osfac.00000000-0000-4000-8000-000000000000/fhir/r4",
        # Both grants are vendor-documented (?page=api/backend-services shows
        # an asymmetric request, a Basic-header request and a client_secret
        # POST request; smart-configuration lists client_secret_basic,
        # client_secret_post and private_key_jwt).
        accepts_jwt_assertion=True,
        accepts_client_secret=True,
        # token_endpoint_auth_signing_alg_values_supported, verbatim from
        # TruBridge's sandbox and production smart-configuration. The
        # vendor's own worked example signs RS256; PHI AI signs RS384; both
        # are accepted. Any other alg (HS256, none, ...) -> invalid_client.
        assertion_algorithms=("RS256", "RS384", "ES256", "ES384"),
        # Bulk Data Access v2.0.0 at system, Group and Patient level is on
        # the portal's Supported Data page and in every CapabilityStatement
        # the vendor's servers return (export, group-export, patient-export).
        supports_bulk_export=True,
        # Not documented by the vendor -> 412 here.
        supports_conditional_create=False,
        # The Backend Services token table marks `scope` REQUIRED ("Must be
        # subset of scopes that were granted ... during registration").
        # Wildcards are NOT refused here: TruBridge's smart-configuration
        # lists system/*.* and system/*.cruds as supported, so
        # refuses_wildcard_scope stays False.
        requires_token_scope=True,
        # assertion_algorithms above is the list TruBridge's own
        # smart-configuration publishes, so the emulator's publishes it too.
        signing_algs_published=True,
        # Every TruBridge document says read-only and its OpenAPI has no
        # POST, so nothing is creatable here. The real servers' metadata
        # DOES advertise create on several types (see the profile's
        # write_notes) - this emulator models the DOCUMENTED surface, so
        # the delivery capability check has the documented refusal to hit.
        creatable=(),
        page_size=2,
        # The scope documentation links SMART App Launch STU2.2 and
        # smart-configuration advertises permission-v2.
        smart_version="2",
        notes=(
            "Honours a JWT assertion (RS256/RS384/ES256/ES384) or a client secret "
            "(Basic or POST); refuses a scope-less client_credentials request "
            "because TruBridge documents scope as required (the vendor's error "
            "text is not documented, so the emulator's generic wording is used); "
            "serves $export at system, Group and Patient level; advertises create "
            "for nothing and 412s If-None-Exist, neither being documented by the "
            "vendor. Not reproduced: the separate OAuth host, the aud question, "
            "and the real servers' over-advertised create interactions."
        ),
    ),

    "medhost": EmulatorVendor(
        key="medhost",
        name="MEDHOST",
        # MEDHOST base URLs are facility tenants:
        # https://fhir.yourcareuniverse.net/tenant/{tenant-guid} - the two
        # public sandbox tenants and all 128 Endpoints in MEDHOST's
        # published base-URL bundle share the shape.
        fhir_path="/tenant/emulator-tenant",
        # Service clients authenticate with private_key_jwt only: "the
        # service client must authenticate by sending a signed JSON Web
        # Token" (yourcareinteract.medhost.com/documentation). The
        # client_secret_* methods MEDHOST lists belong to confidential
        # authorization-code apps, not the client_credentials grant, and
        # the live sandbox answers a client_secret on client_credentials
        # with invalid_client 'client authentication failed'.
        accepts_jwt_assertion=True,
        accepts_client_secret=False,
        # "MEDHOST supports RS384 and ES384 algorithms" (same page; the
        # 6.1.0 sandbox tenant publishes
        # token_endpoint_auth_signing_alg_values_supported ["RS384","ES384"]).
        # Any other alg is invalid_client - the code RFC 7523 s3.2 requires
        # and the sandbox's own wording is 'client authentication failed
        # due to invalid client_assertion'.
        assertion_algorithms=("RS384", "ES384"),
        # The 6.1.0 sandbox tenant publishes that list in its
        # smart-configuration, so this emulator's discovery document does too.
        signing_algs_published=True,
        # Group-level $export is documented (GET /Group/{id}/$export in
        # MEDHOST's swagger.json; Group carries the group-export operation
        # in the sandbox CapabilityStatement). LIMITATION OF THIS EMULATOR:
        # server.py serves $export at every level, but MEDHOST does not -
        # "Currently the System and Patient Export are not supported
        # through FHIR API". A system-level kickoff that passes here fails
        # live.
        supports_bulk_export=True,
        # No write is documented, so no If-None-Exist either.
        supports_conditional_create=False,
        # Not documented by the vendor as a token-request requirement;
        # scopes are approved at registration and by the facility.
        requires_token_scope=False,
        # All 61 published operations are GET; the sandbox
        # CapabilityStatement advertises read and search-type only;
        # system/Group.write was removed in August 2024. Create is
        # advertised for nothing.
        creatable=(),
        page_size=2,
        # The 6.1.0 sandbox tenant advertises permission-v2 and
        # client-confidential-asymmetric; registration takes SMART v2
        # scopes with v1 back-fill.
        smart_version="2",
        notes="private_key_jwt only, RS384 or ES384: a client_secret or an assertion signed "
              "with any other alg gets invalid_client. Group $export only live - this "
              "emulator cannot refuse system/patient-level kickoffs, so a green "
              "system-level export here proves nothing. Advertises create for nothing. "
              "Live seams NOT reproduced here: unauthenticated FHIR calls get HTTP 401 "
              "{'message':'Unauthorized'} rather than an OperationOutcome; empty search "
              "Bundles omit 'entry'; no self/previous links; FHIR ids are not stable "
              "across MEDHOST upgrades; kickoff can answer 429.",
    ),

    "netsmart": EmulatorVendor(
        key="netsmart",
        name="Netsmart",
        # Netsmart's v2 URL shape: /provider/system-access/v2/{tenant-id}
        # (careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/).
        # The real token endpoint lives at /auth/{tenant-id}/oauth2/v1/token,
        # which this emulator cannot model (its token route is fixed at
        # {base_url}/oauth2/token) - PHI_AI_FHIR_TOKEN_URL carries the
        # difference, not this prefix.
        fhir_path="/provider/system-access/v2/EMULATOR-TENANT",
        # BOTH grants, as Netsmart documents them: 'Private Key JWT
        # (Recommended)' and 'Client Secret', the secret either in a Basic
        # Authorization header ('we recommend use of Basic Auth') or in the
        # form body ('we do support their inclusion in the body as well') -
        # this emulator already reads both places. The preview tenant's
        # smart-configuration advertises client_secret_basic,
        # client_secret_post and private_key_jwt (observed 2026-09-01).
        accepts_jwt_assertion=True,
        accepts_client_secret=True,
        # Not documented by the vendor: Netsmart names no signing
        # algorithm and its discovery document carries no
        # token_endpoint_auth_signing_alg_values_supported. RS384 is this
        # field's conservative default for an undocumented point, kept so
        # the client this repository ships stays exercised; it is not a
        # Netsmart statement. The refusal is Netsmart's own documented one
        # for a rejected assertion (Common Errors page):
        #   400 {"error": "invalid_client",
        #        "error_description": "Invalid client assertion JWT"}
        # - use that error_description for the wrong-alg branch.
        assertion_algorithms=("RS384",),
        # Group/{id}/$export documented (Bulk Data 2.0.0, _type honoured).
        # This emulator also answers system- and Patient-level $export,
        # which Netsmart does NOT document - the shipped client only ever
        # calls the Group form, so the divergence is unreachable from it.
        supports_bulk_export=True,
        # Not documented by the vendor; no conditionalCreate on the
        # preview tenant's CapabilityStatement. A real re-run duplicates.
        supports_conditional_create=False,
        # Every documented client_credentials request carries a scope and
        # the preview token endpoint refuses one without: 400
        # invalid_request 'scope is required' (observed 2026-09-01; the
        # emulator answers invalid_scope with its own text). Wildcards are
        # NOT refused: Netsmart documents 'system/*.rs' as valid and the
        # preview tenant advertises it in scopes_supported, so
        # refuses_wildcard_scope stays False.
        requires_token_scope=True,
        # The two clinical types whose Netsmart resource pages show Create =
        # 'Yes' for all five CareRecords (DocumentReference and
        # DiagnosticReport operations tables; POST documented on both).
        # Condition, Encounter, Binary and the other clinical pages show
        # '-', so a create for any of them gets the 422 a real tenant's
        # CapabilityStatement would have warned about.
        creatable=("DocumentReference", "DiagnosticReport"),
        # 'SMART App Launch 2.0' on the Provider APIs overview and the v2
        # migration guide; the preview discovery document advertises
        # permission-v2 and client-confidential-asymmetric.
        smart_version="2",
        notes="Both grants honoured; explicit scopes demanded (wildcards accepted, as Netsmart "
              "documents system/*.rs); RS384-only assertion is the default "
              "for a point Netsmart leaves undocumented; Group $export served; create advertised "
              "for DocumentReference and DiagnosticReport only; If-None-Exist refused with 412; "
              "tenant-scoped URL prefix, emulator's fixed token route.",
    ),

    "nextech": EmulatorVendor(
        key="nextech",
        name="Nextech",
        # Select/NexCloud's documented base is https://select.nextech-api.com/api/r4
        # (nextechsystems.github.io/selectapidocspub/r4.html, "Base API Endpoint").
        fhir_path="/api/r4",
        # The SMART "System apps" door: a client-credentials grant "with JWT
        # credentials", the signed JWT presented "instead of a client secret".
        accepts_jwt_assertion=True,
        # Nextech's live /.well-known/smart-configuration lists
        # token_endpoint_auth_methods_supported ["private_key_jwt"] only
        # (fetched 2026-09-01). The client secret Nextech issues to
        # confidential clients belongs to the authorization-code flow; a
        # client sending it with client_credentials gets invalid_client here,
        # which is the RFC 7523 §3.2 error code for a rejected client
        # credential. Nextech documents no error text for this case.
        accepts_client_secret=False,
        # "must be either an RS384 or ES384 signature" (R4 reference, System
        # apps); token_endpoint_auth_signing_alg_values_supported
        # ["RS384", "ES384"] (live smart-configuration, so
        # signing_algs_published=True below). Both pass; HS*,
        # none, or a non-JWT string is refused as invalid_client.
        assertion_algorithms=("RS384", "ES384"),
        # "Bulk FHIR Export": Patient/$export, Group/{GroupID}/$export and
        # $export, 202 + Content-Location, then a polling endpoint with
        # X-Progress and Retry-After - the live CapabilityStatement
        # advertises rest.operation "export".
        supports_bulk_export=True,
        # If-None-Exist is not documented by the vendor -> 412, as the shared
        # server does for every vendor that does not document it.
        supports_conditional_create=False,
        # Nextech's documented token request carries
        # "&scope=[spaceDelimitedDesiredScopes]" and the Backend Services
        # STU 1.0.1 guide it mandates marks scope required, so a scope-less
        # request (the Epic habit) must fail here. Wildcards are NOT
        # refused: Nextech documents system/*.read, so
        # refuses_wildcard_scope stays False.
        requires_token_scope=True,
        # The live smart-configuration publishes the alg list (above), so
        # this emulator's discovery document publishes it too.
        signing_algs_published=True,
        # "Currently, only creating a document via a POST .../r4/DocumentReference
        # call is supported." Anything else -> 422 OperationOutcome, as live
        # ("422 Unprocessable Entity - Unable to process the contained
        # instructions" is in Nextech's response-code table).
        creatable=("DocumentReference",),
        # Nextech documents SMART App Launch 1.0.0 AND 2.0.0 for user apps and
        # Backend Services STU 1.0.1 for system apps; the live
        # smart-configuration advertises client-confidential-asymmetric,
        # permission-v1 and permission-v2. (Inert in server.py today.)
        smart_version="2",
        notes="Nextech's SMART system-app door: RS384- or ES384-signed JWT assertion "
              "only (a client_secret gets invalid_client), explicit system/{Type}.read "
              "scopes in the token request, $export at Patient, Group and system level "
              "with Retry-After on the poll, create advertised for DocumentReference "
              "only, no conditional create. Not modelled, because the shared server "
              "cannot: Nextech's 900-second non-refreshable tokens, its 20 req/s per "
              "endpoint 429, and export files hosted on a separate storage host with "
              "requiresAccessToken false - see the profile's notes for all three.",
    ),
}

# One port per vendor in VENDORS, so every emulator can run at once and a
# test can talk to any of them. Anything that quotes a port range derives
# it from this dict (min and max of the values) rather than typing it.
DEFAULT_PORTS: dict[str, int] = {
    "epic": 9101,
    "cerner": 9102,
    "athenahealth": 9103,
    "eclinicalworks": 9104,
    "nextgen": 9105,
    "meditech": 9106,
    "modmed": 9107,
    "altera": 9108,
    "greenway": 9109,
    "veradigm": 9110,
    "practicefusion": 9111,
    "trubridge": 9112,
    "medhost": 9113,
    "netsmart": 9114,
    "nextech": 9115,
}

# Fail at import, not with a KeyError at first launch: every vendor has a
# port, no vendor is a port without an entry, and no two share one.
if set(DEFAULT_PORTS) != set(VENDORS) or len(set(DEFAULT_PORTS.values())) != len(DEFAULT_PORTS):
    raise RuntimeError(
        "emulators/vendors.py: DEFAULT_PORTS and VENDORS disagree - "
        f"vendors without a port: {sorted(set(VENDORS) - set(DEFAULT_PORTS))}, "
        f"ports without a vendor: {sorted(set(DEFAULT_PORTS) - set(VENDORS))}, "
        f"duplicate ports: {len(DEFAULT_PORTS) - len(set(DEFAULT_PORTS.values()))}"
    )
# Made by Ryan Gomez & Co. Inc.
