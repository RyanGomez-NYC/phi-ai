# EMR connectivity

This system connects to six EMR platforms: **Epic**, **Oracle Health
(Cerner)**, **athenahealth**, **eClinicalWorks**, **MEDITECH**, and
**NextGen Healthcare**. It did not start that way - the original scope
was deliberately Epic-only, on the principle that doing one integration
correctly (including its real auth model) beats a shallow multi-vendor
abstraction that's never been run against a live endpoint. That
discipline shaped the architecture this document describes, and it is
why adding the other five vendors meant adding *data*, not rewriting
the ingestion engine:

- Every vendor-specific fact - auth flow, supported resources, bulk
  export, page sizes, what a write really means - lives in an
  `EMRProfile` in `core/fhir/emr_profiles.py`. `core/fhir/client.py`
  stays a plain FHIR R4 client.
- Every vendor has an **emulator** (`emulators/`) reproducing its real
  seams - which grant its token endpoint accepts, whether `$export`
  exists, what it advertises as creatable - and the integration tests
  (`tests/test_emulator_integration.py`) drive the real client against
  all of them.
- Everything below distinguishes **verified from the vendor's own
  documentation** from **must be confirmed against the customer
  instance's own CapabilityStatement** (`GET {base_url}/metadata`).
  Every one of these platforms is federated or multi-tenant; the
  vendor's published surface is never a promise about one customer's
  build.

The Epic chapter is the deepest because Epic was the founding
integration and has been run against a live sandbox; the other vendors'
chapters record what their own documentation establishes and flag
explicitly what has only been exercised against the emulators.

The vendor is selected per deployment with `PHI_AI_EMR_VENDOR`
(default `epic`) - see `core/config/settings.py`.

## Epic

Primary source: Epic's own developer guide at
[open.epic.com/DeveloperResources](https://open.epic.com/DeveloperResources)
and [fhir.epic.com](https://fhir.epic.com).

### The federated model

Epic has no central API endpoint. Every customer organization runs its
own independent Epic instance and decides which apps can connect to it.
Epic's role is providing the platform and standard APIs; you connect
directly to each customer's own FHIR base URL, and their production data
never transits anything Epic operates.

Practically, this means `PHI_AI_FHIR_BASE_URL` is per-deployment,
not a constant - ingesting from three different Epic customers means
three different base URLs, not three different codebases.

### Registration flow

1. Register a client ID at [fhir.epic.com](https://fhir.epic.com),
   selecting **Backend Services** as the app type (this is the
   unattended, no-user-present flow ingestion needs - not the interactive
   EHR-launch or patient-facing flows Epic also supports).
2. Generate an RSA keypair (`scripts/generate_epic_keypair.sh`) and host
   the public half at a **JWK Set URL** rather than uploading a static
   key file directly - Epic's own JWK Set documentation states static
   key uploads have not been accepted for new sandbox app registrations
   since August 2025. A world-readable, unauthenticated HTTPS URL
   serving the JWKS JSON (a GitHub Gist raw URL works fine for dev) is
   sufficient; see `PHI_AI_FHIR_JWT_KID` below for the one
   additional JWT claim this requires.
3. Epic issues a client ID once you register; the client ID identifies
   your app across the Epic community, but each customer organization
   must separately sync that client ID to their own instance before you
   can connect to it.
4. Epic issues **separate non-production and production client IDs**.
   The non-production ID only works against Epic's public R4 sandbox
   (`https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/`); the
   production ID only works against a real customer's live instance.
   Using either against the wrong base URL is, by a wide margin, the most
   commonly reported integration failure among teams doing this for the
   first time.
5. Once a customer downloads your client ID to their instance, register
   the specific Incoming APIs you need (see below) and begin testing
   against their non-production environment before going live.

### Auth: JWT client assertion, not a client secret

This is the detail most generic "SMART on FHIR" documentation gets
wrong when applied to Epic specifically: **backend services auth does
not use a client secret.** It uses signed JWT client assertion (RFC
7523):

1. You hold an RSA private key. It never leaves your infrastructure.
2. You host the matching public key at a JWK Set URL registered on the
   client ID (see "Registration flow" above).
3. For every token request, you construct a short-lived JWT (RS384,
   `iss`/`sub` = client ID, `aud` = token endpoint, unique `jti`, `exp`
   no more than 5 minutes out, `kid` matching the key's entry in your
   JWKS) and sign it with the private key.
4. You POST that signed JWT as the `client_assertion` parameter with
   `grant_type=client_credentials` and
   `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.
   Epic's documented backend-services token request takes exactly these
   three POST body parameters - no `scope` parameter is part of this
   flow, which is why `FHIRIngestionClient.authenticate()` omits it by
   default (see `core/fhir/client.py`).
5. Epic verifies the signature against the public key on file and, if
   valid, returns a bearer access token (~1 hour lifetime) scoped to
   whichever Incoming APIs were registered for this client ID - Epic
   determines the granted scope from the app registration, not from
   anything the token request itself asks for.

`core/fhir/client.py` (`FHIRIngestionClient.build_client_assertion` /
`.authenticate`) implements exactly this. `PHI_AI_FHIR_CLIENT_SECRET`
is refused for Epic deployments, on purpose - a client secret is inert
against a real Epic instance. The setting exists solely for the one
vendor whose documented flow is a secret (athenahealth, below);
`Settings.from_env()` requires it exactly when that vendor is selected
and ignores it otherwise, and
`FHIRIngestionClient.authenticate_from_settings()` picks the token flow
from the vendor profile's `auth_flow`.

### Incoming APIs (scopes)

Epic doesn't use a scope string you request at token time (see above) -
instead, each FHIR resource type/operation you need is registered
individually as an "Incoming API" on the client ID in the Epic console,
and the granted token's scope is derived from whatever's registered.
Request only what you ingest; a client registered for resource types
you don't use is unnecessary surface area on the customer's side of the
trust relationship. `EPIC.supported_resources` in `emr_profiles.py`
lists what this codebase currently ingests - each entry needs its own
`{ResourceType}.Search` Incoming API registered before that type will
actually return data. Bulk Data Export needs four additional,
separate registrations - see below.

### Population-scale reads: use Bulk Data Export, not per-type search

`core/fhir/client.py`'s `iter_resources()` pages through Epic's regular
R4 search API per resource type - this works well for looking up
specific, known patients, but **cannot retrieve an entire population**.
Confirmed directly against a live sandbox: an unscoped `GET
{base}/Patient` returns `400 Bad Request` with the diagnostic *"This
resource requires demographics or `_id` parameter for searching."* Epic
requires a specific patient anchor for ordinary search; there is no
"give me every Patient" query available through this API, at any scope.

For a full ingestion run across an entire patient population, use
**FHIR Bulk Data Export** (`core/fhir/bulk_client.py` /
`core/fhir/bulk_scheduler.py`) instead - see the next section.

### Bulk Data Export

Implemented in `core/fhir/bulk_client.py` (kickoff/poll/download
primitives) and `core/fhir/bulk_scheduler.py` (the orchestration entry
point) - not per-resource paging, which cannot do population-scale
reads at all (see above). Every detail below is read directly from
Epic's own documentation at
[fhir.epic.com/Documentation?docId=fhir_bulk_data](https://fhir.epic.com/Documentation?docId=fhir_bulk_data),
cited again in `bulk_client.py`'s own module docstring.

**What Epic actually supports, and what it doesn't:**

- **Group-level export only.** Epic's own words: *"Epic supports only
  the Group Export operation. We do not support `_since` or other bulk
  data operations at this time."* No System-level export, no
  Patient-level export, and critically, **no incremental/delta
  capability of any kind** - every kickoff is a full re-extract of the
  Group's entire scope. This is why `bulk_client.py`'s functions take no
  `since` parameter the way `iter_resources()` does; there's nothing on
  Epic's side for one to do.
- **A Group FHIR ID is not discoverable through any API.** Epic's
  tutorial: *"Contact the organization you are integrating with to
  discuss what group of patients to use for your integration and to get
  the FHIR ID for that group."* For sandbox testing this means emailing
  `openepic@epic.com`; in a real deployment it comes from the healthcare
  organization directly. Set it as `PHI_AI_FHIR_GROUP_ID`.
- **Rate-limited to once per 24 hours per group+client ID by default**
  (documented independently by real integrators at
  good-neighbor.smarthealthit.org/tips/, with the specific rejection
  text *"The Client requested this Group too recently"* when violated).
  This is an enforced technical limit, not a suggestion.
- **Recommended max group size: 1,000 patients** (same source above).
  Not enforced by this codebase - it doesn't control group membership -
  but exported as `bulk_client.RECOMMENDED_MAX_GROUP_SIZE` so the figure
  lives in one documented place.

**Given all three of the above, plus Epic's own explicit "Poor Use Cases
for Bulk Data" guidance** - which names, verbatim, *"Periodic loads of
large amounts of clinical data," "Incremental data loads,"* and *"Data
synchronization with data warehouses or other databases"* -
`bulk_scheduler.py` defaults to a **24-hour run interval**, not
`scheduler.py`'s hourly one. Running it more often would simply get
rejected by Epic, not produce fresher data. Re-processing the same
resources on every run is expected, not wasteful-but-broken:
`core/db/index.py`'s idempotent write path and S3's content-addressed
keys mean a repeat write of an unchanged resource is a safe no-op.

**Required, separate app registrations.** Beyond the regular per-type
search Incoming APIs, bulk export needs four more registered on the
client ID: **Bulk Data Kick-off**, **Bulk Data Status Request**, **Bulk
Data File Request**, and **Bulk Data Delete Request**.

**Recommended poll interval** (Epic's tutorial, verbatim): *"every ten
minutes for groups with a hundred or fewer patients, every thirty
minutes for groups over a hundred, or using exponential backoff."*
`PHI_AI_BULK_POLL_INTERVAL_SECONDS` defaults to the ten-minute
figure; raise it for larger groups.

**Testing without a real Group ID yet:** `scripts/mock_epic_server.py`
simulates the complete Group export flow (kickoff, async status
polling with realistic multi-poll progression, NDJSON file download,
delete) against a stubbed `MOCK_GROUP_ID`. Point `.env` at it the same
way as the regular mock flow (see the file's own docstring) and set
`PHI_AI_FHIR_GROUP_ID=eSynGroup0001` to exercise the entire
real code path - `bulk_scheduler.py` runs completely unmodified against
either the mock or a real Group ID; only the URL and Group ID change.

### Cost note

The standardized USCDI read APIs (currently USCDI v3 / US Core 6.1.0)
are free to use under the 21st Century Cures Act - Epic is required to
expose them. What can cost money on Epic's side is non-USCDI resources
and a Connection Hub / Showroom listing, neither of which are required
for ingestion to work. Bulk Data Export itself carries no separate Epic
fee beyond the underlying APIs it reads. This is all separate from the
AWS-side costs in `docs/COST.md`.

### Validate before ingesting

`EPIC.supported_resources` in `emr_profiles.py` is a reasonable default
set, not a guarantee for any specific customer's instance. Confirm what a
given Epic instance actually exposes against its own conformance
statement before pointing an ingestion run at it:

```
GET {base_url}/metadata
```

Epic's build customization means two customer instances on the same
platform version can expose different resource sets in practice - and,
separately, `supported_resources` listing a type is not the same as that
type having actually been registered as an Incoming API on your specific
client ID. A mismatch between the two surfaces as a `403 Forbidden` at
that resource type specifically (both `scripts/mock_epic_server.py` and
the real sandbox return this shape) - worth checking directly rather
than assuming registration matches the profile.

## Oracle Health (Cerner)

Primary sources: the Millennium Platform APIs documentation on
docs.oracle.com - the historical home, `fhir.cerner.com`, now
301-redirects there. Key pages: the
[FHIR R4 API overview](https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfrap/r4_overview.html),
the [authorization framework](https://docs.oracle.com/en/industries/health/millennium-platform-apis/fhir-authorization-framework/),
and the [Bulk Data Access API](https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfbda/index.html).

- **Registration.** System apps are registered through the Oracle
  Health developer console (Cerner Central / code console); the
  customer's tenant enables them. Registration issues a system account
  (client ID + secret) and, for JWT auth, takes a preregistered JWKS
  via System Account Management.
- **Auth - two documented modes.** (1) OAuth2 client credentials with
  the secret in an **RFC 2617 Basic Authorization header** - the
  primary documented mode; and (2) a **signed JWT client assertion**
  against the preregistered JWKS, which the authorization framework
  calls "the appropriate mode of authentication for Bulk Data Access".
  The profile uses the JWT mode (same client-assertion code as Epic)
  because population ingestion wants `$export`. Either way, **every
  scope must be requested explicitly in the token request**
  (`system/Patient.read system/Observation.read ...`) - Oracle's docs
  are explicit that wildcard scopes are unsupported. This is the
  sharpest contrast with Epic, whose backend token request takes no
  scope parameter at all, and the Cerner emulator enforces it.
  Implements SMART App Launch 1.0.0 / Backend Services 1.0.1.
- **Base URL.** Tenant-scoped:
  `https://fhir-ehr.cerner.com/r4/{tenant-id}/...` (the tenant id is
  part of the path, so one deployment's base URL never reaches another
  tenant's data).
- **Resources.** The R4 docs publish a broad read surface; the profile
  lists the ingestion-relevant set. Verified from docs: Patient,
  Encounter, Condition, Procedure and others document read+search;
  `_count` is supported but not honored on `_id`/`identifier`
  searches. Confirm the tenant's actual surface against `/metadata`.
- **Bulk export.** Documented (Bulk Data Access API above); requires
  the JWT auth mode. Group-level availability is tenant configuration.
- **Writes.** Oracle publishes create endpoints for several types
  (Patient, Condition, DocumentReference POST operations are in the R4
  docs). The If-None-Exist conditional-create support recorded in the
  profile came from the pre-migration Cerner docs and could not be
  re-verified on the public Oracle pages - confirm per tenant before
  designing a re-runnable delivery around it.

## athenahealth

(Predates the 2026-08 vendor expansion; recorded here so the per-vendor
document is complete.) Client-credentials OAuth with a client **secret**
- the one target that does not use a signed JWT assertion, and the
reason `FHIRIngestionClient.authenticate_client_secret()` exists. Set
the secret with `PHI_AI_FHIR_CLIENT_SECRET`; `Settings.from_env()`
refuses to start an athenahealth deployment without it, and the
schedulers pick this flow automatically from the vendor profile
(`authenticate_from_settings()`).
Practice-scoped base URLs; apps are enabled per practice through the
Marketplace. Primary source:
[docs.athenahealth.com](https://docs.athenahealth.com/) (FHIR R4 API).
Confirm per practice; rate limits are tighter than the other targets.

## eClinicalWorks

Primary source: the eClinicalWorks FHIR developer portal at
[fhir.eclinicalworks.com](https://fhir.eclinicalworks.com/ecwopendev/documentation)
(Cures Act / g(10) APIs).

- **Registration.** Through the eCW developer portal
  (`fhir.eclinicalworks.com`); onboarding is per practice.
- **Auth.** SMART Backend Services with **asymmetric private-key JWT**
  - the portal's own words: "Backend Services uses Asymmetric (Private
  Key JWT) Authentication", with the public key registered as a JWKS.
  The same client-assertion flow as Epic; no client secret.
- **Base URL.** Per-practice, issued at onboarding through the portal.
- **Resources.** Read APIs cover the USCDI v1-v3 surface (Patient,
  Encounter, Observation, Condition, MedicationRequest,
  DocumentReference, AllergyIntolerance, Immunization, Procedure,
  DiagnosticReport, MedicationAdministration, ServiceRequest, and
  more). Confirm against the practice's CapabilityStatement.
- **Bulk export.** **Corrected in the 2026-08 review**: this document
  previously recorded eCW as having no bulk path. Their portal now
  documents backend (single-patient) and **bulk (multiple-patient)**
  FHIR APIs. Availability for a specific practice may still require
  contracting - confirm before planning a migration around `$export`
  rather than paged search.
- **Rate limits.** Vendor-documented ceiling of **250 calls per
  minute per base URL** (FHIR resource, authorize and token endpoints,
  effective Oct 2025).
- **Writes.** eCW documents FHIR Create/Update APIs (V12.0.2+: Patient,
  Encounter, MedicationRequest, Immunization, DocumentReference
  variants, Coverage, ServiceRequest) but as a **contracted add-on**
  arranged through `interop@eclinicalworks.com` - not a default
  capability, which is why the profile's `writable_resources` is empty.
  Until a contract says otherwise, deliver as files for their own
  migration tooling.

## MEDITECH

Primary sources: the
[Greenfield Workspace resources page](https://ehr.meditech.com/ehr-solutions/greenfield-workspace-resources)
and the Greenfield API explorer at
[greenfield.meditech.com](https://greenfield.meditech.com)
(`fhir.meditech.com` redirects there). Most technical detail sits
behind the portal login, so this chapter is explicit about what is
vendor-confirmed versus certification baseline.

- **Registration.** Through the MEDITECH Greenfield Workspace; MEDITECH
  issues credentials and endpoints on acceptance. There is no
  self-service public sandbox.
- **Auth.** *Certification baseline, not vendor-page-confirmed*: ONC
  g(10) population services require SMART Backend Services (asymmetric
  JWT client assertion), and MEDITECH's certified stack must implement
  it - but their public pages do not spell out the token request.
  Confirm grant details and scopes with MEDITECH before go-live. The
  profile records `smart_backend_services` on that basis and says so
  in its notes.
- **Base URL.** Verified from the public explorer: operations live
  under `.../v2/uscore/R4/` (the explorer documents
  `v2/uscore/R4/{operation}/`).
- **Resources.** Verified: **US Core FHIR R4** for USCDI data - the
  narrowest published surface of the six vendors, and the profile's
  `supported_resources` is deliberately the narrowest to match
  (Patient, Encounter, Observation, Condition, MedicationRequest,
  DocumentReference, AllergyIntolerance, Immunization, Procedure,
  DiagnosticReport). Absence of the extended types is recorded
  uncertainty, not confirmed inability.
- **Bulk export.** Verified as documented: "Bulk Data" is a topic in
  the Greenfield explorer, and MEDITECH appears in the SMART team's
  [registry of bulk-data implementations](https://github.com/smart-on-fhir/bulk-data-implementations).
- **Writes.** Greenfield describes the patient-access APIs as
  view-only; no general FHIR create is publicly documented. Deliver as
  files unless the customer's MEDITECH contacts confirm a write path.

## NextGen Healthcare

Primary sources: [nextgen.com/api](https://www.nextgen.com/api) and the
[NextGen Enterprise regulatory page](https://www.nextgen.com/api/regulatory-nge);
the full developer guides sit behind
[developer.nextgen.com](https://developer.nextgen.com) onboarding.

- **Which product.** This connector targets **NextGen Enterprise**
  (base URLs shaped `fhir.nextgen.com/nge/prod/fhir-api-r4/...`). Do
  not conflate with **NextGen Office**, a separate small-practice
  product whose public Bulk FHIR API authenticates with Basic
  client_id:secret against `fhir.meditouchehr.com`.
- **Registration.** Through the NextGen developer portal
  (onboarding-gated); apps are enabled per practice.
- **Auth.** Publicly documented: the Patient Access flow
  (`authorization_code` at
  `fhir.nextgen.com/nge/prod/patient-oauth/token`). **No system/backend
  flow is publicly documented** - the profile records
  `smart_backend_services` as the g(10) baseline expectation and flags
  it as needing confirmation through the portal.
- **Resources.** USCDI/US Core R4 for Patient Access is the published
  surface; the profile's broader list must be confirmed against the
  practice's CapabilityStatement.
- **Bulk export.** Recorded as **not available** until proven
  otherwise: nothing public documents an Enterprise-level `$export`.
  ONC g(10) obliges the certified stack to offer population services,
  so it likely exists behind the portal - but this codebase records
  what is verifiable, and `bulk_scheduler.py` will refuse to run
  against this profile until the flag is corrected against evidence
  from the gated docs or a real instance's CapabilityStatement.
- **Writes.** Confirm any write capability per practice before relying
  on it; the profile lists DocumentReference as the conversation
  starter only.

## What the emulators do and do not prove

`emulators/` runs one server per vendor (ports 9101-9106; MEDITECH is
9106) reproducing each vendor's seams: which credential its token
endpoint accepts, whether `$export` exists (and the OperationOutcome
refusal where it does not), what the CapabilityStatement advertises as
creatable, conditional-create behavior, and forced pagination. A green
integration run proves the client handles those shapes - the majority
of integration defects - but it is not certification, and it never
overrides the instance-level confirmation steps each chapter above
calls for.
