# EMR connectivity

This system connects to every EMR platform profiled in
`core/fhir/emr_profiles.py` - one `PROFILES` entry and one chapter each:
**Epic**, **Oracle Health (Cerner)**, **athenahealth**,
**eClinicalWorks**, **MEDITECH**, **NextGen Healthcare**, **ModMed**,
**Altera Digital Health**, **Greenway Health**, **Veradigm**,
**Practice Fusion**, **TruBridge**, **MEDHOST**, **Netsmart** and
**Nextech**. It did not start that way - the original scope
was deliberately Epic-only, on the principle that doing one integration
correctly (including its real auth model) beats a shallow multi-vendor
abstraction that's never been run against a live endpoint. That
discipline shaped the architecture this document describes, and it is
why adding every vendor after Epic meant adding *data*, not rewriting
the ingestion engine:

- Every vendor-specific fact - auth flow, the client-assertion signing
  algorithm (`assertion_algorithm`: RS384 for most, ES384 where the
  vendor documents only that), supported resources, bulk export, page
  sizes, what a write really means - lives in an `EMRProfile` in
  `core/fhir/emr_profiles.py`. `core/fhir/client.py` stays a plain FHIR
  R4 client.
- Every vendor has an **emulator** (`emulators/`) reproducing its real
  seams - which grant its token endpoint accepts and which signing
  algorithms it honours, whether `$export` exists, what it advertises as
  creatable - and the integration tests
  (`tests/test_emulator_integration.py`) drive the real client against
  all of them. The end-to-end matrix (`tests/test_e2e_matrix.py`; see
  "Proof of integration" below) then drives every emulator as a source
  and every emulator as a delivery target.
- Everything below distinguishes **verified from the vendor's own
  documentation** from **must be confirmed against the customer
  instance's own CapabilityStatement** (`GET {base_url}/metadata`).
  Every one of these platforms is federated or multi-tenant; the
  vendor's published surface is never a promise about one customer's
  build.

Each vendor's own documentation is the source of truth for every fact
about that vendor - auth, keys, scopes, consent, bulk scope, writes,
registration, limits. The Epic chapter is the founding integration and
the only one run against a live sandbox; it is a reference for the depth
and format of the other chapters, never for their facts. Where a vendor
documents nothing on a point, its chapter says "not documented by the
vendor" and its profile defaults conservatively; no Epic behaviour,
default or assumption is carried onto another vendor.

The vendor is selected per deployment with `PHI_AI_EMR_VENDOR`
(default `epic`) - the value is the profile's key in `PROFILES`,
validated at startup - see `core/config/settings.py`. Every chapter ends
with a "Setting it up" section: registration, key pair and JWKS (or the
secret), environment, pre-flight, first ingest, first delivery, local
rehearsal against the emulator, and known limits with where to confirm
them - each from that vendor's own documentation, with "not documented
by the vendor" written where it is.

## Contents

- [Epic](#epic) - `epic`
- [Oracle Health (Cerner)](#oracle-health-cerner) - `cerner`
- [athenahealth](#athenahealth) - `athenahealth`
- [eClinicalWorks](#eclinicalworks) - `eclinicalworks`
- [MEDITECH Expanse](#meditech-expanse) - `meditech`
- [NextGen Healthcare](#nextgen-healthcare) - `nextgen`
- [ModMed](#modmed) - `modmed`
- [Altera Digital Health](#altera-digital-health) - `altera`
- [Greenway Health](#greenway-health) - `greenway`
- [Veradigm](#veradigm) - `veradigm`
- [Practice Fusion](#practice-fusion) - `practicefusion`
- [TruBridge](#trubridge) - `trubridge`
- [MEDHOST](#medhost) - `medhost`
- [Netsmart](#netsmart) - `netsmart`
- [Nextech](#nextech) - `nextech`
- [Proof of integration: the end-to-end matrix](#proof-of-integration-the-end-to-end-matrix)
- [What the emulators do and do not prove](#what-the-emulators-do-and-do-not-prove)

The key after each vendor is its `PHI_AI_EMR_VENDOR` value - its key in
`PROFILES` and in `emulators/vendors.py` `VENDORS`.

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
against a real Epic instance. The setting exists for the profiles whose
`auth_flow` is `oauth2_client_credentials` - athenahealth, whose
documented flow is a secret, and any deployment that switches a
both-grant vendor to that flow (Oracle Health, TruBridge and Netsmart
each document a secret as well as an assertion; their profiles ship on
the assertion); `Settings.from_env()` requires it exactly when the
selected profile records that flow and ignores it otherwise, and
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

### What the emulator reproduces

`emulators/vendors.py` `epic` (port 9101 from `DEFAULT_PORTS`, FHIR path
`/api/FHIR/R4`): the JWT-assertion grant only (a client secret is
`invalid_client`), the RS384 assertion (any other `alg`, and `alg: none`,
is `invalid_client`), no scope demanded (Epic's three-parameter token
request passes as-is), `$export` served with the asynchronous handshake,
`create` advertised for DocumentReference only, and no conditional
create (`If-None-Exist` answers 412). Not reproduced: the once-per-24-hours
group rate limit, the 1,000-patient group guidance, Epic's ten- and
thirty-minute poll cadences, per-customer Incoming API registration and
the non-production/production client-ID split.

### Setting it up

Zero to a working Epic connector in the non-PHI setup. Every URL, field
and command below is from Epic's own documentation cited in this chapter
or from this repository; where Epic does not document a step, the step
says so. `$REPO` is the checkout root; run Python as
`$REPO/.venv/bin/python`.

1. **Register the app with Epic.**
   1. Create a **Backend Services** app at
      [fhir.epic.com](https://fhir.epic.com) (the unattended flow; not
      the EHR-launch or patient-facing app types).
   2. Register the public key as a **JWK Set URL** (step 2 builds it).
      Epic's JWK Set documentation states static key uploads have not
      been accepted for new sandbox app registrations since August 2025.
   3. Add the **Incoming APIs** the profile ingests: one
      `{ResourceType}.Search` per entry in `EPIC.supported_resources`,
      plus the four bulk registrations - **Bulk Data Kick-off**, **Bulk
      Data Status Request**, **Bulk Data File Request**, **Bulk Data
      Delete Request**. A type without its registration answers `403` at
      that type, not at startup.
   4. Note the **client ID**. Epic issues a **non-production** and a
      **production** client ID per app; the non-production one works
      only against the public R4 sandbox
      (`https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/`),
      the production one only against a customer's live instance. Each
      customer organisation must sync your client ID to its own instance
      before you can connect.
   5. Ask the organisation for the **Group FHIR ID** to export - it is
      not discoverable through any API. For the sandbox, email
      `openepic@epic.com`.

2. **Generate the RSA key pair and the JWKS.** Epic's documented
   assertion is RS384, so the key is RSA (Epic accepts 2048- or
   4096-bit); keep it outside the repository.

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   "$REPO/scripts/generate_keypair.sh" --alg RS384 .      # writes private_key.pem / public_key.pem
   # (scripts/generate_epic_keypair.sh is the older RSA-only script with Epic's file names)
   ```

   Build the JWK Set (`kty` RSA, `alg` RS384, `use` sig, a stable `kid`):

   ```bash
   "$REPO/.venv/bin/python" - <<'EOF'
   import json
   from jwt.algorithms import RSAAlgorithm
   from cryptography.hazmat.primitives import serialization
   priv = serialization.load_pem_private_key(open("private_key.pem", "rb").read(), password=None)
   jwk = RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
   kid = "phi-ai-epic-2026-09"
   json.dump({"keys": [{**jwk, "kid": kid, "alg": "RS384", "use": "sig"}]},
             open("epic_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

   Host `epic_jwks.json` at a world-readable HTTPS URL
   (`deploy/aws/README_EPIC_JWKS.md` is the S3-hosted pattern; a GitHub
   Gist raw URL works for development) and give that URL to the app in
   step 1.2. The `kid` is `PHI_AI_FHIR_JWT_KID`. There is no client
   secret to store.

3. **Configure the PHI AI environment** (names from
   `core/config/settings.py`, prefix `PHI_AI_`):

   ```bash
   PHI_AI_EMR_VENDOR=epic
   # The customer's own base URL - per deployment, never a constant. The sandbox while rehearsing:
   PHI_AI_FHIR_BASE_URL=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
   # The token endpoint the instance advertises (step 4 reads it); the assertion's aud is this exact string.
   PHI_AI_FHIR_TOKEN_URL=<token endpoint from the instance's .well-known/smart-configuration>
   PHI_AI_FHIR_CLIENT_ID=<the non-production or production client ID that matches the base URL>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem     # mounted, never baked in
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   PHI_AI_FHIR_GROUP_ID=<Group FHIR ID from the organisation>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600     # Epic's "every ten minutes" for groups of 100 or fewer; 1800 for larger
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET: Epic issues none and Settings.from_env() refuses it for this vendor.
   ```

   `Settings.from_env()` validates `PHI_AI_EMR_VENDOR` against `PROFILES`,
   requires the key file to exist and refuses a key that cannot sign
   RS384. No scope is sent: `requires_token_scopes` is False, matching
   Epic's three-parameter token request.

4. **Pre-flight the instance (no token needed).**

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for x in r["security"]["extension"][0]["extension"]: print(x["url"], x.get("valueUri"))
   for res in r["resource"]:
       print(res["type"], [i["code"] for i in res.get("interaction", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); print("token_endpoint", d["token_endpoint"])'
   # Epic requires a patient anchor for ordinary search - an unscoped Patient search is a 400:
   curl -s -o /dev/null -w '%{http_code}\n' -H 'Accept: application/fhir+json' "$B/Patient"
   ```

   Look for: `fhirVersion 4.0.1`; the `token` URL (put it in
   `PHI_AI_FHIR_TOKEN_URL`); every `EPIC.supported_resources` type with
   `read` and `search-type`; and the `400` on the unscoped Patient search
   (*"This resource requires demographics or `_id` parameter for
   searching"*), which is why population reads go through `$export`.

5. **First ingest.** Bulk is the population path (paged search cannot
   enumerate a population on Epic, see above):

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.bulk_scheduler --once
   ```

   Success reads: `Authenticating to Epic FHIR endpoint`; `Kicking off
   bulk export: group=<id>`; one or more `Bulk export still in progress`
   (poll every `PHI_AI_BULK_POLL_INTERVAL_SECONDS`); then the manifest,
   the NDJSON files streamed, resources stored and indexed, and the
   status URL deleted. A `403` at one type means that type's Incoming API
   is not registered on the client ID. Re-running within 24 hours is
   refused by Epic (*"The Client requested this Group too recently"*) -
   that is the documented rate limit, not a defect. Then verify:

   ```bash
   .venv/bin/python -m core.verify --deep
   .venv/bin/python -m core.verify --export-dir <dir the bulk run wrote>
   ```

6. **First delivery (what a write attempt does).** Dry run first, always:

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "<the target instance's base URL>" \
     --vendor epic --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

   `writer.py` reads the destination CapabilityStatement and admits only
   what advertises `create` - on Epic, DocumentReference per the profile
   (write APIs are enabled per-resource *and per-flavor* by each health
   system, so enumerate each one on the app). The destination token
   request is built on the `epic` profile: RS384 assertion, no scope, key
   family checked before the network call
   (`PHI_AI_DELIVERY_CLIENT_ID`, `PHI_AI_DELIVERY_TOKEN_URL`,
   `PHI_AI_DELIVERY_PRIVATE_KEY_PATH`, `PHI_AI_DELIVERY_JWT_KID`; a
   `PHI_AI_DELIVERY_CLIENT_SECRET` is refused for this vendor). Epic
   documents no conditional create, so a confirmed run needs
   `--allow-duplicates` and an external record of what was already sent.
   Never deliver back into the source instance: the writer refuses it.

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor epic        # port 9101 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9101/api/FHIR/R4     token: http://127.0.0.1:9101/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k epic -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k "epic->" -v      # Epic as a source into every target
   ```

   `scripts/mock_epic_server.py` is the older Epic-specific mock with the
   full Group export flow against `MOCK_GROUP_ID` (`eSynGroup0001`); point
   `.env` at it and set `PHI_AI_FHIR_GROUP_ID=eSynGroup0001` to run the
   bulk scheduler unmodified. Record the green run in
   `private-notes/e2e-proof.md` (never in the repository).

8. **Known limits, and where to confirm them.**
   - Group export only, no `_since`, full re-extract every run; once per
     24 hours per group and client ID; recommended group size 1,000
     (`bulk_client.RECOMMENDED_MAX_GROUP_SIZE`) -
     [fhir.epic.com/Documentation?docId=fhir_bulk_data](https://fhir.epic.com/Documentation?docId=fhir_bulk_data)
     and the integrator notes cited above.
   - Poll cadence: ten minutes (groups of 100 or fewer), thirty minutes
     (larger), or exponential backoff - Epic's tutorial.
   - Costs: USCDI read APIs free under the Cures Act; non-USCDI resources
     and a Connection Hub / Showroom listing may cost - "Cost note" above.
   - Not documented on the pages cited here: rate limits on the search
     API, page-size ceiling, token lifetime beyond "~1 hour" - confirm on
     the instance and with the organisation's Epic contacts.
   - Terms: Epic's API subscription agreement, signed by each health
     system before production (fhir.epic.com).

## Oracle Health (Cerner)

Primary sources: the Millennium Platform APIs documentation on
docs.oracle.com - the historical home, `fhir.cerner.com`, now
301-redirects there. Key pages: the
[FHIR R4 API overview](https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfrap/r4_overview.html),
the [authorization framework](https://docs.oracle.com/en/industries/health/millennium-platform-apis/fhir-authorization-framework/),
and the [Bulk Data Access API](https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfbda/index.html).
Everything below is Oracle Health's own statement unless marked
**confirm on the tenant**; where those pages document nothing on a
point, the chapter says "not documented by the vendor".

### Access model: one tenant per base URL

Base URLs are tenant-scoped: `https://fhir-ehr.cerner.com/r4/{tenant-id}/...`.
The tenant id is part of the path, so one deployment's base URL never
reaches another tenant's data, and `PHI_AI_FHIR_BASE_URL` is per
customer. The customer's tenant enables the app.

### Registration and review

System apps are registered through the Oracle Health developer console
(Cerner Central / code console); the customer's tenant enables them.
Registration issues a **system account** (client ID + secret) and, for
JWT auth, takes a preregistered **JWKS** via System Account Management.
Review timelines and what can be changed after registration: not
documented on the pages cited here - confirm with Oracle Health.

### Auth: two documented modes, explicit scopes either way

Two documented client-authentication modes for system apps: (1) OAuth2
client credentials with the secret in an **RFC 2617 Basic Authorization
header** - the primary documented mode; and (2) a **signed JWT client
assertion** against the preregistered JWKS, which the authorization
framework calls "the appropriate mode of authentication for Bulk Data
Access". The profile ships on the JWT mode (`auth_flow=
"smart_backend_services"`) because population ingestion wants `$export`;
`PHI_AI_FHIR_CLIENT_SECRET` is ignored on that flow. Switching a
deployment to the secret mode is a profile field flip, and
`Settings.from_env()` then requires the secret.

Signing algorithm: the profile keeps the module default, RS384, with an
RSA key. The Oracle pages cited here do not name an algorithm list -
**confirm on the tenant's** `.well-known/smart-configuration` before
go-live. Implements SMART App Launch 1.0.0 / Backend Services 1.0.1.

### Scopes: every one explicit, no wildcards

**Every scope must be requested explicitly in the token request**
(`system/Patient.read system/Observation.read ...`); Oracle's docs are
explicit that wildcard scopes are unsupported. This is the sharpest
contrast with Epic, whose backend token request takes no scope parameter
at all. `requires_token_scopes=True` makes `authenticate_from_settings()`
derive one `system/{Type}.read` per `CERNER.supported_resources` entry,
and the Cerner emulator refuses a scope-less request and a wildcard
exactly as documented. What is *granted* is tenant configuration.

### Population-scale reads: Bulk Data Access

Documented (Bulk Data Access API above) and requires the JWT auth mode.
Group-level availability is tenant configuration. Polling cadence,
`Retry-After` values, group sizes and kickoff throttles: not documented
on the pages cited here - the client's `PHI_AI_BULK_POLL_INTERVAL_SECONDS`
default (600) stands until the tenant says otherwise. Per-type paged
search (`core/fhir/scheduler.py`) is the alternative: the R4 docs publish
a broad read surface (Patient, Encounter, Condition, Procedure and others
document read+search); `_count` is supported but not honoured on
`_id`/`identifier` searches.

### Writes

Oracle publishes create endpoints for several types (Patient, Condition
and DocumentReference POST operations are in the R4 docs). The profile's
`writable_resources` names DocumentReference, Condition and Observation
as the conversation starters - **confirm Observation, and every type, on
the tenant's CapabilityStatement** before a delivery is designed around
it. The `If-None-Exist` conditional-create support recorded in the
profile came from the pre-migration Cerner docs and could not be
re-verified on the public Oracle pages - confirm per tenant before
designing a re-runnable delivery around it.

### Limits, errors and fees

Rate limits, page-size ceilings, token lifetime and fees: not documented
on the pages cited here - confirm with the tenant and Oracle Health's
developer program.

### Validate before ingesting

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

Confirm the tenant's resource surface, the `token` URL and the scopes it
lists; `CERNER.supported_resources` is a default, not a guarantee for
any tenant.

### What the emulator reproduces

`emulators/vendors.py` `cerner` (port 9102, FHIR path
`/r4/EMULATOR-TENANT`): both documented grants (a signed JWT assertion,
or a secret in a Basic header or the form body); the RS384 assertion
(any other `alg` is `invalid_client`); a scope-less request refused with
`invalid_scope`; a wildcard scope refused - the one emulator that does,
because Oracle Health documents it; `$export` with the asynchronous
handshake; `create` advertised for DocumentReference, Condition and
Observation; and `If-None-Exist` honoured (a repeat delivery answers
200, not a duplicate). Not reproduced: the tenant-id path being a real
tenant, tenant-level scope grants, and everything "not documented by the
vendor" above.

### Setting it up

Zero to a working Oracle Health connector in the non-PHI setup. Every
step is from the Oracle Health pages cited in this chapter or from this
repository; where Oracle Health documents nothing, the step says so.
`$REPO` is the checkout root.

1. **Register the app with Oracle Health.**
   1. Register a **system** app through the Oracle Health developer
      console (Cerner Central / code console) and have the customer's
      tenant enable it.
   2. Registration issues the **system account** - a client ID and a
      secret. The secret is not used on the profile's JWT flow; keep it
      only if you switch the deployment to the Basic-header mode.
   3. For the JWT mode, preregister the **JWKS** from step 2 via **System
      Account Management**. Whether the JWKS is taken as a URL or pasted
      JSON: not documented on the pages cited here - the console says.
   4. Note the **tenant id** (it is in the base URL) and the client ID
      (`PHI_AI_FHIR_CLIENT_ID`, the assertion's `iss`/`sub`).
   5. Ask the tenant which Group(s) exist for bulk export and for the
      Group id; group-level availability is tenant configuration and no
      discovery procedure is documented.

2. **Generate the key pair and the JWKS.** RSA for the profile's RS384
   default (confirm the tenant's algorithm list in step 4):

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   "$REPO/scripts/generate_keypair.sh" --alg RS384 .
   "$REPO/.venv/bin/python" - <<'EOF'
   import json
   from jwt.algorithms import RSAAlgorithm
   from cryptography.hazmat.primitives import serialization
   priv = serialization.load_pem_private_key(open("private_key.pem", "rb").read(), password=None)
   kid = "phi-ai-cerner-2026-09"
   json.dump({"keys": [{**RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True),
                        "kid": kid, "alg": "RS384", "use": "sig"}]},
             open("cerner_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

   Register `cerner_jwks.json` in step 1.3; the `kid` is
   `PHI_AI_FHIR_JWT_KID`.

3. **Configure the PHI AI environment:**

   ```bash
   PHI_AI_EMR_VENDOR=cerner
   PHI_AI_FHIR_BASE_URL=https://fhir-ehr.cerner.com/r4/<tenant-id>
   PHI_AI_FHIR_TOKEN_URL=<token endpoint from the tenant's .well-known/smart-configuration>
   PHI_AI_FHIR_CLIENT_ID=<system account client ID>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   PHI_AI_FHIR_GROUP_ID=<Group id from the tenant>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600      # vendor documents no cadence; the default stands
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   # PHI_AI_FHIR_CLIENT_SECRET is ignored on the profile's JWT flow; set it only if the
   # deployment's profile is switched to oauth2_client_credentials (the Basic-header mode).
   ```

   Scopes are not an environment variable: `requires_token_scopes=True`
   makes `authenticate_from_settings()` send one `system/{Type}.read` per
   supported type.

4. **Pre-flight the tenant (no token needed).**

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for res in r["resource"]:
       print(res["type"], [i["code"] for i in res.get("interaction", [])], res.get("conditionalCreate"))
   print("operations", [o["name"] for o in r.get("operation", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin)
   print("token_endpoint", d.get("token_endpoint"))
   print("alg list", d.get("token_endpoint_auth_signing_alg_values_supported"))
   print("system scopes", sorted(s for s in d.get("scopes_supported", []) if s.startswith("system/"))[:8], "...")'
   ```

   Look for: the `token` URL; an alg list containing `RS384` (if the
   tenant publishes one - the pages cited here do not); `create` on the
   types you intend to deliver; `conditionalCreate` on them (the
   profile's claim to re-verify); and `export`/`group-export` among the
   operations.

5. **First ingest.**

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.bulk_scheduler --once   # $export
   .venv/bin/python -m core.fhir.scheduler --once                     # or paged search
   .venv/bin/python -m core.verify --deep
   ```

   An `invalid_scope` at the token step means a requested
   `system/{Type}.read` was not granted to the system account: narrow
   `supported_resources` or have the tenant grant it - Oracle refuses
   the whole request, not the one scope.

6. **First delivery (what a write attempt does).** Dry run first:

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "https://fhir-ehr.cerner.com/r4/<tenant-id>" \
     --vendor cerner --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

   The destination token request is built on the `cerner` profile: an
   RS384 assertion carrying `system/DocumentReference.write
   system/Condition.write system/Observation.write` (derived from
   `writable_resources`; SMART v1 grammar - whether the tenant grants
   `.write` to a system account is tenant configuration, confirm it).
   `writer.py` then admits only what the tenant's CapabilityStatement
   advertises `create` for. `If-None-Exist` is sent where the profile
   records conditional create, so a re-run does not duplicate *if the
   tenant honours it* - re-verify per tenant (Writes above).

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor cerner      # port 9102 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9102/r4/EMULATOR-TENANT     token: http://127.0.0.1:9102/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k cerner -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k "cerner" -v
   ```

   Covers: both grants; `invalid_scope` for a scope-less and for a
   wildcard request; paging; the bulk handshake; a real delivery of the
   three creatable types, confirmed by `_source` search; and the
   conditional-create no-duplicate re-run.

8. **Known limits, and where to confirm them.**
   - Explicit scopes, no wildcards - the authorization framework page.
   - Bulk requires the JWT mode; group availability per tenant - the
     Bulk Data Access page and the tenant.
   - `_count` not honoured on `_id`/`identifier` searches - the R4
     overview.
   - Not documented on the pages cited here: rate limits, page-size
     ceiling, poll cadence, token lifetime, signing-algorithm list,
     conditional create (re-verify), fees - confirm on the tenant and
     with Oracle Health.

## athenahealth

(Predates the 2026-08 vendor expansion; recorded here so the per-vendor
document is complete.) Primary source:
[docs.athenahealth.com](https://docs.athenahealth.com/) (FHIR R4 API).
Where that source documents nothing on a point, the chapter says "not
documented by the vendor" and the profile keeps its conservative
default.

### Access model: one practice per base URL

Practice-scoped base URLs - the practice id is part of the FHIR base
URL - and an app is enabled per practice through the athenahealth
Marketplace. `PHI_AI_FHIR_BASE_URL` is therefore per practice.

### Registration and review

Apps are enabled per practice through the Marketplace; the secret is
issued when the app is enabled for the practice. Review timelines,
sandbox access and what can be changed afterwards: not documented in the
source cited here - confirm on docs.athenahealth.com.

### Auth: client credentials with a secret, not a signed assertion

Client-credentials OAuth with a client **secret** - the one profile
shipped with `auth_flow="oauth2_client_credentials"` (TruBridge and
Netsmart document a secret as an alternative grant, but their profiles
ship on the JWT assertion), and the reason
`FHIRIngestionClient.authenticate_client_secret()` exists. The secret is
sent in the POST body, as athenahealth documents. Set it with
`PHI_AI_FHIR_CLIENT_SECRET`; `Settings.from_env()` refuses to start an
athenahealth deployment without it, and the schedulers pick this flow
automatically from the vendor profile (`authenticate_from_settings()`).
A JWT assertion is refused by athenahealth, and by its emulator. The
private key file `PHI_AI_FHIR_PRIVATE_KEY_PATH` names is still required
by `Settings.from_env()` but is never used on this flow.

### Scopes

Scoped per API registration; the profile records no explicit-scope
requirement on the token request (`requires_token_scopes=False`) because
the source cited here documents none - **confirm per practice**.

### Population-scale reads: bulk export

Supported with vendor-specific constraints; confirm per practice. Poll
cadence, group procedure and throttles: not documented in the source
cited here - the client defaults stand. Rate limits are tighter than
the other targets (the profile's `rate_limit_per_min=30` is deliberate).

### Writes

Document attachment is the realistic write path; the profile names
DocumentReference as the conversation starter. Confirm per practice and
per API registration; no conditional create is documented.

### Limits and fees

Not documented in the source cited here beyond "rate limits are
tighter" - confirm per practice on docs.athenahealth.com.

### Validate before ingesting

```
GET {base_url}/metadata
```

### What the emulator reproduces

`emulators/vendors.py` `athenahealth` (port 9103, FHIR path
`/fhir/r4`): the client-secret grant only - a JWT assertion is refused
as `invalid_client`, as it would be live; no scope demanded; `$export`
served; `create` advertised for DocumentReference; no conditional
create. Not reproduced: practice-scoped URLs being real practices,
Marketplace enablement, the tighter rate limits.

### Setting it up

1. **Register the app with athenahealth.** Enable the app for the
   practice through the athenahealth Marketplace; the practice-scoped
   base URL, the client ID and the client **secret** are issued there
   (docs.athenahealth.com). Nothing more is documented in the source
   cited here about review or sandbox access - confirm on the portal.

2. **No key pair is used on this grant.** `Settings.from_env()` still
   requires `PHI_AI_FHIR_PRIVATE_KEY_PATH` to name a readable key (other
   flows use it), so generate one and never register it anywhere:

   ```bash
   "$REPO/scripts/generate_keypair.sh" --alg RS384 ~/phi-ai-keys
   ```

   Store the secret in the environment only (`.env` is mode 600 and
   gitignored); it never appears on a command line.

3. **Configure the PHI AI environment:**

   ```bash
   PHI_AI_EMR_VENDOR=athenahealth
   PHI_AI_FHIR_BASE_URL=<the practice-scoped base URL from the Marketplace registration>
   PHI_AI_FHIR_TOKEN_URL=<the token endpoint the practice's .well-known/smart-configuration names>
   PHI_AI_FHIR_CLIENT_ID=<client ID>
   PHI_AI_FHIR_CLIENT_SECRET=<client secret>        # REQUIRED for this vendor; from_env() refuses to start without it
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem   # required to exist, never used on this flow
   PHI_AI_FHIR_GROUP_ID=<Group id, if the practice's bulk export is group-level - confirm per practice>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600            # vendor documents no cadence; the default stands
   ```

4. **Pre-flight the practice endpoint (no token needed).**

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for res in r["resource"]: print(res["type"], [i["code"] for i in res.get("interaction", [])])
   print("operations", [o["name"] for o in r.get("operation", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); print(d.get("token_endpoint"), d.get("token_endpoint_auth_methods_supported"))'
   ```

   Look for `client_secret_post` (or `client_secret_basic`) among the
   auth methods, `export` among the operations if you plan a bulk run,
   and `create` on DocumentReference if you plan a delivery.

5. **First ingest.**

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.scheduler --once          # paged search, throttled by the profile
   .venv/bin/python -m core.fhir.bulk_scheduler --once                  # $export where the practice has it
   .venv/bin/python -m core.verify --deep
   ```

   `Authenticating to athenahealth FHIR endpoint` is followed by the
   secret grant; an `invalid_client` here means the secret or client ID
   is wrong for this practice, not that an assertion was needed.

6. **First delivery (what a write attempt does).** Dry run first:

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "<practice base URL>" \
     --vendor athenahealth --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

   The destination token is minted with `PHI_AI_DELIVERY_CLIENT_ID` +
   `PHI_AI_DELIVERY_CLIENT_SECRET` (the profile's grant; the CLI refuses
   to run this vendor without the secret). `writer.py` admits only what
   the practice's CapabilityStatement advertises `create` for -
   DocumentReference is the expectation. No conditional create is
   documented, so a confirmed run needs `--allow-duplicates` and an
   external record of what was sent.

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor athenahealth   # port 9103 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9103/fhir/r4     token: http://127.0.0.1:9103/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k athenahealth -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k athenahealth -v
   ```

   Covers: the secret grant issuing a token and a JWT assertion refused
   as `invalid_client`; the dispatcher choosing the secret flow from the
   profile and refusing to run without a secret; paging; the bulk
   handshake; and delivery of DocumentReference.

8. **Known limits, and where to confirm them.**
   - Tighter rate limits than the other vendors - docs.athenahealth.com;
     the profile throttles to 30 per minute by default.
   - Not documented in the source cited here: bulk-export scope and poll
     cadence, token lifetime, conditional create, fees, review timelines
     - confirm per practice.

## eClinicalWorks

Primary source: the eClinicalWorks FHIR developer portal at
[fhir.eclinicalworks.com](https://fhir.eclinicalworks.com/ecwopendev/documentation)
(Cures Act / g(10) APIs). Everything below is the portal's own statement
unless marked **confirm with the practice**; where the portal documents
nothing on a point, the chapter says so.

### Access model: one practice per base URL

Base URLs are per practice, issued at onboarding through the portal;
`PHI_AI_FHIR_BASE_URL` is per practice.

### Registration and review

Through the eCW developer portal (`fhir.eclinicalworks.com`); onboarding
is per practice. Review timelines and what can be changed after
registration: not documented on the portal page cited here.

### Auth: asymmetric private-key JWT, no client secret

SMART Backend Services with **asymmetric private-key JWT** - the
portal's own words: "Backend Services uses Asymmetric (Private Key JWT)
Authentication", with the public key registered as a JWKS. The same
client-assertion flow the client speaks for every SMART Backend
Services profile; no client secret (`PHI_AI_FHIR_CLIENT_SECRET` is
ignored). Signing algorithm: the portal page cited here names none, so
the profile keeps the RS384 default with an RSA key - **confirm on the
practice's** `.well-known/smart-configuration`.

### Scopes

Registered as a JWKS-backed backend service; the portal page cited here
documents no scope requirement on the token request, so
`requires_token_scopes=False` - **confirm with the practice** whether a
scope-less request is accepted.

### Population-scale reads: bulk FHIR APIs

**Corrected in the 2026-08 review**: this document previously recorded
eCW as having no bulk path. Their portal now documents backend
(single-patient) and **bulk (multiple-patient)** FHIR APIs. Availability
for a specific practice may still require contracting - confirm before
planning a migration around `$export` rather than paged search. Group
procedure, poll cadence and throttles: not documented on the portal
page cited here.

Read APIs cover the USCDI v1-v3 surface (Patient, Encounter,
Observation, Condition, MedicationRequest, DocumentReference,
AllergyIntolerance, Immunization, Procedure, DiagnosticReport,
MedicationAdministration, ServiceRequest, and more). Confirm against the
practice's CapabilityStatement.

### Writes

eCW documents FHIR Create/Update APIs (V12.0.2+: Patient, Encounter,
MedicationRequest, Immunization, DocumentReference variants, Coverage,
ServiceRequest) but as a **contracted add-on** arranged through
`interop@eclinicalworks.com` - not a default capability, which is why
the profile's `writable_resources` is empty and the writer refuses every
type. Until a contract says otherwise, deliver as files for their own
migration tooling. Conditional create: not documented.

### Limits and fees

Vendor-documented ceiling of **250 calls per minute per base URL** (FHIR
resource, authorize and token endpoints, effective Oct 2025); the
profile records it as `rate_limit_per_min=250` - throttle well below it.
Fees and token lifetime: not documented on the portal page cited here.

### Validate before ingesting

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

### What the emulator reproduces

`emulators/vendors.py` `eclinicalworks` (port 9104, FHIR path
`/fhir/r4`): the JWT-assertion grant only (a secret is
`invalid_client`); the RS384 assertion; no scope demanded; `$export`
served; `create` advertised for **nothing**, so the delivery writer's
structured refusal is exercised for every type; no conditional create.
Not reproduced: the 250-per-minute ceiling, per-practice contracting of
bulk and of the Create APIs.

### Setting it up

1. **Register with eClinicalWorks.** Onboard through the developer
   portal (`fhir.eclinicalworks.com`), per practice. Register the public
   **JWKS** from step 2 as the portal's Backend Services registration
   asks; the practice's base URL is issued at onboarding. Whether the
   JWKS is taken as a URL or pasted JSON: not documented on the page
   cited here - the portal form says. Note the client ID
   (`PHI_AI_FHIR_CLIENT_ID`). If a bulk (multiple-patient) export is
   needed, confirm with the practice whether it must be contracted, and
   ask for the Group id if the export is group-level.

2. **Generate the key pair and the JWKS.** RSA for the RS384 default
   (confirm the algorithm list in step 4):

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   "$REPO/scripts/generate_keypair.sh" --alg RS384 .
   "$REPO/.venv/bin/python" - <<'EOF'
   import json
   from jwt.algorithms import RSAAlgorithm
   from cryptography.hazmat.primitives import serialization
   priv = serialization.load_pem_private_key(open("private_key.pem", "rb").read(), password=None)
   kid = "phi-ai-ecw-2026-09"
   json.dump({"keys": [{**RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True),
                        "kid": kid, "alg": "RS384", "use": "sig"}]},
             open("ecw_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

3. **Configure the PHI AI environment:**

   ```bash
   PHI_AI_EMR_VENDOR=eclinicalworks
   PHI_AI_FHIR_BASE_URL=<the practice base URL issued at onboarding>
   PHI_AI_FHIR_TOKEN_URL=<token endpoint from the practice's .well-known/smart-configuration>
   PHI_AI_FHIR_CLIENT_ID=<client ID>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   PHI_AI_FHIR_GROUP_ID=<Group id, if the practice's bulk export is group-level>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600      # vendor documents no cadence; the default stands
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET - this vendor takes no secret and the setting is ignored.
   ```

4. **Pre-flight the practice endpoint (no token needed).**

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for res in r["resource"]: print(res["type"], [i["code"] for i in res.get("interaction", [])])
   print("operations", [o["name"] for o in r.get("operation", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin)
   print(d.get("token_endpoint"), d.get("token_endpoint_auth_signing_alg_values_supported"), d.get("scopes_supported"))'
   ```

   Look for: `RS384` in the alg list (if published); `export` among the
   operations if bulk is contracted; and `create` on nothing unless the
   Create APIs were contracted - if `create` appears, that is the
   contract showing, and the profile's `writable_resources` may then be
   widened deliberately.

5. **First ingest.** Paged search is the safe default until bulk is
   confirmed for the practice; stay well under 250 calls per minute:

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.scheduler --once
   .venv/bin/python -m core.fhir.bulk_scheduler --once      # once the practice confirms $export
   .venv/bin/python -m core.verify --deep
   ```

6. **First delivery (what a write attempt does).** With
   `writable_resources` empty, `writer.py` skips every type against a
   CapabilityStatement that advertises no `create`, and the destination
   token request (built on the `eclinicalworks` profile: RS384, no
   scope) is the only thing that reaches the practice. Deliver as files
   until a Create-API contract through `interop@eclinicalworks.com`
   exists.

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "<practice base URL>" \
     --vendor eclinicalworks --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor eclinicalworks   # port 9104 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9104/fhir/r4     token: http://127.0.0.1:9104/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k eclinicalworks -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k eclinicalworks -v
   ```

8. **Known limits, and where to confirm them.**
   - 250 calls per minute per base URL (FHIR, authorize and token
     endpoints) - the portal, effective Oct 2025.
   - Create/Update APIs are a contracted add-on -
     `interop@eclinicalworks.com`.
   - Not documented on the page cited here: signing-algorithm list,
     scope requirement, bulk procedure and cadence, token lifetime,
     conditional create, fees - confirm on the practice's discovery
     document and with the practice.

## MEDITECH Expanse

Primary sources: the
[Greenfield Workspace resources page](https://ehr.meditech.com/ehr-solutions/greenfield-workspace-resources)
and the Greenfield API explorer at
[greenfield.meditech.com](https://greenfield.meditech.com)
(`fhir.meditech.com` redirects there). Most technical detail sits
behind the portal login, so this chapter is explicit about what is
vendor-confirmed versus certification baseline, and says "not documented
by the vendor" where the public pages are silent.

### Access model

Credentials and endpoints are issued by MEDITECH on acceptance into the
Greenfield Workspace; there is no self-service public sandbox. Base URLs
are shaped `.../v2/uscore/R4/` (verified from the public explorer, which
documents `v2/uscore/R4/{operation}/`); `PHI_AI_FHIR_BASE_URL` is per
instance.

### Registration and review

Through the MEDITECH Greenfield Workspace; MEDITECH issues credentials
and endpoints on acceptance. Review timelines, JWKS registration
mechanics and what can be changed afterwards: not documented on the
public pages.

### Auth: SMART Backend Services as the certification baseline

*Certification baseline, not vendor-page-confirmed*: ONC g(10)
population services require SMART Backend Services (asymmetric JWT
client assertion), and MEDITECH's certified stack must implement it -
but their public pages do not spell out the token request. The profile
records `smart_backend_services` on that basis and says so in its
notes; the signing algorithm keeps the RS384 default because MEDITECH
documents none publicly. **Confirm grant details, algorithm and scopes
with MEDITECH before go-live.** No client secret is documented;
`PHI_AI_FHIR_CLIENT_SECRET` is ignored.

### Scopes

Not documented publicly; `requires_token_scopes=False` is the
conservative default, to be confirmed with MEDITECH (the instance's
`.well-known/smart-configuration`, once you have access, says).

### Population-scale reads: Bulk Data

Verified as documented: "Bulk Data" is a topic in the Greenfield
explorer, and MEDITECH appears in the SMART team's
[registry of bulk-data implementations](https://github.com/smart-on-fhir/bulk-data-implementations).
Export level, Group procedure, poll cadence and throttles: not
documented publicly - the client defaults stand until MEDITECH says.
Resources: **US Core FHIR R4** for USCDI data - a deliberately narrow
published surface, and the profile's `supported_resources` is
deliberately narrow to match (Patient, Encounter, Observation,
Condition, MedicationRequest, DocumentReference, AllergyIntolerance,
Immunization, Procedure, DiagnosticReport). Absence of the extended
types is recorded uncertainty, not confirmed inability.

### Writes

Greenfield describes the patient-access APIs as view-only; no general
FHIR create is publicly documented. `writable_resources` is empty and
the writer refuses every type. Deliver as files unless the customer's
MEDITECH contacts confirm a write path.

### Limits and fees

Not documented publicly - confirm with MEDITECH.

### Validate before ingesting

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

The CapabilityStatement is the only public statement of what a given
instance exposes; read it before the first run.

### What the emulator reproduces

`emulators/vendors.py` `meditech` (port 9106, FHIR path
`/v2/uscore/R4`): the JWT-assertion grant only; the RS384 assertion; no
scope demanded; `$export` served; only the US Core read surface, and
`create` advertised for **nothing at all**; no conditional create. Not
reproduced: anything MEDITECH keeps behind the portal login.

### Setting it up

1. **Register with MEDITECH.** Apply to the Greenfield Workspace
   (`ehr.meditech.com/ehr-solutions/greenfield-workspace-resources`);
   MEDITECH issues credentials and endpoints on acceptance. How the
   public key is registered (JWKS URL or upload) and what scopes are
   granted are not documented publicly - both come from MEDITECH with
   the credentials. Note the client ID (`PHI_AI_FHIR_CLIENT_ID`) and ask
   for the Group id if the instance's bulk export is group-level.

2. **Generate the key pair and the JWKS.** RSA for the RS384 default
   (confirm the algorithm with MEDITECH; regenerate with `--alg ES384`
   if they say so):

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   "$REPO/scripts/generate_keypair.sh" --alg RS384 .
   "$REPO/.venv/bin/python" - <<'EOF'
   import json
   from jwt.algorithms import RSAAlgorithm
   from cryptography.hazmat.primitives import serialization
   priv = serialization.load_pem_private_key(open("private_key.pem", "rb").read(), password=None)
   kid = "phi-ai-meditech-2026-09"
   json.dump({"keys": [{**RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True),
                        "kid": kid, "alg": "RS384", "use": "sig"}]},
             open("meditech_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

3. **Configure the PHI AI environment:**

   ```bash
   PHI_AI_EMR_VENDOR=meditech
   PHI_AI_FHIR_BASE_URL=<the instance's .../v2/uscore/R4 base URL from MEDITECH>
   PHI_AI_FHIR_TOKEN_URL=<token endpoint from the instance's .well-known/smart-configuration>
   PHI_AI_FHIR_CLIENT_ID=<client ID>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   PHI_AI_FHIR_GROUP_ID=<Group id from MEDITECH, if group-level>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600      # vendor documents no cadence; the default stands
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET - no secret is documented and the setting is ignored.
   ```

4. **Pre-flight the instance (no token needed).** This is the step that
   turns the certification baseline into vendor fact for your instance:

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for res in r["resource"]: print(res["type"], [i["code"] for i in res.get("interaction", [])])
   print("operations", [o["name"] for o in r.get("operation", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin)
   print(d.get("token_endpoint"), d.get("grant_types_supported"), d.get("token_endpoint_auth_methods_supported"),
         d.get("token_endpoint_auth_signing_alg_values_supported"), d.get("scopes_supported"))'
   ```

   Look for: `client_credentials` in the grants and `private_key_jwt` in
   the auth methods (confirming the baseline); the alg list (regenerate
   the key if it excludes RS384); `system/` scopes (if listed, set
   `requires_token_scopes` deliberately after confirming with MEDITECH);
   `export` among the operations; and `create` on nothing.

5. **First ingest.**

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.bulk_scheduler --once    # $export
   .venv/bin/python -m core.fhir.scheduler --once                      # or paged search
   .venv/bin/python -m core.verify --deep
   ```

6. **First delivery (what a write attempt does).** With
   `writable_resources` empty the writer skips every type against a
   CapabilityStatement that advertises no `create`; the destination
   token request is built on the `meditech` profile (RS384, no scope).
   Deliver as files unless MEDITECH confirms a write path.

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "<instance base URL>" \
     --vendor meditech --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor meditech       # port 9106 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9106/v2/uscore/R4     token: http://127.0.0.1:9106/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k meditech -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k meditech -v
   ```

   Covers: paging the US Core surface; the bulk handshake; and the
   writer skipping every type against a statement that advertises no
   `create`.

8. **Known limits, and where to confirm them.**
   - Everything auth-related (grant details, algorithm, scopes) is
     certification baseline until MEDITECH confirms it - the Greenfield
     Workspace.
   - Not documented publicly: rate limits, page size, bulk cadence and
     Group procedure, token lifetime, write path, fees - confirm with
     MEDITECH.

## NextGen Healthcare

Primary sources: [nextgen.com/api](https://www.nextgen.com/api) and the
[NextGen Enterprise regulatory page](https://www.nextgen.com/api/regulatory-nge);
the full developer guides sit behind
[developer.nextgen.com](https://developer.nextgen.com) onboarding.
Everything below is NextGen's own public statement unless marked
**confirm through the portal**; where the public pages document nothing,
the chapter says so.

### Which product, and the access model

This connector targets **NextGen Enterprise** (base URLs shaped
`fhir.nextgen.com/nge/prod/fhir-api-r4/...`). Do not conflate with
**NextGen Office**, a separate small-practice product whose public Bulk
FHIR API authenticates with Basic client_id:secret against
`fhir.meditouchehr.com`. Apps are enabled per practice.

### Registration and review

Through the NextGen developer portal (onboarding-gated); apps are
enabled per practice. Review timelines, JWKS registration and what can
be changed afterwards: behind the portal, not public.

### Auth: the backend flow is not publicly documented

Publicly documented: the Patient Access flow (`authorization_code` at
`fhir.nextgen.com/nge/prod/patient-oauth/token`). **No system/backend
flow is publicly documented** - the profile records
`smart_backend_services` as the g(10) baseline expectation and flags it
as needing confirmation through the portal; the signing algorithm keeps
the RS384 default for the same reason. No client secret is documented
for a backend service; `PHI_AI_FHIR_CLIENT_SECRET` is ignored.

### Scopes

Not publicly documented for a backend service;
`requires_token_scopes=False` is the conservative default - **confirm
through the portal**.

### Population-scale reads: no `$export` recorded

Recorded as **not available** until proven otherwise: nothing public
documents an Enterprise-level `$export`. ONC g(10) obliges the certified
stack to offer population services, so it likely exists behind the
portal - but this codebase records what is verifiable, and
`bulk_scheduler.py` refuses to run against this profile until the flag
is corrected against evidence from the gated docs or a real instance's
CapabilityStatement. Ingestion runs by paged search
(`core/fhir/scheduler.py`). Resources: USCDI/US Core R4 for Patient
Access is the published surface; the profile's broader list must be
confirmed against the practice's CapabilityStatement.

### Writes

Confirm any write capability per practice before relying on it; the
profile lists DocumentReference as the conversation starter only.
Conditional create: not documented.

### Limits and fees

Not publicly documented - confirm through the portal.

### Validate before ingesting

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

If the CapabilityStatement advertises `export`, that is the evidence the
profile's `supports_bulk_export` flag is waiting for - correct it
deliberately, with the statement recorded.

### What the emulator reproduces

`emulators/vendors.py` `nextgen` (port 9105, FHIR path
`/nge/prod/fhir-api-r4`): the JWT-assertion grant only; the RS384
assertion; no scope demanded; `$export` **refused with an
OperationOutcome** ("does not support Bulk Data Export"), never an empty
result; `create` advertised for DocumentReference; no conditional create
(`If-None-Exist` answers 412). Not reproduced: whatever the gated
developer guides document.

### Setting it up

1. **Register with NextGen.** Onboard at `developer.nextgen.com` and have
   the practice enable the app. The backend (system) registration - how
   the public key is registered, which scopes are granted - is behind
   the portal and not public; record what the portal says here when you
   have access. Note the client ID (`PHI_AI_FHIR_CLIENT_ID`).

2. **Generate the key pair and the JWKS.** RSA for the RS384 default
   (confirm the algorithm through the portal; regenerate with
   `--alg ES384` if it says so):

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   "$REPO/scripts/generate_keypair.sh" --alg RS384 .
   "$REPO/.venv/bin/python" - <<'EOF'
   import json
   from jwt.algorithms import RSAAlgorithm
   from cryptography.hazmat.primitives import serialization
   priv = serialization.load_pem_private_key(open("private_key.pem", "rb").read(), password=None)
   kid = "phi-ai-nextgen-2026-09"
   json.dump({"keys": [{**RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True),
                        "kid": kid, "alg": "RS384", "use": "sig"}]},
             open("nextgen_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

3. **Configure the PHI AI environment:**

   ```bash
   PHI_AI_EMR_VENDOR=nextgen
   PHI_AI_FHIR_BASE_URL=https://fhir.nextgen.com/nge/prod/fhir-api-r4/<practice path from the portal>
   PHI_AI_FHIR_TOKEN_URL=<the system-app token endpoint from the portal - NOT the patient-oauth one>
   PHI_AI_FHIR_CLIENT_ID=<client ID>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/private_key.pem
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   # No PHI_AI_FHIR_GROUP_ID: the profile records no $export, and bulk_scheduler refuses this vendor.
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET - no secret is documented for a backend service.
   ```

4. **Pre-flight the practice endpoint (no token needed).**

   ```bash
   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for res in r["resource"]: print(res["type"], [i["code"] for i in res.get("interaction", [])])
   print("operations", [o["name"] for o in r.get("operation", [])])'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin)
   print(d.get("token_endpoint"), d.get("grant_types_supported"), d.get("token_endpoint_auth_methods_supported"),
         d.get("token_endpoint_auth_signing_alg_values_supported"), d.get("scopes_supported"))'
   ```

   Look for: `client_credentials` in the grants (the evidence the
   backend flow exists for this practice); the alg list; `system/`
   scopes; `create` on DocumentReference; and `export` among the
   operations - if present, it is the evidence to correct
   `supports_bulk_export` on, deliberately.

5. **First ingest.** Paged search - the bulk scheduler refuses this
   profile by design:

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.scheduler --once
   .venv/bin/python -m core.fhir.bulk_scheduler --once    # expected: refused - "records no Bulk Data Export support"
   .venv/bin/python -m core.verify --deep
   ```

6. **First delivery (what a write attempt does).** Dry run first:

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "<practice base URL>" \
     --vendor nextgen --identity-map <map.csv> --purpose-of-use treatment --patient <id>
   ```

   The destination token request is built on the `nextgen` profile
   (RS384, no scope). `writer.py` admits only what the practice's
   CapabilityStatement advertises `create` for - DocumentReference is
   the conversation starter, confirmed per practice. No conditional
   create is documented, so a confirmed run needs `--allow-duplicates`
   and an external record of what was sent. There is no bulk write
   surface; the platform's export managers refuse a bulk delivery aimed
   at this vendor.

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor nextgen        # port 9105 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9105/nge/prod/fhir-api-r4     token: http://127.0.0.1:9105/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k nextgen -v
   .venv/bin/python -m pytest tests/test_e2e_matrix.py -k nextgen -v
   ```

   Covers: paging; the `$export` refusal as an OperationOutcome (a pass
   condition, not a skip); DocumentReference delivery; and the 412 on
   `If-None-Exist`.

8. **Known limits, and where to confirm them.**
   - No public backend flow, no public `$export`, no public scope
     documentation - `developer.nextgen.com` after onboarding, and the
     practice's own CapabilityStatement.
   - NextGen Office is a different product with a different API - do
     not mix the two.
   - Not publicly documented: rate limits, page size, token lifetime,
     conditional create, fees.

## ModMed

Primary sources: ModMed's *MMI Certified FHIR API Documentation* (July
2024 PDF) at
[modmed.com/.../MMI-Certified-FHIR-API-Documentation-July-2024.pdf](https://www.modmed.com/wp-content/uploads/2024/07/MMI-Certified-FHIR-API-Documentation-July-2024.pdf)
- "the PDF" below - and the developer portal at
[portal.api.modmed.com](https://portal.api.modmed.com/) ("the portal";
append `.md` to any page for its markdown, index at
[`/llms.txt`](https://portal.api.modmed.com/llms.txt)). Supporting
ModMed sources: the
[Certified API Terms of Use V2](https://www.modmed.com/wp-content/uploads/2023/01/Certified-API-Terms-of-Use-V2.pdf)
(2022-12-23), the
[EMA Mandatory Disclosures](https://www.modmed.com/wp-content/uploads/2025/02/crp-12146-EMA-Mandatory-Disclosures-Feb-25-Update-FINAL.pdf)
(Feb 2025), the
[EMA 7 compliance certificate](https://www.modmed.com/wp-content/uploads/2026/01/crp-13791-Compliance-Certificate-EMA-7-010526.pdf),
and ModMed's own production **demonstration endpoint**
`https://fhirmp.mmi.prod.fhir.ema-api.com/fhir/r4`, whose `/metadata`,
`OperationDefinition/GroupPatient-it-export` and
`/.well-known/smart-configuration` were read on 2026-09-01.

Everything below is one of three things: **documented by ModMed**
(quoted), **observed on ModMed's demonstration endpoint** (dated), or
**must be confirmed on the practice's own endpoint**. Nothing is carried
over from the Epic chapter: ModMed's access model (one endpoint per
practice, switched on by the practice), key algorithm (ES384), scope
rule (explicit `system/{Type}.rs`, required) and bulk scope (system,
Patient and Group) are all its own.

### One endpoint per practice, activated by the practice

**Documented.** "FHIR endpoints are customer-specific. Each practice has
its own Certified FHIR endpoint" (portal, token endpoint page). The
Certified FHIR API fronts four products - "EMA, MMPM, ModMed GI, and
gGastro" (portal home) - but you never connect to "ModMed"; you connect
to one practice's endpoint, whose shape is
`https://{firm}.mmi.prod.fhir.ema-api.com/fhir/r4` (the portal's OpenAPI
server variable: "Your firm subdomain (e.g. fhirmp, auraderm)"). The PDF
(p. 13): "Vendors will first need to know the base url of the practice
they want to integrate with."

ModMed publishes the practices: the PDF points at
`https://mm-fhir-endpoint-display.prod.fhir.ema-api.com/` ("You can find
the endpoints for our customers here"), and the portal's Getting Started
page frames that directory as the Cures/HTI obligation to make customer
endpoints "publicly available".

**Observed 2026-09-01.** The directory page is an Angular app that reads
two public FHIR `Endpoint` bundles, which are the machine-readable form
you actually want:

- `https://public-api.mmi.prod.fhir.ema-api.com/fhir/r4/Endpoint` -
  EMA / Practice Management practices. The bundle mixes Direct addresses
  with FHIR endpoints, so filter:
  `?connection-type=hl7-fhir-rest&_count=500` (paged with `_offset`).
  Addresses seen: mostly `https://{firm}.ef.prod.fhir.ema-api.com/fhir/r4/`,
  some `https://{firm}.mmi.prod.fhir.ema-api.com/fhir/r4/`.
- `https://public-api.gastro.prod.fhir.ema-api.com/fhir/r4/Endpoint` -
  gGastro practices (1,368 entries), addresses
  `https://{uuid}.gastro.prod.fhir.ema-api.com/fhir/r4/`.

Two consequences for this codebase. `PHI_AI_FHIR_BASE_URL` is per
practice, exactly as for the other vendors. And a token is per practice
too: the access token in the PDF's worked example (p. 12) carries an
`allowedFhirUrl` claim naming one practice base URL. Ingesting from three
ModMed practices is three deployments (or three configurations), each
with its own consent (next section).

**Confirm on the instance.** Which host family a given practice is on -
take the address from the `Endpoint` bundle, not from a pattern.

### Registration, review and practice consent

**Documented.** Registration is self-service: "Register with MMI:
https://fhir-vendor-dashboard.kube.prod.mmicse.com/" (PDF p. 3; the
portal's "Register Now" button points at the same dashboard). The
dashboard asks for (PDF pp. 36-39): Application Name, Description,
**Access Type** (Public / Client-Confidential), **PKCE** (None / s256),
**App Type** (Patient / Provider / Patient and Provider / **Bulk**), FHIR
Version (v4.0.1), Launch Url, Redirect Url, Logo Url, Policy Url, Terms
of Service Url, and the scope list (standard: `openid`, `launch`,
`launch/patient`, `online_access`; optional: `fhirUser`,
`offline_access`, and per-resource `patient/*.rs` and `user/*.rs`
scopes).

For unattended, practice-wide ingestion the app type is **Bulk**. The
portal's register page: "If you are working with a practice and you
require access to their providers and patients' clinical data at the
practice level, it is recommended that you create a Bulk FHIR
application. This allows an Admin at the practice to add your ClientId
to their practice one time to be able to gain access to that data as
needed." Every other app type needs a Provider or Patient to log in.

The lifecycle after registration (PDF pp. 5, 8):

1. "Your app will be created in a 'Disabled' state."
2. For **non-Bulk** apps, ModMed enables it: "MMI will review new apps
   daily and Enable apps that are configured correctly."
3. For **Bulk** apps, ModMed does not enable it - the **practice** does:
   "A Practice can provide your app consent by adding your app's
   ClientID to their 'Manage Bulk FHIR' section in their Admin section",
   and "Once a customer has added you, your app will become 'Enabled'."

So there is no ModMed-side gate between you and a practice's data beyond
a correctly configured registration; the gate is the practice
administrator, per practice, and it is a single one-time action on their
side. The Terms of Use add three obligations worth knowing before you
register: ModMed "may request additional information" as part of
registration review (2.3.1); "for Data to be exchanged via the CAPIs the
applicable ModMed Customer or Patient must activate use of such CAPIs in
our Systems" (2.3.3); and "Credentials may not be embedded in open source
projects" (2.4) - relevant to a project with a public repository. The
Terms also state they "are not a Business Associate Agreement"; the BAA
is between you and the practice.

**Not documented.** What cannot change after registration, and where in
the dashboard the JWKS goes (URL or inline) - the PDF's field list does
not show a key field, and the portal only says your client is
"registered with" a public JWKS. Confirm both on the dashboard itself
and record the answer here.

**Sandbox.** There is no separate Certified-API sandbox. ModMed provides
"a production demonstration endpoint
(https://fhirmp.mmi.prod.fhir.ema-api.com/fhir/r4) that behaves like any
customer endpoint, so vendors can test their integration" (portal). Its
`/metadata` answers without a token (observed); reads need a token,
which needs an enabled client. The Terms (2.3.2) say of any test
environment: "you may only use test data".

### Auth: ES384 private-key JWT, explicit scopes, no client secret

**Documented.** The portal's token endpoint page,
[`POST /auth/realms/fhir/protocol/openid-connect/token`](https://portal.api.modmed.com/reference/post_auth-realms-fhir-protocol-openid-connect-token),
serves both interactive apps and Bulk apps; "which fields you send
depends entirely on your `grant_type`". For `client_credentials`:

- "No `client_secret` - authentication is `private_key_jwt` instead."
- "Send `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
  and a `client_assertion` JWT, signed with the private key matching the
  public JWKS your client is registered with."
- "Assertion claims: `iss`/`sub` = your `client_id`, `aud` = this token
  endpoint URL, plus `jti`/`iat`/`exp` (a short expiry - 5 minutes is
  typical)."
- "Signing algorithm confirmed working: `ES384`."
- "`scope` is required on this grant (space-separated `system/*.rs`
  scopes) - it's not implied the way it can be for the other two grants."
- `client_id` is not a form field on this grant ("client_credentials
  identifies the client via the client_assertion JWT's iss/sub claims
  instead"); `client_secret` is "never sent on the client_credentials
  grant".

The PDF's worked example (pp. 9-11) posts to
`https://sso.ema.md/auth/realms/fhir/protocol/openid-connect/token` with
`grant_type=client_credentials`, the assertion type above, an assertion
whose header is `{"typ":"JWT","alg":"ES384","kid":"..."}`, a `scope` of
twenty-one `system/{Type}.rs` values, and one form field no standard
defines - `encryption_method=ES384`. The response (p. 12) is a Bearer
token with `"expires_in": 1800`, the granted `scope` echoed, and
`allowedFhirUrl` set to one practice base URL.

What that means for this codebase, field by field:

| Fact (ModMed) | Profile / setting | Status in the client |
|---|---|---|
| `private_key_jwt`, no secret | `auth_flow="smart_backend_services"`; `PHI_AI_FHIR_CLIENT_SECRET` inert | Implemented (`FHIRIngestionClient.authenticate`) |
| ES384 signature, EC P-384 key | `assertion_algorithm="ES384"`; `PHI_AI_FHIR_PRIVATE_KEY_PATH` points at an EC key | Implemented: `build_client_assertion(algorithm=profile.assertion_algorithm)` signs ES384, `check_private_key_signs()` names a wrong-family key before signing, and `Settings.from_env()` refuses an RSA key for this profile at startup (`tests/test_assertion_algorithm.py` proves ES384 on the wire) |
| `kid` in the assertion header | `PHI_AI_FHIR_JWT_KID` = the `kid` in your JWKS | Implemented (sent when set) |
| `aud` = the token endpoint URL you post to | `PHI_AI_FHIR_TOKEN_URL` | Implemented (`aud` is set to `token_url`) |
| `scope` required, `system/{Type}.rs` | `requires_token_scopes=True` | **Partly**: the client sends scopes, but derives `system/{Type}.read` (see next section) |
| `expires_in` 1800 in the example | - | **Gap**: the client never refreshes mid-run; a bulk poll loop longer than the token lifetime will get `401` at the status URL |
| `encryption_method=ES384` form field | - | Not sent; whether it is required is not documented |

**Observed 2026-09-01, not documented.** `sso.ema.md` answers a token
request with a missing or malformed `client_assertion` with **HTTP 401**
and `{"error":"invalid_client","error_description":"Invalid client or
Invalid client credentials"}`. A practice endpoint answers an
unauthenticated read or `$export` with HTTP 401, `WWW-Authenticate:
Bearer` (surfaced as `x-amzn-remapped-www-authenticate`) and an empty
body. `sso.ema.md`'s OpenID configuration lists `private_key_jwt` and
signing algorithms including ES384, RS384 and PS384; ModMed's
documentation confirms only ES384, and this profile signs ES384.

**Confirm on the instance.** The token URL: the PDF posts to
`sso.ema.md` directly, while a practice's `/metadata` and
`smart-configuration` advertise a proxied URL under the practice base
(`{base}/auth/realms/fhir/protocol/openid-connect/token`). Either way the
assertion's `aud` must equal the URL you post to. Also the real
`expires_in`, which the vendor documents only by example.

### Scopes

**Documented.** ModMed's Certified API has no Epic-style "register the
API, get the scope" model; the scope is requested on every token request
and must be explicit. Three ModMed sources agree on the grammar - the
registration screen (PDF pp. 37-39), the worked example (PDF p. 9) and
the demonstration endpoint's `scopes_supported` (observed) - and all of
them list only the `.rs` form: `system/Patient.rs`,
`system/Observation.rs`, and so on. (The portal calls this "SMART v1
syntax"; `.rs` is in fact the SMART App Launch 2 grammar, which the
PDF's SMART section - "SMARTv2 scope syntax" under `permission-v2` -
also names.)

The granted scopes then gate what an export contains: "If a
requested/available type isn't covered by the token's granted scopes,
that type is silently skipped (not a failed request) and a note is
written to the error file in the completion manifest instead" (portal,
`POST /$export`). Check `error[]` in every manifest, not just `output[]`.

**The open seam.** `requires_token_scopes=True` makes
`FHIRIngestionClient.authenticate_from_settings()` derive one
`system/{Type}.read` per entry in `supported_resources` - the SMART v1
grammar Oracle Health documents. Whether ModMed accepts `.read` is **not
documented by the vendor**, and its demonstration endpoint advertises no
`.read` scope at all. Until the client can emit `.rs` for this vendor
(a scope-syntax knob on the profile, derived from the same tuple so the
two cannot drift), a live ModMed token request carries scopes ModMed
does not list. Treat a `400 invalid_scope` from `sso.ema.md` as this
seam before suspecting the registration.

### Population-scale reads: `$export` at system, Patient and Group level

**Documented.** ModMed's Certified API is built for this: "ModMed
supports a Bulk FHIR API implementation so that authorized vendors can
access data from practices in a bulk manner. This could be data for all
patients in a practice; data for groups of patients in a practice or all
data from a practice" (PDF p. 6). Three kick-off shapes (PDF p. 13):

```
{base_url}/$export
{base_url}/Patient/$export
{base_url}/Group/{id}/$export        e.g. Group/1.105681.22.0.1/$export
```

"The Patient export returns FHIR resources in the USCDI data set." The
portal documents
[`POST /$export`](https://portal.api.modmed.com/reference/post_export)
("System-level export - all resource types the token is scoped for,
across all patients") and
[`POST /Patient/$export`](https://portal.api.modmed.com/reference/post_patient-export)
("Patient-level export - all patients, resource types the token is
scoped for"), each with its exportable type list - the system-level list
is the profile's `supported_resources`.

**Groups are not self-service.** "If you require a group or cohort of
patients, the Patient IDs must be defined and then someone at MMI can
create a group. Please reach out to synapsys@modmed.com for any help
with this" (PDF p. 13). `Group` supports READ only (PDF p. 25), so a
Group id is never discoverable by search. That id is
`PHI_AI_FHIR_GROUP_ID`, and it comes from ModMed, not from the practice.

Whole-practice export needs no Group at all on ModMed (`/$export` or
`/Patient/$export`), but `core/fhir/bulk_scheduler.py` only knows the
Group-level kick-off and refuses to start without `PHI_AI_FHIR_GROUP_ID`.
For a whole-practice run on ModMed, either ask ModMed for an all-patients
Group, or extend `bulk_client.kickoff_export()` with a Patient-level
mode - a small change, listed in the field-by-field table above as a
client gap.

Per-type paged search (`core/fhir/scheduler.py`) is the alternative;
ModMed publishes read and search for every type with per-type search
parameters (PDF pp. 19-34). Whether a search without a `patient`
parameter is permitted for a system token is not documented by the
vendor - confirm on the instance before planning a paged full history.

### Bulk Data Export mechanics, and where this client differs

**Documented** (portal `POST /$export`,
[`GET /fhir-services/$export-status/{id}`](https://portal.api.modmed.com/reference/get_fhir-services-export-status-id),
[`DELETE /fhir-services/$export-status/{id}`](https://portal.api.modmed.com/reference/delete_fhir-services-export-status-id);
PDF pp. 13-14):

- **Method and parameters.** "Parameters are sent as query params on the
  POST, not as a FHIR Parameters resource body." "Only `_outputFormat`,
  `_since`, and `_type` are supported - sending any other Bulk Data IG
  parameter (`_until`, `_elements`, `patient`, `includeAssociatedData`,
  `_typeFilter`, `organizeOutputBy`, `allowPartialManifests`) fails the
  request." `_since`: "Only include resources with meta.lastUpdated
  after this instant." `_outputFormat` "Must contain 'ndjson'"; "Only
  'ndjson' format is supported for the Output" - a `csv` request is a
  400 whose message "would state 'Invalid Tenant'" (PDF p. 14).
- **Kick-off response.** "The export always runs asynchronously - there
  is no synchronous response. The `Content-Location` response header
  gives the polling URL: `{fhir-base}/fhir-services/$export-status/{jobId}`."
- **Polling.** "While the job is running, returns 202 with a
  `Status: In Progress` header (note: not the Bulk Data IG's `X-Progress`
  header) and `Retry-After: 120` (a fixed value, not based on job size).
  When complete, returns 200 with the export manifest as the body
  (`application/json`, not `application/fhir+json`)." Other status codes:
  401 unauthorized; 404 "Not found (includes a cancelled job)"; 500 "The
  export job failed."
- **Manifest.** `transactionTime`, `expirationTime`, `request`,
  `requiresAccessToken` (**`false`** in ModMed's example), `output[]`
  (`type`, `url`, `count`), `deleted[]`, `error[]`. File URLs are Amazon
  S3 URLs (PDF p. 14: `https://modmed-fhir-batch.s3.us-east-2.amazonaws.com/...ndjson`),
  one file per resource type, and "Each file contains a maximum of 1000
  resources."
- **Cancel.** DELETE on the status URL "Cancels an in-progress export and
  deletes its output. After cancellation, the polling GET on the same URL
  returns 404." Response 202.

**Not documented by ModMed:** any export throttle, any group-size
guidance, how long files persist beyond the manifest's `expirationTime`,
and whether GET kick-off is accepted. None of these is assumed.

**Where `core/fhir/bulk_client.py` matches, and where it does not.**
The primitives were written against Epic's documentation and the
differences are concrete:

| Point | This client today | ModMed documents | Action |
|---|---|---|---|
| Kick-off method | `GET Group/{id}/$export` | `POST` (portal); the demo endpoint's OperationDefinition declares export on `Group` and `Patient` at type and instance level with `system: false`, while the portal documents system-level `POST /$export` | Confirm GET on the instance; add POST if refused |
| Kick-off level | Group only | System, Patient, Group | Patient-level mode for whole-practice runs |
| `_since` | never sent | supported | Pass the watermark; incremental export is real on ModMed |
| `Accept` / `Prefer` | `application/fhir+json`, `respond-async` | not mentioned | Keep; harmless if ignored |
| Poll interval | `PHI_AI_BULK_POLL_INTERVAL_SECONDS`, default 600 | `Retry-After: 120`, fixed | Set 120 |
| Progress header | logs `X-Progress` | `Status: In Progress` | Cosmetic; log line is empty |
| Manifest media type | `Accept: application/json` | `application/json` | Matches |
| File download | always sends `Authorization: Bearer` | `requiresAccessToken: false`, S3 URLs | Honour the flag: send no bearer when it is `false` |
| Token lifetime | one token per run, never refreshed | `expires_in` 1800 in the example | Re-authenticate before each poll/download, or keep runs short |
| Cleanup | `DELETE` status URL, accepts 202/204/404 | 202; later GET 404 | Matches |
| `error[]` in manifest | logged as a warning | carries the scope-skipped types | Read it; a missing type is a scope problem, not "no data" |

`bulk_scheduler.py`'s 24-hour default interval is Epic's throttle, not
ModMed's; ModMed documents no throttle, so the interval is a choice, not
a constraint - the Terms only ask that use "will not generate excessive
load" (3.4.7).

### Writes

**In and out, plainly:** data comes **out** of ModMed over the Certified
FHIR API; nothing goes **in** over it.

**Documented.** "As of now, this Certified FHIR API supports only Read,
Search, and Bulk operations" (PDF p. 1). "Currently this API supports
READ and SEARCH only (no WRITE capabilities) + Bulk FHIR" (portal,
Getting Started). **Observed:** the demonstration endpoint's
CapabilityStatement advertises `read` and `search-type` for every
resource and `create` for none. The profile's `writable_resources` is
therefore empty, `supports_conditional_create` is False, and neither is
a gap to be filled later on this API.

**What `core/fhir/delivery/writer.py` will do.** It reads the
destination's CapabilityStatement first and refuses any type that does
not advertise `create`. Against ModMed it finds none, so a delivery run
skips every type and delivers nothing - the correct outcome, and the
emulator reproduces it (every POST is a `422 OperationOutcome
not-supported`, "does not accept create"). Two further reasons a write
attempt fails before it reaches the FHIR server: `core/fhir/delivery/__main__.py`
builds its destination token request on this profile and derives the
scope from `writable_resources` - empty here, so the request carries no
`scope`, which ModMed requires on `client_credentials`; and the source
system for a ModMed migration is ModMed, which the writer refuses to
write back to by design.

**The real write path is a second client, not this profile.** ModMed's
EMA Proprietary API "allows for READ, SEARCH, CREATE, and UPDATE
Functionality for many resources" (portal, Getting Started). Its facts,
all from the portal:

- FHIR R4-style resources under
  `https://mmapi.ema-api.com/ema-prod/firm/{firm_url_prefix}/ema/fhir/v2/`
  (production) with a public sandbox at `stage.ema-api.com`; an
  `x-api-key` header plus an OAuth2 token - a new `client_credentials`
  flow ("recommended for new integrations") and a legacy `password` grant
  "being sunset".
- Access through the synapSYS Marketplace: vendors get "a backend
  'sandbox'", agree ACLs with the synapSYS team, and face "a technical
  review before they are permitted to gain access to their first
  customer's production system". Offered "for a fee" (Mandatory
  Disclosures).
- "The Proprietary API is for the ModMed EMA and ModMed Practice
  Management systems only. It will not be able to support gGastro
  customers" (portal home).
- Documented creates/updates: Patient, Appointment, Task, Condition,
  AllergyIntolerance, MedicationStatement, Coverage, Composition,
  referring Practitioner and Organization, ChargeItem ("Create Charges
  Into ModMedPM"), and "Upload document from S3 URL to EMA"
  (DocumentReference).
- "By default, each API key is limited to 1250 calls per minute"
  (portal, Rate Limiting).

Delivering a chart into a ModMed practice therefore means the
Proprietary API's DocumentReference upload, negotiated per practice
through the Marketplace - a separate client with a separate credential,
outside `core/fhir/delivery`. Until that exists, deliver as files for
the practice's own import tooling.

### Versions, standards and certification

**Documented.**

- FHIR R4 4.0.1 ("ModMed implements the R4 Version of the HL7 FHIR
  standard"; registration FHIR Version v4.0.1). The PDF maps USCDI v1 to
  "the US Core Implementation Guide - 4.0.0 - STU4 Release"; the
  demonstration endpoint (observed) instantiates the US Core server
  CapabilityStatement and serves later types (Coverage,
  MedicationDispense, Specimen, QuestionnaireResponse) - the practice's
  own `/metadata` is the authority.
- Minimum product version: "For MMI EMA systems, the version of the
  software required is version 7.0 or higher." The gGastro line reads
  "version xxxxx or higher" in ModMed's own PDF - **not documented**.
- "Apps should support securing, sending, or receiving data secured with
  the TLS 1.2 or higher encryption protocol."
- "ModMed utilizes SVAP Version Approved: SMART App Launch 2.0", with
  EHR-launch and standalone launch, a `.well-known/smart-configuration`
  advertising `client-public`, `client-confidential-symmetric`,
  `permission-v2`, `authorize-post`, `code_challenge_methods_supported`
  `S256` "and shall not include support for 'plain'". Observed: the
  demonstration endpoint additionally advertises
  `client-confidential-asymmetric` and lists
  `token_endpoint_auth_methods_supported: ["client_secret_post"]` only,
  while `sso.ema.md`'s OpenID configuration lists `private_key_jwt` -
  the practice discovery document understates the backend flow; trust
  the token endpoint's documentation, not that list.
- No DSTU2 API is mentioned anywhere in ModMed's documentation; there is
  nothing to sunset.
- **Certification.** EMA 7 is certified by Drummond Group to "170.315
  (a)(1-5, 12, 14); (b)(1-3, 10-11); (c)(1); (d)(1-9, 12-13); (e)(1, 3);
  (f)(5); **(g)(2-7, 9-10)**; (h)(1)", certificate
  15.04.04.2002.EMA6.70.18.1.221129, "Date Certified: 11/29/2022"
  (ModMed-hosted certificate). ModMed's 2024 Real World Testing results
  report 77,212,637 (g)(10) API requests served in a 90-day window.
  (g)(10) obliges SMART Backend Services authorization and the
  group-export OperationDefinition; ModMed documents both directly, so
  nothing in this profile rests on the mandate alone.
- **Not documented on ModMed's certification page:** any certificate for
  gGastro / ModMed GI. Check
  [CHPL](https://chpl.healthit.gov/) before relying on (g)(10) behaviour
  from a gGastro practice.

### Limits and fees

**Documented.** "No fee charged for certified APIs. Clients remain
responsible for EHR subscription. ModMed also offers a Proprietary API
for a fee" (Mandatory Disclosures, Feb 2025, under criteria (g)(7),
(g)(9), (g)(10)). "Currently, ModMed does not charge any fees specific to
the CAPI. If in the future we charge fees for the CAPIs, we will update
these CAPI TOU" (Terms V2, 4.2). Practices "are responsible for obtaining
a SSL certificate from a third party" (Disclosures).

**Rate limits: not documented for the Certified API.** The portal's
"Rate Limiting" page (1,250 calls per minute per API key) belongs to the
Proprietary API's `x-api-key` credentials. The Certified API Terms say
only that your use "will not generate excessive load on the CAPI
Services" (3.4.7), and that ModMed may "terminate or suspend your access
... without prior notice" for a security threat or interference with
others' use (5.2). The profile keeps the default 60 requests per minute
client-side and says so.

**Page size: not documented for the Certified API.** The portal's
"Count and Pagination" page describes the Proprietary `/fhir/v2` API's
`page` parameter and 50-per-page Patient maximum. The profile keeps
`page_size=50` as a default, not a fact.

### Validate before ingesting

Two unauthenticated requests against the **practice's own** endpoint
tell you most of what this chapter cannot:

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

Check, in order: `fhirVersion` 4.0.1; every type in
`MODMED.supported_resources` present with `read` and `search-type`;
`create` advertised for nothing (if it ever is, this chapter is out of
date); an `export` operation on `Group` and `Patient` (observed shape:
`OperationDefinition/GroupPatient-it-export`, parameters `_outputFormat`,
`_since`, `_type`); the `oauth-uris` extension's `token` URL (that is
`PHI_AI_FHIR_TOKEN_URL`, and the assertion's `aud`); `ES384` in
`token_endpoint_auth_signing_alg_values_supported`;
`client-confidential-asymmetric` in `capabilities`; and only `.rs`
resource scopes in `scopes_supported`.

Then, with a token, the things ModMed documents by example only: the
real `expires_in`; whether `GET .../$export` is accepted or only `POST`;
whether the Group ModMed created for you exports; and that a file URL
downloads without a bearer header when `requiresAccessToken` is `false`.

### What the emulator reproduces

`emulators/vendors.py` `"modmed"` (port 9107) reproduces the seams a
client must survive, each from ModMed's documentation:

- **A client secret is refused** on `client_credentials` with
  `invalid_client` ("never sent on the client_credentials grant").
- **An RS384-signed assertion is refused** with `invalid_client` - the
  first emulator in this repository to inspect the assertion's `alg`
  (`assertion_algorithms=("ES384",)`). A client that signs everything
  RS384 fails here, not against a practice.
- **A token request without `scope`, or with a wildcard, is refused**
  with `invalid_scope` ("scope is required on this grant").
- **`$export` is served, genuinely async**: 202 + `Content-Location`,
  a 202 on the first poll, then a manifest and NDJSON files.
- **Every create is a `422 OperationOutcome not-supported`** and
  `If-None-Exist` is a `412`, so the delivery writer's refusal path is
  exercised against a server that really advertises no `create`.
- **Pagination at 2 per page** regardless of `_count`.

It does **not** reproduce, because the shared server has no knob for
them, and the practice endpoint will: the live **HTTP 401** status on
`invalid_client` (the emulator uses 400); the **`.rs`-only scope
grammar** (a `.read` scope passes the emulator); `Status: In Progress` +
`Retry-After: 120` (the emulator sends `X-Progress` and `Retry-After: 1`);
`requiresAccessToken: false` with S3 file URLs; POST-only kick-off; and a
30-minute token. Each is listed in the profile's `notes` and in the
emulator entry's `notes` so a green run is read for what it is.

### Setting it up

Zero to a working ModMed connector in the non-PHI setup. Every URL,
field name and command below is from ModMed's own documentation or from
this repository; where ModMed does not document a step, the step says so.
`$REPO` is the checkout root; run Python as `$REPO/.venv/bin/python`.

1. **Register the app with ModMed.**
   1. Open `https://fhir-vendor-dashboard.kube.prod.mmicse.com/` and
      register as a vendor (PDF p. 3; the portal's "Register Now"
      button). Read the Certified API Terms of Use first - by
      registering you accept them, and 2.4 forbids embedding credentials
      in open source projects.
   2. Create an application with: **App Type = Bulk** (unattended,
      practice-level access enabled once by a practice admin); **Access
      Type = Client-Confidential**; **PKCE** = None (no browser flow);
      **FHIR Version = v4.0.1**; Application Name, Description, Logo Url,
      Policy Url and Terms of Service Url as your organisation publishes
      them; Launch Url / Redirect Url are fields the form shows (PDF pp.
      36-39) for interactive app types - **ModMed documents no value for
      them on a Bulk app**. Ask `synapsys@modmed.com` what a Bulk app
      should enter and record the answer here; do not guess.
   3. Enter the public JWKS from step 2. **Where the dashboard takes it
      (URL or pasted JSON) is not shown in ModMed's PDF** - the portal
      only says your client is "registered with" a public JWKS. Whichever
      it is, keep the JWKS reachable at an HTTPS URL as well.
   4. Note the **ClientID** the dashboard issues. It is
      `PHI_AI_FHIR_CLIENT_ID` and also the `iss`/`sub` of every
      assertion. ModMed does not document what can be changed after
      registration; treat the ClientID and the JWKS as fixed, and plan
      key rotation as a JWKS update (add the new key, wait, remove the
      old), not a re-registration.
   5. The app is created **'Disabled'**. For a Bulk app there is no
      ModMed review to wait for: it becomes **'Enabled'** when a practice
      admin adds your ClientID under **Admin > Manage Bulk FHIR** (PDF
      pp. 3, 8). Send the practice your ClientID and those words. For the
      demonstration endpoint (`fhirmp`), ask ModMed at
      `synapsys@modmed.com` whether they will enable your ClientID there;
      ModMed does not document a self-service path for that.
   6. If you need a Group rather than the whole practice, send
      `synapsys@modmed.com` the patient IDs: "the Patient IDs must be
      defined and then someone at MMI can create a group" (PDF p. 13).
      The Group id they return (shape `1.105681.22.0.1`) is
      `PHI_AI_FHIR_GROUP_ID`.

2. **Generate the ES384 key pair and the JWKS.** ModMed confirms ES384
   only, so the key is EC P-384 (secp384r1), not RSA. Keep keys outside
   the repository; `scripts/generate_epic_keypair.sh` makes RSA keys and
   is not used here.

   ```bash
   mkdir -p ~/phi-ai-keys && cd ~/phi-ai-keys
   openssl ecparam -name secp384r1 -genkey -noout -out modmed_private_key.pem
   openssl pkcs8 -topk8 -nocrypt -in modmed_private_key.pem -out modmed_private_key_pkcs8.pem
   openssl ec -in modmed_private_key.pem -pubout -out modmed_public_key.pem
   chmod 600 modmed_private_key*.pem
   ```

   Either private-key file works with PyJWT (verified in this repo's
   venv); use the PKCS#8 one. Build the JWKS (`kty` EC, `crv` P-384,
   `alg` ES384, `use` sig, a stable `kid`):

   ```bash
   "$REPO/.venv/bin/python" - <<'EOF'
   import base64, hashlib, json
   from cryptography.hazmat.primitives import serialization
   b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
   priv = serialization.load_pem_private_key(open("modmed_private_key_pkcs8.pem", "rb").read(), password=None)
   n = priv.public_key().public_numbers()
   x, y = n.x.to_bytes(48, "big"), n.y.to_bytes(48, "big")
   kid = b64u(hashlib.sha256(x + y).digest())[:16]
   json.dump({"keys": [{"kty": "EC", "crv": "P-384", "x": b64u(x), "y": b64u(y),
                        "use": "sig", "alg": "ES384", "kid": kid}]},
             open("modmed_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

   Publish `modmed_jwks.json` at a world-readable HTTPS URL (any static
   host; it contains only the public key) and give that URL or its
   contents to the dashboard in step 1.3. Write the printed `kid` down:
   it is `PHI_AI_FHIR_JWT_KID`. ModMed issues no client secret for this
   grant - there is nothing to store.

3. **Configure the PHI AI environment** (names from
   `core/config/settings.py`, prefix `PHI_AI_`):

   ```bash
   PHI_AI_EMR_VENDOR=modmed
   # The practice's base URL from ModMed's Endpoint bundle (step 4), or the
   # demonstration endpoint while rehearsing. No trailing slash.
   PHI_AI_FHIR_BASE_URL=https://fhirmp.mmi.prod.fhir.ema-api.com/fhir/r4
   # The token endpoint you will POST to; the assertion's aud is set to this
   # exact string. ModMed's worked example uses sso.ema.md; a practice's own
   # /metadata advertises {base}/auth/realms/fhir/protocol/openid-connect/token.
   PHI_AI_FHIR_TOKEN_URL=https://sso.ema.md/auth/realms/fhir/protocol/openid-connect/token
   PHI_AI_FHIR_CLIENT_ID=<ClientID from the dashboard>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/modmed_private_key_pkcs8.pem   # mounted, never baked in
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   PHI_AI_FHIR_GROUP_ID=<Group id from synapsys@modmed.com>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=120      # ModMed's fixed Retry-After
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET: ModMed never takes a secret on this
   # grant, and Settings.from_env() ignores it for this vendor.
   ```

   `Settings.from_env()` validates `PHI_AI_EMR_VENDOR` against `PROFILES`
   at startup and requires the private key file to exist. Scopes are not
   an environment variable: `requires_token_scopes=True` makes
   `authenticate_from_settings()` derive them from
   `MODMED.supported_resources` (see the scope-grammar caveat in the
   chapter above - confirm `.rs` support landed in the client before a
   live run).

4. **Pre-flight the practice endpoint (no token needed).** Find the
   practice in ModMed's public bundle, then read its conformance:

   ```bash
   # EMA / PM practices (FHIR endpoints only; gGastro: replace mmi with gastro)
   curl -s -H 'Accept: application/fhir+json' \
     'https://public-api.mmi.prod.fhir.ema-api.com/fhir/r4/Endpoint?connection-type=hl7-fhir-rest&_count=500' \
     | "$REPO/.venv/bin/python" -c 'import json,sys; [print(e["resource"]["name"], e["resource"]["address"]) for e in json.load(sys.stdin)["entry"]]' | grep -i '<practice name>'

   B=$PHI_AI_FHIR_BASE_URL
   curl -s -H 'Accept: application/fhir+json' "$B/metadata" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d["fhirVersion"])
   for x in r["security"]["extension"][0]["extension"]: print(x["url"], x.get("valueUri"))
   for res in r["resource"]:
       ops=[o["name"] for o in res.get("operation",[])]
       print(res["type"], [i["code"] for i in res["interaction"]], ops or "")'
   curl -s "$B/.well-known/smart-configuration" | "$REPO/.venv/bin/python" -c '
   import json,sys; d=json.load(sys.stdin)
   print("token_endpoint", d["token_endpoint"])
   print("ES384 ok", "ES384" in d["token_endpoint_auth_signing_alg_values_supported"])
   print("asymmetric", "client-confidential-asymmetric" in d["capabilities"])
   print("rs scopes", sorted(s for s in d["scopes_supported"] if s.startswith("system/"))[:5], "...")'
   ```

   Look for: `fhirVersion 4.0.1`; every `MODMED.supported_resources`
   type with `read` and `search-type`; `create` on nothing; `export` on
   `Group` and `Patient`; the `token` URL (put it in
   `PHI_AI_FHIR_TOKEN_URL` if you prefer the practice-advertised URL over
   `sso.ema.md`); `ES384 ok True`; `asymmetric True`; only `.rs` system
   scopes. Any `create` advertised means the chapter is stale - stop and
   update it.

5. **First ingest.** Bulk is the documented population path:

   ```bash
   cd "$REPO" && .venv/bin/python -m core.fhir.bulk_scheduler --once
   ```

   Success reads, in the log: `Authenticating to ModMed FHIR endpoint`;
   `Kicking off bulk export: group=<id> ...`; `Bulk export kicked off,
   status URL: <base>/fhir-services/$export-status/<jobId>`; one or more
   `Bulk export still in progress` (ModMed answers 202 with `Retry-After:
   120`); then the manifest, NDJSON files streamed, resources stored and
   indexed, and the status URL deleted. If the manifest's `error[]` names
   a type, the token lacked that type's scope - a registration/scope
   problem, not empty data.

   What you will see if it is refused: a `401` at kick-off means the
   practice has not added your ClientID under Manage Bulk FHIR, or the
   token has expired (30 minutes in ModMed's example); a `400` with
   `Invalid Tenant` means a non-ndjson `_outputFormat`; a 405 or similar
   on kick-off means GET is not accepted and the POST change is needed.
   (The profile's `supports_bulk_export` is True, so the scheduler's own
   refusal - "ModMed's profile records no Bulk Data Export support. Use
   core/fhir/scheduler.py (paged search) for this vendor" - will not
   appear unless someone flips the profile.)

   The paged alternative, and the verifier:

   ```bash
   .venv/bin/python -m core.fhir.scheduler --once
   .venv/bin/python -m core.verify --deep
   .venv/bin/python -m core.verify --export-dir <dir the bulk run wrote>
   ```

6. **First delivery (what a write attempt does).** There is nothing to
   deliver into over this API. Running

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" \
     --vendor modmed --identity-map <map.json> --purpose-of-use treatment --patient <id>
   ```

   ends with every type skipped: `writer.py` reads the destination
   CapabilityStatement, finds `create` advertised for nothing, and
   refuses each resource - by design. Before that, the delivery token
   request itself fails against ModMed: `core/fhir/delivery/__main__.py`
   derives the token's scope from `writable_resources`, which is empty,
   so it sends no `scope` and ModMed requires one. The real write path is the
   EMA Proprietary API (a second client via the synapSYS Marketplace,
   for a fee, EMA/PM only); until it exists, deliver as files.

7. **Local rehearsal against the emulator.**

   ```bash
   cd "$REPO" && .venv/bin/python -m emulators --vendor modmed          # port 9107 from DEFAULT_PORTS
   # base:  http://127.0.0.1:9107/fhir/r4     token: http://127.0.0.1:9107/oauth2/token
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k modmed -v
   ```

   The selection should cover: paging every type through the real client
   (the `sorted(VENDORS)` parametrize picks `modmed` up automatically);
   the bulk async handshake (add `"modmed"` to that parametrize list);
   `invalid_client` for a client secret and for an RS384-signed
   assertion; `invalid_scope` for a scope-less and for a wildcard
   request; a 200 for an ES384-signed assertion with explicit scopes;
   and delivery skipping every type against a CapabilityStatement that
   advertises no `create`. Record the green run in
   `private-notes/e2e-proof.md` (never in the repository).

8. **Known limits, and where to confirm them.**
   - Certification and the (g)(10) documentation link:
     `https://www.modmed.com/onc-certification/` (EMA 7 certificate,
     Mandatory Disclosures, Real World Testing) and CHPL
     `https://chpl.healthit.gov/` (certificate 15.04.04.2002.EMA6.70.18.1.221129;
     gGastro / ModMed GI not listed on ModMed's page - check CHPL).
   - Rate limit, page size, token lifetime, GET kick-off, file retention:
     **not documented by ModMed for the Certified API** - confirm on the
     practice endpoint (step 4) and with `synapsys@modmed.com`.
   - Scope grammar (`.rs` vs the client's `.read`), POST-only kick-off,
     `requiresAccessToken: false` downloads, mid-run token refresh,
     `_since`, Patient-level kick-off: client changes tracked in the
     chapter's comparison table; confirm each on the instance after it
     lands.
   - Terms: `https://www.modmed.com/public-api-terms-of-use/` (Certified
     API Terms of Use V2, 2022-12-23); support `synapsys@modmed.com`.

## Altera Digital Health

Primary sources: the Altera Developer Program (ADP) portal at
[developer.adp.ahcentral.com](https://developer.adp.ahcentral.com/) -
its [Introduction](https://developer.adp.ahcentral.com/Fhir/Introduction),
[Process Overview](https://developer.adp.ahcentral.com/Fhir/ProcessOverview),
[Application Testing](https://developer.adp.ahcentral.com/Fhir/FHIR_Sandboxes),
[SMART on FHIR](https://developer.adp.ahcentral.com/Fhir/SMARTonFHIR),
[Bulk Data](https://developer.adp.ahcentral.com/Fhir/BulkData),
[Resources](https://developer.adp.ahcentral.com/Fhir/Resources),
[Searching](https://developer.adp.ahcentral.com/Fhir/Searching),
[Endpoint Directory](https://developer.adp.ahcentral.com/Fhir/EndpointDirectory)
and [Learn More](https://developer.adp.ahcentral.com/Home/LearnMore)
pages - plus Altera's
[ONC regulatory compliance page](https://www.alterahealth.com/legal/onc-reg-compliance/)
(Drummond certificates and the May 2026 API fee sheet) and the ADP
sandboxes' own `/metadata` and `/.well-known/smart-configuration`. All
fetched 2026-09-01. The production portal - the one Altera's own
sign-up instruction names and the one its ONC page links as the FHIR
terms and specification - is `developer.adp.ahcentral.com`. The
`developer.adpstg.ahcentral.com` host the research memo reached is a
**staging** host whose ProcessOverview wording differs from the
production page's; the two sentences below that come only from it are
marked *adpstg*. Everything below is Altera's own statement unless
marked **confirm on the instance**.

### One portal, four products, per-client endpoints

Altera fronts Sunrise (acute), TouchWorks (ambulatory), Paragon and
dbMotion with one developer program and one "Altera FHIR" R4 server
family. The Introduction page: the API *"supports FHIR release 4
('R4')"*; *"new deployment of DSTU2 APIs are prohibited as of
6/1/2025"*; *"Any applications onboarding after 1/1/2026 should
implement FHIR R4"*. Paragon additionally has its own "Paragon Open
API"; the portal's FHIR documentation is what governs this connector.

The access model is **per client organisation**. The Endpoint Directory
page: *"At the time that the Altera FHIR API is installed in a client
environment, the client's endpoints are registered in the Altera
'downtown' environment. Only endpoints that are designated for
production environments are listed."* Provider/system endpoints
*"generally end in /fhir"*, patient endpoints in `/open`. The public
directory at
[main.open.ahcentral.com/fhirendpoints](https://main.open.ahcentral.com/fhirendpoints)
(the address Altera's fee sheet gives) shows both Altera-hosted routes
(`fhir.fhirpoint.ahcentral.com/fhirroute/...`) and client-hosted ones.
`PHI_AI_FHIR_BASE_URL` is therefore per deployment, and the token URL is
whatever that instance's `/metadata` security extension says.

Two version facts matter for the base URL:

- **Versionless endpoints.** Some endpoints serve DSTU2 and R4 on one
  URL; the default is *"specified at the time FHIR is installed"*.
  Altera's remedy is `Accept: application/fhir+json; fhirVersion=4.0`.
  `core/fhir/client.py` sends a plain `application/fhir+json`, so use
  the R4-specific base URL and read `fhirVersion` from `/metadata`.
- **US Core 3.1.1 versus 6.1.0.** The Resources page's compatibility
  matrix: US Core 6.1.0 needs Altera FHIR 25.4+ (Sunrise 25.1 PR2+,
  TouchWorks 25.4.1+, Paragon Denali 25.1+, dbMotion 26.1+); older
  installs serve US Core 3.1.1. The Sunrise sandbox publishes separate
  bases for each (`.../R4/fhir-Prod` and `.../R4/fhir-Prod/USCore6.1`).

### Registration, review and client licensing

1. Sign up at `https://developer.adp.ahcentral.com/` (*"You are
   strongly encouraged to register as a corporate account"*), accept the
   User Agreement - the
   [Developer Portal Terms of Use and FHIR API License](https://developer.adpstg.ahcentral.com/files/misc/Altera_FHIR_API_License.pdf)
   - and confirm the email.
2. My Dashboard, My FHIR Applications, `+`. Fill in App Name, **App
   Type = System** (*"The app's intended end-user is an external
   system, not a physician or provider"*; do not pick "Payer System",
   which is *"used only for the internal use for now"*), App
   Description, Additional info link, **JWKS URL** (*"the URL for
   backend authentication access (JWKS) tokens"*), Redirect URLs (up to
   five; `urn:ietf:wg:oauth:2.0:oob` is Altera's suggestion for
   non-web clients), Launch URLs (up to three), Client Type
   (Confidential), and Native/Web.
3. Save. The portal displays the *"OAuth/FHIR Credentials: Client ID,
   Secret, Secret Expiration Date"*.
4. Select a **Purpose of Use** and the **scopes** the app needs
   (*"Never request scopes that are not required for your application
   to function"*). Leave the app on **V1 scopes** - see Scopes below.
5. A new app *"is allowed for testing only"*. After sandbox testing and
   a self-attestation, click **Request Production Access**; the
   production host's ProcessOverview then says *"Once production access
   is approved by Altera and licensed by the Altera client"*. Only the
   staging host (`developer.adpstg.ahcentral.com/Fhir/ProcessOverview`,
   *adpstg*) adds that the app is *"reviewed and, if appropriate,
   approved by Altera Connect"* and *"Do not request production access
   for the application until the application name, type, and Purpose of
   Use are finalized and the application is fully tested. These values
   cannot be changed once production access is granted."* - staging-
   portal wording, confirm with `ADP@alterahealth.com`.
6. Approval is not access. *"Integrators cannot license their
   applications for clients; the clients must activate applications
   themselves through the License Management Portal (LMP)."* There the
   client *"can decide to grant all the requested authorization scopes
   or deny some scopes"*, and *"Some data may be restricted to access
   for the purpose of use by the client licensing."* Integrators have no
   access to the LMP documentation.

Altera's fee sheet says Open members can *"register FHIR apps, test
using development endpoints, declare apps as production ready, and
enable them with provider organizations at no cost and without
intervention from Altera teams"*; the Process Overview says production
access *"is approved by Altera"*. Both are Altera's words - **confirm
with ADP@alterahealth.com** how long the approval step takes before
promising a go-live date.

**Sandboxes.** Shared TouchWorks and Sunrise R4 sandboxes are published
on the Application Testing page (Sunrise system base
`https://sunrise-fhir-r4.adpsandbox.ahcentral.com/R4/fhir-Prod`, token
`https://sunrise-fhir-r4.adpsandbox.ahcentral.com/authorizationV2-Prod/connect/token`;
TouchWorks base `https://tw-fhir-r4.adpsandbox.ahcentral.com/R4/fhir-R4`;
test patient IDs 835800201, 835900201, 836500201). Shared user
credentials are requested *"via the Support Widget"* with your client
ID. EHR-launch testing needs RDS credentials except on TouchWorks;
*"Open Integrators won't be able to test in the EHR Launch mode."*

### Auth: JWT client assertion against a registered JWKS URL

System apps *"make a direct call to the Token URL"* and the body *"must
include"*:

- `client_assertion` - *"a token generated using a private key. The key
  must be signed by a certificate authority."*
- `client_assertion_type` = `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
- `grant_type` = `client_credentials`
- `scope` - Altera's example is `system/*.read` (SMART v1) or
  `system/*.rs` (SMART v2).

The assertion *"includes an expiration time, generally two to 20
minutes, and can only be used once"*.
`FHIRIngestionClient.build_client_assertion()`'s four-minute `exp` and
per-request `jti` fit inside that. The token endpoint is the only
endpoint a System app talks to.

**Key distribution is asynchronous.** *"A nightly job is run 'downtown'
that cycles through all registered FHIR system applications. It
downloads the JWKS information and updates the OAuth clients"* and the
result is *"downloaded to the client systems"*. A newly registered or
rotated key is not usable until that job has run - plan rotations a day
ahead, and make sure the JWKS URL is reachable from Altera's side.

**What the Secret is for.** The portal issues a Secret to every app, and
the SMART on FHIR page says the system app's *"client credentials
(client ID and client secret) establish the authentication flow"* - but
the documented system token body carries the assertion, not the secret.
No Altera page documents a secret-only system grant.
`PHI_AI_FHIR_CLIENT_SECRET` stays unset; `Settings.from_env()` ignores
it for this vendor.

**What the sandboxes' discovery documents show** (Altera's own servers,
fetched 2026-09-01): `grant_types_supported` includes
`client_credentials`; `capabilities` includes both
`client-confidential-asymmetric` and `client-confidential-symmetric`;
`token_endpoint_auth_methods` lists only `client_secret_basic`,
`client_secret_post`, `tls_client_auth` and
`self_signed_tls_client_auth`; the OpenID configuration publishes no
`token_endpoint_auth_signing_alg_values_supported`. So the following are
**not documented by the vendor** and must be confirmed on the sandbox
with a registered client before go-live: the JWT signing algorithm
(`ALTERA.assertion_algorithm` is left at the file default, and the
client signs RS384 - nothing on Altera's side says that is right or
wrong), the key size, whether `kid` is required, and whether a
self-signed key published in a JWKS satisfies *"signed by a certificate
authority"*.

### Scopes: required in the token request, granted by the client

Unlike a registration-derived grant, Altera lists `scope` among the
parameters the token body *"must include"*. `ALTERA.requires_token_scopes`
is therefore `True`, and `authenticate_from_settings()` sends one
explicit `system/{Type}.read` per entry in `ALTERA.supported_resources`.
Each of those appears in the sandboxes' `scopes_supported`. Altera does
**not** document any refusal of wildcard scopes - its own example is a
wildcard.

Three vendor facts to keep straight:

- *"Newly registered applications are assigned V1 scopes by default."*
  Migration to V2 is one-way (*"you won't be able to switch back"*), V2
  needs *"Altera FHIR R4 25.4 or above"* on the client's EHR, and the
  Sunrise US Core 6.1 base advertises only v2 grammar (`.r`/`.rs`) in
  `scopes_supported`. The client emits v1 grammar. Keep the registration
  on V1 and test against a base that lists the grammar you send.
- The scopes ticked at registration *"become the default scope requested
  for the application in the LMP"*, and the client may deny some. What
  the token endpoint returns for a requested-but-denied scope is **not
  documented** - check on the instance.
- Purpose of Use can restrict data: *"Some data may be restricted to
  access for the purpose of use by the client licensing."*

### Population-scale reads: Group-level Bulk Data Export

Altera positions the search API for *"a single patient or small group
of patients"* and says paging a population *"is not technically
feasible"*; bulk requests exist for *"Clinical studies ... Population
health studies ... Transfer of patient records from one clinical system
to another."* Whether an unanchored `GET {base}/Patient` is refused is
**not documented** - the sandbox lists `_id`, `name`, `birthdate`,
`identifier` and similar as Patient search parameters and nothing about
a required anchor; check on the instance before relying on
`core/fhir/scheduler.py` for anything beyond known patients.

`core/fhir/bulk_client.py` and `core/fhir/bulk_scheduler.py` are the
population path. What Altera documents:

- **Who may call it.** *"Only FHIR applications of the type System can
  send bulk data requests. Patient and User application types cannot."*
  and *"Backend authentication for access tokens via JWKS must be
  configured."*
- **Scope: a Group.** `[FHIR path]/Group/INF-101/$export` - *"Get all
  the patients in the Group resource with the ID INF-101."* Both
  sandboxes advertise `$export` on `Group` and nothing at system or
  Patient level, and no page documents either. Groups *"are created by
  organizations in the individual Altera EHRs, and each EHR has a
  different method"* - TouchWorks *"uses the Patient List function"* -
  and they are discoverable: *"To obtain a specific Group resource ID,
  you can query the Group resource."* Set the result as
  `PHI_AI_FHIR_GROUP_ID`. (`bulk_scheduler.py`'s missing-Group-ID
  message still names Epic's mailbox; for Altera, `GET {base}/Group`.)
- **Async handshake.** Kick-off returns *"a 202 Accepted HTTP status
  code and a URL in the Content-Location header"*. While in progress a
  poll returns `X-Progress` (percentage) and *"Retry-After: Suggested
  duration of time until the next status request. This is measured in
  seconds. If a status request is made prior to the retry-after
  date/time, the FHIR API responds with a HTTP 429 Too Many Requests
  error."* `bulk_client.poll_status()` does not read `Retry-After` and
  raises on 429, so set `PHI_AI_BULK_POLL_INTERVAL_SECONDS` above any
  `Retry-After` you observe; typical values are **not documented**.
- **Completion.** `200` with an `Expires` header (*"once they expire,
  they are no longer available"*; files can be downloaded *"as many
  times as necessary"* before that) and a manifest listing per-type
  file URLs, with `requiresAccessToken: true` and a separate `error`
  list of OperationOutcome files - *"An export request can complete
  successfully when some of the data was successfully outputted but
  some was not."* `bulk_scheduler.py` treats a non-empty `error` list as
  a failed run and leaves the server-side export in place for
  inspection. `DELETE [Content Location URL]` cancels or cleans up.
- **Resources.** 28 types on the production-host Bulk Data page:
  AllergyIntolerance, Binary, CarePlan, CareTeam, Condition, Coverage,
  Device, DiagnosticReport, DocumentReference, Encounter, Goal, Group,
  Immunization, Location, Medication, MedicationAdministration,
  MedicationDispense, MedicationRequest, MedicationStatement,
  Observation, Organization, Patient, Practitioner, PractitionerRole,
  Procedure, Provenance, RelatedPerson, Specimen.
- **Provenance rules.** Provenance is included by default when `_type`
  is omitted, and when explicitly listed; *"If provenance is not passed
  as a requested resource, no resources that are included in the
  request should include provenance."* `bulk_scheduler.py` always passes
  `_type = supported_resources`, and Provenance is deliberately not in
  that tuple because Altera *"does not currently support searching on
  the Provenance resource"* (the paged scheduler would fail on it), so
  this connector's exports carry no Provenance until the two schedulers
  can request different type lists.
- **Not documented:** `_since`, any export-frequency limit, any
  group-size guidance, the file-retention period, and the output format
  beyond Altera's sample file URLs ending in `.ndjson` (the completion
  response's `Content-Type` is *"JSON or XML"*). Recorded as absent, not
  as permission - do not schedule on an assumed throttle, observe the
  instance.

### Writes

**The FHIR surface is not writable, by Altera's own statement:** *"The
Altera FHIR API is limited to read-only access and not write-backs."*
`ALTERA.writable_resources` is empty and `supports_conditional_create`
is `False`.

**The real write path is a second client.** *"Altera offers the
bidirectional Unity API, enabling both reads and writes"*; *"To
integrate with Altera Practice Management, developers must utilize
Unity to read or write patient demographic, appointment, or financial
data."* Unity is proprietary, non-FHIR, sold under the Integrator
membership tiers, and *"Certification required for Unity"*. It is not
this profile and `core/fhir/delivery/writer.py` cannot speak it.

**What `writer.py` will actually do.** It reads the destination's live
`/metadata` and refuses any type without a `create` interaction. Against
the Altera emulator (which advertises none) every delivery is refused
with *"the destination does not advertise create for {Type}"*. Against
the ADP sandboxes it is a different story: their CapabilityStatements
(Altera's own servers) advertise `create` and `update` on Condition,
AllergyIntolerance, MedicationRequest, Observation, Immunization,
DocumentReference, MedicationStatement, MedicationAdministration,
ServiceRequest, Questionnaire and QuestionnaireResponse, so the writer
would attempt a POST there. Whether a System app's token is allowed to
exercise those interactions is **not documented** - the only "create"
sentence on Altera's SMART page describes *"The EHR"* itself calling
the API. Before that, `core/fhir/delivery/__main__.py` builds the
destination token request on this profile and derives its `scope` from
`writable_resources` - empty here - so the request carries no `scope`,
which Altera documents as required, and is expected to fail first.
Deliver to Altera sites as files for their own
tooling unless a Unity integration is contracted.

### Limits, errors and fees

- **Rate limits: not documented by the vendor.** The only 429 on any
  Altera page is the bulk-status early-poll case. `rate_limit_per_min`
  and `page_size` in the profile are this file's defaults.
- **Search errors** (Searching page): `401 Unauthorized`, `403
  Forbidden`, `404 Not found`, `413 Request too large`.
- **Redaction.** *"the Altera FHIR API may return data marked as
  'redacted'"* when *"the Altera Clinical Authorization Service
  determines that the person requesting the data is not authorized to
  view it"*. A redacted resource is a real resource with content
  withheld - an ingestion run must not treat it as complete.
- **Formats.** JSON by default, XML via `Accept: application/fhir+XML`.
- **Fees** (Altera's May 2026 sheet *"Conditions & Maintenance of
  Certification API-Related Fees & Licensing for API Users"* and the
  Learn More page): *"Open accounts are free to build and deploy by
  anyone"*; FHIR API license *"Variable rates, starting at $0"*;
  Integrator tiers $49, $99, $499, $1,499 and $2,499 per month (Bronze
  to Platinum Plus) are *"mainly directed towards companies that want
  to utilize Unity API"*; integration certification is *"optional for
  FHIR"*, $3,500 each beyond the tier allowance, re-certification
  $2,800; API workshop passes $299. The client-side fee schedule is a
  separate spreadsheet on the same page.
- **License terms** worth reading before production: the Developer
  Portal Terms of Use forbid using the SDK *"to benchmark or monitor the
  availability, performance or functionality"* of Altera software, cap
  Altera's liability at US$100, and let Altera terminate for a
  *"Critical Security Issue"*.

### Certification (170.315(g)(10))

Altera publishes Drummond certificates on its ONC compliance page:
[Sunrise Acute Care 25.1](https://www.alterahealth.com/download/211/compliance-certificate/17414/compliance-certificate-sunrise-acute-care-25-1-043026.pdf)
(certificate 15.04.04.3123.Sunr.25.10.1.250905, certified 09/05/2025)
and
[TouchWorks EHR 2026](https://www.alterahealth.com/download/211/compliance-certificate/17843/compliance-certificate-touchworks-ehr-2026-031826)
(15.04.04.3123.Touc.26.13.1.260316, 03/16/2026), each listing
`170.315 ... (g)(2-7, 9-10)` among the criteria tested; Paragon, Paragon
Denali, dbMotion and Sunrise Ambulatory Care certificates sit alongside.
The same page names `developer.adp.ahcentral.com/Fhir/ProcessOverview`
as the FHIR terms and specification link and the Endpoint Directory as
the patient-facing endpoint list. Per ONC's
[(g)(10) resource guide](https://onc-healthit.github.io/api-resource-guide/g10-criterion/),
the criterion requires SMART Backend Services authorization
(§ 170.315(g)(10)(v)(B)), Bulk Data Access v1.0.1 with group export
(§ 170.315(g)(10)(ii)(B)) and documentation *"available via a publicly
accessible hyperlink without any preconditions or additional steps"*
(§ 170.315(g)(10)(viii)(B)). Every one of those is also directly
documented by Altera above; the certificate corroborates, it does not
substitute.

### Validate before ingesting

`ALTERA.supported_resources` is a retention-relevant subset of Altera's
published list, not a promise about any client's build. Before pointing
a run at an instance:

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

What the sandboxes showed on 2026-09-01, and what to compare against:

- `fhirVersion` `4.0.1`; `software.name` `Altera FHIR` (Sunrise
  3.19.7.164 of 2026-03-02, TouchWorks 3.19.9.181 of 2026-07-06);
  `instantiates` both the HL7 bulk-data and US Core server
  CapabilityStatements; `implementationGuide` says which US Core.
- `rest[0].security.extension` carries the `authorize`, `token`,
  `introspect`, `revoke` and `manage` URLs - the `token` value is
  `PHI_AI_FHIR_TOKEN_URL`.
- `Group` carries the `export` operation; `rest[0].operation` is empty
  (no system-level export).
- Every type in `supported_resources` advertises `read` and
  `search-type`, and `system/{Type}.read` (v1 base) or
  `system/{Type}.rs` (v2 base) appears in `scopes_supported`. A type
  missing from either is a type to drop from the tuple for that
  deployment - with `requires_token_scopes` it is also a requested scope.
- `MedicationAdministration` is present on both US sandboxes although
  the Resources page badges it UK-only; treat its presence on the
  target instance as the deciding fact.
- `create`/`update` interactions appear on eleven types - see Writes.

Then read one known patient with the ingestion client before scheduling
anything; a `redacted` result or a `403` at a type the profile lists is
a licensing question for the client's LMP administrator, not a bug.

### What the emulator reproduces

- **A JWT client assertion is the only accepted system credential.** A
  `client_secret` gets `400 invalid_client`, as does an assertion whose
  header algorithm is not RS384 - the RS384 half is the module default,
  not an Altera fact, and the error texts are the emulator's own (Altera
  documents none).
- **A scope-less token request is refused with `invalid_scope`.** Altera
  documents `scope` as required in the body. The shared flag also
  refuses wildcards, which is Oracle Health's rule and not Altera's -
  the emulator is stricter than Altera there, and no test asserts that
  refusal for Altera.
- **Group `$export` exists and is genuinely asynchronous**: `202` +
  `Content-Location`, `202` + `X-Progress` + `Retry-After` on the first
  poll, then a manifest of NDJSON files. The `429`-on-early-poll is not
  reproduced.
- **Nothing is creatable and `If-None-Exist` is not honoured**: the
  CapabilityStatement advertises `create` for no type, so
  `writer.py` refuses every delivery; a conditional create gets `412`.
- **Base path `/R4/fhir-Prod`** - the Sunrise sandbox's documented
  provider/system shape - and two resources per page regardless of
  `_count`.
- **Not reproduced:** versionless endpoints and the `fhirVersion=4.0`
  Accept parameter, redacted resources, the eleven `create`-advertising
  types the real sandboxes expose, v2 scope grammar, and the nightly
  JWKS sync delay.

### Setting it up

Non-PHI rehearsal path: the ADP shared sandbox first, the emulator for
CI, a client's real endpoint only after the client has licensed the app
in its LMP. `$PY` below is the repo's interpreter
(`.venv/bin/python`); run every command from the repo root.

1. **Register at Altera.**
   1. Open `https://developer.adp.ahcentral.com/` and click **Sign Up**
      (corporate email; accept the User Agreement - the Developer Portal
      Terms of Use and FHIR API License; confirm the email).
   2. **My Dashboard** -> **My FHIR Applications** -> `+`.
   3. Enter: **App Name** (company + product, it is what the client sees
      in its LMP); **App Type = System**; App Description; Additional
      info link; **JWKS URL** (the HTTPS URL you will publish in step 2 -
      you can register it before the file exists, but the nightly sync
      only succeeds once it does); Redirect URL
      `urn:ietf:wg:oauth:2.0:oob`; Launch URL blank; **Client Type =
      Confidential Client**; Native/Web as applicable.
   4. **Save.** Record the **Client ID**. The **Secret** and **Secret
      Expiration Date** are also shown; the system flow does not use
      them - store the secret in your secret manager anyway and note the
      expiry, because it is the app's credential for any user-facing
      test.
   5. Back on the app: set **Purpose of Use**; tick scopes
      `system/Patient.read`, `system/Encounter.read`,
      `system/Observation.read`, `system/Condition.read`,
      `system/MedicationRequest.read`, `system/DocumentReference.read`,
      `system/AllergyIntolerance.read`, `system/Immunization.read`,
      `system/Procedure.read`, `system/DiagnosticReport.read`,
      `system/ServiceRequest.read`, `system/MedicationAdministration.read`
      and `system/Group.read` - Altera documents the Group query ("To
      obtain a specific Group resource ID, you can query the Group
      resource") but not which scope it needs; select `system/Group.read`
      to be safe, an inference, not a vendor statement. Do **not** click
      "migrate v1 scopes to v2" - the client emits v1 grammar and
      migration is one-way.
   6. Stay in testing. **Request Production Access** only after step 7
      passes. The production portal says access is *"approved by Altera
      and licensed by the Altera client"*; the staging portal's wording
      that the name, type and Purpose of Use *"cannot be changed once
      production access is granted"* and that Altera Connect reviews the
      app is **staging-portal wording (adpstg) - confirm with
      `ADP@alterahealth.com`** before treating those fields as fixed.
      Then the client organisation activates the app and grants or
      denies scopes in its **License Management Portal** - you cannot do
      that for them.

2. **Generate the key pair and publish the JWKS.** Altera documents a
   private-key JWT verified against your JWKS URL and no algorithm or
   key size; the client signs RS384, so the key is RSA.
   ```bash
   mkdir -p secrets && chmod 700 secrets
   openssl genrsa -out secrets/altera_private_key.pem 2048
   openssl rsa -in secrets/altera_private_key.pem -pubout -out secrets/altera_public_key.pem
   chmod 600 secrets/altera_private_key.pem
   $PY - <<'EOF'
   import json, jwt
   from cryptography.hazmat.primitives import serialization
   pub = serialization.load_pem_public_key(open("secrets/altera_public_key.pem", "rb").read())
   jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
   jwk.update({"kid": "phi-ai-altera-2026-09", "alg": "RS384", "use": "sig"})
   json.dump({"keys": [jwk]}, open("altera_jwks.json", "w"), indent=2)
   print(open("altera_jwks.json").read())
   EOF
   ```
   Host `altera_jwks.json` at a world-readable HTTPS URL (the same
   pattern as `deploy/aws/README_EPIC_JWKS.md`) and confirm it is the
   URL on the FHIR App page. Altera fetches it with *"a nightly job"*
   and pushes the result to client systems - expect the key to be
   usable the following day, and plan rotations the same way. Altera's
   sentence *"The key must be signed by a certificate authority"* is not
   elaborated; if the sandbox refuses a self-signed key, ask
   `ADP@alterahealth.com` what they require. The private key never
   leaves the deploying organisation.

3. **PHI AI environment.** Names are exactly those `core/config/settings.py`
   reads.
   ```bash
   export PHI_AI_EMR_VENDOR=altera
   # Sandbox (Sunrise, US Core 3.1.1 base - the one whose scopes_supported lists v1 grammar):
   export PHI_AI_FHIR_BASE_URL=https://sunrise-fhir-r4.adpsandbox.ahcentral.com/R4/fhir-Prod
   export PHI_AI_FHIR_TOKEN_URL=https://sunrise-fhir-r4.adpsandbox.ahcentral.com/authorizationV2-Prod/connect/token
   # Production: the client's provider/system endpoint (ends in /fhir) from the Endpoint
   # Directory or the client, and the 'token' URL from that instance's /metadata.
   export PHI_AI_FHIR_CLIENT_ID=<Client ID from the FHIR App page>
   export PHI_AI_FHIR_PRIVATE_KEY_PATH=$PWD/secrets/altera_private_key.pem
   export PHI_AI_FHIR_JWT_KID=phi-ai-altera-2026-09
   export PHI_AI_FHIR_GROUP_ID=<filled in at step 4>
   export PHI_AI_BULK_POLL_INTERVAL_SECONDS=600   # raise above any Retry-After you observe; 429 otherwise
   export PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   unset PHI_AI_FHIR_CLIENT_SECRET                 # Altera's system grant is the assertion, not the secret
   ```
   Scopes are not an environment variable: `requires_token_scopes=True`
   makes the client derive `system/{Type}.read` from
   `ALTERA.supported_resources`. `Settings.from_env()` also requires the
   storage and audit set - `PHI_AI_CLOUD_PROVIDER`,
   `PHI_AI_STORAGE_BUCKET`, `PHI_AI_STORAGE_REGION`, `PHI_AI_KMS_KEY_ID`,
   `PHI_AI_AUDIT_BUCKET`, `PHI_AI_AUDIT_KMS_KEY_ID` - per
   `runbooks/RUNBOOK_INSTALL.md`. Or run `python -m install` and pick
   `Altera Digital Health  [altera]`; it asks for the key pair, not a
   secret.

4. **Pre-flight against the instance.**
   ```bash
   curl -s -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/metadata" | $PY -c '
   import json,sys; d=json.load(sys.stdin); r=d["rest"][0]
   print("fhirVersion", d.get("fhirVersion"), "| software", d.get("software"))
   print("instantiates", d.get("instantiates"))
   print("token url  ", [e["valueUri"] for e in r["security"]["extension"][0]["extension"] if e["url"]=="token"])
   print("Group ops  ", [o["name"] for x in r["resource"] if x["type"]=="Group" for o in x.get("operation",[])])
   print("system ops ", [o["name"] for o in r.get("operation",[])])
   want = ["Patient","Encounter","Observation","Condition","MedicationRequest","DocumentReference","AllergyIntolerance","Immunization","Procedure","DiagnosticReport","ServiceRequest","MedicationAdministration"]
   have = {x["type"]: {i["code"] for i in x.get("interaction",[])} for x in r["resource"]}
   print("missing    ", [t for t in want if t not in have])
   print("creatable  ", sorted(t for t,i in have.items() if "create" in i))'
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" | $PY -c '
   import json,sys; d=json.load(sys.stdin)
   print("grants", d["grant_types_supported"]); print("caps", [c for c in d["capabilities"] if "confidential" in c or "permission-v" in c])
   print("system scopes", sorted(s for s in d["scopes_supported"] if s.startswith("system/")))'
   ```
   Look for: `fhirVersion 4.0.1`; `instantiates` containing the HL7
   `bulk-data` statement; the `token` URL equal to
   `PHI_AI_FHIR_TOKEN_URL`; `Group ops ['export']` and `system ops []`;
   `missing []`; `client_credentials` among the grants;
   `client-confidential-asymmetric` among the capabilities; and
   `system/{Type}.read` for every type in the tuple. Then mint a token
   and find a Group:
   ```bash
   $PY - <<'EOF'
   from core.config.settings import Settings
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for
   s = Settings.from_env()
   c = FHIRIngestionClient(base_url=s.fhir_base_url, profile=profile_for(s.emr_vendor),
                           storage=None, encryptor=None, audit=None, retention_years=s.retention_years)
   c.authenticate_from_settings(s)
   print("TOKEN", c.access_token)
   EOF
   curl -s -H "Authorization: Bearer <TOKEN>" -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/Group" | $PY -c '
   import json,sys; b=json.load(sys.stdin); print([(e["resource"]["id"], e["resource"].get("name")) for e in b.get("entry",[])])'
   ```
   A `400 invalid_client` here is the key (JWKS not yet synced, wrong
   `kid`, or an algorithm Altera does not accept); `invalid_scope` is a
   scope the app was not granted. Put the chosen Group id in
   `PHI_AI_FHIR_GROUP_ID`.

5. **First ingest.**
   ```bash
   $PY -m core.fhir.bulk_scheduler --once
   ```
   Success reads, in order: `Kicking off bulk export for group <id> (12
   resource types)`, `Bulk export still in progress: NN%` on the early
   polls, `Stored <Type> from bulk export: N resources` per type, and
   `Bulk export run complete: N resources stored`, followed by the
   `DELETE` of the status URL. A run that logs `Bulk export manifest
   reported N error file(s)` exits 1 and leaves the server-side export
   in place - Altera's partial-success shape. An HTTP 429 in the log
   means the poll ran before `Retry-After`: raise
   `PHI_AI_BULK_POLL_INTERVAL_SECONDS`. If the profile ever records
   `supports_bulk_export=False`, this command refuses at startup with
   `Altera Digital Health's profile records no Bulk Data Export
   support. Use core/fhir/scheduler.py (paged search) for this
   vendor...` and exits 1. For known patients or an instance without a
   usable Group, the paged path is `$PY -m core.fhir.scheduler --once`.
   Verify with `$PY -m core.verify --deep`.

6. **First delivery - expect a refusal.**
   ```bash
   $PY -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" --vendor altera \
       --identity-map identity.csv --purpose-of-use treatment --patient <source-id>
   ```
   Against the emulator the writer reads `/metadata`, finds no `create`
   interaction and refuses every item with `the destination does not
   advertise create for <Type>` - Altera's API is read-only by its own
   statement. Against the real sandbox two things differ: the delivery
   token is minted without a `scope` (the delivery entry point uses the
   Epic profile for its token client), which Altera documents as
   required, so expect the token request to fail; and if it did not,
   the sandbox advertises `create` on eleven types, so the writer would
   POST - an undocumented interaction for a System app. Do not run this
   with `--confirm` against Altera until `ADP@alterahealth.com` confirms
   a write is permitted; the documented write path is the Unity API, a
   separate client.

7. **Local rehearsal.**
   ```bash
   $PY -m emulators --vendor altera          # http://127.0.0.1:9108/R4/fhir-Prod
   $PY -m pytest tests/test_emulator_integration.py -k altera -v
   $PY -m pytest tests/test_delivery.py tests/test_smart_launch.py -k altera -v
   ```
   The integration run should show the paged read passing, the
   `$export` handshake passing, a client secret refused with
   `invalid_client`, a scope-less token refused with `invalid_scope`,
   and the delivery capability check refusing every type.

8. **Known limits and where to confirm them.**
   - Altera documents no request rate limit, no `_since`, no
     export-frequency limit, no group-size guidance and no file-retention
     period; observe the instance rather than assume.
   - Certification: Altera's ONC page
     (`https://www.alterahealth.com/legal/onc-reg-compliance/`) holds the
     Drummond certificates (Sunrise Acute Care 25.1 no.
     15.04.04.3123.Sunr.25.10.1.250905; TouchWorks EHR 2026 no.
     15.04.04.3123.Touc.26.13.1.260316) and links
     `https://developer.adp.ahcentral.com/Fhir/ProcessOverview` as the
     (g)(10) specification; cross-check on CHPL at
     `https://chpl.healthit.gov/#/search` by those certificate numbers.
   - Fees: the *"API-Related Fees & Licensing for API Users"* sheet on
     the same page (Open tier free; Integrator tiers for Unity).
   - Vendor contact: `ADP@alterahealth.com`, or the Support Widget on
     the portal (ticket type Sandbox for RDS or shared credentials).
   - Client endpoints: `https://main.open.ahcentral.com/fhirendpoints`
     (production endpoints only; provider filter for `/fhir` bases).

## Greenway Health

Primary sources: the Greenway Health Developer Platform at
[developers.greenwayhealth.com](https://developers.greenwayhealth.com/developer-platform/docs/api-an-overview)
- public, in its own words "No registration login is required to view
our documentation" - specifically
[How to Register a SMART Backend Service](https://developers.greenwayhealth.com/developer-platform/docs/how-to-create-a-backend-services-application),
[JWKS Authorization](https://developers.greenwayhealth.com/developer-platform/docs/jwks),
[Bulk Export](https://developers.greenwayhealth.com/developer-platform/docs/fhir-bulk-access),
[Authorization Scopes](https://developers.greenwayhealth.com/developer-platform/docs/authorization-scopes-1),
the [API reference](https://developers.greenwayhealth.com/developer-platform/reference/getting-started-1)
and the [FAQ](https://developers.greenwayhealth.com/developer-platform/docs/frequently-asked-questions-for-developers);
Greenway's own [certification information](https://www.greenwayhealth.com/sites/default/files/files/2025-12/Greenway-Health-Certification-Information.pdf)
and [2026 Real World Testing plan](https://www.greenwayhealth.com/sites/default/files/files/2026-01/2026%20Greenway%20RWT%20Plan.pdf);
and, for the facts marked *observed*, one production tenant's own
`/metadata` and `/.well-known/smart-configuration`, reached on
2026-09-01 through Greenway's public
[endpoint bundle](https://fhir-servicebaseurl.fhirhlprod.greenwayhealth.com/servicebundle.json).
Nothing in this chapter comes from a third party, and nothing is carried
over from another vendor's chapter.

Two words recur below. **Verified** means Greenway's own documentation
or its own server says it. **Confirm on the tenant** means the answer is
configuration that only the customer's site can give.

### Products and the tenant model

Verified. Greenway ships two ambulatory EHRs, Intergy and Prime Suite,
behind one FHIR R4 API that is "consistent for all of our EHR products".
A customer site is a **tenant**, and the tenant id - the site's OID - is
the only per-customer part of the URL:

```
https://fhir-api.fhirprod.aws.greenwayhealth.com/fhir/R4/{TENANT_ID}
```

Every tenant shares one authorization server
(`auth-api.login.greenwayhealth.com`), so `PHI_AI_FHIR_TOKEN_URL` is a
constant for this vendor while `PHI_AI_FHIR_BASE_URL` is per-tenant.
Greenway publishes every customer endpoint as a FHIR Bundle of Endpoint
+ Organization pairs (1,333 pairs on 2026-09-01); the OID in each
Endpoint's `address` is the tenant id. The URL is therefore public; the
authorisation to use it is not (next section).

Verified. An Intergy tenant may hold several practices: "If you have
configured your Intergy instance to support multiple practices, you can
designate which practice(s) the Back-end Service can access." Practice
designation is a property of the backend service's enablement, not of
the URL - two practices in one tenant share a base URL.

Confirm on the tenant: which practices a given backend service was
designated for, and (for Prime Suite, which the multi-practice paragraph
does not mention) whether the tenant is single-practice.

### Registration, review and site permission

Verified, in order:

1. Register at
   [devplatform.greenwayhealth.com/developer/registration](https://devplatform.greenwayhealth.com/developer/registration)
   and choose **Add App**. Launch type **Back-end Service** - "No UI;
   runs independently; used for Bulk FHIR". Standalone and EHR-embedded
   are the interactive SMART flows ingestion does not use.
2. On the app, "input a URL where your ES384 JWKS Public Key resides" -
   a JWKS URL, not a pasted key - then "select the desired scopes and
   click Save".
3. "Submit For Review": "The submitted application information is
   reviewed by our Greenway team against relevant standards before
   getting published." Corrections come by email; no timeline is
   documented.
4. "Once published, the system generates a client ID which you then use
   to authenticate your backend service." One client ID. Greenway
   documents no non-production/production split - do not expect a
   second one.
5. Per-site enablement. The FAQ ("We have registered our Backend Service
   SoF application, what are the next steps?") describes a workflow that
   runs after publication: Greenway collects the site's identifiers
   (Intergy licence/GID or Prime Suite ID, practice IDs, what
   confidential data the service needs, contacts), obtains **written
   permission from the site**, provisions access internally and
   notifies both parties - a process the FAQ says can take a week or
   longer depending on the customer's responsiveness. This step turns a
   public base URL into one you may call.

Not documented by the vendor: what is frozen after publication (JWKS
URL, scopes, name), how long review takes, and whether changing the
scope set re-triggers review. Ask through the
[FHIR support form](https://developers.greenwayhealth.com/developer-platform/docs/fhir-support)
before assuming any of it.

### Auth: ES384 JWT client assertion against a hosted JWKS

Verified. "Back End Services operate autonomously without user
interaction. As such, they do not have a typical user/password
authentication flow. Instead, backend services use client credentials
which are comprised of a public/private JWKS key pair." There is no
client secret in the backend flow; `PHI_AI_FHIR_CLIENT_SECRET` is inert
here and `Settings.from_env()` does not ask for it.

Verified. The algorithm is **ES384**: "Use the ES384 (ECDSA using P-384
and SHA-384) signature algorithm when generating your keys." That is the
instruction attached to the JWKS-URL field on the registration form; the
JWKS walkthrough's more general "asymmetric cryptographic key pair
(e.g., RSA or ECDSA)" does not override it. The profile records
`assertion_algorithm="ES384"` and the emulator refuses anything else.

Verified, from the JWKS walkthrough - the token request:

- Discovery: `GET {base}/metadata`; the conformance statement's
  `oauth-uris` extension carries the token and authorize endpoints
  (observed: it does). The token endpoint is
  `https://auth-api.login.greenwayhealth.com/oauth2/as/token.oauth2`
  for every tenant.
- Assertion claims: `iss` and `sub` = client ID; `aud` = "the URL of
  the authorization server's token endpoint"; `jti` = "a UUID"; `exp` -
  "The exp claim in the JWT assertion must not exceed 60 minutes from
  the time of issuance. Our authorization server only supports a maximum
  token validity of 60 minutes. Assertions with longer expiration times
  will be rejected." `build_client_assertion()`'s four-minute `exp` is
  inside that.
- POST form body: `grant_type=client_credentials`,
  `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
  `client_assertion`, and **`scope`** - "SMART-on-FHIR scopes needed for
  the app". The scope parameter is part of Greenway's documented
  request; the next section says what the profile sends.

Observed on the tenant's `/.well-known/smart-configuration`:
`grant_types_supported` includes `client_credentials`; `capabilities`
include `client-confidential-asymmetric`, `permission-v1` and
`permission-v2`; `code_challenge_methods_supported` is `S256` only. The
document carries no `token_endpoint_auth_signing_alg_values_supported`
and no `scopes_supported`, so the ES384 fact rests on the registration
page, not on discovery.

Not documented by the vendor: `kid` handling (the SMART asymmetric
profile Greenway advertises requires one - put a `kid` in the JWKS and
set `PHI_AI_FHIR_JWT_KID` to it), the access token's `expires_in` (the
client reads it from the response), and `iat`/`nbf` (harmless to send).

**What the client does.** `core/fhir/client.py` signs with the
profile's `assertion_algorithm`: `build_client_assertion(algorithm=
profile.assertion_algorithm)` produces an ES384 assertion for this
profile, `check_private_key_signs()` refuses a key of the wrong family
before anything is signed, and `Settings.from_env()` refuses an RSA key
in `PHI_AI_FHIR_PRIVATE_KEY_PATH` for this profile at startup, naming
both sides (`tests/test_assertion_algorithm.py` proves ES384 on the
wire). The Greenway emulator returns `invalid_client` for an RS384
assertion, so a regression here is caught by
`tests/test_emulator_integration.py`, not by a live tenant.

### Scopes: chosen at registration, sent at token time

Verified. Scopes are selected on the app registration and sent again in
the token request. Greenway's scopes page documents both syntaxes -
SMART 1.0 `<context>/<resource or *>.<read|write|*>` and SMART 2.0
`<context>/<resource or *>.<cruds>` with `system` as a context - and the
observed discovery document advertises `permission-v1` and
`permission-v2`. `GREENWAY.requires_token_scopes` is True, so
`authenticate_from_settings()` sends one `system/{Type}.read` per entry
in `supported_resources` (v1 syntax, which the tenant advertises). The
reason is Greenway's own documented payload, not another vendor's rule.

Verified. Greenway's
[unsupported-scopes notice](https://developers.greenwayhealth.com/developer-platform/docs/unsupported-scopes-removal):
Appointment and Consent "are not supported by our FHIR ecosystem";
requests for them used to be "silently rejected" and now return
`BAD_REQUEST` / `invalid_scope` - "The requested scope is invalid,
unknown, malformed, or exceeds that which the client is permitted to
request." Two consequences: never request either (Consent appears in
other profiles in `emr_profiles.py`; it is deliberately absent from
Greenway's), and **the scopes selected on the app must cover every type
in `supported_resources`**, because a scope beyond what the app was
granted is refused, not trimmed.

Not documented by the vendor: whether a wildcard `system/*.read` is
accepted - the explicit per-type list never has to find out - and
whether `Group/{id}/$export` needs a `system/Group.read` scope. Select
it on the app to be safe; it is not in `supported_resources` because
Group is not a record type this system archives, so it is not in the
derived scope string either. If a tenant refuses `$export` for want of
it, that is a scope-derivation change in `client.py`, not a profile
change.

### Resources

Verified. The getting-started page lists "most of the resources
available in our API" - AllergyIntolerance, Binary, CarePlan, CareTeam,
Condition, Coverage, Device, DiagnosticReport, DocumentReference,
Encounter, Goal, Immunization, Location, Medication, MedicationRequest,
Observation, Organization, Patient, Practitioner, PractitionerRole,
Procedure, Provenance - and the API reference adds Group,
MedicationDispense, Person, RelatedPerson, ServiceRequest and Specimen
endpoints. The reference site's regulatory page cites 45 CFR
170.315(g)(10), US Core STU6.1 and USCDI v3.

Observed on one tenant: FHIR 4.0.1, "Greenway FHIR Server" 1.0.0,
instantiating the US Core server and Bulk Data CapabilityStatements;
every listed type advertises `read` and `search-type` only (Binary:
`read` only); Group carries the `export` operation, Patient carries
`$everything`; there is no `create`, `update` or conditional interaction
anywhere.

`GREENWAY.supported_resources` is that surface restricted to this
project's retention scope - 18 types - and the profile's per-field
comment says which published types are left out and why (Binary has no
search; Medication has no reference endpoint and was not advertised;
Location, Organization, Practitioner, PractitionerRole, Person,
RelatedPerson, Specimen and Group are reference data; Appointment and
Consent are unsupported). AdverseEvent, MedicationAdministration and
ExplanationOfBenefit are not published by Greenway at all.

Confirm on the tenant: the resource list and the `export` operation on
Group, against `GET {base}/metadata`. One tenant was observed; the
vendor's page says "most of" the resources, and per-practice designation
may narrow what a token can reach.

### Population-scale reads: Group `$export`, tenant-wide by default

Verified. "FHIR Bulk Export is authenticated using Backend Services
Authorization" and the documented operation is

```
GET {base}/Group/{id}/$export      Prefer: respond-async   (required)
```

**Group-level only.** No system-level or Patient-level export is
documented, and the observed conformance statement carries the `export`
operation on Group alone. `core/fhir/bulk_client.py` already kicks off
at `Group/{id}/$export`, so the URL shape needs no change.

Verified - **what a Group covers**: "bulk export operations via the Bulk
Data API endpoint occur at the tenant (site) level by default, exporting
data from all practices for the tenant. To export a subset of the data,
filter parameters can be applied to the Bulk API request." A
multi-practice Intergy tenant exports every practice unless filtered.

Verified - **Groups are discoverable.** `GET {base}/Group` returns "the
metadata/attributes for tenant-wide patient groups" and
`GET {base}/Group/{id}` reads one. `PHI_AI_FHIR_GROUP_ID` therefore
comes from a search against the tenant, not from an email to the
vendor. How a tenant's groups come to exist is not documented by the
vendor; ask the practice.

Verified, and the single most important line in this chapter:
**`_since` defaults to the last 24 hours.** "Resources updated after
this period will be included in the response. *If this parameter is
absent only resources created or updated in the past 24 hours will be
exported.*" A kickoff without `_since` is a one-day delta, never a full
history. The first load against a Greenway tenant must pass an early
`_since` (the practice's go-live date, or `1900-01-01T00:00:00Z`), and
every later run either passes the previous `transactionTime` or accepts
the 24-hour window. **`bulk_client.kickoff_export()` has no `since`
parameter today**, so this is a required code change before the first
Greenway run - the omission was right for a vendor without `_since`; it
is wrong here.

Verified. `_type` is "a string of comma-delimited FHIR resource type";
omitted, "the server returns all supported resources within the scope
of the client authorization". Handshake: `202 Accepted` +
`Content-Location`; polling returns `202` with `X-Progress` until `200`
with the manifest (`transactionTime`, `request`,
`requiresAccessToken: true`, `output[]`, `error[]`); `DELETE` on the
status URL cancels (`202`); files are `application/fhir+ndjson`; errors
are `4XX`/`5XX` with an OperationOutcome body.
`bulk_client.wait_for_export()` and `delete_export()` implement exactly
this shape.

Not documented by the vendor: any kickoff-frequency throttle,
`Retry-After`, a recommended or enforced group size, how long output
files remain downloadable, or a rate limit. `bulk_scheduler.py`'s
default 24-hour cadence happens to line up with the 24-hour default
window; treat that as coincidence and set the cadence on the practice's
needs. `PHI_AI_BULK_POLL_INTERVAL_SECONDS` keeps its default; there is
no vendor figure to prefer.

### Writes

Verified: **the FHIR API is read-only.** "The current Greenway FHIR API
supports read operations only at the present time." The observed
conformance statement agrees - no `create` on any type - and
`GREENWAY.writable_resources` is empty, `supports_conditional_create`
False, `supports_bulk_import` False.

What `core/fhir/delivery/writer.py` does against a Greenway tenant: it
reads `GET {destination}/metadata` before writing anything, collects the
types that advertise `create`, finds none, logs "destination advertises
create for 0 resource type(s)", and skips every resource.
`python -m core.fhir.delivery --vendor greenway ...` therefore completes
without writing, and that is the correct result. If the metadata call
itself fails the writer raises rather than guessing.

Verified: the real write path is **GAPI**, "a Proprietary API with
separate and distinct API calls and data structures for each of our EHR
products" that "supports reads and writes across a variety of clinical
and financial data elements". It lives on a different portal
(`developer.greenwayhealth.com`; an API key plus an authorization token
per its overview) and the platform describes its dashboard as for
"Marketplace partners and clients". It is not FHIR, it differs between
Intergy and Prime Suite, its element list is not on the public site, and
it would be a second client with its own onboarding - never a
`writable_resources` entry in this profile. Until such a client exists,
deliver to a Greenway practice as files for their own import tooling.

Confirm on the tenant: nothing for now. A future FHIR write capability
would surface as a `create` interaction in `/metadata`, and `writer.py`
would honour it without a profile change.

### Fees, limits and the sandbox that is not there

Verified. Fees: "There are currently no fees for the use of the Greenway
Health FHIR API, Developer Platform, and Application Gallery. We do,
however, reserve the right to modify this policy", with changes
effective "at least 30 days following their announcement". The Terms of
Service reserve the right to charge, to "impose limits on certain
features and services or suspend your access", and to act against "an
unreasonable or disproportionately large load on the infrastructure".

Not documented by the vendor: a numeric rate limit or a `_count`
ceiling. `rate_limit_per_min` and `page_size` are left at the dataclass
defaults (60 per minute, 50 per page) on purpose; no other vendor's
figure was borrowed.

Verified. Sandbox: the FAQ - "At this time, we do not have a sandbox
environment available for developers ... The sandbox environment is on
our roadmap, but we do not have a specific ETA." The getting-started
page says one "will be available soon". A
`fhir-api.fhirstaging.aws.greenwayhealth.com` host appears in one
reference code sample and is described nowhere. Rehearse against
`emulators/` (port 9109) and go to a real tenant only after site
permission.

Verified. Minimum product versions for the HTI-1-era API: Intergy v22
and Prime Suite v22. PKCE S256 became mandatory for SMART app-launch
clients on 2025-10-20; that is the interactive flow and does not touch
backend services.

### Certification baseline

Verified from Greenway's own certification-information PDF (updated
2025-12-11): Intergy v22, certification ID
`15.04.04.2913.Inte.22.06.0.250814`, and Prime Suite v22,
`15.04.04.2913.Prim.22.04.1.250814`, both certified 2025-08-14 and both
listing §170.315(g)(10) "Standardized API for patient and population
services". The 2026 Real World Testing plan measures, among others,
"Number of authorized Bulk Applications". The CHPL listings themselves
were not fetched (the site is an application, not a page); search
[chpl.healthit.gov](https://chpl.healthit.gov) by certification ID to
read the registered documentation URL.

g(10) obliges a certified module to offer SMART Backend Services and
Group-level bulk export. Greenway documents both directly, so in this
chapter certification is corroboration rather than the source of any
profile value.

### Validate before ingesting

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

Look for: `fhirVersion` 4.0.1; every type in
`GREENWAY.supported_resources` present with `read` and `search-type`;
Group present with the `export` operation; the `oauth-uris` extension's
`token` URL equal to `PHI_AI_FHIR_TOKEN_URL`; `grant_types_supported`
including `client_credentials` and `capabilities` including
`client-confidential-asymmetric`; and no `create` interaction anywhere
(if one appears, `write_notes` is out of date - good news, but re-read
it). Then `GET {base_url}/Group` with a token to pick
`PHI_AI_FHIR_GROUP_ID`. A 401/403 on any of these after a successful
token means the site-permission step has not completed for this tenant
or this practice.

### What the emulator reproduces

`emulators/vendors.py` `"greenway"` (port 9109, path
`/fhir/R4/EMULATOR-TENANT-OID`):

- The token endpoint honours only a JWT client assertion signed
  **ES384**: an RS384 assertion gets `400 invalid_client`, as does a
  client secret in the form body or a Basic header.
- A token request with no `scope` gets `400 invalid_scope` (Greenway's
  documented payload carries scope). The wildcard refusal that rides on
  the same flag is the emulator's existing behaviour, not a documented
  Greenway rule.
- `$export` is served with the real async handshake (202, then 202 +
  `X-Progress`, then 200 manifest, NDJSON files, DELETE).
- The CapabilityStatement advertises `create` for nothing, so the
  delivery writer skips every type; a direct POST gets
  `422 not-supported`; `If-None-Exist` gets `412`.
- Pagination at 2 per page regardless of `_count`.
- **Not reproduced**, and said so in the entry's notes: Group-only
  export (the emulator answers `$export` at any level), the 24-hour
  default `_since` window, the tenant-wide default scope, and Greenway's
  real discovery document (the emulator's
  `/.well-known/smart-configuration` is hard-coded for every vendor and
  does not list `client-confidential-asymmetric`). Signature checking is
  header-only unless the emulator is given the client's JWKS.

### Setting it up

Zero to a working Greenway Health connector - first in the non-PHI
rehearsal, then against a real tenant. Every step is Greenway's
documented path or this repository's own entry point; nothing is
borrowed from another vendor's setup.

1. **Register the backend service** on the Greenway Developer Platform.
   1. Create a developer account at
      https://devplatform.greenwayhealth.com/developer/registration and
      sign in.
   2. **Add App**. Name, short description, intended purpose and user;
      launch type **Back-end Service** (Greenway's label: "No UI; runs
      independently; used for Bulk FHIR"). Do not pick Standalone or
      EHR embedded.
   3. Fill Technical Details and General Information (both mandatory).
      In Technical Details enter the **JWKS URL** from step 2 -
      Greenway asks for "a URL where your ES384 JWKS Public Key
      resides"; a pasted key is not an option.
   4. **Select scopes**: `system/{Type}.read` for every type in
      `GREENWAY.supported_resources` (18 types - print them from the
      profile with `python -c "from core.fhir.emr_profiles import GREENWAY; print(' '.join(f'system/{t}.read' for t in GREENWAY.supported_resources))"`,
      do not retype them), plus `system/Group.read` so `GET /Group` and
      `$export` discovery work. Do not select Appointment or Consent;
      Greenway refuses them with `invalid_scope`.
   5. **Save**, then **Submit For Review**. Greenway's team reviews the
      submission and publishes it to the App Gallery; corrections come
      by email. Timeline: not documented.
   6. On publication, copy the generated **client ID**. There is one;
      Greenway issues no separate non-production ID.
   7. **Site permission** (per tenant, after publication): follow the
      FAQ's "next steps" workflow - Greenway collects the site's
      identifiers (Intergy licence/GID or Prime Suite ID, practice
      IDs), the data the service needs, and contacts; the site gives
      **written permission**; Greenway provisions. Budget a week or
      longer. Until this completes, a valid token still gets 401/403
      from the tenant.
   8. What you cannot change later: not documented by the vendor - ask
      via the FHIR support form
      (https://developers.greenwayhealth.com/developer-platform/docs/fhir-support)
      before publishing if the JWKS URL or scope set is still in flux.

2. **Generate the ES384 key pair and publish the JWKS.** EC P-384
   (secp384r1), not RSA.

   ```bash
   # Private key - never leaves your infrastructure; referenced by PHI_AI_FHIR_PRIVATE_KEY_PATH
   openssl ecparam -name secp384r1 -genkey -noout -out greenway_private_key.pem
   chmod 600 greenway_private_key.pem
   # Public half
   openssl ec -in greenway_private_key.pem -pubout -out greenway_public_key.pem
   ```

   Build the JWKS (`kty` EC, `crv` P-384, plus `kid`, `alg` ES384,
   `use` sig) with the repository's own PyJWT:

   ```bash
   KID="greenway-$(date +%Y%m)"
   .venv/bin/python - "$KID" <<'EOF'
   import json, sys
   from cryptography.hazmat.primitives import serialization
   from jwt.algorithms import ECAlgorithm
   pub = serialization.load_pem_public_key(open("greenway_public_key.pem", "rb").read())
   jwk = json.loads(ECAlgorithm.to_jwk(pub))
   jwk.update({"kid": sys.argv[1], "alg": "ES384", "use": "sig"})
   json.dump({"keys": [jwk]}, open("greenway_jwks.json", "w"), indent=2)
   print(open("greenway_jwks.json").read())
   EOF
   ```

   Host `greenway_jwks.json` at a world-readable HTTPS URL (any static
   host; it holds only the public key) and enter that URL on the app
   (step 1.3). Keep `$KID`; it becomes `PHI_AI_FHIR_JWT_KID`. Rotating
   the key means replacing the hosted JWKS - Greenway documents no
   separate rotation procedure.

3. **PHI AI environment** (names from `core/config/settings.py`; the
   cloud, storage, KMS and audit variables in
   `runbooks/RUNBOOK_AWS_SETUP.md` are unchanged):

   ```bash
   PHI_AI_EMR_VENDOR=greenway
   PHI_AI_FHIR_BASE_URL=https://fhir-api.fhirprod.aws.greenwayhealth.com/fhir/R4/<TENANT_OID>   # from the site, or Greenway's public endpoint bundle
   PHI_AI_FHIR_TOKEN_URL=https://auth-api.login.greenwayhealth.com/oauth2/as/token.oauth2         # the same for every tenant
   PHI_AI_FHIR_CLIENT_ID=<client id issued on publication>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/greenway_private_key.pem                              # the EC P-384 key, mounted, never baked in
   PHI_AI_FHIR_JWT_KID=<the kid in your hosted JWKS>
   PHI_AI_FHIR_GROUP_ID=<from GET {base}/Group - step 4>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600      # default; Greenway documents no figure
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400         # default
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET - Greenway documents no secret flow for backend services.
   ```

   Scopes need no variable: `requires_token_scopes=True` makes
   `authenticate_from_settings()` derive `system/{Type}.read` from
   `GREENWAY.supported_resources`.

   Code prerequisite (flagged in the profile notes): `core/fhir/client.py`
   must sign with `profile.assertion_algorithm` (ES384) instead of the
   hard-coded RS384, and `bulk_client.kickoff_export()` must accept a
   `since` value. Until both land, step 5 cannot pass against Greenway;
   the emulator in step 7 shows the first failure as `invalid_client`.

4. **Pre-flight** (no PHI; the conformance statement and discovery
   document are unauthenticated by design):

   ```bash
   curl -s -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/metadata" | .venv/bin/python -c '
   import json, sys
   m = json.load(sys.stdin); rest = m["rest"][0]
   print("fhirVersion", m["fhirVersion"])
   sec = rest["security"]["extension"][0]["extension"]; print({e["url"]: e["valueUri"] for e in sec})
   for r in rest["resource"]:
       print(r["type"], [i["code"] for i in r.get("interaction", [])], [o["name"] for o in r.get("operation", [])])'
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" | .venv/bin/python -m json.tool
   ```

   Expect: `fhirVersion 4.0.1`; the `token` URI equal to
   `PHI_AI_FHIR_TOKEN_URL`; every type in `GREENWAY.supported_resources`
   with `read` and `search-type`; `Group ... ['export']`; no `create`
   anywhere; `grant_types_supported` containing `client_credentials` and
   `capabilities` containing `client-confidential-asymmetric`. Then a
   token and the Group list:

   ```bash
   .venv/bin/python - <<'EOF'
   import os, time, uuid, jwt, requests
   cid, turl = os.environ["PHI_AI_FHIR_CLIENT_ID"], os.environ["PHI_AI_FHIR_TOKEN_URL"]
   key = open(os.environ["PHI_AI_FHIR_PRIVATE_KEY_PATH"], "rb").read()
   now = int(time.time())
   assertion = jwt.encode(
       {"iss": cid, "sub": cid, "aud": turl, "jti": str(uuid.uuid4()), "iat": now, "exp": now + 240},
       key, algorithm="ES384", headers={"kid": os.environ["PHI_AI_FHIR_JWT_KID"], "typ": "JWT"})
   r = requests.post(turl, data={
       "grant_type": "client_credentials",
       "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
       "client_assertion": assertion,
       "scope": "system/Patient.read system/Group.read"}, timeout=30)
   print(r.status_code, {k: v for k, v in r.json().items() if k != "access_token"})
   tok = r.json()["access_token"]
   g = requests.get(os.environ["PHI_AI_FHIR_BASE_URL"] + "/Group",
                    headers={"Authorization": f"Bearer {tok}", "Accept": "application/fhir+json"}, timeout=30)
   print(g.status_code, [(e["resource"]["id"], e["resource"].get("name")) for e in g.json().get("entry", [])])
   EOF
   ```

   `200` and a list of `(id, name)` pairs: pick the group and set
   `PHI_AI_FHIR_GROUP_ID`. `400 invalid_client`: wrong algorithm, wrong
   `aud`, `exp` beyond 60 minutes, or the JWKS URL is not reachable from
   Greenway. `400 invalid_scope`: a scope not selected on the app.
   `401`/`403` on `/Group` with a good token: site permission not yet
   provisioned for this tenant.

5. **First ingest.** Population-scale, via Group export - pass an early
   `_since` on the first run or you get only the last 24 hours:

   ```bash
   python -m core.fhir.bulk_scheduler --once      # needs the `since` change in bulk_client.kickoff_export()
   ```

   Success looks like `Kicking off bulk export: group=...`, one or more
   `Bulk export still in progress`, then per-type NDJSON ingestion and
   an index write; verify with `python -m core.verify --export-dir <dir>`.
   If the profile ever records `supports_bulk_export=False` you would
   instead see, with exit code 1: `Greenway Health's profile records no
   Bulk Data Export support. Use core/fhir/scheduler.py (paged search)
   for this vendor, or correct the profile if the instance's
   CapabilityStatement proves otherwise.` The paged path
   (`python -m core.fhir.scheduler --once`) also works against Greenway
   - every type advertises `search-type` - and is the right tool for a
   single known patient.

6. **First delivery.** A write to a Greenway tenant is a documented
   no-op:

   ```bash
   python -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" --vendor greenway \
     --identity-map ./patient-mapping.csv --patient <source id> --purpose-of-use "<reason>"
   ```

   with `PHI_AI_DELIVERY_CLIENT_ID`, `PHI_AI_DELIVERY_TOKEN_URL` and
   `PHI_AI_DELIVERY_PRIVATE_KEY_PATH` set (or
   `PHI_AI_DELIVERY_ACCESS_TOKEN`). The writer reads `/metadata`, logs
   `destination advertises create for 0 resource type(s)`, and skips
   every resource - Greenway's FHIR API "supports read operations only".
   Adding `--confirm` changes nothing. Deliver as files, or build a GAPI
   client (a separate project with its own onboarding).

7. **Local rehearsal** (no vendor account needed):

   ```bash
   python -m emulators --vendor greenway                       # http://127.0.0.1:9109/fhir/R4/EMULATOR-TENANT-OID
   python -m pytest tests/test_emulator_integration.py -k greenway -q
   ```

   The emulator refuses RS384 assertions and client secrets with
   `invalid_client`, refuses scope-less token requests with
   `invalid_scope`, serves the async `$export` handshake, and advertises
   `create` for nothing. Point `.env` at it
   (`PHI_AI_FHIR_BASE_URL=http://127.0.0.1:9109/fhir/R4/EMULATOR-TENANT-OID`,
   `PHI_AI_FHIR_TOKEN_URL=http://127.0.0.1:9109/oauth2/token`) to run
   steps 5 and 6 end to end before touching a tenant.

8. **Known limits and where to confirm them.**
   - Not documented by Greenway: rate limits, `_count` ceiling, kickoff
     throttle, `Retry-After`, group size, output-file expiry, token
     `expires_in`, post-publication immutability, how Groups are
     created, wildcard scopes. Ask via the FHIR support form,
     https://developers.greenwayhealth.com/developer-platform/docs/fhir-support
     (the one contact route the cited Greenway pages document).
   - Certification: Intergy v22 `15.04.04.2913.Inte.22.06.0.250814`,
     Prime Suite v22 `15.04.04.2913.Prim.22.04.1.250814` -
     https://www.greenwayhealth.com/sites/default/files/files/2025-12/Greenway-Health-Certification-Information.pdf;
     search https://chpl.healthit.gov by those IDs for the registered
     API documentation URL.
   - Fees: none today, 30 days' notice -
     https://developers.greenwayhealth.com/developer-platform/page/fees.
   - Minimum versions: Intergy v22 / Prime Suite v22 -
     https://developers.greenwayhealth.com/developer-platform/reference/minimum-product-requirements.

## Veradigm

Primary sources: Veradigm's own developer portal -
[FHIR Introduction](https://developer.veradigm.com/Fhir/Introduction),
[Process Overview](https://developer.veradigm.com/Fhir/ProcessOverview)
(registration, System-app token request, JWKS requirements, C# sample),
[SMART on FHIR](https://developer.veradigm.com/Fhir/SMARTonFHIR)
(backend services, keys, the nightly JWKS sync),
[Bulk Data](https://developer.veradigm.com/Fhir/BulkData),
[Searching](https://developer.veradigm.com/Fhir/Searching),
[Resources](https://developer.veradigm.com/Fhir/Resources),
[Endpoint Directory](https://developer.veradigm.com/Fhir/EndpointDirectory)
and [FHIR Partner Testing Environments](https://developer.veradigm.com/Fhir/FHIR_Sandboxes)
- plus the public Veradigm EHR sandbox the last page names,
`https://fhir.fhirpoint.open.allscripts.com/fhirroute/fhir/CP00101/`,
whose `/metadata` and `/.well-known/smart-configuration` were read on
2026-09-01. Where this chapter says "documented" it means one of those
pages; where it says "the sandbox advertises" it means that server;
where it says "not documented by the vendor" the profile takes the
conservative default and nothing from another vendor's chapter is
substituted.

### Which product this is

Veradigm's portal is explicit: *"the term 'product' refers to Veradigm
EHR."* This connector targets Veradigm EHR (the Professional / clinical
EHR line). Three things it is not:

- **Altera TouchWorks, Sunrise and Paragon.** Veradigm's own pages say
  *"For information on Altera's Developer Program, contact Altera at
  ADP@alterahealth.com."* Altera is a separate program with its own
  endpoint directory (the `altera` profile is a separate entry - see
  the Altera Digital Health chapter above; do not point
  `PHI_AI_EMR_VENDOR=veradigm` at a TouchWorks site).
- **Practice Fusion.** Same parent company, separate product and API
  (its own profile, `practicefusion`).
- **Veradigm Practice Management.** Not reachable over FHIR at all:
  *"To integrate with Veradigm Practice Management, developers must
  utilize Unity to read or write patient demographic, appointment, or
  financial data."* Unity is Veradigm's proprietary API - see Writes.

### Per-organization endpoints: the Endpoint Directory

Veradigm has no single API host. *"At the time that the Veradigm FHIR
API is installed in a client environment, the client's endpoints are
registered in the Veradigm 'downtown' environment. Only endpoints that
are designated for production environments are listed in the Veradigm
Endpoint Directory."* Provider/system endpoints *"generally end in
/fhir"*, patient endpoints in `/open`. Some sites are **Versionless** -
*"Multiple FHIR versions (DSTU2 and R4) are functioning on the same
endpoint"* - and the default version there *"is specified at the time
FHIR is installed."*

Practically:

- `PHI_AI_FHIR_BASE_URL` is per organization. The sandbox shape is
  `https://fhir.fhirpoint.open.allscripts.com/fhirroute/fhir/{site}/`;
  a customer's production URL comes from the Endpoint Directory (portal
  login) or from the organization itself.
- `PHI_AI_FHIR_TOKEN_URL` is per organization too. Veradigm's documented
  way to find it is the site's own CapabilityStatement: *"The Veradigm
  FHIR server returns the Capability Statement which includes two
  endpoints: authorize endpoint ... token endpoint."* On the sandbox
  that is
  `https://fhir.fhirpoint.open.allscripts.com/fhirroute/authorizationV2/CP00101/connect/token`
  (the `oauth-uris` extension under `rest.security`). Never reuse one
  site's token URL for another.
- On a Versionless site, send
  `Accept: application/fhir+json; fhirVersion=4.0` (the Endpoint
  Directory page's own header example) and confirm `fhirVersion` in
  `/metadata` before trusting a response. `core/fhir/client.py` sends
  `Accept: application/fhir+json` without the version parameter; on a
  Versionless site whose default is DSTU2 that is a real gap - confirm
  the site's default first.
- The Endpoint Directory can be downloaded as an NDJSON FHIR bundle of
  Organization resources with their endpoints (portal login required).

### Registration, review and per-client activation

Every step below is from the Process Overview page.

1. Sign up at [developer.veradigm.com](https://developer.veradigm.com/Account/RegisterSelf).
   The **Open** tier is free: *"Open accounts are free to build and
   deploy by anyone. This option gives developers full access and
   rights to use our FHIR-enabled APIs."* Integrator tiers (paid) are
   for Unity, EHR-launch testing and Veradigm certification - none of
   which ingestion needs.
2. On **My Dashboard > My FHIR Applications**, add an application. Set
   **App Type: System** - *"The app's intended audience is an external
   system, not a patient or provider."* Bulk export is gated on this:
   *"Only FHIR applications of the type System can send bulk data
   requests."* Set **Client Type: Confidential**. Fill **JWKS URL** -
   *"URL for backend authentication access (JWKS) tokens"*. Redirect
   and Launch URLs are for user/patient apps; a System app can leave
   the documented placeholder `urn:ietf:wg:oauth:2.0:oob`.
3. On Save the portal displays *"Client ID, Secret, Secret Expiration
   Date"*. The Client ID is `PHI_AI_FHIR_CLIENT_ID`. The Secret's role
   for a System app is **not documented by the vendor** - the documented
   System token request carries no secret - so `PHI_AI_FHIR_CLIENT_SECRET`
   stays unset. Note the expiration date anyway; the portal will ask
   you to rotate it.
4. Select a **Purpose of Use** and the app's **scopes**. Veradigm's
   rules: *"The scopes must match the FHIR App Type"* (System apps use
   `system/` scopes); *"Never request scopes that are not required"*;
   *"SMART version 1 scopes end in .read, and SMART version 2 scopes end
   in .rs. ... Do not request both SMART version 1 and 2 scopes for a
   single FHIR application. The app will not be approved, and this will
   prolong the registration process."* Pick v1 (`.read`) to match the
   `system/*.read` Veradigm's own token example uses.
5. Test (see the sandbox section), then click **Request Production
   Access**. What cannot change afterwards: *"the application name,
   type, and Purpose of Use ... cannot be changed once production access
   is granted."*
6. Review board: *"The FHIR application is reviewed and, if
   appropriate, approved by Veradigm Connect. Once approved, clients can
   begin activating the FHIR application."*
7. Per-client activation: *"Veradigm Connect developers cannot license
   their applications for clients; the clients must activate
   applications themselves through the client License Management
   Portal."* One registration; each organization switches it on for its
   own site. Token validity for user apps is *"defined in the Veradigm
   License Management Portal"*; the system-app token lifetime is not
   documented by the vendor (the sandbox returns `expires_in`; read it).

### Auth: JWT client assertion against a hosted RSA JWKS

Documented System-app token request (Process Overview, "System
Applications" - *"The body of the request must include the
following"*):

```
POST {token URL}
grant_type=client_credentials
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion={signed JWT}
scope=system/*.read
```

`FHIRIngestionClient.authenticate()` sends the first three exactly;
scope is discussed in the next section. The assertion Veradigm's C#
sample builds: `iss` and `sub` = client ID, `aud` = token URL, random
`jti`, expiry five minutes out - what `build_client_assertion()`
produces. Veradigm's description of it: the *"system token ... includes
an expiration time, generally two to 20 minutes, and can only be used
once."*

Keys (Process Overview, "JWKS Requirements" - all verbatim):

- *"Each key must use the RSA key type (kty) and be suitable for
  signature verification (use: "sig")."*
- *"Keys should be 2048-bit RSA keys. The modulus (n) and exponent (e)
  must be base64url-encoded without padding."*
- *"Include a unique kid (Key ID) to allow key selection during JWT
  validation."* - so `PHI_AI_FHIR_JWT_KID` is mandatory here, not
  optional as the `.env.example` comment (written for Epic) suggests.
- *"The JWKS must be hosted at a publicly accessible HTTPS endpoint."*

**Signing algorithm - not documented by the vendor.** Veradigm names
the key type, never the algorithm: its sample JWKS carries
`"alg": "RS256"`, its C# sample signs with `X509SigningCredentials`,
and the sandbox's `smart-configuration` publishes no
`token_endpoint_auth_signing_alg_values_supported` (its
`token_endpoint_auth_methods` field lists `client_secret_basic`,
`client_secret_post`, `tls_client_auth`, `self_signed_tls_client_auth`,
while `capabilities` includes `client-confidential-asymmetric`). The
profile leaves `assertion_algorithm` at RS384 because RS384 is an RSA
algorithm (it satisfies the documented key rule) and because SMART App
Launch 2.0.0, which the Introduction page says Veradigm implements,
requires servers to accept RS384 or ES384. Confirm on the partner
testing environment; if Veradigm accepts only RS256, the change is in
`build_client_assertion()`, not the profile. An EC key is out: the
JWKS rule says RSA.

**The nightly sync.** From the SMART on FHIR page: *"a nightly job is
run 'downtown' that cycles through all registered FHIR system
applications. It downloads the JWKS information and updates the OAuth
clients. If information has changed, then the normal mechanism of
distributing OAuth information, including OAuth secrets, kicks in. That
information is then downloaded to the client systems."* A newly
registered or rotated key is therefore not usable the moment it is
published. Allow a day; on rotation keep the old key in the JWKS until
the new `kid` authenticates.

What a misconfiguration looks like, from the sandbox token endpoint
(2026-09-01, no registered client): a client_credentials request with
a `client_secret` and no assertion got `400 {"error":"invalid_client"}`;
a malformed assertion got `400 {"error":"invalid_request"}` with or
without scope. Bare bodies, no `error_description`.

### Scopes: the wildcard Veradigm documents, and what this client sends

Veradigm's documented System token body carries `scope=system/*.read`
- a wildcard. The sandbox's `scopes_supported` lists both that wildcard
(`system/*.read`, `system/*.rs`) and per-type scopes
(`system/Patient.read`, `system/Observation.rs`, ...), and its
`capabilities` includes `permission-v1` and `permission-v2`. What is
**not documented by the vendor** is whether a token request with no
scope is refused, or whether a request may ask for scopes the app
registration did not select.

The profile sets `requires_token_scopes=False`. That flag exists for a
different vendor's documented shape (explicit per-type scopes, no
wildcards) and cannot express the wildcard Veradigm shows, so the
client currently sends **no scope** to Veradigm. The pre-flight in the
setup section tests exactly this. If the token endpoint refuses a
scope-less request, pass the documented value explicitly
(`FHIRIngestionClient.authenticate(..., scope="system/*.read")`) - a
client change to track here, not a profile flag to flip.

### Versions: R4 only, DSTU2 sunset, US Core

*"The current version of the Veradigm implementation of the FHIR API
supports FHIR release 4 ('R4')"*; *"as of 6/1/2025, Veradigm will no
longer be providing support for DSTU2 ... The API is not turned off, but
there is no longer technical support or resolution provided for any
issues that arise."* The sandbox reports `fhirVersion 4.0.1`, software
"Veradigm FHIR 4.22.0.1", implementation guide US Core 3.1.1; the
Resources page lists US Core 3.1.1 and 6.1.0 profiles per resource and
says *"Veradigm EHR version 26.0 supports these FHIR resources. For
versions earlier than 26.0, consult the Capability Statement."* JSON
and XML are both served; JSON is the default. Veradigm does not
support FHIR STU3 at all.

### Population-scale reads: Group export, not per-type search

Veradigm's own framing: *"The Veradigm FHIR API is used to work with
clinical data for a single patient or small group of patients. However,
it is not technically feasible to use the usual Veradigm FHIR API
requests to pass bulk data because of the number of requests and system
resources involved. To support these cases, bulk data requests are
available."*

Concretely for `core/fhir/scheduler.py` (paged search per type):

- Every clinical resource's documented searches are patient-anchored
  (Resources page: `Observation?patient=`, `Condition?patient=`,
  `DocumentReference?patient=` ...), and the sandbox CapabilityStatement
  lists **required search-parameter combinations** per resource
  (Observation: patient+category, patient+code, ...; Encounter:
  patient+status, ...). Patient itself searches by name, birthdate,
  gender, identifier - the required combinations are gender+name and
  the like. An unanchored `GET {base}/{Type}?_count=50` - the first-run
  shape of `iter_resources()` - is not a documented query for any
  clinical type.
- The one unanchored example Veradigm does document (Searching page) is
  `GET [FHIR path]/Observation?_lastUpdated=ge2022-03-01`, together with
  `_count` and `_id`. That is the shape an incremental cycle sends
  (`_lastUpdated=gt...&_count=`). Whether it works for every type is
  not documented by the vendor; confirm on the sandbox.
- Documented search errors: `401`, `403`, `404`, `413 Request too
  large`. Data may also be returned *"marked as 'redacted' ... when the
  Veradigm Clinical Authorization Service determines that the person
  requesting the data is not authorized to view it"* - a redacted
  resource is not an error and will be stored as returned.
- Provenance cannot be searched at all (*"If you have the resource _ID
  value, you can perform a GET, but you cannot search"*), which is why
  it is not in `supported_resources` although `$export` includes it.

For a full population, use `$export` (next section).

### Bulk Data Export

Every item below is from the Bulk Data page unless marked "sandbox".

- **Group-level only.** `[FHIR path]/Group/INF-101/$export` - *"Get all
  the patients in the Group resource with the ID INF-101."* The sandbox
  advertises `export` (`OperationDefinition/group-export`) on Group and
  on no other resource, and instantiates
  `hl7.org/fhir/uv/bulkdata/CapabilityStatement/bulk-data`. No
  system-level or Patient-level export is documented.
- **Who may call it.** *"Only FHIR applications of the type System can
  send bulk data requests. Patient and User application types cannot
  send bulk data requests."* And: *"Backend authentication for access
  tokens via JWKS must be configured."*
- **Where the Group ID comes from.** *"Before an application can request
  bulk data, they must know the ID of the Group resource they are
  requesting. Group resources are created by organizations in Veradigm
  EHR. Veradigm EHR uses segments in the Reporting module to create
  Group resources."* So `PHI_AI_FHIR_GROUP_ID` is something the
  organization builds and hands over. Unlike some vendors, Group is
  searchable here (`Group?characteristic=`, `?type=`, `?member=`), so a
  candidate list is at least enumerable with the token.
- **The async handshake.** Kickoff returns *"a 202 Accepted HTTP status
  code and a URL in the Content-Location header"*. While in progress a
  status GET returns *"Status: Accepted. X-Progress: Percentage
  complete. Retry-After: Suggested duration of time until the next
  status request. This is measured in seconds. If a status request is
  made prior to the retry-after date/time, the FHIR API responds with a
  HTTP 429 Too Many Requests error."* `bulk_client.poll_status()` logs
  `X-Progress` but does **not** read `Retry-After` and raises on any
  status other than 200/202 - so `PHI_AI_BULK_POLL_INTERVAL_SECONDS`
  must be at or above the `Retry-After` the site actually returns. The
  default (600 s) is a guess; the vendor publishes no figure.
- **Completion.** *"Status: OK"*, an **`Expires`** header (*"Time when
  the export package will expire and thus will no longer be
  available"*), and a manifest whose example carries
  `requiresAccessToken: true`, one or more NDJSON `output` files per
  type, and possibly `error` files (*"An export request can complete
  successfully when some of the data was successfully outputted but
  some was not"*). `wait_for_export()` logs manifest-level errors and
  returns the manifest; `iter_ndjson_resources()` sends the bearer
  token. Download promptly: *"You can download the file packages as
  many times as necessary, but once they expire, they are no longer
  available."* The expiry length is not documented by the vendor.
- **Delete.** `DELETE [Content Location URL]` - `delete_export()` does
  this after processing.
- **Provenance.** *"Provenance is included by default for those
  requests that do not specify which resource to include"*; when
  `_type` is given, Provenance arrives only if listed, and *"If
  provenance is passed as a requested resource, all other resources that
  are included in the request should then include provenance."*
  `bulk_scheduler.py` passes the profile's `supported_resources` as
  `_type`, so by default a Veradigm export carries **no** Provenance;
  add it to the type list if the retention purpose needs it.
- **Exportable types (25 listed on the page as fetched 2026-09-01):**
  AllergyIntolerance, CarePlan, CareTeam, Condition, Binary, Device,
  DiagnosticReport, DocumentReference, Encounter, Goal, Group,
  Immunization, Location, Medication, MedicationAdministration,
  MedicationRequest, MedicationStatement, Observation, Organization,
  Patient, Practitioner, PractitionerRole, Procedure, Provenance,
  RelatedPerson. Four of these (Binary, MedicationAdministration,
  MedicationStatement, Provenance) are not on the R4 REST resource list;
  an export can deliver types a search cannot.
- **Not documented by the vendor:** `_since`, any export-frequency
  limit, group-size guidance, file retention period, poll cadence. The
  24-hour `bulk_scheduler.py` default interval is not a Veradigm number;
  nothing published says a second export the same day is refused, and
  nothing says it is allowed.

### Writes

**In: yes. Out: no.** *"The Veradigm FHIR API is limited to read-only
access."* (Process Overview, "Functionality Considerations"). The
profile's `writable_resources` is empty on the vendor's own word, and
this connector is ingestion-only.

Two things an operator will still run into:

1. **The sandbox CapabilityStatement advertises `create` and `update`**
   on twelve types - Condition, AllergyIntolerance, MedicationRequest,
   Observation, Immunization, DocumentReference, MedicationStatement,
   MedicationAdministration, ServiceRequest, Questionnaire,
   QuestionnaireResponse, MedicationDispense (never Patient) - while no
   Veradigm page documents a create request, a body, or a system write
   scope; the sandbox's `scopes_supported` has `system/*.read` and
   `system/*.rs` and no system write scope at all. Because
   `core/fhir/delivery/writer.py` refuses only what the live
   CapabilityStatement does not advertise, against such an instance it
   **will attempt** POSTs for those types. Expect them to be refused by
   scope (403 is in Veradigm's documented error set); treat any 201 as
   undocumented behaviour to raise with VeradigmConnect@veradigm.com,
   not as a write path. `--confirm` is required before anything is
   sent, and a dry run lists exactly which types the destination
   advertised.
2. **The real write path is Unity.** *"Veradigm Connect offers the
   bidirectional Unity API, enabling both reads and writes."* Unity is
   proprietary (not FHIR), Integrator-tier, and a second client - not
   this profile. Until a deployment builds that client, deliver to a
   Veradigm organization as files for their own tooling.

Conditional create (`If-None-Exist`) and `$import` are not documented
by the vendor; both flags are False.

### Tiers, fees and support

From the portal's Learn More page (`developer.veradigm.com/Home/LearnMore`):
the Open tier is free - *"Open accounts are free to build and deploy by
anyone"*, with *"no upfront fees"* - and covers the FHIR APIs;
Integrator tiers (Bronze to Platinum Plus, monthly or annual) add the
SDK, Unity, EHR-launch sandbox testing and certification. Which portal
sections apply only to Integrator tiers, and whether certification is
withheld from Open accounts, could not be re-verified on a fetchable
Veradigm page (the Additional Information page returned nothing) - not
documented by the vendor on the pages cited here; confirm with
Veradigm. A Veradigm-certified listing on the App Expo is a marketing
step, not a connectivity one. No per-call or per-export fee is
documented by the vendor. Support and questions:
VeradigmConnect@veradigm.com.

### Validate before ingesting

`VERADIGM.supported_resources` is the vendor's published EHR 26.0 list
narrowed to clinical record types, not a promise about one site. Before
pointing a run at an organization:

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

Look for: `fhirVersion` 4.0.1 (a Versionless site may answer DSTU2
without the `fhirVersion=4.0` Accept parameter); the `oauth-uris`
`token` URL (that is `PHI_AI_FHIR_TOKEN_URL`); `rest.resource[]` for
each type you ingest, with the `capabilitystatement-search-parameter-combination`
extensions telling you which searches are actually allowed;
`operation: export` on Group (bulk is available on this site);
`grant_types_supported` containing `client_credentials` and
`capabilities` containing `client-confidential-asymmetric`;
`scopes_supported` containing the `system/` scopes your registration
selected. A site older than EHR 26.0 may expose fewer types than the
portal lists.

### What the emulator reproduces

`python -m emulators --vendor veradigm` (port 9110):

- FHIR under `/fhirroute/fhir/CP-EMULATOR`, the sandbox's path shape.
- Token endpoint honours only the JWT client assertion. A
  `client_secret` (form or Basic header) gets `400 invalid_client`, as
  the real sandbox returned; an assertion whose header algorithm is not
  RS384 or RS256 (for example ES384 - Veradigm's JWKS rule is RSA only)
  gets `400 invalid_client`. Requests with `scope=system/*.read`, a
  per-type list, or no scope all succeed, because Veradigm documents the
  wildcard and documents no refusal for the others.
- Group-level `$export`: `202` + `Content-Location`, a first poll of
  `202` with `X-Progress` and `Retry-After`, then a manifest with
  `requiresAccessToken: true` and NDJSON files served as
  `application/fhir+ndjson`; `DELETE` on the status URL is accepted.
- CapabilityStatement advertises `create` for nothing, so a delivery
  dry run skips every type and a raw POST gets `422 not-supported`;
  `If-None-Exist` gets `412`.
- Pagination at 2 per page regardless of `_count`.

Not reproduced, and worth knowing: the `429` for polling before
`Retry-After`; the `Expires` deadline on export files; Provenance
appearing by default in an untyped export; the twelve `create`/`update`
interactions the real sandbox advertises despite the read-only
documentation; the `smart-configuration` document (the emulator's is
shared and hardcoded); and any real signature verification - the
emulator checks the assertion's algorithm header, not its signature.

### Setting it up

All steps use the non-PHI sandbox first. Paths below are relative to
the repository root; `python` is the repository's `.venv/bin/python`.
Nothing here writes to Veradigm - the FHIR API is read-only.

1. **Register at Veradigm.**
   1. Sign up (Open tier, free) at
      <https://developer.veradigm.com/Account/RegisterSelf>; accept the
      user agreement.
   2. Portal: **My Dashboard > My FHIR Applications > +**. Fill:
      - *App Name*: a name that identifies your organization and PHI
        AI - it is what a client sees in their License Management
        Portal.
      - *App Type*: **System** (bulk export is refused for any other
        type).
      - *Client Type*: **Confidential**. *App Type (native/web)*: Web.
      - *JWKS URL*: the public HTTPS URL from step 2 (you can save the
        app first and fill this after the key exists; the nightly sync
        only picks it up once it resolves).
      - *Redirect URLs*: `urn:ietf:wg:oauth:2.0:oob` (documented
        placeholder; unused by a System app). *Launch URLs*: leave
        empty.
   3. Save. Record the **Client ID** (this is `PHI_AI_FHIR_CLIENT_ID`).
      Note the Secret Expiration Date but do not put the Secret in
      `.env` - the documented System token request does not use it.
   4. On the FHIR App page choose a **Purpose of Use** and the
      **scopes**: SMART v1 `system/...read` scopes only (or the
      `system/*.read` wildcard, which is what Veradigm's own token
      example sends). Do not mix `.read` and `.rs` - the app will not be
      approved.
   5. Request sandbox credentials through the form on
      <https://developer.veradigm.com/Fhir/FHIR_Sandboxes>. The sandbox
      base URL is public: 
      `https://fhir.fhirpoint.open.allscripts.com/fhirroute/fhir/CP00101/`.
   6. Only after steps 3-7 below succeed against the sandbox, click
      **Request Production Access**. Name, App Type and Purpose of Use
      are frozen from that point; Veradigm Connect reviews and
      approves; each client organization then activates the app
      itself in its License Management Portal and gives you its
      production base URL (or you take it from the Endpoint Directory).

2. **Generate the key pair and publish the JWKS.** Veradigm requires
   RSA, 2048-bit, `use: sig`, a unique `kid`, hosted on public HTTPS.
   Do not reuse `scripts/generate_epic_keypair.sh` (it makes 4096-bit
   keys; Veradigm says *"Keys should be 2048-bit RSA keys"*).
   ```bash
   openssl genrsa -out veradigm_private_key.pem 2048
   openssl rsa -in veradigm_private_key.pem -pubout -out veradigm_public_key.pem
   chmod 600 veradigm_private_key.pem
   python - <<'EOF'
   import json
   from cryptography.hazmat.primitives import serialization
   from jwt.algorithms import RSAAlgorithm
   pub = serialization.load_pem_public_key(open("veradigm_public_key.pem", "rb").read())
   jwk = json.loads(RSAAlgorithm.to_jwk(pub))
   jwk.update({"kid": "phi-ai-veradigm-2026", "use": "sig", "alg": "RS384"})
   json.dump({"keys": [jwk]}, open("veradigm_jwks.json", "w"), indent=2)
   EOF
   ```
   Host `veradigm_jwks.json` at an unauthenticated HTTPS URL you
   control (a raw GitHub URL of a public repository works; the same
   pattern as `deploy/aws/README_EPIC_JWKS.md`) and paste that URL into
   the app's **JWKS URL** field. The `kid` value becomes
   `PHI_AI_FHIR_JWT_KID`. Then wait: Veradigm's nightly job is what
   loads your JWKS into its OAuth clients, so the first token request
   that can succeed is the day after the URL is saved. On rotation, add
   the new key to the file, keep the old one until the new `kid`
   authenticates, then remove it. `alg` is set to RS384 because that is
   what `core/fhir/client.py` signs; Veradigm's sample shows RS256 and
   never states which RS algorithms it accepts - step 4 is where you
   find out.

3. **PHI AI environment** (`.env`; every other variable - cloud
   provider, buckets, KMS - as in `.env.example`, unchanged by the
   vendor):
   ```bash
   PHI_AI_EMR_VENDOR=veradigm
   PHI_AI_FHIR_BASE_URL=https://fhir.fhirpoint.open.allscripts.com/fhirroute/fhir/CP00101
   PHI_AI_FHIR_TOKEN_URL=https://fhir.fhirpoint.open.allscripts.com/fhirroute/authorizationV2/CP00101/connect/token
   PHI_AI_FHIR_CLIENT_ID=<Client ID from step 1.3>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/absolute/path/to/veradigm_private_key.pem
   PHI_AI_FHIR_JWT_KID=phi-ai-veradigm-2026
   # PHI_AI_FHIR_CLIENT_SECRET - leave unset: not part of Veradigm's documented System token request
   PHI_AI_FHIR_GROUP_ID=<Group ID the organization built from a Reporting segment>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600   # must be >= the Retry-After the site returns; polling early is a 429
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   PHI_AI_BULK_INTERVAL_SECONDS=86400      # not a Veradigm figure; the vendor documents no export frequency limit
   ```
   The token URL is the `oauth-uris` `token` value from the base URL's
   own `/metadata` - for a production site, take it from that site's
   CapabilityStatement, never from the sandbox line above. No
   `PHI_AI_FHIR_SCOPE`-style variable exists: the client sends no scope
   to Veradigm (see the Scopes section); step 4 tells you whether that
   is accepted.

4. **Pre-flight.**
   ```bash
   curl -s -H 'Accept: application/fhir+json; fhirVersion=4.0' "$PHI_AI_FHIR_BASE_URL/metadata" \
     | python -c 'import json,sys; d=json.load(sys.stdin); r=d["rest"][0]; \
       print("fhirVersion", d["fhirVersion"]); \
       print("token", [x["valueUri"] for e in r["security"]["extension"] for x in e["extension"] if x["url"]=="token"]); \
       print("group export", [o["name"] for x in r["resource"] if x["type"]=="Group" for o in x.get("operation",[])]); \
       print("types", sorted(x["type"] for x in r["resource"]))'
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" \
     | python -c 'import json,sys; d=json.load(sys.stdin); \
       print(d["grant_types_supported"]); print([c for c in d["capabilities"] if "confidential" in c]); \
       print([s for s in d["scopes_supported"] if s.startswith("system/*")])'
   ```
   Expect `fhirVersion 4.0.1`, the token URL you put in `.env`, `export`
   on Group, every type in `VERADIGM.supported_resources` present,
   `client_credentials` in the grants, `client-confidential-asymmetric`
   in capabilities and `system/*.read` in the scopes. Then the token
   request itself (the day after the JWKS URL was saved):
   ```bash
   python - <<'EOF'
   from core.config.settings import Settings
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for
   s = Settings.from_env()
   c = FHIRIngestionClient(base_url=s.fhir_base_url, profile=profile_for(s.emr_vendor),
                           storage=None, encryptor=None, audit=None, retention_years=s.retention_years)
   c.authenticate_from_settings(s)
   print("token OK, length", len(c.access_token))
   EOF
   ```
   `400 invalid_client` means the JWKS/kid/Client ID do not line up yet
   (or the nightly sync has not run); `400 invalid_request` with a
   correctly signed assertion means the request shape - most likely the
   missing scope. In that case re-run with
   `c.authenticate(s.fhir_client_id, s.fhir_private_key_pem, s.fhir_token_url, s.fhir_jwt_kid, scope="system/*.read")`;
   if that succeeds, record it in this chapter and raise the client
   change (the profile flag cannot send a wildcard). If RS384 is refused
   outright, that is the algorithm question from step 2.

5. **First ingest - Group export.**
   ```bash
   python -m core.fhir.bulk_scheduler --once
   ```
   Success looks like: `Kicking off bulk export: group=...`, one or more
   `Bulk export still in progress: <percent>` lines spaced
   `PHI_AI_BULK_POLL_INTERVAL_SECONDS` apart, then per-type NDJSON
   downloads, index writes and a status-0 exit. A `429` on the first
   poll means your interval is below the site's `Retry-After` - raise
   it. Because the profile records bulk support, you will **not** see
   the refusal `Veradigm's profile records no Bulk Data Export support`
   - that message is what a vendor without `$export` produces at
   startup; if you ever see it for Veradigm, the profile was edited.
   Without `PHI_AI_FHIR_GROUP_ID` the run stops at once with
   `PHI_AI_FHIR_GROUP_ID is not set` (its wording still names Epic's
   mailbox; for Veradigm the ID comes from the organization's Reporting
   segment). For an incremental paged cycle after that:
   `python -m core.fhir.scheduler --once` - and read the Population
   reads section first, because an unanchored first-run search is not a
   documented Veradigm query. Verify: `python -m core.verify` (add
   `--export-dir <dir>` to check a downloaded export against the
   profile's expected types).

6. **First delivery (what a write attempt does).**
   ```bash
   python -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" --vendor veradigm \
     --identity-map map.csv --purpose-of-use "rehearsal" --patient <source id>
   ```
   Against your own source URL this exits `4 REFUSED` - delivery to any
   source EMR is blocked by design. Against a different Veradigm site
   the dry run reads its CapabilityStatement and prints what it would
   send. Because Veradigm's sandbox advertises `create` on twelve
   types, the dry run can list them; with `--confirm` the POSTs would go
   out and, per Veradigm's read-only statement and its scope list, be
   refused (expect 403). Do not treat a 201 as a write path - report it
   to VeradigmConnect@veradigm.com. Writing into Veradigm is Unity, a
   separate client this repository does not have.

7. **Local rehearsal.**
   ```bash
   python -m emulators --vendor veradigm          # 127.0.0.1:9110/fhirroute/fhir/CP-EMULATOR
   python -m pytest tests/test_emulator_integration.py -k veradigm -q
   python -m pytest tests/test_delivery.py -q
   ```
   Point `.env` at
   `http://127.0.0.1:9110/fhirroute/fhir/CP-EMULATOR` with token URL
   `http://127.0.0.1:9110/oauth2/token` to run steps 4-6 end to end on
   synthetic data; the emulator refuses a client secret and any non-RSA
   assertion, serves Group `$export`, and advertises create for nothing.

8. **Known limits and where to confirm them.**
   - ONC certification (what g(10) obliges the product to offer):
     Veradigm EHR 26, certificate 15.04.04.2891.Vera.26.14.1.251231,
     Drummond Group, 2025-12-31, criteria including (g)(10) -
     <https://veradigm.com/img/legal/onc/Compliance-Certificate-Veradigm-EHR-26-020426.pdf>,
     linked from <https://veradigm.com/legal/onc-reg-compliance/>. The
     CHPL listing for that certificate number is the authoritative
     record of the registered API documentation URL.
   - Not documented by the vendor (ask before relying on any of them):
     signing algorithms accepted; whether a scope-less token request is
     accepted; system token lifetime; `_since`; export frequency
     limits; export file expiry; page-size ceiling; any REST rate limit.
     Contact: VeradigmConnect@veradigm.com; sandbox credential and
     provider-login forms on
     <https://developer.veradigm.com/Fhir/FHIR_Sandboxes>.
   - Endpoint Directory (production base URLs, per organization;
     portal login): <https://developer.veradigm.com/Fhir/EndpointDirectory>.

## Practice Fusion

Primary sources: Practice Fusion's own FHIR developer pages -
[FHIR API - Get Started](https://www.practicefusion.com/fhir/get-started/),
[FHIR API Specifications](https://www.practicefusion.com/fhir/api-specifications/),
the [FHIR Developer Sandbox](https://www.practicefusion.com/fhir/api-specifications/sandbox-documentation/),
the published [Service Base URLs](https://www.practicefusion.com/assets/static_files/ServiceBaseURLs.json)
(a FHIR Bundle of Organization and Endpoint resources), the
[PDS API Terms of Service](https://www.practicefusion.com/pds-api/termsofservice/)
("Application Access Developer Agreement", revised 2023-03-31), the
[Certified EHR API Fees workbook](https://www.practicefusion.com/assets/misc/API-Fees-ONC-Cert-Criteria-for-Health-IT_May-2024.xlsx),
the [ONC Certified EHR page](https://www.practicefusion.com/onc-certified-ehr/),
and Practice Fusion's own explainer
[FHIR and Practice Fusion: Everything You Need to Know](https://www.practicefusion.com/blog/fhir-integration-guide/)
(2024-02-29). Every fact in this chapter was read from those pages on
2026-09-01. Nothing here is carried over from Epic, and nothing comes
from Veradigm EHR's documentation. Each section separates what is
**verified from the vendor** from what **must be confirmed on the
practice's instance**.

### Which product

Practice Fusion is a Veradigm Network product, but it has its own EHR,
its own FHIR API, its own developer registration (the "PDS API
Portal") and its own fee schedule. `developer.veradigm.com` documents
Veradigm EHR and is the wrong source for this connector - the two
platforms share an owner, not an API.

One thing the published CapabilityStatement gives away: the FHIR server
calls itself `NXT` ("NXT API conformance statement", software name
`NXT`, publisher `MedicaSoft, LLC`, dated 2025-06-12). The FHIR facade
is a third-party product in front of the EHR. When a behaviour looks
unlike the EHR's own screens, that is why.

### The per-practice model

Verified from the vendor: there is no single Practice Fusion endpoint,
and no federated instance per health system either. Every practice has
its own base URL. The vendor publishes them all in `ServiceBaseURLs.json`
- 3,422 Organizations at fetch time, each with two Endpoints:

- **Patient Access** - `https://api.patientfusion.com/fhir/r4/v1/{id}`
  (Patient Fusion portal) or
  `https://api.practicefusion.com/fhir/r4/v1/fmh/...` (FollowMyHealth
  portal). Patient-app flows; **not this connector**.
- **Provider / System Access** -
  `https://api.practicefusion.com/fhir/r4/v1/{practice-guid}`. This is
  the base URL a system (bulk) app uses, and the one
  `PHI_AI_FHIR_BASE_URL` takes.

Each Organization entry carries the practice guid under
`urn:oid:2.16.840.1.113883.3.3388.3.1` next to the practice NPI, so a
practice can be looked up in the Bundle by NPI when the practice does
not know its own guid.

The token endpoint is per practice too: `{BaseURL}/token`, as published
by `{BaseURL}/.well-known/smart-configuration` (alongside
`authorization_endpoint`, `introspection_endpoint`, and a `jwks_uri` of
`{BaseURL}/.well-known/jwk` that is the **server's** key set, not where
yours goes). Practically: ingesting from three practices means three
base URLs, three token URLs and three JWT `aud` values - not three
codebases.

### Registration and review

Verified from the vendor (Get Started page):

1. Complete the **PDS API Partner Registration Form** at
   `https://pfpds.practicefusion.com/s/Registration` with developer and
   company details.
2. Practice Fusion emails login credentials for the **PDS API Portal**.
3. In the portal, select Application and complete the **Partner
   Application form**: application name and description, homepage URL,
   Data Usage / Privacy policy, application type, **JWKS URL** ("Required
   if your application will use asymmetric key pair authentication with
   signed JSON Web Tokens"), redirect and launch URLs (provider/patient
   apps only), and **requested scopes**.
4. "API credentials will be delivered to you via your PDS API Portal."

The application type to choose is **System or Bulk export** -
"third-party applications that may request large practice level data
exports". The other two types (Patient; Provider, i.e. SMART standalone
or EHR launch) are interactive and are not what unattended ingestion
needs. The sandbox page adds: "To use Practice Fusion's bulk FHIR export
API, an app must be registered as a system app."

Review: the developer agreement says the developer tests the
integration and remedies defects first, after which "Practice Fusion
may in its sole discretion enable the Integration"; the sandbox itself
is gated on the app being "approved and available in the Practice
Fusion App Marketplace". The agreement also binds the developer not to
"submit or make available to Practice Fusion any Individually
Identifiable Health Information other than sufficient information to
identify a patient" - consistent with the read-only surface below.

Must be confirmed with the vendor (not documented): review timelines;
whether the application type or JWKS URL can be changed after approval.
Plan the JWKS URL as permanent - key rotation happens *inside* the JWKS
(a new `kid`), not by re-registering.

### Practice authorisation inside the EHR

Verified from the vendor - this is the gate Epic does not have and the
one most likely to be missed:

- "Before providers and patients can use any FHIR applications,
  Practice Fusion administrators must enable FHIR capabilities from
  within the EHR."
- "Once FHIR has been enabled within the EHR, a practice administrator
  must approve all FHIR apps individually before they can be used."
- "To authorize a system/bulk export app, a practice administrator must
  select the Authorize App button from the application details view."
- API Specifications: "The practice needs to authorize system
  applications in the EHR before the applications can begin requesting
  access tokens for retrieving FHIR resources of the practice. System
  applications can only request scopes that have been authorized by the
  EHR user."

Consequence for this codebase: the scopes requested at registration,
the scopes the practice administrator authorises, and
`PRACTICEFUSION.supported_resources` should be the same 22-type set,
because the token request is derived from that tuple (next two
sections). Must be confirmed on the instance: what the token endpoint
does with a scope the practice did not authorise - refuse the request,
or grant the subset. The vendor documents neither.

### Auth: JWT client assertion with a required `kid`, no secret

Verified from the vendor (API Specifications, "System Apps"; sandbox
page). System apps use the 2-legged `client_credentials` grant with a
`client_assertion` JWT - "no secret is required, though the JWT should
be signed with a key whose public portion is published via a JWKS URL
that is registered for the app".

The assertion, as documented:

| Part | Field | Practice Fusion's text |
|---|---|---|
| header | `alg` | Required - "JWA algorithm (e.g., RS384, ES384) used for signing the authentication JWT" |
| header | `kid` | Required - "identifier of the key pair used to sign this JWT; this identifier SHALL be unique within the client's JWK Set" |
| header | `typ` | Required - fixed `JWT` |
| header | `jku` | Optional - when present "SHALL match the JWKS URL value that the client supplied to the FHIR authorization server at client registration time" |
| claim | `iss`, `sub` | the client_id |
| claim | `aud` | "FHIR Base URL's Token Endpoint" - i.e. `{BaseURL}/token`, per practice |
| claim | `exp` | "should not be > 300 seconds in the future" |
| claim | `jti` | "A nonce string value that uniquely identifies this authentication JWT" |

Token request body (`POST {BaseURL}/token`, form-encoded):
`grant_type=client_credentials`, `scope`, `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
`client_assertion`. Response: `token_type` ("Will always be Bearer"),
`scope` ("Permissions Granted"), `expires_in` ("Duration in seconds";
the worked example shows `3600`), `access_token`.

How the codebase maps onto this:

- `FHIRIngestionClient.build_client_assertion()` already produces this
  shape: RS384, `iss`/`sub` = client id, `aud` = `PHI_AI_FHIR_TOKEN_URL`,
  unique `jti`, `exp` = now + 240 s (inside the 300 s ceiling), and
  `kid` from `PHI_AI_FHIR_JWT_KID`. **Set `PHI_AI_FHIR_JWT_KID`**: here
  `kid` is Required, and the client only emits it when the variable is
  set.
- The profile's `assertion_algorithm` stays at the RS384 default because
  RS384 is inside the vendor's own example list. ES384 is named there
  too; the emulator honours both.
- `PHI_AI_FHIR_CLIENT_SECRET` is not read for this vendor
  (`auth_flow="smart_backend_services"`), so a stray secret is inert.
- Practice Fusion's `.well-known/smart-configuration` advertises
  `client-confidential-asymmetric`, `permission-v1`, `permission-v2`
  and `grant_types_supported` including `client_credentials`; it also
  lists `client-confidential-symmetric`, which applies to the
  interactive provider/patient app types, not to system apps.

Must be confirmed on the instance (not documented): the HTTP status and
error body for a rejected assertion; any key-length requirement (the
vendor states none - RFC 7518 §3.3 sets the floor at 2048-bit RSA for
RS384); whether `jku` is checked when present.

### Scopes

Verified from the vendor: the complete list of system scopes is 22
types, published twice - SMART V1 `system/{Type}.read` and SMART V2
`system/{Type}.rs` - for AllergyIntolerance, CarePlan, CareTeam,
Condition, Coverage, Device, DiagnosticReport, DocumentReference,
Encounter, Goal, Immunization, MedicationDispense, MedicationRequest,
Observation, Organization, Patient, Practitioner, Procedure, Provenance,
RelatedPerson, ServiceRequest, Specimen. There is no wildcard form in
the list.

`PRACTICEFUSION.requires_token_scopes` is therefore `True`:
`authenticate_from_settings()` derives `system/{Type}.read` for every
entry of `supported_resources` and sends it - the V1 spelling the
vendor publishes and `permission-v1` covers. Request exactly these 22
scopes at registration and have the practice authorise all of them;
the profile cannot be trimmed per deployment.

Must be confirmed on the instance: compare the `scope` the token
response returns with the one requested on the first live token. If a
type is missing, the practice authorised less than the profile lists,
and that type's reads (and its share of `$export`) will fail or be
absent.

### Resources

Verified from the vendor: "FHIR resources return USCDI V3 data classes
and elements and are implemented using US Core v6.1.0 Profiles."
The published CapabilityStatement lists roughly fifty types for
`read` and `search-type` (including MedicationAdministration,
ExplanationOfBenefit, Location, List, Task, Questionnaire,
QuestionnaireResponse, Composition, Contract, EpisodeOfCare,
FamilyMemberHistory, Flag, ImagingStudy, Medication, Substance,
AuditEvent, PractitionerRole, HealthcareService, InsurancePlan and
`Binary` read). Only the 22 types above have a **system scope**, so
only those are in `supported_resources` - a system app cannot obtain a
token for the rest, however the CapabilityStatement reads.

Two vendor-documented specifics:

- **Continuity of Care Documents.** "A Continuity of Care Document
  (CCD) is automatically generated when a patient encounter is signed
  in Practice Fusion. To request these existing CCDs, query the FHIR
  server's DocumentReference endpoint for type 34133-9" -
  `GET {BaseURL}/DocumentReference?type=34133-9`, with the document
  body reachable through the DocumentReference's attachment.
- **Group** is the one type advertised with `create`, `update`,
  `search-type` and the `export` operation - see Bulk and Writes below.

Must be confirmed on the instance: the practice's own
`GET {BaseURL}/metadata`. The statement on the vendor's page is a
generic `NXT` statement, not that practice's. Search parameters per
type are published, but `_count` and `_lastUpdated` are not among them
(see "Paged search").

### Population-scale reads: Bulk Data at practice scope

Verified from the vendor (API Specifications, "Bulk-Data Access for
System Apps"; Get Started: "Bulk Data Access v1.0.1"):

- **Two kickoffs.** `GET {BaseURL}/Patient/$export` - "bulk data exports
  for all patients in the practice represented in the base URL" - and
  `GET {BaseURL}/Group/{GroupId}/$export` - "a subset of patients in
  the practice". The scope of an export is the practice; there is no
  cross-practice or system-wide export because there is no
  cross-practice base URL.
- **Groups are defined in the EHR by the practice.** "The practice will
  first need to define a group of patients in the EHR and provide the
  associated group ID value" (step-by-step at
  `help.practicefusion.com/s/article/what-is-a-fhir-group-and-how-do-i-create-one`),
  and **"Groups are limited to a maximum of 1,000 patients each."**
  Set the id as `PHI_AI_FHIR_GROUP_ID`. It comes from the practice, not
  from Practice Fusion.
- **Handshake.** Kickoff header `Accept: application/fhir+json`;
  response `202 Accepted` with `Content-location: {BaseURL}/Export/{BulkExportGuid}`
  and no body. Status `GET {BaseURL}/Export/{guid}`: `202` with no body
  while in progress; `200` with the manifest (`transactionTime`,
  `request`, `requiresAccessToken: true`, `output[]` of `{type, url}`,
  `error[]`) when complete. `DELETE {BaseURL}/Export/{guid}` returns
  `202`. Output URLs are shaped
  `{BaseURL}/Binary/export/{guid}/{type}/{n}`.
- The vendor's Patient/$export manifest example lists eighteen types,
  including **Location**, which has no system scope - expect manifest
  entries for types you did not (and cannot) request.

How the codebase maps onto this: `core/fhir/bulk_client.py` speaks
exactly this handshake - kickoff, `Content-Location`, 202-until-200
polling, manifest, per-file download, delete - and
`core/fhir/bulk_scheduler.py --once` drives it. Two differences to
know:

- `bulk_client.kickoff_export()` calls **only** the Group form. The
  vendor's all-patients form (`Patient/$export`) is an integrator
  follow-up (a kickoff-level option), not a profile switch. Until then
  a whole practice larger than 1,000 patients needs more than one
  Group.
- `bulk_client` sends `Prefer: respond-async` (absent from the vendor's
  header table but required by the Bulk Data IG the CapabilityStatement
  instantiates) and `_type` (not in the vendor's documentation at all).

Must be confirmed on the first live export - **none of this is
documented by the vendor**: whether `_type` and `_since` are honoured or
ignored; whether the 202 carries `Retry-After` or `X-Progress`; 429
behaviour when polling; any limit on how often a kickoff may be
repeated for the same practice or group; how long output files persist;
and the **file format** - the vendor's "Retrieve Output (FHIR resource)"
example shows a single pretty-printed CarePlan with
`Content-Type: application/json`, while `iter_ndjson_resources()` parses
`application/fhir+ndjson` line by line. Inspect the first output file
before trusting a full run. `bulk_scheduler.py`'s 24-hour default
interval is this platform's choice; Practice Fusion documents no
kickoff throttle, so keep the daily cadence unless the practice agrees
to more.

### Paged search as the fallback

`core/fhir/scheduler.py --once` pages `GET {BaseURL}/{Type}?_count=50`
per type and, on later runs, adds `_lastUpdated=gt{watermark}`. Verified
from the vendor: `_id` and the usual clinical search parameters are
published per type. **Not documented by the vendor**: `_count`,
`_lastUpdated`, whether an unanchored `GET {BaseURL}/Patient` returns
the whole practice, and whether unknown parameters are ignored or
rejected. Treat paged search as the fallback for a practice whose
group cannot be defined, and confirm those three behaviours on the
instance before relying on incremental runs.

### Writes

**The Practice Fusion FHIR surface is not writable for clinical data.**
Verified from the vendor's own explainer: "FHIR apps are never
bidirectional in their data access ability" and "Although FHIR apps can
read patient information, they cannot change or write over EHR data."

The one exception in the CapabilityStatement is **Group**
(`create`, `update`) - the bulk-export cohort container, not clinical
content - and the vendor's narrative documents Group definition only
inside the EHR by the practice. `PRACTICEFUSION.writable_resources` is
therefore empty and the emulator advertises `create` for nothing.

What `core/fhir/delivery/writer.py` will do against a Practice Fusion
destination: read the live CapabilityStatement, find `create` advertised
for Group alone, and **skip every clinical type** with "the destination
does not advertise create for {Type}". Because
`supports_conditional_create` is `False`, a non-dry run also refuses to
proceed without `--allow-duplicates`. Nothing is ever written.

The real path into a Practice Fusion practice is outside FHIR: deliver
records as files for the practice's own import, or - for laboratory and
imaging partners only - Practice Fusion's separate
[partner Lab API](https://www.practicefusion.com/labs-documentation/)
(HL7 v2 results into the EHR and orders out, with its own partnership
onboarding). That is a second client, not this profile, and it carries
lab results, not a patient's prior clinical history.

### Limits and fees

Verified from the vendor:

- **Fees.** API fee workbook (May 2024): "Practice Fusion does not
  charge API fees for development, deployment or upgrade at this time"
  and "does not charge API fees for API usage at this time, but may add
  these at some point in the future. Notice will be provided in such
  event." Developer agreement: "The Developer/API User does not pay fees
  for use of the Application Access APIs", while "Practice Fusion
  customers shall pay to Practice Fusion the then standard Transaction
  Fee if any". Explainer: "At present, FHIR is complimentary to all
  Practice Fusion clients." The Veradigm subscription tiers in the same
  workbook apply to Veradigm/Unity marketing programmes, not to using
  this API.
- **Group size** - 1,000 patients maximum.
- **Assertion lifetime** - `exp` no more than 300 s ahead; access
  token `expires_in` shown as 3600 s.

Not documented by the vendor (profile keeps conservative defaults):
FHIR API rate limits (the legacy non-FHIR PDS guide's "HTTP 429" is a
different API and is not transferred), page size, bulk file retention,
kickoff frequency, and concurrency. `rate_limit_per_min=60` is a
client-side courtesy throttle only.

### Sandbox

Verified from the vendor: a **shared** developer sandbox exists, with a
system-app test base URL, a "Bulk Data Testing Group ID" referencing
nine test patients, and test patient logins for the two patient
portals (published on the vendor page; not copied here). Its
prerequisites are the part that matters for planning:

- "App Approval: Your application must be approved and available in the
  Practice Fusion App Marketplace"
- "Production Credentials: You must use your production credentials to
  access the sandbox environment"

So there is **no pre-approval test tenant**. Before approval the
emulator (`emulators/`, port 9111) is the only rehearsal. After
approval, expect the sandbox to be "periodically reset", which "may
result in changes to test patient logins, group IDs, and other
information", and to be shared with other developers. Provider apps and
EHR launches are not supported in the sandbox at all.

### Certification

Verified from the vendor: Practice Fusion EHR 3.7 is certified under the
ONC Health IT Certification Program - CHPL ID
`15.04.04.2924.Prac.37.01.1.240826`, certified 2024-08-26 by Drummond
Group, with 170.315(g)(10) among the criteria (the vendor's ONC page and
the Drummond certificate it links). The (g)(10) mandate - SMART Backend
Services for system scopes, multi-patient group data, publicly reachable
documentation - is the regulatory backstop for everything above, but
nothing in the profile rests on the mandate alone: every value has a
vendor citation.

### Validate before ingesting

```
GET {BaseURL}/.well-known/smart-configuration
GET {BaseURL}/metadata
```

Look for `token_endpoint` equal to `PHI_AI_FHIR_TOKEN_URL`,
`client_credentials` in `grant_types_supported`,
`client-confidential-asymmetric` and `permission-v1` in `capabilities`;
`fhirVersion` 4.0.1, `instantiates` including the bulk-data
CapabilityStatement, `Group` carrying the `export` operation, and each
of the 22 profile types with `read` and `search-type`. Then take one
token and compare its returned `scope` with the request; then kick off
one export against the practice's test group and read the first output
file by eye before the first full run. A type present in the profile but
absent from the practice's CapabilityStatement or from the granted scope
is the practice's configuration, not a platform defect.

### What the emulator reproduces

`emulators/` on port 9111 (`fhir_path` `/fhir/r4/v1/EMULATOR-PRACTICE`):

- Token endpoint honours `client_credentials` with a JWT
  `client_assertion` only; a `client_secret` gets `400 invalid_client`
  ("requires a signed JWT client assertion, not a client secret").
- Assertions signed RS384 or ES384 are honoured (both named by the
  vendor); any other `alg` is refused as `invalid_client`.
- A token request with no `scope`, or a wildcard scope, gets
  `400 invalid_scope` - the practice-authorised, per-type scope model.
- `$export` at Patient, Group and system level: `202` +
  `Content-Location`, first poll `202`, then the `200` manifest, NDJSON
  files, `DELETE` -> `202`.
- The CapabilityStatement advertises `create` for nothing, so the
  delivery capability check skips every type; `If-None-Exist` gets
  `412 not-supported`.
- Pagination at 2 per page regardless of `_count`.

Not reproduced, so the live instance must be checked for them: the
per-practice token URL (`{BaseURL}/token`; the emulator keeps
`/oauth2/token`), the practice administrator's "Authorize App" gate,
the 1,000-patient Group cap, the real CapabilityStatement's Group
`create`/`update`, the `Binary/export/...` output URL shape, and every
"not documented by the vendor" item above.

### Setting it up

Every URL and quoted requirement below is Practice Fusion's own; the
environment variable names are the ones `core/config/settings.py`
reads (`PHI_AI_` + suffix). "Non-PHI setup" here means the emulator and
the vendor's sandbox - the sandbox holds synthetic test patients only.

1. **Register the app with Practice Fusion.**
   1. Fill in the PDS API Partner Registration Form:
      `https://pfpds.practicefusion.com/s/Registration` (developer and
      company details; agree to the terms at
      `https://www.practicefusion.com/pds-api/termsofservice/`).
   2. Wait for the email with your **PDS API Portal** login.
   3. In the portal choose Application and complete the Partner
      Application form. Application type: **System or Bulk export**.
      Submit: application name and description, homepage URL, Data
      Usage / Privacy policy, your **JWKS URL** (step 2 below - the form
      needs it, so publish the JWKS first), and the **requested scopes**
      - request exactly these 22, the full system-scope list the
      vendor publishes, because the profile's token request will ask for
      all of them:
      `system/AllergyIntolerance.read system/CarePlan.read system/CareTeam.read system/Condition.read system/Coverage.read system/Device.read system/DiagnosticReport.read system/DocumentReference.read system/Encounter.read system/Goal.read system/Immunization.read system/MedicationDispense.read system/MedicationRequest.read system/Observation.read system/Organization.read system/Patient.read system/Practitioner.read system/Procedure.read system/Provenance.read system/RelatedPerson.read system/ServiceRequest.read system/Specimen.read`
   4. "API credentials will be delivered to you via your PDS API Portal"
      - the client id goes into `PHI_AI_FHIR_CLIENT_ID`. There is no
      client secret for a system app.
   5. Review: the agreement says you test first and then "Practice
      Fusion may in its sole discretion enable the Integration"; the
      sandbox is only usable once the app is "approved and available in
      the Practice Fusion App Marketplace". The vendor documents no
      timeline and does not say whether the app type or JWKS URL can be
      changed afterwards - treat both as fixed and rotate keys inside
      the JWKS by `kid`.
   6. For a real practice: a practice administrator must enable FHIR in
      the EHR, approve the app, and press **Authorize App** on the
      application details view, authorising the scopes above. Nothing
      works before that.

2. **Generate the key pair and publish the JWKS.** RS384 (the vendor's
   documented example algorithms are "RS384, ES384"; RS384 is what the
   client signs with). The vendor states no key length; RFC 7518 §3.3
   requires at least 2048 bits for RS384.
   ```bash
   openssl genrsa -out practicefusion_private_key.pem 2048
   openssl rsa -in practicefusion_private_key.pem -pubout -out practicefusion_public_key.pem
   chmod 600 practicefusion_private_key.pem
   ```
   Build the JWK Set (uses the repo's own PyJWT and cryptography; the
   `kid` is Required by the vendor and must be unique within the set):
   ```bash
   .venv/bin/python - <<'EOF'
   import json
   from cryptography.hazmat.primitives import serialization
   from jwt.algorithms import RSAAlgorithm

   pub = serialization.load_pem_public_key(open("practicefusion_public_key.pem", "rb").read())
   jwk = RSAAlgorithm.to_jwk(pub, as_dict=True)
   jwk.update({"kid": "phi-ai-practicefusion-2026-09", "alg": "RS384", "use": "sig"})
   json.dump({"keys": [jwk]}, open("jwks.json", "w"), indent=2)
   print("kid:", jwk["kid"])
   EOF
   ```
   Host `jwks.json` at a world-readable TLS URL (for example
   `https://<your-host>/.well-known/jwks.json`) and enter that URL as
   the JWKS URL in the Partner Application. Never commit or email the
   private key; mount it into the container.

3. **Configure PHI AI.** Add to `.env` (alongside the platform-wide
   `PHI_AI_CLOUD_PROVIDER`, `PHI_AI_STORAGE_*`, `PHI_AI_KMS_KEY_ID`,
   `PHI_AI_AUDIT_*` and database settings from `runbooks/RUNBOOK_INSTALL.md`):
   ```bash
   PHI_AI_EMR_VENDOR=practicefusion
   # The practice's "Provider / System Access" endpoint from
   # https://www.practicefusion.com/assets/static_files/ServiceBaseURLs.json
   PHI_AI_FHIR_BASE_URL=https://api.practicefusion.com/fhir/r4/v1/<practice-guid>
   # Per practice: the base URL plus /token (see {BaseURL}/.well-known/smart-configuration)
   PHI_AI_FHIR_TOKEN_URL=https://api.practicefusion.com/fhir/r4/v1/<practice-guid>/token
   PHI_AI_FHIR_CLIENT_ID=<client id from the PDS API Portal>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/practicefusion_private_key.pem
   PHI_AI_FHIR_JWT_KID=phi-ai-practicefusion-2026-09      # Required by the vendor
   # Group id the practice created in the EHR (max 1,000 patients per group)
   PHI_AI_FHIR_GROUP_ID=<group id from the practice>
   # PHI_AI_BULK_POLL_INTERVAL_SECONDS: leave at the default (600) - the vendor documents no
   # poll cadence and no Retry-After; a shorter interval is the operator's choice, not a vendor figure.
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   # Do NOT set PHI_AI_FHIR_CLIENT_SECRET - this vendor takes no secret and the setting is ignored.
   ```
   Scopes need no variable: `requires_token_scopes=True` in the profile
   makes `authenticate_from_settings()` derive `system/{Type}.read` for
   each of the 22 `supported_resources`. Load it:
   ```bash
   set -a; source .env; set +a
   ```

4. **Pre-flight against the practice.**
   ```bash
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" \
     | jq '{token_endpoint, grant_types_supported, capabilities}'
   curl -s -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/metadata" \
     | jq '{fhirVersion, instantiates, software, resources: [.rest[0].resource[] | {type, interaction: [.interaction[].code], operation: [.operation[]?.name]}]}'
   ```
   Look for: `token_endpoint` equal to `PHI_AI_FHIR_TOKEN_URL`;
   `client_credentials` in `grant_types_supported`;
   `client-confidential-asymmetric` and `permission-v1` in
   `capabilities`; `fhirVersion` `4.0.1`; `instantiates` including
   `.../bulkdata/CapabilityStatement/bulk-data`; `Group` with
   `operation: ["export"]`; every one of the 22 profile types with
   `read` and `search-type`. Then take one token with the repo's own
   assertion builder and compare granted with requested scope (the
   vendor documents no behaviour for an unauthorised scope, so this is
   the only signal):
   ```bash
   .venv/bin/python - <<'EOF'
   import requests
   from core.config.settings import Settings
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for

   s = Settings.from_env()
   p = profile_for(s.emr_vendor)
   assertion = FHIRIngestionClient.build_client_assertion(
       s.fhir_client_id, s.fhir_token_url, s.fhir_private_key_pem, s.fhir_jwt_kid)
   requested = " ".join(f"system/{t}.read" for t in p.supported_resources)
   r = requests.post(s.fhir_token_url, data={
       "grant_type": "client_credentials",
       "scope": requested,
       "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
       "client_assertion": assertion,
   }, timeout=30)
   print("status:", r.status_code)
   body = r.json()
   granted = set((body.get("scope") or "").split())
   print("expires_in:", body.get("expires_in"))
   print("not granted:", sorted(set(requested.split()) - granted) or "none")
   EOF
   ```
   A 400 here means the assertion (check `kid` and `aud`), the JWKS URL,
   or the practice's "Authorize App" step. Any scope under "not granted"
   is a type the practice did not authorise - fix it in the EHR, not in
   the profile.

5. **First ingest - bulk export of the practice's group.**
   ```bash
   python -m core.fhir.bulk_scheduler --once
   ```
   Success looks like, in order: `Authenticating to Practice Fusion FHIR
   endpoint`; `Kicking off bulk export for group <id> (22 resource
   types)`; `Bulk export kicked off, status URL: .../Export/<guid>`;
   one or more `Bulk export still in progress`; then `Stored <Type>: N
   resources` per type and exit 0. If every count is 0 while the
   manifest listed files, the output files are not NDJSON - the vendor's
   example shows one pretty-printed resource with `Content-Type:
   application/json` - and `iter_ndjson_resources()` needs adapting
   before a full run. Without `PHI_AI_FHIR_GROUP_ID` the scheduler exits
   1 with `PHI_AI_FHIR_GROUP_ID is not set` (the message's advice to
   email Epic is hand-written Epic wording; for Practice Fusion the id
   comes from the practice). The refusal `Practice Fusion's profile
   records no Bulk Data Export support` cannot appear as shipped
   (`supports_bulk_export=True`); it is what a wrongly edited profile
   would produce. The vendor's all-patients form `Patient/$export` is
   not called by `bulk_client.py` yet, so a practice above 1,000 patients
   needs several groups. Fallback, paged search per type:
   ```bash
   python -m core.fhir.scheduler --once
   ```
   (`_count` and `_lastUpdated` are not among the vendor's documented
   parameters - confirm the first incremental run returns what you
   expect.)

6. **First delivery - what a write attempt does.**
   ```bash
   PHI_AI_DELIVERY_CLIENT_ID=<client id> \
   PHI_AI_DELIVERY_TOKEN_URL=https://api.practicefusion.com/fhir/r4/v1/<practice-guid>/token \
   PHI_AI_DELIVERY_PRIVATE_KEY_PATH=/run/secrets/practicefusion_private_key.pem \
   python -m core.fhir.delivery \
     --destination https://api.practicefusion.com/fhir/r4/v1/<practice-guid> \
     --vendor practicefusion \
     --identity-map ./patient-mapping.csv \
     --patient <source patient id> \
     --purpose-of-use "Continuity of care, patient transferred to <practice>"
   ```
   The dry run reads the practice's CapabilityStatement, finds `create`
   advertised for Group alone, and skips every clinical type with `the
   destination does not advertise create for <Type>`. Adding `--confirm`
   without `--allow-duplicates` is refused because the profile records
   no conditional create. Nothing is written - by the vendor's own
   statement FHIR apps "cannot change or write over EHR data". Deliver
   as files for the practice's own import instead; the vendor's Lab API
   (`https://www.practicefusion.com/labs-documentation/`) is a separate
   HL7 v2 partner interface for laboratories, not a chart-history path.

7. **Local rehearsal on the emulator (the only rehearsal available
   before the app is approved).**
   ```bash
   python -m emulators --vendor practicefusion
   #   Practice Fusion   http://127.0.0.1:9111/fhir/r4/v1/EMULATOR-PRACTICE
   pytest tests/test_emulator_integration.py -k practicefusion -v
   pytest tests/test_delivery.py tests/test_smart_launch.py -k practicefusion -v
   ```
   To run the real schedulers against it, point `.env` at
   `PHI_AI_FHIR_BASE_URL=http://127.0.0.1:9111/fhir/r4/v1/EMULATOR-PRACTICE`,
   `PHI_AI_FHIR_TOKEN_URL=http://127.0.0.1:9111/oauth2/token` (the
   emulator's shared token route - the live one is `{BaseURL}/token`),
   any RSA key, and any `PHI_AI_FHIR_GROUP_ID`. Expect from the
   emulator: `invalid_client` for a client secret or a non-RS384/ES384
   assertion, `invalid_scope` for a scope-less or wildcard request,
   `$export` completing after one in-progress poll, and delivery
   skipping every type. Integrator note: the hand-maintained
   parametrize lists in `tests/test_emulator_integration.py` (bulk
   handshake, JWT-only vendors) and `tests/test_delivery.py` must
   include `practicefusion` for `-k` to select anything beyond the
   `sorted(VENDORS)` paging test.

8. **Known limits and where to confirm them.**
   - Not documented by the vendor: FHIR rate limits, page size,
     `_type`/`_since`, `Retry-After`/`X-Progress`, 429 handling,
     kickoff frequency, output-file retention and format, behaviour for
     an unauthorised scope, error shape of a rejected assertion, review
     timeline. Confirm each on the practice's instance and record what
     you find in the profile's `notes`.
   - Vendor-documented limits: groups of at most 1,000 patients;
     assertion `exp` at most 300 s ahead; sandbox shared and
     periodically reset; sandbox needs an approved app and production
     credentials.
   - Where to confirm: the API Specifications page
     (`https://www.practicefusion.com/fhir/api-specifications/`), the
     sandbox page
     (`https://www.practicefusion.com/fhir/api-specifications/sandbox-documentation/`),
     the knowledge base the vendor links for developers
     (`https://help.practicefusion.com/s/article/using-fhir-in-practice-fusion#fordevelopers`)
     and for Groups
     (`https://help.practicefusion.com/s/article/what-is-a-fhir-group-and-how-do-i-create-one`),
     the PDS API Portal itself (the vendor publishes no developer
     support email), the fee workbook
     (`https://www.practicefusion.com/assets/misc/API-Fees-ONC-Cert-Criteria-for-Health-IT_May-2024.xlsx`),
     and the certification record - `https://www.practicefusion.com/onc-certified-ehr/`
     (CHPL ID `15.04.04.2924.Prac.37.01.1.240826`; search "Practice
     Fusion" at `https://chpl.healthit.gov/` for the (g)(10) listing and
     its registered documentation link).

## TruBridge

Primary sources: the TruBridge FHIR developer portal at
[fhir-developer.plt.trubridge.com](https://fhir-developer.plt.trubridge.com/) -
a single-page application whose documentation pages are
`?page=api/overview`, `?page=api/endpoints`, `?page=api/backend-services`,
`?page=api/standalone-launch`, `?page=api/ehr-launch` and
`?page=supported-data` (a plain fetch of the root returns only a title;
the content lives in the page's JavaScript chunks, which is why earlier
research recorded the portal as "a title page") - its
[FHIR API Terms and Conditions](https://fhir-developer.plt.trubridge.com/assets/termsandconditions.pdf)
(revised 20 November 2025), its
[OpenAPI document](https://fhir-developer.plt.trubridge.com/openapi/openapi.yaml),
the vendor's [FHIR API page](https://trubridge.com/fhir-api/) and
[one-page FHIR API PDF](https://trubridge.com/wp-content/uploads/2026/06/TruBridge-FHIR-API.pdf),
its [certifications page](https://trubridge.com/certifications/), its
published [sandbox](https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4/.well-known/endpoint)
and [production](https://thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint)
endpoint directories, and the sandbox and production servers' own
`/metadata` and `/.well-known/smart-configuration`. Everything below was
fetched on 2026-09-01. No registered client exists yet: TruBridge issues
credentials by e-mail after review, so the token and bulk paths have been
exercised against the emulator only, and each section says which facts
are the vendor's and which must be confirmed on the facility.

### Which product, and the per-facility model

The portal's own words: "The TruBridge FHIR R4 API delivers read-only
access to USCDI v3 patient clinical data", and it "applies to the
following TruBridge products": **TruBridge EHR, Version 22** (CHPL
`15.04.04.3104.Thri.22.04.1.241210`,
[listing 11541](https://chpl.healthit.gov/#/listing/11541)) and
**TruBridge Provider EHR, Version 22** (`15.04.04.3104.Thri.PR.04.1.241210`,
[listing 11540](https://chpl.healthit.gov/#/listing/11540)). The
certifications page records the EHR certified by Drummond Group on
10 December 2024 with criteria including 170.315 (b)(10) and
(g)(2-7, 9-10). "Thri" is Thrive - the former CPSI/Evident hospital
product - and every CapabilityStatement the servers return still says
`software.name: cpsi-fhir-thrive` (5.5.80 on the day of writing),
`publisher: Evident`, "Evident implementation of FHIR on top of Thrive".
The hosts are `*.cpsi-cloud.com`. Do not read those names as a different
vendor.

TruBridge is a hosted, per-facility service, not a federation of
customer-run servers: one FHIR base URL per facility, all behind a
TruBridge-operated gateway. The production endpoint directory listed
**583** Organization/Endpoint pairs on 2026-09-01, each with an address
shaped

```
https://thrive-gw.cpsi-cloud.com/api/smart/{site}/id-osfac.{uuid}/fhir/r4
```

(`osfac` is a facility id; several site codes share one facility uuid).
Which facilities have an endpoint at all is stated in the PDF: "All
client facilities that participate in Promoting Interoperability and have
a MyCareCorner instance configured" - MyCareCorner is TruBridge's patient
portal, and a facility without one has no FHIR API to connect to. Confirm
that before contracting, not after.

The API is not a live query of the EHR. The PDF describes a repository:
"When an EHR user adds or updates USCDI patient clinical data within the
TruBridge EHR, a background service transfers the data to a queue for
delivery to the TruBridge FHIR API repository, ensuring near real-time
synchronization." `_lastUpdated` therefore reflects the repository, not
the chart; the scheduler's watermark comes from its own successful runs,
which is the right clock here.

*Must confirm on the facility:* that its Endpoint is `active` in the
directory, and what "near real-time" means in practice for the ingestion
interval.

### Registration and approval

The portal's Registration Process is three steps, verbatim: "Review and
accept the TruBridge FHIR API Terms and Conditions", "Review the
documentation herein", "Submit a registration request via email to
info@trubridge.com" (the portal's `mailto:` carries the subject
"TruBridge FHIR API"). There is no self-service console, no form and no
published turnaround; TruBridge assigns the client id and, for
asymmetric clients, records the JWKS URL: the client assertion is "signed
using a private key corresponding to one of the public keys published in
the backend service's JWKS document. This document is obtained via the
URL provided during the backend service's registration." Key *rotation*
is therefore a JWKS edit on your side; changing the JWKS *URL* or the
client id is a registration change, and TruBridge documents no
self-service way to do it - assume another e-mail and lead time.

Registration is not enablement. Terms s.4(b): "Your Developed App must be
approved in writing and registered for use by the applicable TruBridge
client before TruBridge will enable your Developed App within such
TruBridge client's environment, excluding direct-to-consumer applications
which do not need to be pre-approved by TruBridge clients." A backend
service is not direct-to-consumer, so **every facility approves in
writing**, and TruBridge enables the client per facility.

Other Terms that bind a deployment: s.2(a) registration collects contact
and organisation details; s.5 "Developer credentials (such as passwords,
keys, and client IDs)" are confidential, with prompt notice on
compromise; s.4(b) "If a secret is leaked, you will notify TruBridge
immediately"; s.2(c) no benchmarking, performance or availability
testing, and no disclosure of vulnerability-test results, without written
approval; s.4(c) TruBridge "may ... suspend, throttle or otherwise limit"
an app it believes threatens its clients' systems; s.9(b) TruBridge may
"discontinue the TruBridge FHIR API(s) or any portion or feature or your
access thereto for any reason and at any time"; s.15(a) no statement
suggesting TruBridge partnership or validation without written approval;
s.2(e) updates are announced only on the Developer Portal, and reviewing
it is the developer's responsibility. The Developer Sandbox "will not
include any actual patient data".

### Base URL discovery: the endpoint directory

TruBridge documents base-URL discovery, which no other vendor in this
file does. Verbatim: "TruBridge publishes a directory of organizations
and their FHIR endpoints" - sandbox
`https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4/.well-known/endpoint`,
production `https://thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint`.
"The endpoint directory is a JSON document containing a FHIR Bundle ...
Each entry is either a FHIR Organization resource or a FHIR Endpoint
resource. The FHIR Base URL for a given Organization resource is found in
the Endpoint resource referenced by that Organization." Find the
facility's Organization by name, follow
`Organization.endpoint[0].reference` (a `urn:uuid:`), and
`Endpoint.address` is `PHI_AI_FHIR_BASE_URL`.

Two things the page does not say: the production document is a real
`Bundle` (`resourceType`, `type`, `entry[].resource`), but the sandbox
document served on 2026-09-01 was a bare JSON array of the same
resources - parse both shapes. And the sandbox directory lists three
Organizations that all resolve to the single sandbox base URL
`https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4`.

The token endpoint is not derivable from the base URL and is on a
different host. "Every authorization workflow requires consultation of
the 'SMART Configuration' endpoint. The location of this endpoint is
convention-based: `{{FHIR Base URL}}` + `/.well-known/smart-configuration`."
Read `token_endpoint` from there; in production it is shaped
`https://thrive-oauth.cpsi-cloud.com/oauth/smart/{site}/id-osfac.{uuid}/token`,
in the sandbox `https://thrive-oauth-dev.cpsi-cloud.com/oauth/smart/sandbox/token`.
That is `PHI_AI_FHIR_TOKEN_URL`, and it changes per facility exactly as
the base URL does.

### Auth: both grants are documented; the profile uses the JWT assertion

TruBridge's Backend Services page ("a non-user-facing headless or
automated app") documents the client_credentials token request as a
parameter table:

- `grant_type` - required, fixed `client_credentials`.
- `scope` - **required** (next section).
- `client_id` - conditional: "required for confidential clients that use
  symmetric authentication by providing credentials via POST body.
  Otherwise, this value is omitted".
- `client_assertion_type` - conditional, fixed
  `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, "required for
  clients using asymmetric authentication and omitted otherwise".
- `client_assertion` - conditional: "A JWT signed using a private key
  corresponding to one of the public keys published in the backend
  service's JWKS document."

with three worked requests: asymmetric (assertion), symmetric via a
`Basic` Authorization header, and symmetric with `client_secret` in the
POST body. The servers' `smart-configuration` agrees:
`grant_types_supported: [authorization_code, client_credentials]`,
`token_endpoint_auth_methods_supported: [client_secret_basic,
client_secret_post, private_key_jwt]`,
`token_endpoint_auth_signing_alg_values_supported: [RS256, RS384, ES256,
ES384]`, capabilities `client-confidential-asymmetric` and
`client-confidential-symmetric`.

The profile takes the asymmetric path (`auth_flow="smart_backend_services"`)
because the private key never leaves the deployment and RS384 - what
`FHIRIngestionClient.build_client_assertion` signs - is in TruBridge's
list. `FHIRIngestionClient.authenticate()` sends exactly the vendor's
asymmetric shape: `grant_type`, `client_assertion_type`,
`client_assertion`, plus `scope`, and no `client_id`. The secret path is
equally documented; a deployment that prefers it would set
`auth_flow="oauth2_client_credentials"` and `PHI_AI_FHIR_CLIENT_SECRET`
(the POST-body form the client already speaks) - a documented
alternative, not a workaround, and the emulator honours both.

Details from the vendor's worked example (the portal's `auth-fixtures`
chunk): the assertion is signed **RS256** with `kid: key-256`, `iss` and
`sub` = client id, `jti`, `iat`/`nbf`, `exp` one hour after `iat`, and
**`aud` = the FHIR base URL**
(`https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4`). The
SMART client-confidential-asymmetric profile - and this codebase - set
`aud` to the **token endpoint**. TruBridge's prose does not say which it
validates; the example is the only evidence, and it points the other
way. *Must confirm on the sandbox:* if the first token request returns
`invalid_client`, this is the first thing to test, and it is a client
change (`build_client_assertion` takes `token_url` as `aud`), not a
configuration knob. The documented token response is
`{"scope": ..., "expires_in": 3600, "access_token": ...}` - one-hour
tokens, no refresh token for this grant.

`PHI_AI_FHIR_CLIENT_SECRET` is ignored for this vendor
(`Settings.from_env()` requires it only for `oauth2_client_credentials`
profiles).

### Scopes: required in the token request, granted at registration

Verbatim from the token table: `scope` "Must be subset of scopes that
were granted to the backend service during registration. See: Scopes
documentation. `system/` scope prefix is appropriate for backend
services", linking SMART App Launch STU2.2's scopes page. The worked
example requests `system/Observation.rs system/Patient.rs` and the token
response echoes the granted `scope`.

This is the opposite of a scope-less token request, which is why the
profile sets `requires_token_scopes=True`:
`FHIRIngestionClient.authenticate_from_settings()` then derives one
`system/{Type}.read` per entry in `TRUBRIDGE.supported_resources` and
sends them. Three vendor facts make that safe: `smart-configuration`
advertises both `permission-v1` and `permission-v2` (so v1 `.read`
strings are within the advertised capability, though the vendor's own
examples are v2 `.rs`); `scopes_supported` lists `system/*.*` and
`system/*.cruds`, so TruBridge does not forbid wildcards the way Oracle
Health does (this codebase never sends one regardless); and the
Supported Data page documents granular category scopes for Condition and
Observation (`Condition.rs?category=...problem-list-item`,
`Observation.rs?category=...laboratory`, and so on), which a
registration may be narrowed to.

Practical consequence: at registration, request the read scope for
**exactly** the eleven types in the profile - a type in
`supported_resources` that was not granted makes the whole token request
fail (`invalid_scope`), because the profile sends all of them in one
request. Adding a type to the profile later means a scope change at
TruBridge first. *Must confirm on the facility:* whether the granted
scope string is v1 or v2 syntax, and whether the facility narrowed any
type to a category.

### Resources: the published USCDI v3 surface

The Supported Data page publishes the surface as US Core 6.1.0 profiles
with USCDI v3 data-class mappings: AllergyIntolerance, CarePlan,
CareTeam, Condition (encounter-diagnosis and problems/health-concerns
profiles), Coverage, Device (implantable), DiagnosticReport (lab and
note), DocumentReference (with `$docref`), Encounter, Goal, Group,
Immunization, Location and Medication ("Exposed as a contained
resource", no scope), MedicationDispense, MedicationRequest, Observation
(clinical result, lab, vital signs and the social-history/survey/SDOH
profiles), Organization, Patient, Practitioner, PractitionerRole,
Procedure, Provenance, QuestionnaireResponse, RelatedPerson,
ServiceRequest, Specimen. The OpenAPI document defines the same types
(plus Binary) as **GET-only** read and search operations, most with
`_count`, `_lastUpdated` and `_total`, and `Patient` search by
demographics, `identifier` or `_id`. Note a second vendor-internal
contradiction: that same `openapi.yaml` defines **no `$export` path** at
system, Group or Patient level and declares only an `authorization_code`
SMART security scheme (the `client_credentials` flow is commented out) -
the portal's Supported Data and Backend Services pages and both servers'
CapabilityStatements are the sources for bulk and backend auth, not the
OpenAPI document.

`TRUBRIDGE.supported_resources` lists the retention-relevant clinical
types among those - the US Core set MEDITECH's entry carries, plus
ServiceRequest, which TruBridge publishes. It deliberately omits:

- MedicationAdministration, Consent and AdverseEvent - not on
  TruBridge's published list. The servers' CapabilityStatements do
  advertise MedicationAdministration and Consent (without a US Core
  profile); absence from the profile records that the portal does not
  document them, not a confirmed inability.
- CarePlan, CareTeam, Coverage, Device, Goal, MedicationDispense,
  Provenance, QuestionnaireResponse, RelatedPerson, Specimen and the
  directory types - published, but outside the retention scope
  `docs/DATA_SCOPE_REVIEW.md` settled; add them only after that review,
  and only with a matching scope grant.
- Location and Medication - contained in their parents; nothing to
  search.

Whether an unanchored `GET {base}/Patient` (no `patient`/`_id`) returns
the facility's population to a system client is **not documented**; the
sandbox returns `401` without a token, so it could not be checked
anonymously. *Must confirm on the facility* before relying on
`core/fhir/scheduler.py` for population-scale reads; the documented
population path is bulk export.

### Population-scale reads and Bulk Data

Vendor-documented, not inferred from certification: the Supported Data
page lists "System-Level" with profiles "US Core v6.1.0" and "**Bulk
Data Access v2.0.0**" and the operation `[base]/$export`, `Group` with
`[id]/$export`, and `Patient` with `$export`; both the sandbox and
production CapabilityStatements `instantiates` the bulk-data
CapabilityStatement and advertise `export`, `group-export` and
`patient-export` in `rest.operation`. `core/fhir/bulk_scheduler.py` /
`bulk_client.py` use Group-level export (`Group/{id}/$export` with
`Prefer: respond-async`), which is within what TruBridge publishes, and
`supports_bulk_export=True` lets the scheduler run.

What TruBridge does **not** document, and what the scheduler's defaults
therefore do not come from:

- **How a Group id is obtained.** Nothing on the portal. The
  CapabilityStatements advertise `read` and `search-type` (and `create`,
  `update`) on Group, so `GET {base}/Group` with a system token is the
  first thing to try; otherwise it is a question for info@trubridge.com
  and the facility. Set it as `PHI_AI_FHIR_GROUP_ID`.
- **Kickoff frequency, `Retry-After`, poll interval, output retention,
  `_since`/`_type`/`_typeFilter` support.** Bulk Data Access 2.0.0
  defines all of these and TruBridge names that version, but publishes
  no server-specific behaviour. `PHI_AI_BULK_POLL_INTERVAL_SECONDS` and
  `PHI_AI_BULK_MAX_WAIT_SECONDS` keep their defaults because there is no
  TruBridge figure to set them to; the 24-hour run interval in
  `bulk_scheduler.py` is likewise not a TruBridge limit.
- **Whether export is per facility or wider.** Every base URL is a
  facility, and the directory maps several site codes to one facility
  uuid, so a Group is presumably facility-scoped - presumably, not
  documented.

*Must confirm on the facility:* that a real kickoff returns `202` with
`Content-Location`, what the status endpoint's `Retry-After` says, and
whether the manifest's `requiresAccessToken` is true (the client sends
the bearer token either way).

### Limits and fees

- **Page size.** Terms s.4(b): "TruBridge FHIR APIs are designed to
  support real time queries. TruBridge may restrict the amount of data
  returned by certain queries to a specific page size and require you to
  implement logic to incrementally page through the data set". No number
  is published; the profile keeps `page_size=50` and `iter_resources()`
  follows the `next` link regardless.
- **Rate limit.** Not documented. The only vendor statement is s.4(c)'s
  discretionary "suspend, throttle or otherwise limit"; the profile
  keeps the default client-side 60/min.
- **Fees.** Terms s.3: sandbox access "free of charge ... after your
  registration"; Patient Access "free of charge"; "Additional fees may
  apply to your TruBridge client for use of TruBridge FHIR APIs outside
  the Developer Sandbox for any use case other than Patient Access. Any
  such fees will be set forth in a separate written agreement between
  your prospective client and TruBridge." A backend ingestion service is
  such a use case; the fee, if any, is between the facility and
  TruBridge. The certifications page adds that regulatory updates "May
  require purchase of additional software, implementation and support
  fee" and gives an e-mail for FHIR API fee questions (Cloudflare
  obfuscates it to automated fetch; the address the portal and the Terms
  publish is info@trubridge.com).
- **Support.** Terms s.2(e): questions "via email to info@trubridge.com";
  no developer SLA is published.

### Writes

**The FHIR surface is documented as read-only, and the profile records
no writable resource.** Four vendor sources agree: the portal
("read-only access"), the FHIR API page ("read-only access to patient
clinical data"), the PDF ("read only access via FHIR API access"), and
the OpenAPI, which defines no POST, PUT, PATCH or DELETE on any path.

Now the contradiction, because it changes how this codebase behaves: the
**sandbox and production CapabilityStatements advertise `create`,
`update` and `delete`** on Patient, Condition, Observation,
DocumentReference, Procedure, AllergyIntolerance, Coverage, Consent,
Group, Appointment, QuestionnaireResponse and others (checked on a
production facility's `/metadata` as well as the sandbox on 2026-09-01).
`core/fhir/delivery/writer.py` reads the live CapabilityStatement and
refuses only what is *not* advertised - so against TruBridge it will
**not** refuse a DocumentReference, Observation or Condition create on
the capability check. The gate this codebase relies on for every other
vendor does not protect you here; the documentation does. Treat the
advertised interactions as an artefact of the underlying platform (the
same server also lists Appointment scheduling and MeasureReport), not as
permission.

What a delivery to TruBridge would actually do today: `python -m
core.fhir.delivery --vendor trubridge ...` is a dry run by default. With
`--confirm`, its token request is built on the `trubridge` profile
(`core/fhir/delivery/__main__.py`) with the scope derived from
`writable_resources` - empty, so **no scope** - and TruBridge documents
scope as required, so the token request itself should fail before any
write. If it did not, no `system/*.c` or `.u` scope grant is
documented anywhere for a backend service, and the POST would land on an
undocumented path. Conditional create (`If-None-Exist`) is not
documented, so `supports_conditional_create=False` and the writer's
duplicate guard would need `--allow-duplicates`. **Do not run a
confirmed delivery against TruBridge until TruBridge states in writing
which resources a system client may write and under which scopes.**

The realistic write paths are not this profile: a file handoff
(`core/fhir/bulk_export.py` produces NDJSON in the Bulk Data shape and
needs no destination credentials) for the facility's own TruBridge
tooling, or a patient-facing app that the patient authorises through
MyCareCorner - a second client, not this profile.

### Validate before ingesting

Per facility, before the first run:

```
GET {base_url}/.well-known/smart-configuration
GET {base_url}/metadata
```

In the first, confirm `client_credentials` in `grant_types_supported`,
`private_key_jwt` in `token_endpoint_auth_methods_supported`, `RS384` in
`token_endpoint_auth_signing_alg_values_supported`, and read
`token_endpoint`. In the second, confirm `group-export` in
`rest.operation`, and `read` + `search-type` on each of the eleven types
in `TRUBRIDGE.supported_resources` - and ignore `create` there for the
reason above. Record `software.version` in the deployment notes:
TruBridge announces API updates only on the portal, and the server
version is the one fact that shows a change happened.

### What the emulator reproduces

`emulators/vendors.py` `"trubridge"` (port 9112):

- A token endpoint that honours **both** a JWT client assertion and a
  client secret (Basic header or POST body), as TruBridge documents; an
  assertion whose `alg` is not one of RS256, RS384, ES256, ES384 (the
  vendor's advertised list) is refused with `invalid_client`.
- **Scope required**: a `client_credentials` request with no `scope`
  fails with `invalid_scope`, as the vendor's table requires. (The
  emulator's shared flag also rejects wildcard scopes, which is Oracle
  Health's rule and not TruBridge's - TruBridge advertises `system/*.*`;
  PHI AI never sends a wildcard, so the difference is inert for the real
  client.)
- `$export` at system, Group and Patient level with the real async
  handshake (`202`, `Content-Location`, a first poll still in progress).
- A per-facility base-URL shape
  (`/api/smart/{site}/id-osfac.{uuid}/fhir/r4`), so a client that assumes
  a short tenant-less path fails here.
- **Nothing creatable**, and `412` on `If-None-Exist`: the documented
  read-only surface. The emulator does *not* reproduce the real servers'
  over-advertised `create` interactions - that gap is precisely why the
  Writes section above exists.
- Not reproduced: the separate OAuth host, the vendor's error texts
  (none are documented), page-size and throttle behaviour (none are
  documented), and the `aud` question.

### Setting it up

Non-PHI first: the TruBridge Developer Sandbox ("will not include any
actual patient data" - Terms, definition of Developer Sandbox), then a
facility. Every command runs from the repo root with the repo's
virtualenv.

1. **Register with TruBridge** (portal `?page=api/overview`, "Registration
   Process").
   1. Read and accept the Terms:
      https://fhir-developer.plt.trubridge.com/assets/termsandconditions.pdf
      (revised 2025-11-20). Clauses that bind this deployment: s.2(c) no
      benchmarking or performance testing, and no disclosure of
      vulnerability-test results, without written approval; s.4(b) written
      approval by each facility, protect OAuth secrets and notify on a
      leak; s.4(c) discretionary throttling; s.5 credentials confidential;
      s.9(b) TruBridge may discontinue access at any time; s.15(a) no
      public claim of TruBridge validation or partnership.
   2. Read the four portal pages - `?page=api/overview`,
      `?page=api/endpoints`, `?page=api/backend-services`,
      `?page=supported-data`. They are the whole developer documentation.
   3. E-mail the registration request to **info@trubridge.com**, subject
      "TruBridge FHIR API" (the portal's own `mailto:`). TruBridge publishes
      no form; the token table and Terms s.2(a) say what it needs. Include:
      organisation and contact details; the app name "PHI AI" and one line
      of description (a backend ingestion service - the portal's own
      example workflow is "A data integration service that periodically
      queries for new data and synchronizes an external database");
      workflow **Backend Services**; client authentication **asymmetric
      (private_key_jwt)** with your **JWKS URL** from step 2; the
      **scopes** to grant, one per entry of `TRUBRIDGE.supported_resources`:
      `system/Patient.read system/Encounter.read system/Observation.read
      system/Condition.read system/MedicationRequest.read
      system/DocumentReference.read system/AllergyIntolerance.read
      system/Immunization.read system/Procedure.read
      system/DiagnosticReport.read system/ServiceRequest.read` (ask whether
      the grant will be in v1 `.read` or v2 `.rs` form - the servers
      advertise both); that you need Group-level `$export`, and how a
      Group id is issued; and which `aud` the token endpoint validates in
      the client assertion (the FHIR base URL, as in their worked example,
      or the token endpoint, as the SMART profile says). Keep the reply:
      it carries the client id, and the JWKS URL and client id are the
      registration facts TruBridge documents no self-service change for.
   4. Wait for the review. Nothing is self-service and no turnaround is
      published. Sandbox use is free (Terms s.3(a)); "Once registered, Try
      the API" at https://fhir-developer.plt.trubridge.com/openapi/.
   5. For a real facility, obtain the facility's **written approval**
      (Terms s.4(b)) and confirm with it that it "participate[s] in
      Promoting Interoperability and [has] a MyCareCorner instance
      configured" (the PDF's participation condition). TruBridge then
      enables the client for that facility; production fees, if any, are
      in the facility's agreement with TruBridge (s.3(c)).

2. **Generate the keypair and publish the JWKS.** TruBridge accepts RS384
   (`token_endpoint_auth_signing_alg_values_supported: RS256, RS384,
   ES256, ES384`), so an RSA key - not the Epic key:
   ```bash
   umask 077
   openssl genrsa -out trubridge_private_key.pem 2048
   openssl rsa -in trubridge_private_key.pem -pubout -out trubridge_public_key.pem
   ```
   Build the JWKS with the repo's own PyJWT (verified on PyJWT 2.13.0 in
   `.venv`):
   ```bash
   .venv/bin/python - <<'EOF'
   import json, jwt
   from cryptography.hazmat.primitives import serialization
   pub = serialization.load_pem_public_key(open("trubridge_public_key.pem", "rb").read())
   jwk = jwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True)
   jwk.update({"kid": "phi-ai-trubridge-2026-09", "alg": "RS384", "use": "sig"})
   json.dump({"keys": [jwk]}, open("trubridge_jwks.json", "w"), indent=2)
   print(json.dumps({"keys": [jwk]}, indent=2))
   EOF
   ```
   Host `trubridge_jwks.json` at a world-readable HTTPS URL you control
   and that will not move - TruBridge records the URL, not the file ("This
   document is obtained via the URL provided during the backend service's
   registration"). That URL goes in the registration e-mail; the `kid` is
   the value for `PHI_AI_FHIR_JWT_KID`. Rotate by adding a new key to the
   same document, switching the kid, then removing the old key. Mount the
   private key into the container (never bake it into the image) at the
   path `PHI_AI_FHIR_PRIVATE_KEY_PATH` names. No client secret exists on
   this path. If you chose symmetric auth instead, TruBridge issues the
   secret with the client id: keep it in the deployment's secret store,
   set `PHI_AI_FHIR_CLIENT_SECRET`, and switch the profile's `auth_flow`
   to `"oauth2_client_credentials"` (the POST-body form the client already
   speaks and TruBridge documents).

3. **Configure PHI AI** (names from `core/config/settings.py`; the
   globals - `PHI_AI_CLOUD_PROVIDER`, storage, KMS and audit buckets -
   per `runbooks/RUNBOOK_AWS_SETUP.md`). Find the base URL in the
   endpoint directory: production
   `https://thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint`,
   sandbox
   `https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4/.well-known/endpoint`.
   The production document is a Bundle; the sandbox one was a bare array
   on 2026-09-01, so the `jq` below handles both:
   ```bash
   DIR=https://thrive-gw.cpsi-cloud.com/api/fhir/r4/.well-known/endpoint
   curl -s "$DIR" | jq -r 'if type=="array" then .[] else .entry[].resource end
     | select(.resourceType=="Organization") | "\(.name)\t\(.endpoint[0].reference)"' | grep -i "FACILITY NAME"
   curl -s "$DIR" | jq -r 'if type=="array" then .[] else .entry[].resource end
     | select(.resourceType=="Endpoint" and .id=="UUID-FROM-THE-REFERENCE-ABOVE") | .address'
   ```
   Then the token endpoint from that base URL's SMART configuration
   (TruBridge: "Every authorization workflow requires consultation of the
   'SMART Configuration' endpoint"):
   ```bash
   BASE=<address from above>
   curl -s "$BASE/.well-known/smart-configuration" | jq -r .token_endpoint
   ```
   `.env` (sandbox values shown; production values are the facility's):
   ```
   PHI_AI_EMR_VENDOR=trubridge
   PHI_AI_FHIR_BASE_URL=https://thrive-gw-dev.cpsi-cloud.com/api/smart/sandbox/fhir/r4
   PHI_AI_FHIR_TOKEN_URL=https://thrive-oauth-dev.cpsi-cloud.com/oauth/smart/sandbox/token
   PHI_AI_FHIR_CLIENT_ID=<client id from TruBridge's registration reply>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/trubridge_private_key.pem
   PHI_AI_FHIR_JWT_KID=phi-ai-trubridge-2026-09
   PHI_AI_FHIR_GROUP_ID=<see step 5 - leave unset until then>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   ```
   Leave `PHI_AI_FHIR_CLIENT_SECRET` unset; it is ignored for this vendor.
   There is no scope variable: the profile's `requires_token_scopes=True`
   makes `authenticate_from_settings()` derive `system/{Type}.read` for
   every entry in `supported_resources`, which is why step 1 requested
   exactly those.

4. **Pre-flight the facility.**
   ```bash
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" \
     | jq '{token_endpoint, grant_types_supported, token_endpoint_auth_methods_supported, token_endpoint_auth_signing_alg_values_supported, scopes_supported}'
   curl -s -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/metadata" \
     | jq '{software, instantiates, operations: [.rest[0].operation[].name], types: [.rest[0].resource[] | {type, interactions: ([.interaction[].code] | unique)}]}'
   ```
   Look for: `client_credentials` in the grants, `private_key_jwt` in the
   auth methods, `RS384` in the algs, `token_endpoint` equal to
   `PHI_AI_FHIR_TOKEN_URL`; `group-export` among the operations; `read`
   and `search-type` on all eleven profile types. Ignore
   `create`/`update`/`delete` in the interactions - see step 6. Then a
   token smoke test with the real client code:
   ```bash
   set -a; . ./.env; set +a
   .venv/bin/python - <<'EOF'
   import os
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import TRUBRIDGE
   c = FHIRIngestionClient(base_url=os.environ["PHI_AI_FHIR_BASE_URL"], profile=TRUBRIDGE,
                           storage=None, encryptor=None, audit=None, retention_years=10)
   scope = " ".join(f"system/{t}.read" for t in TRUBRIDGE.supported_resources)
   c.authenticate(os.environ["PHI_AI_FHIR_CLIENT_ID"],
                  open(os.environ["PHI_AI_FHIR_PRIVATE_KEY_PATH"], "rb").read(),
                  os.environ["PHI_AI_FHIR_TOKEN_URL"], os.environ.get("PHI_AI_FHIR_JWT_KID"),
                  scope=scope)
   print("token obtained; first 8 chars:", c.access_token[:8])
   EOF
   ```
   `invalid_client`: the JWKS URL is unreachable, the `kid` does not
   match, or the `aud` question (TruBridge's example sets `aud` to the
   FHIR base URL; `build_client_assertion` sets the token endpoint - ask
   TruBridge; a change is in `core/fhir/client.py`, not in configuration).
   `invalid_scope`: a type in `supported_resources` was not granted, or
   the grant is v2-only syntax. When the smoke test passes, run
   `.venv/bin/python -m core.healthcheck`.

5. **First ingest.** The documented population path is Group-level bulk
   export. TruBridge documents no way to obtain a Group id; the
   CapabilityStatement advertises Group search, so try:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" -H 'Accept: application/fhir+json' \
     "$PHI_AI_FHIR_BASE_URL/Group?_count=20" | jq '.entry[]?.resource | {id, name, type, quantity}'
   ```
   If nothing comes back, ask TruBridge and the facility. Set
   `PHI_AI_FHIR_GROUP_ID`, then:
   ```bash
   .venv/bin/python -m core.fhir.bulk_scheduler --once
   ```
   Success: `Kicking off bulk export: group=<id> resource_types=(all)`,
   polling lines until the manifest returns `200`, one download per
   output file, objects stored (and indexed when Postgres is configured);
   then `.venv/bin/python -m core.verify --export-dir <dir>`. If the
   facility does not actually serve group export you will see
   `BulkExportError: Expected 202 Accepted from kickoff, got 4xx` - the
   profile says True, so the scheduler does not refuse at startup the way
   it does for a False profile (that refusal reads `<vendor>'s profile
   records no Bulk Data Export support`). Without a Group id,
   `.venv/bin/python -m core.fhir.scheduler --once` runs per-type paged
   search (`_count`, `_lastUpdated`) - but whether an unanchored search
   returns a population to a system client is not documented; an
   OperationOutcome demanding a `patient` parameter means the answer is
   no.

6. **First delivery - what a write does here.** Nothing is written without
   `--confirm`:
   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" --vendor trubridge \
     --identity-map ./patient-mapping.csv --patient <source-id> --purpose-of-use "rehearsal - no write"
   ```
   Expect the dry run to list DocumentReference, Observation and
   Condition as *accepted* by the capability check - the servers
   advertise `create` - even though every TruBridge document says
   read-only. Do not add `--confirm`: the delivery token request omits
   scope (it is built with the Epic profile), which TruBridge documents
   as required, so it should fail at the token endpoint; if it did not,
   the POST would go to an undocumented surface with no documented write
   scope. Deliver as files instead (`core/fhir/bulk_export.py`) until
   TruBridge confirms a write surface in writing.

7. **Local rehearsal on the emulator.**
   ```bash
   .venv/bin/python -m emulators --vendor trubridge        # port 9112
   ```
   Base URL
   `http://127.0.0.1:9112/api/smart/emulator/id-osfac.00000000-0000-4000-8000-000000000000/fhir/r4`,
   token endpoint `http://127.0.0.1:9112/oauth2/token` (the emulator
   cannot put it on a second host as TruBridge does). Point `.env` at
   those with any client id, the step-2 key and any
   `PHI_AI_FHIR_GROUP_ID`; both `core.fhir.scheduler --once` and
   `core.fhir.bulk_scheduler --once` complete against it. Tests:
   ```bash
   .venv/bin/pytest tests/test_emulator_integration.py -k trubridge -v
   .venv/bin/pytest tests/test_emulator_integration.py tests/test_delivery.py
   ```
   The parametrised "every vendor" cases pick the entry up from
   `VENDORS`; add `trubridge` to the bulk-handshake parametrisation and
   add cases for: a scope-less request -> `invalid_scope`; a client secret
   accepted; an RS384 assertion accepted and an `HS256`/`none` header ->
   `invalid_client`; Group-level `$export` -> `202`; a create -> `422`.

8. **Known limits and where to confirm them.** Not documented by
   TruBridge, so confirm per facility and keep the answers with the
   deployment: Group id issuance; kickoff frequency, `Retry-After`,
   output retention; page size; rate limit; the assertion `aud`;
   token-endpoint error texts; whether unanchored search works for a
   system client; the write surface. Where: the portal
   (https://fhir-developer.plt.trubridge.com/ - updates are announced only
   there, Terms s.2(e)); the CHPL listing
   https://chpl.healthit.gov/#/listing/11541; the Terms PDF;
   info@trubridge.com (support, registration, fees); the facility's own
   `/metadata` and `/.well-known/smart-configuration`; and
   https://trubridge.com/certifications/ for certification changes.

## MEDHOST

Primary sources: MEDHOST's developer portal at
[yourcareinteract.medhost.com](https://yourcareinteract.medhost.com/documentation)
(the "Developer Network" link on
[medhost.com/ehr/interoperability](https://www.medhost.com/ehr/interoperability/);
`developer.yourcareinteract.com` serves the same application), the OpenAPI
file that portal renders as "FHIR 4.0 API"
([assets/doc/swagger.json](https://yourcareinteract.medhost.com/assets/doc/swagger.json)),
its [Upcoming changes](https://api.mhdi10xasayd.com/medhost-developer-composition/v1/upcoming-changes)
document (PDF, "Last Updated: 11 Sep 2025"), its
[Terms of Use](https://yourcareinteract.medhost.com/terms), the published
[FHIR base-URL bundle](https://api.mhdi10xasayd.com/medhost-developer-composition/v1/fhir-base-service-url-bundle),
the two public sandbox tenants' own `/metadata` and
`/.well-known/smart-configuration`, and MEDHOST's
[Enterprise certification page](https://www.medhost.com/about-us/regulatory-and-compliance/onc-certified-health-it/enterprise-onc-certified-health-it/).
Everything below was read on 2026-09-01. The portal is a JavaScript
application - a plain HTTP fetch of any of its pages returns an empty
shell, which is why an earlier survey recorded "no portal found". Every
section says what is verified from those documents and what must be
confirmed on the facility's own instance.

### The facility-tenant model

MEDHOST is hosted and multi-tenant, and the tenant is the facility. Every
facility that exposes the API has its own FHIR base URL of one shape:

```
https://fhir.yourcareuniverse.net/tenant/{tenant-guid}
```

MEDHOST publishes the production list as a FHIR `collection` Bundle of
`Endpoint` + `Organization` pairs (128 of each when fetched; each
Endpoint is named "<Facility> - FHIR R4 Base URL", `connectionType`
`hl7-fhir-rest`, and each Organization carries the facility NPI and
address). That bundle is the source for `PHI_AI_FHIR_BASE_URL` in
production; there is no central data endpoint.

The other half of the model is what the base URL does NOT vary: one
authorization server serves every tenant. Both sandbox tenants'
`smart-configuration` and MEDHOST's OIDC discovery document
(`https://idp.yourcareuniverse.net/.well-known/openid-configuration`)
name the same token endpoint, `https://api.mhdi10xasayd.com/smart/oauth2/token`,
so `PHI_AI_FHIR_TOKEN_URL` is a constant across MEDHOST facilities while
`PHI_AI_FHIR_BASE_URL` is not.

*Verified:* URL shape, the bundle, the shared token endpoint. *Confirm on
the instance:* whether a given facility is in the bundle at all - a
facility that has not licensed MEDHOST's Interoperability package (see
"Limits, fees and terms") has no FHIR door.

### Registration, sandbox, and the weekly review

MEDHOST's Technical Guide for App Developers lays out five steps, and a
retention/ingestion connector follows the "Service Client" path:

1. **Sign up** at the portal (active email, unique username, password;
   a temporary password is emailed; the Terms are accepted at sign-up).
2. **Create a sandbox app** with app type **Service Client**. The guide's
   words: "Service client apps can obtain a token from the MEDHOST
   authorization server using the 'client_credentials' workflow. These
   apps do not require patient or provider login to access the MEDHOST
   API." At creation you must supply a **JWK Set URL** ("App developers
   must provide the JWK Set URL that will return the client's JWKS key")
   and, since July 2025, an Application Category and User Category from
   pre-populated lists. Sandbox apps get their credentials from the
   portal immediately.
3. **Test against a sandbox tenant** (see "Versions and the two
   sandboxes").
4. **Create a production app** with the same settings (Dashboard ->
   Production Apps -> Add Production App). "Production apps have access
   to real data, but only after MEDHOST and the facility have approved
   the app." For service clients that is two approvals: "Provider-facing
   apps and service client apps also require approval from the
   appropriate facility. For patient-facing apps, only MEDHOST approval
   is required."
5. **The review runs weekly**: "New or updated apps must be submitted by
   Monday to be considered for review that week. Apps submitted after
   Monday are reviewed the following week."

Two warnings MEDHOST puts in its own guide, worth reading before the
form is filled in: "Before submitting a production app, review every
app field. Some fields are not editable, and once an app is created, you
may be required to create a new app altogether." And "The app must go
through the review and approval process to access any API."

The production public key is not set on the portal: "App developers
must work with individual facilities to set the JWKS keys for production
apps during the approval process. The key format remains the same for
production apps." The contact MEDHOST publishes for the API is
`api-dev-admin@medhost.com`.

*Verified:* all of the above, from the Technical Guide. *Confirm with
MEDHOST and the facility:* which facility administrator owns the
approval, and whether one production app can be approved by several
facilities or needs one per facility - the guide does not say.

### Auth: private_key_jwt, RS384 or ES384, keys by JWK Set URL

MEDHOST's own description of service-client authentication: "When
calling the token endpoint, the service client must authenticate by
sending a signed JSON Web Token and assertion framework. This type of
authentication is called 'private_key_jwt' in OpenID terms." That is the
RFC 7523 client-assertion flow `FHIRIngestionClient.authenticate()`
already speaks, and `Settings.from_env()` ignores
`PHI_AI_FHIR_CLIENT_SECRET` for this vendor.

Signing algorithms are documented explicitly: "MEDHOST supports RS384
and ES384 algorithms. Developers must generate keys using either of
these algorithms and register the public key with the MEDHOST
authorization server." The guide gives one JWKS example for each - an
RSA key (`kty: RSA`, `use: sig`, `kid`, `n`, `e`) and an EC P-384 key
(`kty: EC`, `crv: P-384`, `x`, `y`). The US Core 6.1.0 sandbox tenant's
`smart-configuration` publishes the same pair as
`token_endpoint_auth_signing_alg_values_supported: ["RS384", "ES384"]`
and lists `client-confidential-asymmetric` among its capabilities. The
profile records `assertion_algorithm="RS384"` because that is what
`core/fhir/client.py` signs today; ES384 is equally valid at MEDHOST.

The key is published, not uploaded: the JWK Set URL is a field on the
app, and for production it is set with each facility during approval.
The `kid` you put in the JWKS is the `kid` the assertion must carry -
set it as `PHI_AI_FHIR_JWT_KID`. MEDHOST's July 2025 change note adds:
"It is recommended to use different keys for different clients."

What the token endpoint does when it is spoken to wrongly (observed on
the sandbox token endpoint on 2026-09-01; MEDHOST does not document the
error bodies):

- a `client_secret` on `client_credentials` -> HTTP 401,
  `{"error":"invalid_client","error_description":"client authentication failed"}`
- a malformed `client_assertion` -> HTTP 400,
  `{"error":"invalid_client","error_description":"client authentication failed due to invalid client_assertion"}`
- no credential at all -> HTTP 401, the same `invalid_client` body

Note that MEDHOST's discovery documents also list `client_secret_basic`
and `client_secret_post`. The guide ties those to **confidential
authorization-code apps** ("Client Type as Confidential ... called
'client_secret_basic' in OpenID terms"), not to service clients, and the
profile does not use them. Implicit is "not recommended and is not
supported by the MEDHOST authorization server". TLS 1.2 is required.

*Verified:* flow, algorithms, JWKS-by-URL, the per-facility production
key. *Confirm on the instance:* nothing further - the token endpoint is
shared; if the sandbox app authenticates, the production app fails only
on approval or key registration.

### Scopes: approved at registration, System Client gets system/ only

MEDHOST does not document a scope parameter on the service-client token
request at all. What it documents is scope selection on the app: the
scopes are chosen at registration and updated through review ("Any
additional scope requests will have to follow the review process"), in
SMART v2 form with v1 back-fill - "For backward compatibility, SMART v1
scopes will be configured based on the selected SMART v2 scopes upon app
registration or scope updates" - and MEDHOST asks that you "use specific
scopes whenever possible instead of wildcard scopes. This helps the
administrator understand the specific request from the application."

The Upcoming-changes document publishes a compatibility matrix: a
**System Client** may hold `system/*` scopes only; `patient/` and
`user/` are marked N for it. A facility approves the system scopes it is
willing to grant.

Because the token-time requirement is undocumented, the profile keeps
`requires_token_scopes=False` and the client sends no `scope` parameter;
the grant is what was approved. Two facts to check before anyone flips
that: the 6.1.0 tenant's `scopes_supported` carries no `system/*.read`
wildcard (the 3.1.1 tenant's does), and MEDHOST's published v1 scope
strings spell DiagnosticReport as `system/DiagnositcReport.read` (the v2
list carries both spellings). A client that derived
`system/DiagnosticReport.read` from `supported_resources` would be
asking for a scope MEDHOST's v1 list does not contain.

*Verified:* everything above. *Confirm with the facility:* the exact
scope set it approved for the production app.

### Versions and the two sandboxes

MEDHOST's interoperability page: "MEDHOST utilizes the FHIR R4 v 4.0.1
as a guideline for the structure of electronic health information".
Both sandbox CapabilityStatements report `fhirVersion 4.0.1`,
`publisher MEDHOST`, and `instantiates` both the US Core server and the
Bulk Data CapabilityStatements.

The guide names two sandbox base URLs:

| Sandbox | Base URL | What it advertises |
|---|---|---|
| US Core 3.1.1 | `https://fhir.yourcareuniverse.net/tenant/174285d6-efb7-4560-a7ba-f3ae332b091f` | v1 scopes only; no signing-alg values in `smart-configuration` (MEDHOST's own note: the attribute is not returned) |
| US Core 6.1.0 | `https://fhir.yourcareuniverse.net/tenant/7b158079-391d-484b-a078-bee596d2f165` | `permission-v2`, `client-confidential-asymmetric`, RS384/ES384, `CapabilityStatement.version "6.1.0"`, five extra resources (Coverage, Endpoint, Flag, MedicationDispense, RelatedPerson, Specimen) |

MEDHOST's certified module (below) documents USCDI v3 API access, which
is the 6.1.0 sandbox's generation. MEDHOST also warns that "data in the
sandbox may be modified at any point", that "Saved FHIR IDs will become
invalidated after the upgrade - Do not store FHIR IDs in your
application", and that the 3.1.1_v1 sandbox was removed in November 2024
with its client IDs.

*Verified:* both tenants and their statements. *Confirm on the
instance:* the base-URL bundle does not say which US Core generation a
production facility runs; read its `/metadata` and look at
`CapabilityStatement.version` and the `capabilities` list in its
`smart-configuration`.

### Paging seams MEDHOST documents

The Upcoming-changes entry for US Core 3.1.1_v2 (April 17 2024) is the
most operationally useful page MEDHOST publishes:

- default page 10, "Default maximum number of results" 100, and the
  instruction "Configure your app to pass a result count of 20" - the
  profile's `page_size=20` comes from here;
- "Previous link ... This is no longer supported", "Self link ... This
  is no longer supported", `_getpageoffset` removed;
- a Bundle with no data has no `entry` element ("This is not returned
  when no data is available in the response") and no `meta`;
- `_total=accurate` "MUST be specified to retrieve the actual count";
- `meta.versionId` "is no longer supported";
- "Referenced resources are not guaranteed to be there. Validate that
  referenced resources are returned before accessing those."
- Binary: from September 30 2025 the response honours `Accept` /
  `_format`, and without `application/fhir+json` "the default behavior
  will be to return the direct content, i.e. XML".

`iter_resources()` reads only `entry` (defaulting to an empty list) and
the `next` link, and sends `Accept: application/fhir+json`, so every one
of these is already survivable. What it cannot do is population-scale
reads through search - see the next section.

### Population-scale reads: the Group comes from MEDHOST Support

MEDHOST's Data Export step is short and specific. "Currently the System
and Patient Export are not supported through FHIR API." Group export is
the one population path, and the Group is not something an app creates
or searches for:

1. "Coordinate with facility to determine the list of patients that
   should be included in the Group resource."
2. "The facility administrator will collaborate with MEDHOST Support to
   submit a support ticket containing the patient MRNs to be included in
   the Group."
3. "Once the Group has been created by MEDHOST, the group ID will be
   provided to the facility administrator. They will share the Group ID
   with you for use in the export process."
4. "Use the provided Group ID to initiate and monitor the export process
   using the FHIR Bulk Data Group Export operation."

And the cap: "Group export is limited to 5000 patients per group (with
patient number and request date criteria). If the request is for more
than 5000 patients, MEDHOST will create multiple groups, and the
customer will receive an ID for each group." A facility of more than
5,000 patients therefore means several `PHI_AI_FHIR_GROUP_ID` values and
one `bulk_scheduler` run per group. (`system/Group.write`, the one scope
that might once have let an app maintain a Group, was removed in August
2024 per the same document.)

### Bulk Data Export

Implemented by `core/fhir/bulk_client.py` and `core/fhir/bulk_scheduler.py`
unchanged. What MEDHOST publishes about the operation itself:

- `GET /Group/{id}/$export` is the only `$export` path in the OpenAPI
  file, with documented responses **202, 401, 404, 429 Too Many
  Requests, 500**. `kickoff_export()` raises `BulkExportError("Expected
  202 Accepted from kickoff, got 429")` on the 429 - treat that as
  MEDHOST's answer and do not retry in a loop.
- The Group resource's operation in the CapabilityStatement is
  `http://hl7.org/fhir/uv/bulkdata/OperationDefinition/group-export`;
  the server `instantiates` the Bulk Data CapabilityStatement.
- **No kickoff parameters are documented** - the OpenAPI entry has
  `parameters: []`, and `_type`, `_since`, `_outputFormat` and
  `_typeFilter` appear nowhere in it. `kickoff_export()` sends `_type`
  when the scheduler passes resource types; whether MEDHOST honours or
  ignores it is a facility-instance question.
- **Not documented by the vendor:** any minimum interval between
  kickoffs, a recommended poll interval, a per-file resource cap, or how
  long a manifest stays downloadable. `PHI_AI_BULK_POLL_INTERVAL_SECONDS`
  and `PHI_AI_BULK_MAX_WAIT_SECONDS` keep their defaults until MEDHOST or
  the facility supplies a number.
- Unauthenticated calls to `$export` (and to every FHIR path) on the
  sandbox return HTTP 401 with a plain `{"message": "Unauthorized"}`
  JSON body and no `WWW-Authenticate` header - not an OperationOutcome.

*Verified:* the operation, its documented status codes, the Group
procedure and cap. *Confirm on the instance:* `_type` handling, poll
cadence, and whether the facility's Group(s) exist yet - the Group ID
comes from the facility administrator, never from an API.

### Writes

The MEDHOST FHIR surface is read-only, and that is documented three ways
rather than inferred:

- All 61 operations in MEDHOST's OpenAPI file are `GET`; there is no
  `POST`, `PUT` or `PATCH` on any path.
- Both sandbox CapabilityStatements advertise exactly `read` and
  `search-type` on every resource type (the system-level `batch` and
  `transaction` interactions are listed, but no resource advertises
  `create`, `update` or `delete`).
- The one write scope MEDHOST ever published, `system/Group.write`, is
  recorded as removed on August 29 2024 ("System/Group.read is still
  available").

The criterion the API is certified to agrees: ONC's (g)(10) text says
these services "specifically exclude 'write' capabilities".

**How delivery behaves:** `core/fhir/delivery/writer.py` reads the
destination's CapabilityStatement first and refuses anything it does not
advertise. Against a MEDHOST tenant every record is skipped with
`does not advertise create for {Type}`; nothing is POSTed. The profile
records `writable_resources=()` and `supports_conditional_create=False`
to match.

**The real write path:** none is documented on the developer portal. The
Terms of Use describe the API as patient-access tooling; the portal's
only contact is `api-dev-admin@medhost.com`. Until the facility and
MEDHOST confirm a path, a delivery INTO a MEDHOST facility is files, not
FHIR. Do not mistake MEDHOST's (b)(10) EHI export - the administrator-run
export whose extension fields the
[MEDHOST-FHIR-Extension-Fields.xlsx](https://www.medhost.com/fhir/extensions/MEDHOST-FHIR-Extension-Fields.xlsx)
spreadsheet documents - for an import; it moves data out.

### Limits, fees and terms

- **Facility-side licensing.** MEDHOST's own costs-and-considerations
  workbook (linked from the certification page, sheet "Ent.Clinicals
  24R1") states: "Customers must purchase and activate the MEDHOST
  Interoperability package to use API access to patient health
  information", with "A one-time set up fee and recurring monthly
  maintenance fees ... for the use of YourCareUniverse framework." A
  facility that has not done this is not in the base-URL bundle.
- **Developer-side fees:** not documented by the vendor. The Terms of
  Use mention none.
- **Traffic limits:** no number is documented. The Terms reserve that
  "MEDHOST Cloud Services may also limit the use of the access to or use
  of API, including but not limited to the volume of traffic or data
  flow permitted, in its discretion", and the kickoff documents 429. The
  profile's `rate_limit_per_min` stays at its default for lack of a
  vendor figure.
- **Acceptable use.** Section 5 of the Terms: "Developer agrees it will
  not use the API for any purpose other than development related to,
  and towards the end of, providing health care consumers who utilize
  MEDHOST Cloud Services' patient portal product ('End Users') access to
  such End User's data held by MEDHOST Cloud Services. Any other use is
  strictly forbidden." The same Terms let MEDHOST "terminate or suspend
  Developer access to the API or to revoke Developer credentials" at its
  discretion, and can be modified "without notice". The Technical Guide
  nonetheless documents service clients and facility-approved
  provider-facing apps. A retention or ingestion service client
  therefore rests on the facility's written approval and MEDHOST's
  production approval - not on the click-through Terms. Settle this
  with the facility and `api-dev-admin@medhost.com` before go-live and
  keep the record.
- **Sandbox data** "may be modified at any point"; FHIR ids are not
  stable across upgrades.

### Certification

MEDHOST's own certification page lists **MEDHOST Enterprise - Clinicals
2024 R1**, CHPL ID `15.04.04.2788.MEDH.CL.10.1.250806`, certified
August 6 2025, with criteria 170.315 (a)(1-5, 12, 14); (b)(1-3, 10-11);
(d)(1-9, 12-13); (e)(3); (f)(1-3, 5); (g)(3-7, 9-10) - so (g)(10) is on
the listing - and "Additional Software" including "MEDHOST Cloud
Services" and "MEDHOST Cures 2023, Interoperability Package". MEDHOST's
Real World Testing results for CY2024 and CY2025 report the (g)(10)
measure against Enterprise 2022 R1 / 2023 R1 and describe the model
plainly: "Provider facing apps register with the MEDHOST API and then
the facility acts as the gatekeeper for which apps they will allow their
providers to use." The CY2026 plan extends it to 2024 R1 and adds the
"HTI-1 Package" to the relied-upon software.

Nothing in this chapter relies on (g)(10) for a fact: Backend Services
and Group export are documented by MEDHOST directly. The certification
matters for two things - the facility must be on a certified, licensed
release for the API to exist, and the criterion's public-documentation
rule is why the portal's Technical Guide is readable without a login.

### Validate before ingesting

`MEDHOST.supported_resources` is the retention-relevant subset of what
MEDHOST publishes, not a promise about one facility. MEDHOST's own
instruction: "For a List of applicable FHIR Resources and SMART version
supported at a facility, please refer to the metadata". Before pointing
a run at a tenant:

```
GET {PHI_AI_FHIR_BASE_URL}/metadata
GET {PHI_AI_FHIR_BASE_URL}/.well-known/smart-configuration
```

Both are unauthenticated. In the CapabilityStatement, check
`fhirVersion` (4.0.1), `version` (which US Core generation), that each
type in `supported_resources` is present with `read` and `search-type`,
that `Group` carries the `export` operation, and that no resource
advertises `create` (if one ever does, the writer will honour it - so
know before it happens). In `smart-configuration`, check
`token_endpoint` is `https://api.mhdi10xasayd.com/smart/oauth2/token`,
`private_key_jwt` is in `token_endpoint_auth_methods_supported`, and
RS384 is in `token_endpoint_auth_signing_alg_values_supported` (absent on
the 3.1.1 generation, by MEDHOST's own note). A type listed in the
profile but absent from the facility's CapabilityStatement, or a scope
the facility did not approve, surfaces as an authorization failure at
that resource, not as an empty result.

### What the emulator reproduces

The `medhost` emulator (`emulators/vendors.py`, port 9113) reproduces,
from MEDHOST's own documentation and the sandbox's observed behaviour:

- **private_key_jwt only** - a `client_secret` on `client_credentials`
  gets `invalid_client`, as the live sandbox answered.
- **RS384 or ES384** - an assertion signed with any other algorithm gets
  `invalid_client` (`assertion_algorithms=("RS384", "ES384")`).
- **No scope parameter demanded** at the token endpoint.
- **Group `$export` present** with the genuinely-async 202 / status /
  NDJSON handshake.
- **Create advertised for nothing**, so the delivery capability check
  skips every record.
- **No conditional create** (412 on `If-None-Exist`).
- **Forced pagination** at two per page.
- **A tenant-shaped base path** (`/tenant/emulator-tenant`).

What it does NOT reproduce, so a green run must not be read as proof:

- MEDHOST refuses **system- and patient-level** `$export`; the emulator
  serves `$export` at every level.
- MEDHOST answers unauthenticated FHIR calls with **HTTP 401
  `{"message":"Unauthorized"}`**; the emulator checks no bearer token.
- MEDHOST omits `entry` on empty Bundles and publishes no `self` or
  `previous` links; the emulator returns standard Bundles.
- MEDHOST's kickoff can return **429**; MEDHOST's FHIR ids are **not
  stable** across upgrades; the emulator does neither.
- The weekly review, facility approval, and the JWKS-by-URL registration
  are procedural and cannot be emulated.

### Setting it up

Every command assumes the repository root as the working directory and
the project virtualenv (`.venv/bin/python`, `.venv/bin/pytest`). No PHI
is involved until step 5 runs against a production tenant; the sandbox
tenants hold MEDHOST's synthetic data.

1. **Register with MEDHOST.**
   1. Open `https://yourcareinteract.medhost.com/` and click **Sign Up**
      (active email, unique username, password; accept the Terms; a
      temporary password is emailed; sign in and change it).
   2. Dashboard -> **Sandbox Apps** -> add an app with app type
      **Service Client** (the `client_credentials` + `private_key_jwt`
      type). Fill in Application Category and User Category (required
      since July 2025), the **JWK Set URL** from step 2, and the
      `system/` scopes for the types in `MEDHOST.supported_resources`
      (`system/Patient.rs`, `system/Encounter.rs`, ... - MEDHOST asks for
      specific scopes, not wildcards; v1 `.read` forms are back-filled
      automatically). Record the sandbox **Client ID**.
   3. When the sandbox works (steps 4-5), Dashboard -> **Production
      Apps** -> **Add Production App** with the same settings. Submit
      **by Monday** for that week's review; MEDHOST approves, then each
      facility approves, and the production JWKS is "set with individual
      facilities during the approval process". Review every field before
      saving: MEDHOST says some fields are not editable and a mistake
      means a new app. Contact: `api-dev-admin@medhost.com`.
   4. Ask the facility administrator, in writing, for (a) their
      production tenant base URL (also in MEDHOST's bundle:
      `https://api.mhdi10xasayd.com/medhost-developer-composition/v1/fhir-base-service-url-bundle`),
      (b) confirmation that the MEDHOST Interoperability package is
      licensed, and (c) a support ticket to MEDHOST Support with the
      patient MRN list for the export Group (max 5,000 per group; several
      Group IDs if larger). Keep the facility's approval alongside the
      Terms - see the chapter's "Limits, fees and terms".

2. **Generate the RS384 key pair and publish the JWKS.** MEDHOST accepts
   RS384 (RSA) or ES384 (EC P-384); the client signs RS384, so generate
   RSA. The private key never leaves your infrastructure.

   ```bash
   openssl genrsa -out medhost_private_key.pem 2048
   openssl rsa -in medhost_private_key.pem -pubout -out medhost_public_key.pem
   chmod 600 medhost_private_key.pem

   .venv/bin/python - <<'EOF'
   import json, uuid
   from jwt.algorithms import RSAAlgorithm
   pub = open("medhost_public_key.pem").read()
   kid = str(uuid.uuid4())
   jwk = RSAAlgorithm.to_jwk(RSAAlgorithm(RSAAlgorithm.SHA384).prepare_key(pub), as_dict=True)
   jwk.update({"kid": kid, "use": "sig", "alg": "RS384"})
   json.dump({"keys": [jwk]}, open("medhost_jwks.json", "w"), indent=2)
   print("kid =", kid)
   EOF
   ```

   Host `medhost_jwks.json` at a world-readable HTTPS URL (the same
   pattern as `deploy/aws/README_EPIC_JWKS.md`, with a separate file and
   key for MEDHOST - MEDHOST recommends "different keys for different
   clients"). Paste that URL into the app's **JWK Set URL** field. Keep
   the printed `kid`. Generate a second pair for production; the
   production JWKS is registered with each facility, not on the portal.

3. **Configure PHI AI.** Names are exactly as `core/config/settings.py`
   reads them (`Settings.from_env()`); the storage, KMS and audit
   variables every deployment needs are unchanged and not repeated here.

   ```bash
   PHI_AI_EMR_VENDOR=medhost
   # sandbox (US Core 6.1.0) for steps 4-5; the facility's Endpoint.address in production
   PHI_AI_FHIR_BASE_URL=https://fhir.yourcareuniverse.net/tenant/7b158079-391d-484b-a078-bee596d2f165
   PHI_AI_FHIR_TOKEN_URL=https://api.mhdi10xasayd.com/smart/oauth2/token
   PHI_AI_FHIR_CLIENT_ID=<client id from the portal>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/medhost_private_key.pem
   PHI_AI_FHIR_JWT_KID=<kid printed in step 2>
   # bulk export - set after the facility hands you the Group ID (one run per Group)
   PHI_AI_FHIR_GROUP_ID=<group id from the facility administrator>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=600
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   ```

   Do **not** set `PHI_AI_FHIR_CLIENT_SECRET`: MEDHOST service clients
   have no secret, and the profile's `auth_flow` makes
   `authenticate_from_settings()` take the assertion path. No scope
   variable exists or is needed - the grant is what the facility
   approved.

4. **Pre-flight the tenant (unauthenticated) and the token (authenticated).**

   ```bash
   curl -s -H "Accept: application/fhir+json" "$PHI_AI_FHIR_BASE_URL/metadata" | .venv/bin/python -c '
   import json,sys; cs=json.load(sys.stdin); r=cs["rest"][0]
   print("fhirVersion", cs["fhirVersion"], "| version", cs.get("version"))
   for x in r["resource"]:
       ops=[o["name"] for o in x.get("operation",[])]; ints=[i["code"] for i in x["interaction"]]
       if x["type"] in ("Patient","Group") or "create" in ints: print(x["type"], ints, ops)
   print("types:", sorted(x["type"] for x in r["resource"]))'
   curl -s "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" | .venv/bin/python -c '
   import json,sys; c=json.load(sys.stdin)
   print(c["token_endpoint"]); print(c["token_endpoint_auth_methods_supported"])
   print(c.get("token_endpoint_auth_signing_alg_values_supported"), [x for x in c.get("capabilities",[]) if "asymmetric" in x or "permission-v2"==x])'
   ```

   Expect `fhirVersion 4.0.1`, `Group ['read','search-type'] ['export']`,
   `Patient ['read','search-type'] ['everything']`, every type in
   `MEDHOST.supported_resources` present, **no** type with `create`, the
   token endpoint above, `private_key_jwt` listed, and `['RS384','ES384']`
   plus `client-confidential-asymmetric` on a 6.1.0-generation tenant
   (both absent on a 3.1.1 tenant, by MEDHOST's own note). Then get a
   token with the real client:

   ```bash
   .venv/bin/python - <<'EOF'
   import os
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for
   c = FHIRIngestionClient(base_url=os.environ["PHI_AI_FHIR_BASE_URL"], profile=profile_for("medhost"),
                           storage=None, encryptor=None, audit=None, retention_years=10)
   c.authenticate(client_id=os.environ["PHI_AI_FHIR_CLIENT_ID"],
                  private_key_pem=open(os.environ["PHI_AI_FHIR_PRIVATE_KEY_PATH"], "rb").read(),
                  token_url=os.environ["PHI_AI_FHIR_TOKEN_URL"], jwt_kid=os.environ["PHI_AI_FHIR_JWT_KID"])
   print("token ok:", bool(c.access_token))
   print(sum(1 for _ in c.iter_resources("Patient")), "patients readable")
   EOF
   ```

   A 401 `invalid_client` / `client authentication failed` means the
   JWKS URL, `kid` or client ID does not match the app; a 400
   `invalid client_assertion` means the JWT itself (alg, aud, exp) is
   wrong. Both are MEDHOST's real bodies.

5. **First ingest.** Paged search first, then bulk.

   ```bash
   .venv/bin/python -m core.fhir.scheduler --once
   ```

   Success looks like `Authenticating to MEDHOST FHIR endpoint`,
   `Ingesting 12 resource types (since=None)`, one `Stored <Type>: N
   resources` per type, and `Run complete: N resources stored`. Pages are
   20 per request (`page_size`); empty types log 0, because MEDHOST omits
   `entry` on an empty Bundle and the client treats that as no rows.

   With the Group ID set:

   ```bash
   .venv/bin/python -m core.fhir.bulk_scheduler --once
   ```

   Success: `Kicking off bulk export: group=<id> resource_types=[...]`,
   one or more `Bulk export still in progress`, `Stored <Type> from bulk
   export: N resources` per type, `Bulk export run complete: N resources
   stored`. Failures you will actually see: `PHI_AI_FHIR_GROUP_ID is not
   set` (no Group yet - it comes from the facility administrator, never
   an API); `Expected 202 Accepted from kickoff, got 429` (MEDHOST's
   documented throttle - stop and ask, do not loop); `got 404` (wrong
   Group ID or wrong tenant). The scheduler's own refusal, `MEDHOST's
   profile records no Bulk Data Export support. Use core/fhir/scheduler.py
   (paged search) for this vendor ...`, appears only if the profile's
   `supports_bulk_export` is ever set False after a facility's
   CapabilityStatement shows no Group export - it will not appear with
   the shipped profile. A system-level `$export` is not supported by
   MEDHOST and `bulk_client` never issues one.

6. **First delivery (and why it is a no-op).**

   ```bash
   .venv/bin/python -m core.fhir.delivery --destination "$PHI_AI_FHIR_BASE_URL" --vendor medhost \
       --identity-map identity.csv --purpose-of-use treatment --patient <source patient id>
   ```

   Without `--confirm` nothing is sent; with it, nothing is sent either:
   the writer reads the tenant's CapabilityStatement, finds no resource
   advertising `create`, and skips every item with `does not advertise
   create for <Type>`. That is the correct result against a read-only
   surface. (The delivery token client is built on the `medhost`
   profile - RS384, no scope, since `writable_resources` is empty - and
   MEDHOST accepts RS384, so the token step itself succeeds. `--vendor`
   takes any `PROFILES` key.)

7. **Local rehearsal against the emulator.**

   ```bash
   .venv/bin/python -m emulators --vendor medhost      # http://127.0.0.1:9113/tenant/emulator-tenant
   .venv/bin/pytest tests/test_emulator_integration.py -k medhost -q
   .venv/bin/pytest tests/test_delivery.py -q
   ```

   The emulator's token endpoint refuses a `client_secret` and any
   non-RS384/ES384 assertion with `invalid_client`, serves Group
   `$export` asynchronously, and advertises `create` for nothing. Three
   parametrize lists in the tests are hand-written and must include
   `medhost` (or, better, be derived from `VENDORS` / `PROFILES`):
   `test_jwt_only_vendors_reject_a_client_secret`,
   `test_bulk_export_completes_its_async_handshake` (every vendor whose
   `supports_bulk_export` is True), and
   `tests/test_delivery.py::test_every_target_emr_has_an_ingestion_profile`.
   Point the emulator at the SMART launch endpoint with
   `--record-url http://127.0.0.1:8080/smart/launch` when rehearsing
   in-context launch, as `runbooks/RUNBOOK_EMULATORS.md` describes.

8. **Known limits and where to confirm them.**
   - Group export only, 5,000 patients per Group, Group created by
     MEDHOST Support; no `_type`/`_since` documented; 429 documented:
     the Technical Guide ("Data Export") at
     `https://yourcareinteract.medhost.com/documentation` and
     `https://yourcareinteract.medhost.com/assets/doc/swagger.json`.
   - Paging defaults, missing `entry`/`self`/`previous`, unstable ids,
     sandbox changes:
     `https://api.mhdi10xasayd.com/medhost-developer-composition/v1/upcoming-changes`.
   - Certification (CHPL `15.04.04.2788.MEDH.CL.10.1.250806`, (g)(10))
     and the facility-side Interoperability package requirement:
     `https://www.medhost.com/about-us/regulatory-and-compliance/onc-certified-health-it/enterprise-onc-certified-health-it/`
     and its linked Costs and Considerations workbook; the CHPL listing
     itself at `https://chpl.healthit.gov/` (search "MEDHOST").
   - Terms (patient-access acceptable use, revocation, traffic limits):
     `https://yourcareinteract.medhost.com/terms`.
   - Anything the documents do not answer (production US Core
     generation, `_type` handling, poll cadence, a write path):
     the facility's own `/metadata` and `api-dev-admin@medhost.com`.

## Netsmart

Primary sources: the Netsmart CareConnect developer documentation at
[careconnect.netsmartcloud.com](https://careconnect.netsmartcloud.com/docs/) -
the [Provider System Access API](https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/),
its per-resource pages (for example
[Group](https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/resources/group/),
[DocumentReference](https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/resources/documentreference/),
[CapabilityStatement](https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/resources/capabilitystatement/))
and [Common Errors](https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/errors/);
the getting-started pages for
[Registration](https://careconnect.netsmartcloud.com/docs/getting-started/registration/),
[Authorization](https://careconnect.netsmartcloud.com/docs/getting-started/authorization/),
[Sandbox Environments](https://careconnect.netsmartcloud.com/docs/getting-started/sandbox/) and
[Network Configuration](https://careconnect.netsmartcloud.com/docs/getting-started/network-configuration/);
the [Certified CareRecords](https://careconnect.netsmartcloud.com/docs/certified/carerecords/),
[Service Base URLs](https://careconnect.netsmartcloud.com/docs/certified/service-base-urls/) and
[v2 migration](https://careconnect.netsmartcloud.com/docs/migration/v2/overview/) pages;
the two Postman tutorials
([System Access](https://careconnect.netsmartcloud.com/docs/tutorials/testing-fhir-system-access-apis-with-postman/),
[Bulk Data Export](https://careconnect-dev.netsmartdev.com/docs/tutorials/testing-fhir-bulk-data-export-apis-with-postman/));
the [API Terms of Service](https://careconnect.netsmartcloud.com/terms-of-service/);
and Netsmart's own certification disclosures at
[ntst.com/lp/certifications](https://www.ntst.com/lp/certifications).
Where a fact below was **observed on the vendor's preview tenant** rather
than read from a page, it says so with the date (2026-09-01); those are
the vendor's own systems, but they are observations, not documentation,
and can change without notice.

### Which surface this is

Netsmart CareConnect is the interoperability layer in front of Netsmart's
behavioral-health and post-acute CareRecords - myAvatar, myEvolv, myUnity,
GEHRIMED and TheraOffice. The certified APIs come in three categories:
**Patient Access** (authorization code + PKCE, patient consent),
**Practitioner Access** (authorization code) and **System Access** -
*"Automated system-to-system data exchange with bulk export capabilities"*.
This connector targets the **Certified v2 Provider System Access API**:
FHIR R4 4.0.1, US Core 6.1.0, Bulk Data 2.0.0, and per Netsmart's own
standards table "SMART Backend Services 1.0" (its overview and migration
guide say "SMART App Launch 2.0"; nothing here depends on which).

Two other Netsmart surfaces exist and are **not** what the profile models:

- The **v1 certified API** (`https://fhir.netsmartcloud.com/uscore/v1`,
  bulk at `/uscore/v1/bulk-data`, auth at `oauth.netsmartcloud.com`, US
  Core 3.1.1, Bulk Data 1.0.0). Netsmart: *"v1 APIs remain operational"*,
  *"Deprecation timelines will be communicated with advance notice"*, and
  *"Your v1 application credentials will NOT work with v2 APIs."*
- The **General Purpose R4 and STU3 APIs** (`/v4`, `/fhir`) and the CCD
  API, which sit on the original authorization server and are not the
  certified surface.

### The tenant model

Verified from Netsmart's docs: *"Each CareConnect FHIR tenant now has
dedicated authorization and FHIR base URLs."* The shapes are

```
FHIR base:  https://fhir.netsmartcloud.com/provider/system-access/v2/{tenant-id}
Token:      https://fhir.netsmartcloud.com/auth/{tenant-id}/oauth2/v1/token
Preview:    the same paths on https://fhirtest.netsmartcloud.com
```

The tenant id is a UUID; *"Once your application is authorized for a
tenant, the tenant ID is displayed in the developer portal under your
application's Tenant Authorization tab."* And the constraint that shapes
every deployment: *"Currently, each application can only be authorized
for a single tenant. If you need to access multiple tenants, you must
register a separate application for each tenant."* So
`PHI_AI_FHIR_BASE_URL`, `PHI_AI_FHIR_TOKEN_URL` and `PHI_AI_FHIR_CLIENT_ID`
are all per-tenant; three Netsmart customers mean three registrations and
three sets of settings, not one client id synced around.

Production endpoints are also published as a SMART User-access Brands
bundle (Netsmart's Service Base URLs page, implementing SMART App Launch
v2.2.0's brands profile): `GET https://fhir.netsmartcloud.com/brand/brands.json`
(preview: `fhirtest.netsmartcloud.com/brand/brands.json`). On the preview
bundle the myAvatar sandbox's System Access endpoint appears as "Netsmart
Sandbox USCore v2" (observed 2026-09-01).

Must be confirmed on the instance: which CareRecord sits behind a tenant.
Netsmart's resource pages carry a per-CareRecord operations table and say
*"support varies by the targeted CareRecord or solution"*; the connector
version (`software.version` in `/metadata`, `1.2.17` on the preview
tenant) is not the whole story.

### Registration and tenant authorization

Verified from the Registration, Sandbox and migration pages:

1. Create a developer account on the portal for the environment you want -
   preview `https://fhirtest.netsmartcloud.com/developers`, production
   `https://fhir.netsmartcloud.com/developers`. The username *"must be
   unique and cannot be your email address"*. Netsmart flags the portal as
   *"currently in early testing and is subject to change"* and the account
   *"Verification process to be documented."*
2. **Applications → Create Application** runs a guided setup: *Type*
   (Patient-facing application or **System integration**), *Platform*
   (Web server application or Mobile/Desktop), *Access* (**Provider APIs**
   or Payer APIs), *Authentication* (**Client Secret or Private Key JWT**),
   then *Details* (application name, company, redirect URIs). For Private
   Key JWT, *"you will need to include your JWK Set URI with the
   registration"* - have the JWKS hosted before you start. After creation
   *"you can retrieve the client id and secret."*
3. **Tenant Authorization tab → search for the Organization (tenant) →
   Request Authorization.** *"Your authorization request has now been
   submitted and will be reviewed by a owner of that tenant."* This is the
   review step: the customer, not Netsmart, approves. For the sandbox,
   *"Allow 3-5 business days for tenant authorization approval."* Once
   approved the tenant name and id are displayed.
4. Bulk export is a separate permission: *"Your CareConnect app
   registration must include bulk data export permissions to access the
   $export operations."* Ask for it at registration.

What cannot change later (verified): one tenant per application; v1
credentials never work on v2; the sandbox's *"No Custom Data - Cannot
upload or modify sandbox data."* Not documented by the vendor: whether the
authentication method or the JWK Set URI can be edited after creation, and
what a revoked authorization looks like beyond *"API calls to that tenant
will fail with authorization errors. You must request re-authorization."*

The preview environment: *"All sandbox environments use synthetic, non-PHI
data."* The one provider sandbox available is myAvatar - tenant
`d6c40265-c5c6-494f-b1aa-a27bf9a8c3f1`, tenant name "Internal CGI Avatar",
CareFabric scope `CGIAV_KS!UAT:PROD`; myUnity, myEvolv, GEHRIMED and
TheraOffice sandboxes are listed as "TBD".

### Auth: two documented grants, and which one the profile uses

Netsmart documents **both** confidential-client grants for System Access,
and the preview tenant's `.well-known/smart-configuration` advertises
`token_endpoint_auth_methods_supported: client_secret_basic,
client_secret_post, private_key_jwt` and the capabilities
`client-confidential-asymmetric` and `client-confidential-symmetric`
(observed 2026-09-01).

**Private Key JWT** - *"Private Key JWT (Recommended)"* on the System
Access page, and the one `NETSMART.auth_flow` selects:

1. Host a JWKS at a public HTTPS URL and give that URL as the JWK Set URI
   at registration. This is the only documented way to register the public
   key (the bulk tutorial: *"ensure your JWKS URI is publicly accessible"*).
   Set `PHI_AI_FHIR_JWT_KID` to the `kid` in that JWKS so the assertion
   header carries it.
2. Netsmart's documented token request is
   `grant_type=client_credentials`,
   `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
   `client_assertion=<signed JWT>`, **and `scope`** (see the next
   section). The Common Errors page names the claims it checks - *"iss,
   sub, aud, iat, exp, jti"*, *"Verify audience matches token endpoint
   URL"*, *"Confirm private key matches registered public key"* - which is
   what `FHIRIngestionClient.build_client_assertion` produces.
3. The documented refusal: `400 {"error": "invalid_client",
   "error_description": "Invalid client assertion JWT"}`. Observed on the
   preview tenant (2026-09-01): a value that is not a JWT at all gets
   `invalid_request` / `"Invalid client_assertion format"` instead, and a
   client the tenant has not authorized gets `invalid_scope` /
   `"Application not authorized for this tenant"`.
4. **Signing algorithm: not documented by the vendor.** No Netsmart page
   names RS384 or ES384, and the discovery document has no
   `token_endpoint_auth_signing_alg_values_supported`. The profile leaves
   `assertion_algorithm` at the RS384 default because the SMART
   client-confidential-asymmetric profile Netsmart advertises obliges the
   server to validate *"at least one of RS384 or ES384"*
   ([hl7.org](https://hl7.org/fhir/smart-app-launch/client-confidential-asymmetric.html)),
   and RS384 is what the shipped client signs. The first preview token
   request is the confirmation; an `"Invalid client assertion JWT"` with a
   correct `kid`, `aud` and `exp` is the signal to try the other grant.

**Client secret** - equally documented: *"This most commonly used method is
to pass the client id and secret using Basic auth"* and *"When using a
client secret we recommend use of Basic Auth, however we do support their
inclusion in the body as well."* `authenticate_client_secret()` sends the
body form, which Netsmart documents as accepted, so flipping the profile
to `auth_flow="oauth2_client_credentials"` and setting
`PHI_AI_FHIR_CLIENT_SECRET` is a documented fallback rather than a code
change. `Settings.from_env()` would then require the secret at startup.

Token lifetime: the documented response is `"expires_in": 3600` with a
`"tenant"` claim; the System Access page also promises *"Long-Lived
Tokens"* and the tutorial says system tokens *"typically have longer
expiration times than patient access tokens"* - no figure is documented.
The schedulers authenticate once per run, so nothing here depends on it.

### Scopes: explicit system scopes on every token request

Verified: every `client_credentials` example Netsmart publishes carries a
`scope` (`scope=system/Patient.rs system/AllergyIntolerance.rs` on the
authorization page; a 24-type list in the System Access tutorial;
`system/*.rs` in the bulk tutorial), and both tutorials document an
*"Invalid scope"* refusal (*"use system/ prefix; verify resource names"*).
Observed (2026-09-01): a scope-less request to the preview token endpoint
returns `400 {"error": "invalid_request", "error_description": "scope is
required"}`. `NETSMART.requires_token_scopes` is therefore `True`, and
`authenticate_from_settings()` sends one `system/{Type}.read` per entry
in `supported_resources`.

Syntax: Netsmart documents SMART v2 scopes (`.rs` = read + search) and
states *"The v2 APIs support both v1 and v2 scope syntax for backwards
compatibility"*; the preview discovery document advertises
`permission-v1` and `permission-v2`. The v1 `.read` strings the client
derives are therefore documented-valid. Wildcards are documented too -
`system/*.rs` is the bulk tutorial's example and the preview tenant's
`scopes_supported` - but the client never needs one.

Must be confirmed on the instance: **what the tenant owner granted.** The
documented read-time refusal is *"HAPI-0333: Access denied by rule:
Request is not authorized for this Tenant"* with the guidance *"Check
that your requested scopes were actually granted during the
authorization process."* Whether the token endpoint narrows a request
that names an ungranted type or refuses the whole request is not
documented by the vendor, so every type in `supported_resources` must be
one the registration was granted; there is no per-deployment override of
that tuple today.

### Population-scale reads: Group-level Bulk Data Export

Verified from the Group page, the System Access page, the bulk tutorial
and the Common Errors page:

- **Group-level export is what is documented**: *"Group-Level Export -
  Export data for specific patient populations"*, `GET` or `POST
  /Group/{id}/$export`, operation definition
  `http://hl7.org/fhir/uv/bulkdata/OperationDefinition/group-export|2.0.0`.
  The System Access page's headline example shows a system-level
  `{tenant-id}/$export` URL, but no page documents that operation and the
  preview tenant advertises `export` on Group only (observed
  2026-09-01). `bulk_client.py` calls the Group form.
- **Group ids are discoverable through the API.** The tutorial's own step:
  `GET {base}/Group?_count=10`, *"Copy a Group ID from
  entry[].resource.id."* Set it as `PHI_AI_FHIR_GROUP_ID`. Group search
  parameters are `code`, `member`, `type`.
- **Required headers**: `Accept: application/fhir+json` and `Prefer:
  respond-async` - both sent by `bulk_client.kickoff_export`.
- **Parameters**: `_type` (comma-separated), `_since` (instant),
  `_outputFormat` (`application/fhir+ndjson`, `ndjson`,
  `application/ndjson`) and `_typeFilter`. `bulk_client.py` sends `_type`
  from `supported_resources` and has **no `since` parameter at all**, so
  every run is a full re-extract of the Group even though Netsmart
  documents incremental export - a gap in this codebase, not at Netsmart,
  and the first thing to add if a Netsmart deployment runs bulk daily.
- **Handshake**: `202 Accepted` with `Content-Location:
  https://fhir.netsmartcloud.com/provider/export-status/v2/{tenant-id}/jobs/{id}`;
  polls return `202` with `X-Progress: Running (45% complete)` (also
  `Queued (0% complete)`, `Finalizing (98% complete)`) and
  `Retry-After: 120`; completion is `200` with a manifest whose `output[]`
  URLs are `Binary/{id}/$export-download` and `"requiresAccessToken":
  true`; `error[]` may carry an OperationOutcome file. Netsmart: *"Use the
  Retry-After header value to determine when to poll again"*, *"If no
  Retry-After header is present, wait 1-2 minutes between polls"*,
  *"Large exports may take 10-30 minutes or longer."* `bulk_client.py`
  ignores `Retry-After` and polls at `PHI_AI_BULK_POLL_INTERVAL_SECONDS`
  (default 600); set it to **120-180** for this vendor. It sends the
  bearer token on downloads; Netsmart's download example also sends
  `Accept: application/fhir+ndjson`, which the client does not - whether
  that is required is not documented by the vendor.
- **Cleanup**: `DELETE` on the status URL returns `202` (*"Cancel
  in-progress export"* or *"Clean up completed export"*);
  `bulk_client.delete_export` accepts 202/204/404.
- **Limits**: *"429 Too Many Requests - Export Limit"* for *"Too many
  concurrent bulk export requests"*, *"Avoid concurrent export
  requests"*, and on the sandbox *"Limited concurrent export jobs."*
  Export-job failures surface as a `500` during processing with an
  `error[]` entry; Netsmart suggests *"breaking large exports into
  smaller date ranges"* - which needs `_since`. **No per-day throttle is
  documented**: `bulk_scheduler.py`'s 24-hour default interval is a
  codebase default, not a Netsmart limit.
- **Permission**: bulk export scopes must be on the registration (see
  above); the tutorial's example scope is `system/*.rs`.

### Paged search and incremental ingestion

`core/fhir/scheduler.py` pages `GET {base}/{Type}?_count=50` and, after
the first run, adds `_lastUpdated=gt{watermark}`. Verified from Netsmart's
pages: `_lastUpdated` is a documented search parameter on Encounter,
DocumentReference and DiagnosticReport, and the preview tenant's
CapabilityStatement lists it on nearly every type - **but not on
Condition** (neither on the Condition page nor in the tenant's Condition
`searchParam` list, observed 2026-09-01). Netsmart's documented answer to
an unsupported parameter is `400` *"Invalid search parameter"*, so an
incremental Condition cycle may fail after the first full run; confirm on
the tenant before relying on the paged scheduler for Condition. The
Condition page also warns *"Not all Netsmart solutions support Condition
search."* `POST /{Type}/_search` is documented and recommended *"when
searching with patient identifiers or other sensitive data"*;
`iter_resources()` uses GET. No maximum `_count` is documented
(examples use 5-20); `page_size` stays at 50.

### Writes

The FHIR surface **is writable, for two clinical types and nothing
else.** Verified: the DocumentReference and DiagnosticReport pages carry an
operations table with Create, Read, Update and Search all "Yes" for
GEHRIMED, myAvatar, myEvolv, myUnity and TheraOffice, and document `POST
/DocumentReference`, `PUT /DocumentReference/{id}`, `POST
/DiagnosticReport` and `PUT /DiagnosticReport/{id}` with example bodies.
Condition, Encounter and Binary show Create and Update as "-" (Binary is
Read-only). `NETSMART.writable_resources` records exactly the two. The
preview tenant's live CapabilityStatement advertises `create` and
`update` for those two among the clinical types, and additionally for a
set of Da Vinci PAS/DTR, scheduling, Consent and Subscription types no
Netsmart page documents (observed 2026-09-01) - none of which this
system delivers.

What a write means: Netsmart's create example is a DocumentReference
whose content is an attachment **URL** (`"contentType":
"application/pdf"`, `"url": "https://example.com/document.pdf"`); whether
a tenant fetches an external URL or requires inline `data` is not
documented by the vendor. Conditional create (`If-None-Exist`) is not
documented and the preview tenant declares no `conditionalCreate`, so
`supports_conditional_create` is `False`: a repeated delivery duplicates
unless gated on the prior-record tag `writer.py` applies.

How `core/fhir/delivery/writer.py` behaves: it reads the destination
tenant's own `/metadata` and refuses any type the CapabilityStatement
does not advertise `create` for. Against the preview tenant that admits
DocumentReference and DiagnosticReport and skips everything else. Two
blockers sit in front of that check:

1. `core/fhir/delivery/__main__.py` builds its destination token
   request on the `netsmart` profile and, because the profile requires
   explicit scopes, sends `system/DocumentReference.write
   system/DiagnosticReport.write` (SMART v1 grammar, derived from
   `writable_resources`). The preview token endpoint refuses a
   scope-less request with `"scope is required"` (observed 2026-09-01);
   whether it honours the `.write` form is **not documented by the
   vendor** - Netsmart documents no system-level write scope string, and
   its SMART 2.0 example syntax is `patient/Observation.cruds`. If the
   `.write` request is refused, mint the token out-of-band
   (`PHI_AI_DELIVERY_ACCESS_TOKEN`) with `system/DocumentReference.cruds`,
   the shape to try on the preview tenant.
2. The registration must have been granted write permission by the
   tenant owner (tenant configuration; not documented beyond the 403
   *"Insufficient system scopes in access token"* / *"Application not
   authorized for requested operation"*).

Netsmart's terms make the Client responsible for testing a Developer
Application *"before ... a live production environment"* and require a
BAA (*"Business Associate Agreements - Required for all system
integrations"*). Deliver DocumentReference/DiagnosticReport only after
both are in place; everything else goes as files for the customer's own
tooling.

### Limits, throttling and fees

Verified from the Common Errors, Sandbox and Terms pages, and Netsmart's
myAvatar certification disclosure:

- **Rate limiting** is documented as a shape, not a number: `429` with an
  OperationOutcome `code: throttled`, *"Rate limit exceeded. Please retry
  after 60 seconds."*, and example headers `Retry-After: 60`,
  `X-RateLimit-Limit: 1000` (*"Requests per time window"* - window
  unspecified), `X-RateLimit-Remaining`. *"Contact support if you need
  higher rate limits for system operations."* `rate_limit_per_min` stays
  at the default 60; `core/fhir/client.py` has no 429/`Retry-After`
  handling, so a throttled run fails on the `raise_for_status()` rather
  than backing off - worth adding before a large Netsmart tenant.
- **Maintenance**: `503` with `Retry-After: 300`.
- **Sandbox limits**: *"Similar to production but may be more
  restrictive"*, *"Limited concurrent export jobs"*, *"Automatic
  throttling for high-volume testing."*
- **Contractual throttling**: Netsmart *"may, in its sole and reasonable
  discretion, suspend, throttle or otherwise limit your Developed
  Application activity"* on the grounds the Terms list.
- **Fees**: Netsmart's Drummond disclosure for myAvatar Certified Edition
  states that 170.315(g)(7), (g)(9) and (g)(10) are *"achieved by
  leveraging Netsmart's CareConnect FHIR Interface"* and *"A CareConnect
  FHIR subscription is required"* - the customer's cost, not the app
  developer's. The Terms add: *"Connection services may be needed ...
  The scope of such services, and any associated fees, would be set
  forth in a separate written agreement."* Nothing is documented as a
  per-call or per-app fee to the developer.
- **Consent**: there is no consent screen in System Access; the tenant
  owner's authorization stands in for it, and the Terms make the
  *"Client ... responsible for managing and capturing any applicable
  patient level consents."*

### Network and environments

Verified from Network Configuration: outbound HTTPS 443 to
`fhir.netsmartcloud.com` (preview `fhirtest.netsmartcloud.com`) for both
FHIR and OAuth, plus the
`careconnect-prod-fhir-user-pool.auth.us-east-2.amazoncognito.com` user
pool host (`careconnect-uat-...` for preview); `oauth.netsmartcloud.com`
is legacy v1 only. TLS 1.2+. No inbound access is needed for this
connector (the listed egress IPs are for webhooks/SFTP integrations).
Must be confirmed with the tenant owner: the System Access page lists
*"IP whitelisting"* under network security and the 403 causes include
*"IP address not whitelisted (if IP restrictions apply)"* - whether a
given tenant restricts by IP is not documented.

Moving from preview to production is, per Netsmart's checklist: change
`fhirtest.` to `fhir.` in both URLs, use the production tenant id, and
use production credentials from a **separate** production-portal
registration.

### Validate before ingesting

Netsmart's own first step (*"Always begin by retrieving the
CapabilityStatement"*) and both are served without authentication:

```
GET {base_url}/metadata
GET {base_url}/.well-known/smart-configuration
```

Check, in this order: `rest.resource[].type` contains every entry in
`NETSMART.supported_resources` with a `search-type` interaction (the
preview tenant does, 2026-09-01); the `Group` entry carries the
`export` operation; `DocumentReference`/`DiagnosticReport` carry `create`
if a delivery is planned; `_lastUpdated` appears in the `searchParam`
list of each type you will ingest incrementally (Condition is the one
to look at); and in the discovery document, `token_endpoint` equals
`PHI_AI_FHIR_TOKEN_URL`, `private_key_jwt` (or `client_secret_post`) is
in `token_endpoint_auth_methods_supported`, and `permission-v1` is in
`capabilities`. A `403` on `/metadata` means the tenant id is wrong (a
non-existent tenant returns 403, observed 2026-09-01), and a `404` on
the discovery document *"Verify your base_url and tenant_id."* The
CapabilityStatement's `software.name` is "Netsmart CareConnect FHIR
Connector" and `implementation.description` is "HAPI FHIR", which is
why HAPI error codes (HAPI-0333) appear in Netsmart's own
troubleshooting text.

### What the emulator reproduces

`emulators/vendors.py` `netsmart` (port 9114) reproduces, from Netsmart's
documentation:

- **Both grants.** A JWT client assertion or a client secret - the secret
  in a Basic `Authorization` header or the form body - are both accepted,
  as Netsmart documents; a request with neither gets `invalid_request`.
- **Explicit scopes demanded.** A scope-less `client_credentials` request
  is refused (Netsmart's preview endpoint says `"scope is required"`; the
  emulator's text differs). *Known divergence:* the same flag refuses
  wildcard scopes, which Netsmart documents as valid (`system/*.rs`);
  the shipped client never sends one, so only a hand-written request
  reaches the difference.
- **RS384-only assertion** via `assertion_algorithms=("RS384",)`, with
  Netsmart's documented refusal text `"Invalid client assertion JWT"`.
  RS384 is the emulator's conservative default for a point Netsmart
  leaves undocumented, not a Netsmart statement.
- **Group `$export`** with the real async handshake (202 →
  in-progress → manifest → NDJSON) and `_type` honoured. The emulator
  also answers system- and Patient-level `$export`, which Netsmart does
  not document; the client never calls them.
- **Create advertised for DocumentReference and DiagnosticReport only**,
  so `writer.py` has something real to admit and something real to
  refuse (a Condition create gets the 422 a tenant's CapabilityStatement
  would have predicted).
- **`If-None-Exist` refused with 412**, matching the absence of any
  documented conditional create.
- **Tenant-scoped URL prefix** `/provider/system-access/v2/EMULATOR-TENANT`.

It does **not** reproduce Netsmart's per-tenant token URL
(`/auth/{tenant-id}/oauth2/v1/token` - the emulator's token route is
fixed), the Binary `$export-download` URL shape, `Retry-After` on polls,
`_since`, the `Condition` `_lastUpdated` gap, IP restrictions, the tenant
owner's scope grants, or any rate limit. A green run proves the client
handles Netsmart's documented shapes; it does not replace the preview
tenant.

### Setting it up

Everything below is the NON-PHI path: the preview environment
(`fhirtest.netsmartcloud.com`), which Netsmart documents as *"synthetic,
non-PHI data"*. Production is the same steps with `fhir.` in place of
`fhirtest.`, a production-portal registration, and the customer's
production tenant id.

1. **Register at Netsmart** (Netsmart's Registration and Sandbox pages).
   1. Open the preview developer portal
      `https://fhirtest.netsmartcloud.com/developers` (production:
      `https://fhir.netsmartcloud.com/developers`) and **Sign Up** -
      username (not an email address), given/family name, email,
      password. Netsmart marks the portal *"in early testing and subject
      to change"*.
   2. **Applications → Create Application**. In the guided setup choose:
      Type = **System integration**; Platform = **Web server
      application**; Access = **Provider APIs**; Authentication =
      **Private Key JWT** (what the profile uses - you will be asked for
      the JWK Set URI, so do step 2 first) or **Client Secret** (the
      documented fallback); Details = application name, company name.
      Review the resulting OAuth configuration, then **Create
      Application** and record the client id (and the secret, if you
      chose that method).
   3. **Tenant Authorization tab → Request Tenant Authorization**: search
      for the sandbox tenant by any of its identifiers - tenant name
      `Internal CGI Avatar`, tenant id
      `d6c40265-c5c6-494f-b1aa-a27bf9a8c3f1`, or CareFabric scope
      `CGIAV_KS!UAT:PROD` - and **Request Authorization**. The request
      *"will be reviewed by a owner of that tenant"*; for the sandbox
      *"Allow 3-5 business days."* Ask, in the same breath, for **bulk
      data export permissions** (*"Your CareConnect app registration must
      include bulk data export permissions"*) and for write permission
      on DocumentReference/DiagnosticReport if you intend to deliver.
   4. When approved, the tenant id is shown on the Tenant Authorization
      tab. What cannot change later: one tenant per application
      (register another application per additional tenant); v1
      credentials never work on v2; whether the auth method or JWK Set
      URI is editable after creation is not documented by the vendor -
      treat the JWKS URL as permanent.

2. **Generate the key pair and publish the JWKS.** Netsmart documents no
   key size or algorithm; the profile signs RS384, so this is an RSA key
   (2048-bit minimum).

   ```bash
   openssl genrsa -out netsmart_private_key.pem 2048
   openssl rsa -in netsmart_private_key.pem -pubout -out netsmart_public_key.pem
   chmod 600 netsmart_private_key.pem
   ```

   Build the JWKS with the repo's own dependencies (the `kid` you choose
   here becomes `PHI_AI_FHIR_JWT_KID`):

   ```bash
   .venv/bin/python - <<'EOF'
   import json, jwt
   from cryptography.hazmat.primitives import serialization
   pub = serialization.load_pem_public_key(open("netsmart_public_key.pem", "rb").read())
   jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
   jwk.update({"kid": "phi-ai-netsmart-2026-09", "alg": "RS384", "use": "sig"})
   json.dump({"keys": [jwk]}, open("jwks.json", "w"), indent=2)
   print(open("jwks.json").read())
   EOF
   ```

   Host `jwks.json` at a world-readable HTTPS URL (Netsmart: *"ensure your
   JWKS URI is publicly accessible"*) and paste that URL as the JWK Set
   URI in step 1.2. Never commit or email `netsmart_private_key.pem`;
   `docker-compose.yml` mounts it read-only at the path in
   `PHI_AI_FHIR_PRIVATE_KEY_PATH`. Client-secret alternative: the portal
   shows the secret after Create Application; keep it in your secret
   manager and expose it only as `PHI_AI_FHIR_CLIENT_SECRET`, and only if
   you flip the profile's `auth_flow` to `oauth2_client_credentials`.

3. **PHI AI environment** (names from `core/config/settings.py`; put them
   in `.env`, which `docker-compose.yml` reads, or run
   `python3 install/installer_chatbot.py` and pick `Netsmart  [netsmart]`).
   Replace `<tenant-id>` with the id from step 1.4.

   ```bash
   PHI_AI_EMR_VENDOR=netsmart
   PHI_AI_FHIR_BASE_URL=https://fhirtest.netsmartcloud.com/provider/system-access/v2/<tenant-id>
   PHI_AI_FHIR_TOKEN_URL=https://fhirtest.netsmartcloud.com/auth/<tenant-id>/oauth2/v1/token
   PHI_AI_FHIR_CLIENT_ID=<client id from the portal>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/secrets/netsmart_private_key.pem
   PHI_AI_FHIR_JWT_KID=phi-ai-netsmart-2026-09
   # PHI_AI_FHIR_CLIENT_SECRET is NOT set for the JWT flow (ignored if present)
   PHI_AI_FHIR_GROUP_ID=<filled in at step 5>
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=120      # Netsmart documents Retry-After: 120
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   # PHI_AI_BULK_INTERVAL_SECONDS defaults to 86400 - a codebase default, not a Netsmart limit
   ```

   Plus the storage/audit/cloud variables the deployment already has
   (`PHI_AI_CLOUD_PROVIDER`, `PHI_AI_STORAGE_BUCKET`, `PHI_AI_KMS_KEY_ID`,
   `PHI_AI_AUDIT_BUCKET`, ...). There is no scope variable: the
   `system/{Type}.read` scopes are derived from
   `NETSMART.supported_resources` at authentication time because the
   profile sets `requires_token_scopes=True`.

4. **Pre-flight.** Both discovery documents are served without a token.

   ```bash
   curl -sS -H "Accept: application/fhir+json" "$PHI_AI_FHIR_BASE_URL/metadata" \
     | .venv/bin/python -c '
   import json, sys
   from core.fhir.emr_profiles import NETSMART
   cs = json.load(sys.stdin)
   print(cs["software"], cs["implementation"]["description"])
   have = {r["type"]: r for r in cs["rest"][0]["resource"]}
   for t in NETSMART.supported_resources:
       r = have.get(t)
       codes = [i["code"] for i in r.get("interaction", [])] if r else []
       params = [p["name"] for p in r.get("searchParam", [])] if r else []
       print(f"{t:22} {codes}  _lastUpdated={"_lastUpdated" in params}")
   print("Group ops:", [o["name"] for o in have.get("Group", {}).get("operation", [])])
   print("create advertised:", sorted(t for t, r in have.items() if any(i["code"] == "create" for i in r.get("interaction", []))))
   '
   curl -sS "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" | .venv/bin/python -m json.tool
   ```

   Look for: every listed type with `search-type`; `Group ops: ['export']`;
   `_lastUpdated=True` on the types you will ingest incrementally
   (Condition is the one Netsmart does not document it on);
   `token_endpoint` equal to `PHI_AI_FHIR_TOKEN_URL`; `private_key_jwt` in
   `token_endpoint_auth_methods_supported`; `permission-v1` in
   `capabilities`. A `403` on `/metadata` means the tenant id is wrong.

   Then the token smoke test through the real client (this is the step
   that confirms Netsmart accepts RS384, which its docs do not state):

   ```bash
   set -a; . ./.env; set +a
   .venv/bin/python - <<'EOF'
   import os
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for
   c = FHIRIngestionClient(base_url=os.environ["PHI_AI_FHIR_BASE_URL"], profile=profile_for("netsmart"),
                           storage=None, encryptor=None, audit=None, retention_years=10)
   c.authenticate(client_id=os.environ["PHI_AI_FHIR_CLIENT_ID"],
                  private_key_pem=open(os.environ["PHI_AI_FHIR_PRIVATE_KEY_PATH"], "rb").read(),
                  token_url=os.environ["PHI_AI_FHIR_TOKEN_URL"],
                  jwt_kid=os.environ.get("PHI_AI_FHIR_JWT_KID"),
                  scope="system/Patient.read")
   print("token issued:", bool(c.access_token))
   EOF
   ```

   Refusals and what they mean (Netsmart's Common Errors page, plus what
   the preview endpoint returned on 2026-09-01): `invalid_client` /
   `Invalid client assertion JWT` - kid, aud (must equal the token URL),
   exp, or the JWKS does not match, or the algorithm is not accepted;
   `invalid_scope` / `Application not authorized for this tenant` - the
   tenant owner has not approved the request, or wrong tenant id in the
   token URL; `invalid_request` / `scope is required` - no scope was sent.

5. **First ingest.** Find a Group (Netsmart's own step) and export it:

   ```bash
   TOKEN=$(.venv/bin/python - <<'EOF'
   import os
   from core.fhir.client import FHIRIngestionClient
   from core.fhir.emr_profiles import profile_for
   c = FHIRIngestionClient(base_url=os.environ["PHI_AI_FHIR_BASE_URL"], profile=profile_for("netsmart"),
                           storage=None, encryptor=None, audit=None, retention_years=10)
   c.authenticate(os.environ["PHI_AI_FHIR_CLIENT_ID"], open(os.environ["PHI_AI_FHIR_PRIVATE_KEY_PATH"], "rb").read(),
                  os.environ["PHI_AI_FHIR_TOKEN_URL"], os.environ.get("PHI_AI_FHIR_JWT_KID"), scope="system/Group.read")
   print(c.access_token)
   EOF
   )
   curl -sS -H "Authorization: Bearer $TOKEN" -H "Accept: application/fhir+json" \
     "$PHI_AI_FHIR_BASE_URL/Group?_count=10" \
     | .venv/bin/python -c 'import json,sys; [print(e["resource"]["id"], e["resource"].get("name"), e["resource"].get("quantity")) for e in json.load(sys.stdin).get("entry", [])]'
   ```

   Put one id in `PHI_AI_FHIR_GROUP_ID`, then:

   ```bash
   .venv/bin/python -m core.fhir.bulk_scheduler --once
   ```

   Success looks like `Bulk export kicked off, status URL:
   https://fhirtest.netsmartcloud.com/provider/export-status/v2/<tenant-id>/jobs/...`,
   one or more `Bulk export still in progress: Running (45% complete)`
   lines, `Stored Patient from bulk export: N resources` per type, and
   `Bulk export run complete: N resources stored`; exit status 0. A `429`
   at kickoff is Netsmart's concurrent-export limit - wait for the
   previous job. Every run is a full re-extract of the Group
   (`bulk_client.py` sends no `_since`). If the profile ever records
   `supports_bulk_export=False` you will instead see `Netsmart's profile
   records no Bulk Data Export support. Use core/fhir/scheduler.py
   (paged search) for this vendor ...` and exit 1 - that is the refusal
   path, not a crash. The paged alternative:

   ```bash
   .venv/bin/python -m core.fhir.scheduler --once
   ```

   which logs `Authenticating to Netsmart FHIR endpoint`, `Ingesting 18
   resource types (since=None)` and `Stored Patient: N resources` per
   type. On the second and later runs watch Condition for a `400`
   (`_lastUpdated` is not documented on it).

6. **First delivery (write).** Always dry-run first - `--confirm` is what
   writes:

   ```bash
   export PHI_AI_DELIVERY_ACCESS_TOKEN=<token minted with a system write scope, e.g. system/DocumentReference.cruds>
   export PHI_AI_SOURCE_EMR_URLS=$PHI_AI_FHIR_BASE_URL
   .venv/bin/python -m core.fhir.delivery \
       --destination https://fhirtest.netsmartcloud.com/provider/system-access/v2/<tenant-id> \
       --vendor netsmart \
       --identity-map ./patient-mapping.csv \
       --patient <source patient id> \
       --purpose-of-use "Continuity of care - preview rehearsal, synthetic data"
   ```

   What happens: `writer.py` reads the destination's `/metadata`, admits
   DocumentReference and DiagnosticReport (the two types Netsmart
   documents as creatable and the preview tenant advertises) and skips
   every other type with a logged reason. The CLI's own token request
   (`PHI_AI_DELIVERY_CLIENT_ID` + `PHI_AI_DELIVERY_TOKEN_URL` +
   `PHI_AI_DELIVERY_PRIVATE_KEY_PATH`) is built on the `netsmart`
   profile and carries `system/DocumentReference.write
   system/DiagnosticReport.write`; Netsmart documents no system
   write-scope string (`.cruds` is its SMART 2.0 example syntax), so if
   the preview tenant refuses the `.write` form, supply
   `PHI_AI_DELIVERY_ACCESS_TOKEN` minted out-of-band with the `.cruds`
   form instead - confirm on the preview tenant. A second `--confirm`
   run duplicates unless gated: no conditional create is documented.

7. **Local rehearsal against the emulator.**

   ```bash
   .venv/bin/python -m emulators --vendor netsmart --port 9114
   #   FHIR base:  http://127.0.0.1:9114/provider/system-access/v2/EMULATOR-TENANT
   #   token:      http://127.0.0.1:9114/oauth2/token   (fixed emulator route, not Netsmart's /auth/{tenant}/... shape)
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k netsmart -v
   ```

   To point the real schedulers at it: `PHI_AI_EMR_VENDOR=netsmart`,
   `PHI_AI_FHIR_BASE_URL=http://127.0.0.1:9114/provider/system-access/v2/EMULATOR-TENANT`,
   `PHI_AI_FHIR_TOKEN_URL=http://127.0.0.1:9114/oauth2/token`, any RS384
   key, `PHI_AI_FHIR_GROUP_ID` set to anything (the emulator ignores
   it). The `-k netsmart` selection covers the paging test automatically
   (it is parametrised over `VENDORS`); the bulk-handshake test's vendor
   list, `tests/test_delivery.py`'s profile list and the runbook table are
   hand-maintained and need `netsmart` added - `netsmart` must NOT be added
   to `test_jwt_only_vendors_reject_a_client_secret`, because Netsmart
   accepts both grants.

8. **Known limits and where to confirm them.**
   - Rate limits: shape only (429 `throttled`, `Retry-After`,
     `X-RateLimit-*`), no published number -
     `https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/errors/`;
     sandbox limits *"may be more restrictive"* -
     `https://careconnect.netsmartcloud.com/docs/getting-started/sandbox/`.
   - Concurrent exports: 429 *"Export Limit"* (same errors page);
     `_since`/`_typeFilter` for smaller jobs -
     `https://careconnect.netsmartcloud.com/docs/api/fhir/certified/provider/system-access/resources/group/`.
   - Signing algorithm, key size, `_count` maximum, token lifetime
     beyond 3600, system write-scope syntax, `_lastUpdated` on Condition:
     not documented by the vendor - confirm on the preview tenant, then
     with Netsmart support through the developer portal (*"Use the
     support contact in your developer portal account"*).
   - Certification and fees: Netsmart's disclosures at
     `https://www.ntst.com/lp/certifications` (myAvatar Certified
     Edition, CHPL `15.04.04.2816.myAv.05.08.1.241227`, certified
     2024-12-27, *"A CareConnect FHIR subscription is required"*); the
     CHPL listing itself at `https://chpl.healthit.gov/` (search
     "Netsmart"); connection-service fees per
     `https://careconnect.netsmartcloud.com/terms-of-service/`.
   - Production base URLs and brands:
     `https://fhir.netsmartcloud.com/brand/brands.json`; firewall
     domains:
     `https://careconnect.netsmartcloud.com/docs/getting-started/network-configuration/`;
     whether the tenant restricts by IP: the tenant owner.

## Nextech

Primary sources: Nextech's
[developers portal](https://www.nextech.com/developers-portal), the
[Select/NexCloud FHIR R4 reference](https://nextechsystems.github.io/selectapidocspub/r4.html)
and the
[IntelleChartPRO FHIR R4 reference](https://nextechsystems.github.io/intellechartapidocspub/r4.html)
(both single-page references with an STU3/R4 toggle), the R4
[OpenAPI file](https://nextechsystems.github.io/selectapidocspub/nextech-partner-api-r4.openapi.json)
the Select reference links, the live and unauthenticated
[`/metadata`](https://select.nextech-api.com/api/r4/metadata) and
[`/.well-known/smart-configuration`](https://select.nextech-api.com/api/r4/.well-known/smart-configuration)
of both R4 servers (fetched 2026-09-01), Nextech's
[API Terms of Use](https://www.nextech.com/hubfs/Orthopedics_EHR/Nextech%20API%20Terms%20and%20Conditions%20Agreement%202022.pdf),
and the certification pages for
[Select/NexCloud](https://www.nextech.com/compliance/onc-health-it/nextech)
and [IntelleChartPRO](https://www.nextech.com/compliance/onc-health-it/intellechart).
Everything below distinguishes what those pages establish from what has
to be confirmed on the practice's own instance. Nothing in this chapter
is inferred from Epic or from any other vendor.

### Which product, and which door

Nextech's developers portal lists four products with API documentation:
**NexCloud/Select**, **IntelleChartPRO**, **Practice+ PM** and
**SRSPro** (whose documentation is hosted on MeldRx). Nextech's own
guidance: *"We will continue to support our STU3 APIs, however we
recommend that all new projects utilize the FHIR r4 APIs."*

Each R4 reference documents *"two different authorization models"*:

- **Partner authorization** - the older per-practice integration API.
  An OAuth 2.0 *password* grant (*"Use `password` (Resource owner
  credentials grant)"*) against
  `https://login.microsoftonline.com/nextech-api.com/oauth2/token`, an
  `nx-practice-id` header on every request, credentials that *"expire on
  your first login and must be reset through Microsoft"*, and an
  STU3-first surface. The Select reference is explicit: *"Partner
  integrations use resource owner credentials, not SMART."* Practice+
  documents only this door, as `client_credentials` with a Partner
  Secret and an Azure `resource` parameter, on FHIR *"STU 3 (3.0.1)"*.
  This codebase implements neither the password grant nor the Azure
  `resource` parameter; the partner door is **a second client, not this
  connector**.
- **SMART App authorization** - SMART App Launch 1.0.0 or 2.0.0 for
  user-facing apps, and for *"the information from an entire practice
  via background system apps"* the *"SMART Backend Services
  Authorization (STU 1.0.1) specification"*. **This connector is the
  SMART door, as a system app.**

The SMART model is published identically for **Select/NexCloud** (base
`https://select.nextech-api.com/api/r4`) and **IntelleChartPRO** (base
`https://api.intellechart.net/icp-fhir-api/`); both use the
authorization server `https://sts.mypatientvisit.com`. The profile is
written for Select/NexCloud and applies to IntelleChartPRO with a
different base URL and the differences called out below (no writes, a
slightly different resource list).

### One base URL per product, one client per practice

Nextech is multi-tenant, not federated. Its machine-readable endpoint
lists - [`NexCloud_R4.json`](https://www.nextech.com/hubfs/NexCloud_R4.json)
(1,772 entries) and [`ICP_R4.json`](https://www.nextech.com/hubfs/ICP_R4.json)
(1,499 entries) - each contain a single `Endpoint` resource with the
product base URL and one `Organization` per practice. The practice a
system app reaches is fixed **at registration**: the registration form
asks for *"The Portal Practice ID of the practice the client will
communicate with"*, and *"The practice that the system app wishes to
access must be setup with myPatientVisit, and established at app
registration time."*

Practically: `PHI_AI_FHIR_BASE_URL` is the product base URL,
`PHI_AI_FHIR_TOKEN_URL` is `https://sts.mypatientvisit.com/connect/token`,
and *which practice* lives in the client ID. Ingesting from three
Nextech practices means three registrations (three client IDs), one base
URL. Whether the `nx-practice-id` header is also required on the SMART
door is **not documented by the vendor** - the R4 OpenAPI requires it on
"partner-facing endpoints" and exempts `/metadata` and
`/.well-known/smart-configuration`; confirm on the instance.

### Registration

Verified from the R4 reference's "Registration" section and the
developers portal:

1. Submit Nextech's **app registration form** (the developers portal's
   "connection request form"; practice-initiated requests go through
   the client portal at `nextech.my.site.com`, *"Client login is
   required"*). Choose the app type *"A secure SMART Backend Service with
   no user interaction"*. *"The app must be confidential if it is a SMART
   Backend Service."*
2. Provide the application name, and for a backend service *"A TLS 1.2
   protected URL to the public JWK Set utilized by the app for JWT (JSON
   Web Key) credential signing"* and the Portal Practice ID. Redirect
   URIs are *"not required for SMART Backend Services"*. No static
   public-key upload is documented; the JWK Set URL is the registration
   artefact, so the URL (and the `kid` values it serves) is what cannot
   change casually afterwards.
3. Nextech issues a client ID. Confidential clients are also issued a
   client secret that *"will only be received once"*; it serves the
   authorization-code (user-facing) flow and is inert for this
   connector - `PHI_AI_FHIR_CLIENT_SECRET` is ignored for `nextech`.
4. The practice must have *"activated their FHIR services"* with Nextech
   (developers portal). Screening: the portal's patient-app text says an
   app *"will be screened for security and if the app meets requirements
   a connection will be made"*; no review timeline, SLA, sandbox or
   non-production environment is documented by the vendor.

### Auth: private-key JWT, RS384 or ES384, fifteen-minute tokens

From the R4 reference's "System apps" section, corroborated by the live
`smart-configuration` (`token_endpoint_auth_methods_supported:
["private_key_jwt"]`, `token_endpoint_auth_signing_alg_values_supported:
["RS384","ES384"]`, `grant_types_supported` includes
`client_credentials`):

1. You hold a private key; the matching public key is served from the
   JWK Set URL you registered. Nextech accepts *"either an RS384 or
   ES384 signature"*. `core/fhir/client.py` signs RS384 with an RSA key,
   so an RSA key pair is sufficient; the profile leaves
   `assertion_algorithm` at RS384.
2. Build a JWT with `iss` and `sub` both equal to the Nextech-issued
   client ID (*"must be exactly the same"*), `aud` = the token endpoint
   `https://sts.mypatientvisit.com/connect/token`, a unique `jti`,
   `iat` and `exp`. Nextech prints `kid` alongside the claims in its
   sample; the Backend Services guide Nextech points to defines `kid` as
   a JWT *header* parameter naming the key in your JWK Set, which is
   where `client.py` puts it when `PHI_AI_FHIR_JWT_KID` is set. Set it.
   Whether Nextech also reads a payload `kid` is not documented by the
   vendor.
3. POST to `/connect/token`: `grant_type=client_credentials`,
   `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
   `client_assertion=[signed JWT]`, **and**
   `scope=[spaceDelimitedDesiredScopes]` - the scope parameter is part
   of Nextech's documented request (see Scopes).
4. Nextech's documented response: `"expires_in": 900` and the granted
   `scope`. *"system apps are not issued refresh tokens, and so must
   always request a new access token upon previous access token
   expiration."* **Fifteen-minute tokens.** `client.py` authenticates
   once per run and tracks no expiry; a paged run longer than about
   fifteen minutes will start receiving `401 Unauthorized` until it
   re-authenticates. Close that gap before a production run.

`FHIRIngestionClient.authenticate_from_settings()` selects this flow
from the profile's `auth_flow`; `Settings.from_env()` does not require
a client secret for `nextech`.

### Scopes

Nextech documents its scope list in full. *"Nextech currently only
supports scopes that adhere to the format defined in version 1.0.0 of
the SMART app specification"* - that is `system/{Type}.read` - while
also listing the permission-v2 `.rs` forms, category sub-scopes and the
wildcard `system/*.read`. Rules stated by Nextech:

- *"SMART apps must only request the bare minimum scopes that are
  required for their app to function."*
- *"With the exception of the DocumentReference.write or
  DocumentReference.cud scopes, writing-related scopes are currently
  not supported."*
- When sub-scopes are requested, *"the parent resource-level scope ...
  will not be granted."*

The profile sets `requires_token_scopes=True`, so
`authenticate_from_settings()` sends one `system/{Type}.read` per entry
in `NEXTECH.supported_resources` - the documented v1 shape and the bare
minimum for what this system ingests. What Nextech does with a
scope-less request, or with a scope the registration was not granted,
is not documented by the vendor; keep `supported_resources` to types the
practice's CapabilityStatement lists.

### Population-scale reads: paged search works, bulk export exists

Unlike some vendors, Nextech documents unanchored searches
(`GET https://select.nextech-api.com/api/r4/Patient?_count=25`), so
`core/fhir/scheduler.py`'s paged path can walk an entire practice.
Verified paging facts: *"the first ten matches ordered by entered date
are returned by default"*, `_count` *"up to fifty"*, *"Search results
are limited to 50 matches per page"*, and bundles carry first/next/
previous/last links (`page_size=50`). `_lastUpdated` is documented
(`yyyy-MM-dd`, and *"As of version 14.3"* a full dateTime) and Nextech
recommends it *"to avoid re-querying unmodified data"*. TLS 1.2 is
mandatory and only JSON is served (*"any type explicitly defined in the
request's Accept header will be ignored"*).

Nextech's rate-limit guidance is aimed at interactive apps - *"The API
is intended for on-demand requests for user interaction in real-time,
try to avoid synchronizing data"*, *"stagger"*, *"non-peak business
hours"* - so for a whole-practice history, bulk export is the better
fit where the practice's release has it.

### Bulk Data Export

Documented in the R4 reference's "Bulk FHIR Export" chapter for
Select/NexCloud (every parameter *"Initial Version 16.9"*) and for
IntelleChartPRO; the live Select CapabilityStatement advertises
`rest.operation: export` and instantiates the Bulk Data
CapabilityStatement.

**Three kick-offs**, all `GET` with two **required** headers,
`Accept: application/fhir+json` and `Prefer: respond-async`
(`bulk_client.py` sends both):

- `{base}/Patient/$export` - *"all patients"*
- `{base}/Group/{GroupID}/$export` - *"a subset of patients within a
  defined group"*
- `{base}/$export` - *"all available data contained in the FHIR server"*

**Parameters:** `_outputFormat` (default `application/fhir+ndjson`),
`_type` (comma-delimited types; all if omitted) and `_since` -
*"Resources will be included in the response if their state has changed
after the supplied time"*. Nextech therefore has an incremental path;
`bulk_client.py` exposes no `since` parameter today, so every run is a
full re-extract until that is added.

**Status:** `202 Accepted` + `Content-Location` pointing at
`{base}/Export/{ExportJobID}`; a failed kick-off returns 4XX/5XX with an
`OperationOutcome`. Polling `GET {base}/Export/{ExportJobID}` has three
states: **In-Progress** (202 with `X-Progress` and *"The Retry-After
HTTP response header gives a delay time in seconds indicating the amount
of time to wait before making another polling request"* - their example
is `Retry-After: 120`), **Error** (500 with an `OperationOutcome`; their
example text is *"An internal timeout has occurred"*), **Complete** (200
with the manifest; the `Expires` header *"indicates when the files in
the response will no longer be available for access"*).
`bulk_client.poll_status()` logs `X-Progress` but does not read
`Retry-After`; set `PHI_AI_BULK_POLL_INTERVAL_SECONDS` to Nextech's
figure (120) rather than the default.

**Manifest:** `transactionTime`, `request`, `requiresAccessToken`,
`output[{type,url}]`, `error[]`. Nextech's sample has
`"requiresAccessToken": false` and file URLs on Azure blob storage
(`storagesample.blob.core.windows.net`). `bulk_client.iter_ndjson_resources()`
sends the bearer token on every file download regardless; against
Nextech that would present a live token to a non-Nextech host. Honour
`requiresAccessToken` before a production run. `DELETE
{base}/Export/{ExportJobID}` cancels a job.

**Groups.** Nextech's groups are the practice's *"letter writing"*
groups: Patient search documents `group-id` (*"The letter writing group
of the patient"*, v16.8) and `GET /r4/Patient/ID?group-id=20` returns a
group's patient ids. The `Group` resource itself is not searchable on
Select (live CapabilityStatement: `Group` carries only the export
operation; IntelleChartPRO's advertises search-type too). A Group ID
therefore comes from the practice's own Nextech configuration.
`bulk_scheduler.py` performs Group-level export only and refuses to
start without `PHI_AI_FHIR_GROUP_ID`; with Nextech either obtain a
letter-writing-group id or extend the scheduler to the documented
`Patient/$export`, which needs no group at all.

**Not documented by the vendor:** any export frequency limit, any group
size guidance, and how long export files stay available. `bulk_scheduler.py`'s
24-hour default interval is a configuration default, not a Nextech
fact; Nextech's only scheduling guidance is *"If you need to
synchronize data, it is best to do so during non-peak business hours.
Which vary on a per practice basis."*

### Writes

Plainly: **the Select/NexCloud R4 surface is writable for one type.**
*"Currently, only creating a document via a POST
https://select.nextech-api.com/api/r4/DocumentReference call is
supported"*, gated by the `system/DocumentReference.write` (v1) or
`system/DocumentReference.cud` (v2) scope - the only *"writing-related
scopes"* Nextech supports. **IntelleChartPRO's R4 surface is
read-only:** *"Currently, no write calls are supported."*

How the platform behaves: `core/fhir/delivery/writer.py` reads the
destination's CapabilityStatement and refuses any type it does not
advertise `create` for. The live Select CapabilityStatement advertises
`create` on `DocumentReference` and also on `DiagnosticReport`; the
latter is absent from Nextech's prose, is not in
`NEXTECH.writable_resources`, and should be treated as unconfirmed until
Nextech says otherwise. Conditional create (`If-None-Exist`) is not
documented by the vendor, so `supports_conditional_create=False` and
re-runnable delivery must be gated on an external record of what was
sent.

Operationally: `core/fhir/delivery/__main__.py` builds the destination
token request on the `nextech` profile and sends
`system/DocumentReference.write` (derived from `writable_resources`),
the scope Nextech documents for the create; and the fifteen-minute,
non-refreshable tokens apply to delivery too.

The real write path for anything else is not FHIR R4: Nextech's
**partner** APIs (Select/NexCloud STU3 and Practice+ STU3) document
Patient create/update, Appointment book/confirm (`PUT`/`PATCH`),
`PaymentReconciliation` create and Composition (*"Create non-clinical
patient notes"*), authenticated with the password grant or a client
secret at `login.microsoftonline.com` and the `nx-practice-id` header.
That is a second client, not this profile.

### Limits, fees and terms

- **Rate limit:** *"restricted to a rate limit of 20 requests per second
  per endpoint"*, enforced with `429 Too Many Requests`; *"We advise to
  design to handle these requests with Exponential backoff."* The limit
  is *"subjet to change"* (sic). The profile throttles to 600/min
  (half the per-endpoint ceiling, because the client's throttle is
  global). The older partner "Getting Started" PDF the portal still
  links states *"1,000 API calls per day (12AM - 12AM UTC) combined
  across all applications for a single client"* - confirm with Nextech
  which figure binds your registration.
- **Fees:** API Terms (effective 10/1/2022): *"At this time Nextech does
  not charge API connection fees for APIs required under the 2015 or
  2015 Cures Act Update CEHRT Regulations"*, reserving the right to do
  so in future; the developers portal adds that patients are not
  charged. The v20 mandatory disclosures list practice-side costs
  (per-doctor licences, conversions, implementation and training,
  annual support, third-party interfaces) and state the practice
  contract *"does not contain limitations for the certified
  capabilities."*
- **Termination:** *"Either you, Nextech, or a provider engaging your
  application services may terminate your right to use the Nextech API
  at any time, with or without cause or notice."*
- **Transport:** TLS 1.2 required; JSON only.
- **Response codes** Nextech documents beyond the usual: 408, 422, 501,
  502 and *"523 Authentication Fault - Unexpected exception in regards
  to authentication"*.

### Certification

Nextech Select and NexCloud v20 is ONC-certified by Drummond
(certificate dated 12/02/2025, ONC-ACB Certification ID
`15.04.04.2051.Ntec.20.12.1.251202`, [CHPL listing 11722](https://chpl.healthit.gov/#/listing/11722));
the certificate's criteria include *"(g)(2-7, 9-10)"* and the
[v20 Mandatory Disclosures](https://cdn.prod.website-files.com/688a8f75f9f5115eb8891d4a/6a5002f3420cced0d7586687_Nextech%20v20%20Mandatory%20Disclosures%2012.15.2025.pdf)
list `170.315 (g)(10)` by name. Nextech EHR (ICP) 9 is certified
separately (`15.04.04.2051.Inte.09.02.0.251202`,
[CHPL 11724](https://chpl.healthit.gov/#/listing/11724)); SRS EHR v13
too (`15.04.04.2051.SRSE.13.04.1.260220`, CHPL 11775). The (g)(10)
criterion mandates SMART Backend Services for system scopes, group-level
Bulk Data and publicly accessible documentation
([healthit.gov](https://www.healthit.gov/test-method/standardized-api-patient-and-population-services));
for Nextech that only corroborates what its own pages already document
- nothing in the profile rests on the mandate alone.

### Validate before ingesting

Both endpoints below are served without credentials (the OpenAPI exempts
them from the bearer-token requirement), so this is a two-minute check:

```
GET {base_url}/.well-known/smart-configuration
GET {base_url}/metadata
```

Look for: `token_endpoint` = `https://sts.mypatientvisit.com/connect/token`,
`token_endpoint_auth_methods_supported` containing `private_key_jwt`,
`token_endpoint_auth_signing_alg_values_supported` containing `RS384`;
in the CapabilityStatement, `fhirVersion 4.0.1`, `instantiates`
containing the Bulk Data and US Core server statements,
`rest[0].operation` containing `export`, and every type in
`NEXTECH.supported_resources` listed with `read` and `search-type`. Then
confirm with the practice: its release (bulk needs 16.9+, ServiceRequest
19.2+), that FHIR services are activated, the letter-writing group id if
Group export is planned, and - for IntelleChartPRO - that the narrower
resource list matches what you ingest. A scope the registration was not
granted, or a type the release does not expose, is a mismatch the
profile cannot see; Nextech does not document the failure shape, so
check before the first scheduled run rather than after.

### What the emulator reproduces

`emulators/vendors.py` `"nextech"` (port 9115, base
`http://127.0.0.1:9115/api/r4`) reproduces the seams Nextech documents:

- The token endpoint honours a JWT client assertion signed **RS384 or
  ES384** (both are Nextech-documented and both appear in its live
  smart-configuration) and refuses any other algorithm or a non-JWT
  value with `invalid_client`.
- A **client secret gets `invalid_client`**: `private_key_jwt` is the
  only token-endpoint auth method Nextech advertises.
- A token request **without explicit `system/{Type}.read` scopes is
  refused** - Nextech's documented request carries `scope`, and the
  Backend Services STU 1.0.1 guide it mandates requires it. Caveat: the
  shared `requires_token_scope` field also refuses `*` scopes, which is
  Oracle Health's rule, not Nextech's (Nextech documents
  `system/*.read`); the real client never sends a wildcard, so this is
  inert for a green run, but it is not a Nextech behaviour.
- **`$export` exists** at Patient, Group and system level, returns
  `202` + `Content-Location`, stays in progress across the first poll,
  then serves NDJSON.
- The CapabilityStatement advertises `create` for **DocumentReference
  only**; a create for any other type gets a 422 OperationOutcome, and
  `If-None-Exist` gets 412 because Nextech does not document it.
- Forced pagination (`page_size=2`) so the `next` link is exercised.

Not reproduced, because the shared server cannot: Nextech's
fifteen-minute non-refreshable tokens, the 20 req/s per-endpoint 429,
`Retry-After`/`X-Progress` values, and export files hosted on a separate
storage host with `requiresAccessToken: false`. Those are the profile's
notes, not the emulator's, and a green run does not prove them.

### Setting it up

Non-PHI setup, from nothing to a first ingest and a first (dry-run)
delivery. Every fact about Nextech below comes from its R4 reference
(`nextechsystems.github.io/selectapidocspub/r4.html`) and developers
portal; every command is this repository's own entry point. Run Python
commands with the repo's interpreter (`.venv/bin/python`) from the repo
root.

1. **Register with Nextech.**
   1. Open `https://www.nextech.com/developers-portal` and submit the
      **connection request form** (the "3rd Party API Access" section;
      the R4 reference calls it "the app registration form"). If a
      practice is initiating on your behalf, its staff use the client
      portal at `https://nextech.my.site.com/nextech/s/login/`
      ("Client login is required").
   2. App type: **"A secure SMART Backend Service with no user
      interaction"**. It **must** be registered as a **confidential**
      client.
   3. Submit: the application name; a **TLS 1.2 protected URL to your
      public JWK Set** (step 2 produces it - host it first, then
      register); and the **Portal Practice ID** of the practice you will
      read from. Redirect URIs are not required for a backend service.
   4. Nextech screens the app ("The app will be screened for security
      and if the app meets requirements a connection will be made") and
      issues a **client ID**; a confidential client also receives a
      client secret **once** - store it, but this connector does not use
      it. No sandbox, non-production environment or review timeline is
      documented by Nextech; ask your Nextech representative.
   5. What cannot change casually later: the JWK Set URL (and the `kid`
      values it serves) and the practice bound to the client ID. A
      second practice is a second registration.
   6. Ask the practice to confirm its **FHIR services are activated**
      and which **release** it runs (bulk export needs Select 16.9+).

2. **Generate the key pair and publish the JWK Set.** Nextech accepts
   RS384 or ES384; this codebase signs RS384 with an RSA key, so make an
   RSA key (2048 is sufficient; 4096 costs nothing).

   ```bash
   mkdir -p ~/phi-ai-keys/nextech && cd ~/phi-ai-keys/nextech
   openssl genrsa -out nextech_private_key.pem 4096
   openssl rsa -in nextech_private_key.pem -pubout -out nextech_public_key.pem
   chmod 600 nextech_private_key.pem
   ```

   Build the JWK Set (kid, alg, use) with the repo's own PyJWT:

   ```bash
   cd "<repo root>" && .venv/bin/python - <<'EOF'
   import json, jwt
   from cryptography.hazmat.primitives import serialization
   import os
   pem = open(os.path.expanduser("~/phi-ai-keys/nextech/nextech_public_key.pem"), "rb").read()
   pub = serialization.load_pem_public_key(pem)
   jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
   jwk.update({"kid": "phi-ai-nextech-2026-09", "alg": "RS384", "use": "sig"})
   json.dump({"keys": [jwk]}, open(os.path.expanduser("~/phi-ai-keys/nextech/jwks.json"), "w"), indent=2)
   print("wrote jwks.json with kid", jwk["kid"])
   EOF
   ```

   Host `jwks.json` at a world-readable **HTTPS (TLS 1.2)** URL you
   control - that URL is what you register in step 1.3. Nextech
   documents no static public-key upload. Never publish, commit or
   email `nextech_private_key.pem`.

3. **Configure the PHI AI environment** (names are exactly those
   `core/config/settings.py` reads, prefix `PHI_AI_`; add these to the
   deployment's existing `.env` alongside the non-EMR settings it
   already has):

   ```bash
   PHI_AI_EMR_VENDOR=nextech
   PHI_AI_FHIR_BASE_URL=https://select.nextech-api.com/api/r4        # IntelleChartPRO: https://api.intellechart.net/icp-fhir-api
   PHI_AI_FHIR_TOKEN_URL=https://sts.mypatientvisit.com/connect/token
   PHI_AI_FHIR_CLIENT_ID=<client ID issued by Nextech>
   PHI_AI_FHIR_PRIVATE_KEY_PATH=/run/secrets/nextech_private_key.pem   # mount the file in; never bake it into an image
   PHI_AI_FHIR_JWT_KID=phi-ai-nextech-2026-09                          # must equal the kid in the hosted jwks.json
   PHI_AI_FHIR_GROUP_ID=<letter-writing group id from the practice>    # required by bulk_scheduler; omit for paged search only
   PHI_AI_BULK_POLL_INTERVAL_SECONDS=120                                # Nextech's documented Retry-After example
   PHI_AI_BULK_MAX_WAIT_SECONDS=14400
   PHI_AI_BULK_INTERVAL_SECONDS=86400                                   # not a Nextech limit; schedule for the practice's off-peak hours
   PHI_AI_INTERVAL_SECONDS=3600                                         # paged scheduler cadence
   ```

   Do **not** set `PHI_AI_FHIR_CLIENT_SECRET` - the profile's
   `auth_flow` is `smart_backend_services`, `Settings.from_env()` does
   not require a secret for `nextech`, and Nextech's system-app flow
   presents a signed JWT "instead of a client secret". Scopes are
   derived automatically: `requires_token_scopes=True` makes
   `authenticate_from_settings()` send one `system/{Type}.read` per
   entry in `NEXTECH.supported_resources`; there is no scope setting to
   configure. Both `_require`d FHIR variables above must be present or
   startup fails with a `ConfigError` naming the variable.

4. **Pre-flight against the practice's server** (no credentials needed
   for either endpoint):

   ```bash
   curl -sS "$PHI_AI_FHIR_BASE_URL/.well-known/smart-configuration" | jq '{token_endpoint, token_endpoint_auth_methods_supported, token_endpoint_auth_signing_alg_values_supported, grant_types_supported}'
   curl -sS -H 'Accept: application/fhir+json' "$PHI_AI_FHIR_BASE_URL/metadata" | jq '{fhirVersion, instantiates, ops: [.rest[0].operation[]?.name], types: [.rest[0].resource[] | {type, interactions: [.interaction[]?.code], ops: [.operation[]?.name]}]}'
   ```

   Expect: `token_endpoint` = `https://sts.mypatientvisit.com/connect/token`,
   `private_key_jwt` in the auth methods, `RS384` in the signing
   algorithms, `client_credentials` in the grant types; `fhirVersion`
   `4.0.1`, `instantiates` containing `.../uv/bulkdata/CapabilityStatement/bulk-data`
   and `.../us-core-server`, `export` in `ops`, and every type in
   `NEXTECH.supported_resources` with `read` and `search-type`. On
   Select, `DocumentReference` should show `create`; on IntelleChartPRO
   nothing should. If `export` is missing, the practice's release is
   below 16.9 or bulk is not enabled - use paged search (step 5b) and
   ask Nextech.

5. **First ingest.**
   - **(a) Bulk export** (needs `PHI_AI_FHIR_GROUP_ID`):

     ```bash
     .venv/bin/python -m core.fhir.bulk_scheduler --once
     ```

     Success looks like: a token request to `sts.mypatientvisit.com`,
     the log line `Kicking off bulk export: group=<id> resource_types=...`,
     one or more `Bulk export still in progress: <X-Progress>` lines
     (Nextech's example is `"50% complete"`), then the manifest, NDJSON
     downloads per type, and exit code 0. Failure shapes you may see:
     `PHI_AI_FHIR_GROUP_ID is not set - bulk export requires a Group
     FHIR ID` (exit 1, before any request); `Expected 202 Accepted from
     kickoff, got 4xx` with Nextech's `OperationOutcome` in the log if
     the release or scope does not allow `$export`; a `BulkExportError`
     from a 500 poll response if Nextech reports an export error
     (their example: `An internal timeout has occurred`). The refusal
     `<vendor>'s profile records no Bulk Data Export support` cannot
     occur for `nextech` - the profile records support - it is what
     you would see if the flag were ever set to `False`.
   - **(b) Paged search** (no group needed; Nextech documents unanchored
     `GET /r4/Patient?_count=...`):

     ```bash
     .venv/bin/python -m core.fhir.scheduler --once
     ```

     Success: one paged walk per type in `supported_resources` with
     `_count=50`, following `next` links, exit code 0. Watch for `401`
     after roughly fifteen minutes - Nextech's system-app tokens are
     documented at `expires_in: 900` with no refresh tokens, and the
     client does not yet re-authenticate mid-run - and for `429`, which
     Nextech returns above 20 requests/second/endpoint and which the
     600/min throttle should keep you under.

6. **First delivery (dry run first).** Delivery uses its own
   credentials; without `--confirm` nothing is sent:

   ```bash
   export PHI_AI_DELIVERY_CLIENT_ID=<client ID for the destination practice>
   export PHI_AI_DELIVERY_TOKEN_URL=https://sts.mypatientvisit.com/connect/token
   export PHI_AI_DELIVERY_PRIVATE_KEY_PATH=/run/secrets/nextech_private_key.pem
   .venv/bin/python -m core.fhir.delivery --destination https://select.nextech-api.com/api/r4 --vendor nextech --identity-map identity_map.csv --purpose-of-use "record transfer - <ticket>" --patient <source patient id>
   ```

   What happens against Nextech: `writer.py` reads the destination's
   CapabilityStatement and skips every type it does not advertise
   `create` for - on Select that leaves `DocumentReference` (and, in the
   live statement, `DiagnosticReport`, which Nextech's prose does not
   document); on IntelleChartPRO it skips everything, because Nextech
   states "no write calls are supported". Two Nextech-specific reasons a
   real write can still fail: the registration must have been granted
   `system/DocumentReference.write` - the delivery token request
   (built on the `nextech` profile) asks for exactly that scope, and
   Nextech refuses a scope the registration lacks; and the destination
   token expires after fifteen minutes. Nextech documents no conditional
   create, so re-running a delivery requires `--allow-duplicates` and an
   external record of what was already sent. Do not add `--confirm`
   until a dry run shows exactly the DocumentReferences you expect.
   (`--vendor` accepts every `PROFILES` key, `nextech` included.)

7. **Local rehearsal against the emulator** (synthetic data only):

   ```bash
   .venv/bin/python -m emulators --vendor nextech        # http://127.0.0.1:9115/api/r4, launch page /emulator/launch
   .venv/bin/python -m pytest tests/test_emulator_integration.py -k nextech -q
   ```

   To point the real client at it: `PHI_AI_FHIR_BASE_URL=http://127.0.0.1:9115/api/r4`,
   `PHI_AI_FHIR_TOKEN_URL=http://127.0.0.1:9115/oauth2/token`, any
   client ID, your RSA key from step 2. The emulator reproduces the
   RS384/ES384-only assertion check, the `invalid_client` on a client
   secret, the scope-required token request, `$export` with an async
   poll, create for `DocumentReference` only, and forced pagination. It
   does not reproduce the 900-second token, the 429, or `Retry-After`.

8. **Known limits and where to confirm them.**
   - Rate limit 20 req/s/endpoint with 429 and exponential backoff; the
     older partner "Getting Started" PDF says 1,000 calls/day per client
     - confirm which binds your registration:
     `https://nextechsystems.github.io/selectapidocspub/r4.html` (Rate
     Limiting) and
     `https://www.nextech.com/hubfs/Developers%20Portal/Nextech%20API%20Getting%20Started%20Document%20-%20Published.pdf`.
   - Token lifetime 900 s, no refresh tokens: same reference, "System
     apps".
   - Bulk from Select 16.9; `_since` supported; `Retry-After` on polls;
     manifest `requiresAccessToken` and file `Expires`: same reference,
     "Bulk FHIR Export".
   - Writes: `DocumentReference` create only (Select); none
     (IntelleChartPRO): same references, "Writing".
   - Certification and CHPL: `https://www.nextech.com/compliance/onc-health-it/nextech`
     (CHPL listing 11722) and `.../intellechart` (11724).
   - Fees and termination: API Terms
     `https://www.nextech.com/hubfs/Orthopedics_EHR/Nextech%20API%20Terms%20and%20Conditions%20Agreement%202022.pdf`.
   - Contact: the developers-portal request form; Nextech's own
     Getting Started PDF gives `code.nextech@nextech.com` for API
     questions.
   - Not documented by Nextech, so ask before relying on it: sandbox or
     non-production environment, review timeline, export frequency and
     file retention, behaviour on a scope-less or ungranted-scope token
     request, whether `nx-practice-id` is needed on the SMART door.

## Proof of integration: the end-to-end matrix

`tests/test_e2e_matrix.py` - and its scriptable twin
`scripts/e2e_matrix.py` - is the one run that exercises every profile in
this file in both directions, on synthetic data, with no vendor account:

- **Every vendor emulator as a source.** For each entry in
  `emulators/vendors.py` `VENDORS` the matrix starts that emulator,
  authenticates with the grant the vendor documents (a JWT client
  assertion or a client secret - either, where the vendor documents
  both) signed with the profile's `assertion_algorithm`, reads the
  emulator's CapabilityStatement, ingests by paged search through
  `FHIRIngestionClient.iter_resources()`, and ingests by `$export`
  through `core/fhir/bulk_client.py` where the profile records
  `supports_bulk_export` - asserting the OperationOutcome refusal, never
  a silent empty result, where it does not.
- **Every vendor as a target.** For each emulator the matrix delivers
  records through `core/fhir/delivery/writer.py`, which reads the
  destination's CapabilityStatement first: delivery must succeed for
  every type the statement advertises `create` for, and must end in a
  structured refusal ("does not advertise create for {Type}") for every
  type it does not. The read-only vendors therefore prove the refusal
  path; the vendors whose emulator entry has a non-empty `creatable`
  tuple prove the write path.
- **The full matrix.** Every source against every target - `VENDORS`
  squared, fifteen by fifteen today - so records that have been through
  one vendor's seams (its grant, its paging, its export shape) are
  delivered into every other vendor's write surface, and every cell
  records success or a structured refusal, never an exception. The
  diagonal is asserted too: a delivery pointed back at its own source is
  refused by the writer (`assert_not_source_system`), and that refusal
  is what the diagonal proves.
- **Non-PHI, by construction.** The emulators serve the synthetic
  dataset `scripts/mock_epic_server.py` defines (the one set of fake
  patients in this repository); nothing in the matrix touches a real
  endpoint or a real patient.

Run it:

```bash
.venv/bin/python -m pytest tests/test_e2e_matrix.py -v                 # one test per cell, e.g. test_pair[epic->cerner]
.venv/bin/python scripts/e2e_matrix.py --out ../private-notes/e2e-proof.md   # the same code, writing the proof table
.venv/bin/python scripts/e2e_matrix.py --vendors epic,cerner --keep-running  # a subset, emulators left running
```

The test module imports its helpers from the script, so the pytest run
and the proof-writing run execute the same code; the emulators bind
their `DEFAULT_PORTS` ports, so a port already in use fails the session
rather than moving elsewhere. Before each source's real grant the
matrix sends that vendor's documented refusals - an assertion in an
algorithm the vendor does not accept, an unsigned one, one signed by an
unregistered key, a scope-less request where scopes are required, the
wrong grant - and asserts each observed 400 and its error code; the
proof's Sources table records what was refused per source.

What the matrix does **not** cover: `python -m core.fhir.delivery`'s own
token request. The matrix delivers through `writer.py` with tokens it
mints itself; the delivery CLI's per-profile token request (the
destination's algorithm, grant, `kid` and `system/{Type}.write` scopes)
is pinned by `tests/test_delivery.py`, parametrised over every profile,
against a recording stand-in rather than an emulator.

The script writes the proof table - per vendor as a source (grant,
algorithm, CapabilityStatement, paged ingest, `$export` or its refusal)
and per vendor as a target (delivery success or refusal, type by type),
plus the source-by-target matrix - to `private-notes/e2e-proof.md`
beside the checkout (the sibling `private-notes/` directory, never
inside the repository). A green matrix proves the client and the
delivery writer handle every documented shape in this file. It is not
certification, and it does not replace the per-instance "Validate
before ingesting" step in each chapter.

## What the emulators do and do not prove

`emulators/` runs one server per vendor in `emulators/vendors.py`
`VENDORS`, on the port `DEFAULT_PORTS` assigns it (9101-9115 today;
`python -m emulators` prints every base URL as it starts), reproducing
each vendor's seams:

- **Which credential its token endpoint accepts** - a JWT client
  assertion, a client secret, or both. Oracle Health, TruBridge and
  Netsmart document both grants and their emulators honour both; a
  secret sent to a JWT-only vendor, or an assertion sent to
  athenahealth, is `invalid_client`.
- **Which signing algorithms it honours for that assertion**
  (`EmulatorVendor.assertion_algorithms`): ES384 only where the vendor
  documents only ES384; RS384 or ES384 where the vendor documents both;
  a vendor's own published list where it publishes one; RS384 alone
  where the vendor documents nothing - each entry in
  `emulators/vendors.py` says which, with the citation. An assertion
  signed with any other algorithm is refused as `invalid_client`, so a
  client that signs everything RS384 fails against an ES384-only
  vendor's emulator, not against a practice.
- **Whether the token request must carry explicit system scopes**
  (`requires_token_scope`), as Oracle Health, ModMed, Altera, Greenway,
  Practice Fusion, TruBridge, Netsmart and Nextech each document.
  Whether a **wildcard** scope is refused is a separate flag
  (`refuses_wildcard_scope`), true only for Oracle Health, which
  documents the refusal; every other emulator accepts one, because its
  vendor either documents a wildcard form or documents nothing.
- **Whether `$export` exists** - the genuinely asynchronous handshake
  where it does, and the OperationOutcome refusal, never an empty
  result, where the profile records none (NextGen, the only profiled
  vendor without one). Where a vendor documents the operation but not
  its parameters, throttle or Group-id procedure (TruBridge and MEDHOST
  publish Group export and a bulk-data version, and nothing about
  `_since`, `Retry-After` or how a Group id is issued), the emulator
  serves the documented handshake and the chapter lists what a green
  run does not prove.
- **What the CapabilityStatement advertises as creatable.** Nothing at
  all on the read-only surfaces (eClinicalWorks, MEDITECH, ModMed,
  Altera, Greenway, Veradigm, Practice Fusion, TruBridge, MEDHOST), so
  the delivery writer's structured refusal is exercised against a server
  that really advertises no `create`; DocumentReference and
  DiagnosticReport on Netsmart, and DocumentReference on Nextech, so the
  write path is exercised too. **Conditional-create behaviour** (only
  Oracle Health honours `If-None-Exist`; the rest answer `412`) and
  **forced pagination** round it out.

A green integration run proves the client handles those shapes - the
majority of integration defects - but it is not certification, and it
never overrides the instance-level confirmation steps each chapter above
calls for. Several chapters record where the emulator is deliberately
looser or stricter than the vendor - Veradigm's, TruBridge's and
Altera's real servers over-advertise `create` that the emulators do not;
MEDHOST refuses the system-level `$export` the shared emulator serves;
Greenway's `_since` defaults to the last 24 hours - so read "What the
emulator reproduces" in each chapter before trusting a green run for
that vendor.
