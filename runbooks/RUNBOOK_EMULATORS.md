# Runbook: EMR emulators

```bash
python -m emulators                    # all six
python -m emulators --vendor cerner    # one
```

Synthetic stand-ins for Epic, Oracle Health (Cerner), athenahealth,
eClinicalWorks, MEDITECH and NextGen, so the whole integration can be
exercised without a real EMR — registration with six vendors takes weeks
each and sandboxes are not always obtainable (MEDITECH has no
self-service sandbox at all).

**All data is synthetic.** The dataset is imported from
`scripts/mock_epic_server.py` rather than copied, so there is one set of
fake patients in this repository.

| Vendor | Port | Auth | `$export` | Creatable |
|---|---|---|---|---|
| Epic | 9101 | JWT assertion | yes | DocumentReference |
| Cerner | 9102 | JWT assertion **or** Basic secret; explicit scopes required | yes | DocumentReference, Condition, Observation |
| athenahealth | 9103 | **client secret** | yes | DocumentReference |
| eClinicalWorks | 9104 | JWT assertion | yes | nothing |
| NextGen | 9105 | JWT assertion | **no** | DocumentReference |
| MEDITECH | 9106 | JWT assertion | yes | nothing |

---

## They reproduce the seams, not just the happy path

Testing against one generic FHIR server proves very little. The
differences that break EMR integrations are not in the resources — those
are standardised — they are in the seams, and each emulator reproduces
its vendor's honestly, **including the unhelpful behaviours**:

- **athenahealth rejects a JWT assertion** with `invalid_client`, as it
  would live. A client assuming every vendor takes an assertion fails
  here rather than in production. The inverse holds everywhere else: a
  client secret sent to a JWT-only vendor gets the same refusal.
- **Cerner demands explicit system scopes at the token endpoint** —
  a scope-less request (Epic's normal shape, since Epic's backend token
  request takes no scope parameter at all) or a wildcard scope gets
  `invalid_scope`, exactly as Oracle Health documents. It also accepts
  the secret in an RFC 2617 Basic Authorization header, Oracle's
  primary documented system-account mode, alongside the JWT assertion.
- **NextGen refuses `$export`** with an `OperationOutcome`, not an
  empty result — a caller must not be able to mistake "unsupported" for
  "no data". (eClinicalWorks used to be modelled this way too; their
  portal now documents bulk FHIR APIs, so its emulator serves
  `$export`.)
- **Epic advertises `create` for almost nothing**, and **MEDITECH and
  eClinicalWorks advertise it for nothing at all** (MEDITECH's public
  surface is view-only; eCW's Create APIs are a contracted add-on the
  emulator models as absent), so the delivery capability check has
  something real to refuse.
- **Only Cerner honours `If-None-Exist`.** The others return `412`, which
  is what makes duplicate-prevention testable.
- **Pagination is 2 per page regardless of `_count`**, so a client that
  mishandles the `next` link fails here, not against a large practice.
- **Bulk export is genuinely async** — the first status poll returns
  `202`. A client assuming the first poll is ready breaks against every
  real implementation.

---

## In-context launch, end to end

Open `http://127.0.0.1:9101/emulator/launch` — this stands in for a
clinician viewing a patient's chart. The button sends a SMART EHR launch
at the platform, which should land on that patient without a second login.

```bash
python -m emulators --record-url http://127.0.0.1:8080/smart/launch
```

The flag points at the platform's SMART launch endpoint. The argument
parser reads it verbatim, so type it exactly as printed.

**The id_token is really signed.** The emulator generates an RSA key at
startup and publishes it at `/.well-known/jwks.json`. An emulator
returning an unsigned or fixed token would let a client that *skips
signature verification* pass its tests — which is precisely the defect
worth catching, since an unverified id_token is an identity assertion by
whoever sent it.

PKCE is enforced, authorization codes are single-use, and a mismatched
verifier is rejected.

Register an emulator in `config/smart_issuers.yaml` like any EMR:

```yaml
issuers:
  - label: "Epic emulator"
    issuer: "http://127.0.0.1:9101/api/FHIR/R4"
    vendor: epic
    client_id: "emulator-client"
    roles: [viewer]
    record_source: true
```

`record_source` is the config loader's own key name — it marks an issuer
whose data this deployment ingests. The loader matches it literally, so
type it exactly as printed.

> **A misspelling here is silent.** The loader treats an unrecognised key
> as forward-compatible rather than fatal, so a mistyped `record_source`
> raises no error — the issuer is loaded as though the flag were absent,
> stops being treated as a source EMR, and the refuse-to-write-to-a-source
> guard (`runbooks/RUNBOOK_EMR_EXCHANGE.md`) stops covering it. Confirm it
> by launching, not by re-reading the YAML.

`http` is accepted **only for loopback** (127.0.0.1, ::1, localhost) —
traffic that never crosses a network cannot be rewritten in transit, the
same carve-out RFC 8252 makes for native apps. A remote `http` issuer is
still refused outright.

---

## What a green run proves, and what it does not

It proves the client handles the shapes these servers produce: auth flows,
pagination, the async bulk-export handshake, capability-gated writes,
conditional create, and in-context launch with a verified id_token. That
is the majority of integration defects.

**It is not certification.** A particular customer's build may behave
differently — every profile in `core/fhir/emr_profiles.py` still says to
confirm against the instance's own `CapabilityStatement`, and that stands.
