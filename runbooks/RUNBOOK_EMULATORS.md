# Runbook: EMR emulators

```bash
python -m emulators                    # every vendor in VENDORS, ports from DEFAULT_PORTS
python -m emulators --vendor cerner    # one
```

Synthetic stand-ins for every vendor in `emulators/vendors.py`
`VENDORS` — one per profile in `core/fhir/emr_profiles.py` — so the
whole integration can be exercised without a real EMR: registration with
each vendor takes weeks, and sandboxes are not always obtainable
(MEDITECH, Greenway and Nextech document no self-service sandbox at all;
Practice Fusion's is usable only after the app is approved).

**All data is synthetic.** The dataset is imported from
`scripts/mock_epic_server.py` rather than copied, so there is one set of
fake patients in this repository.

| Vendor | Port | Auth | `$export` | Creatable |
|---|---|---|---|---|
| Epic | 9101 | JWT assertion (RS384) | yes | DocumentReference |
| Oracle Health (Cerner) | 9102 | JWT assertion **or** client secret (RS384); explicit scopes required; wildcards refused | yes | DocumentReference, Condition, Observation |
| athenahealth | 9103 | client secret | yes | DocumentReference |
| eClinicalWorks | 9104 | JWT assertion (RS384) | yes | nothing |
| NextGen Healthcare | 9105 | JWT assertion (RS384) | **no** | DocumentReference |
| MEDITECH Expanse | 9106 | JWT assertion (RS384) | yes | nothing |
| ModMed | 9107 | JWT assertion (ES384); explicit scopes required | yes | nothing |
| Altera Digital Health | 9108 | JWT assertion (RS384); explicit scopes required | yes | nothing |
| Greenway Health | 9109 | JWT assertion (ES384); explicit scopes required | yes | nothing |
| Veradigm | 9110 | JWT assertion (RS384/RS256) | yes | nothing |
| Practice Fusion | 9111 | JWT assertion (RS384/ES384); explicit scopes required | yes | nothing |
| TruBridge | 9112 | JWT assertion **or** client secret (RS256/RS384/ES256/ES384); explicit scopes required | yes | nothing |
| MEDHOST | 9113 | JWT assertion (RS384/ES384) | yes | nothing |
| Netsmart | 9114 | JWT assertion **or** client secret (RS384); explicit scopes required | yes | DocumentReference, DiagnosticReport |
| Nextech | 9115 | JWT assertion (RS384/ES384); explicit scopes required | yes | DocumentReference |

The table is a copy; `emulators/vendors.py` (`VENDORS`, `DEFAULT_PORTS`)
is the source of truth, and
`tests/test_emr_profiles_coverage.py::test_the_emulator_runbook_table_is_the_registry_rendered`
fails the moment the rows below differ from what the snippet prints.
Regenerate them with:

```bash
python - <<'EOF'
from emulators.vendors import VENDORS, DEFAULT_PORTS
for k, v in sorted(VENDORS.items(), key=lambda kv: DEFAULT_PORTS[kv[0]]):
    grants = [g for g, ok in (("JWT assertion", v.accepts_jwt_assertion),
                              ("client secret", v.accepts_client_secret)) if ok]
    auth = " **or** ".join(grants) + (f" ({'/'.join(v.assertion_algorithms)})" if v.accepts_jwt_assertion else "")
    auth += "; explicit scopes required" if v.requires_token_scope else ""
    auth += "; wildcards refused" if v.refuses_wildcard_scope else ""
    print(f"| {v.name} | {DEFAULT_PORTS[k]} | {auth} | {'yes' if v.supports_bulk_export else '**no**'} "
          f"| {', '.join(v.creatable) or 'nothing'} |")
EOF
```

---

## They reproduce the seams, not just the happy path

Testing against one generic FHIR server proves very little. The
differences that break EMR integrations are not in the resources — those
are standardised — they are in the seams, and each emulator reproduces
its vendor's honestly, **including the unhelpful behaviours**:

- **athenahealth rejects a JWT assertion** with `invalid_client`, as it
  would live. A client assuming every vendor takes an assertion fails
  here rather than in production. The inverse holds for the JWT-only
  vendors: a client secret sent to one gets the same refusal. Oracle
  Health, TruBridge and Netsmart honour both grants, because each
  documents both.
- **ModMed and Greenway refuse an RS384 assertion** with
  `invalid_client`: each documents ES384 only, and each emulator's
  `assertion_algorithms` says so. Every emulator checks the assertion
  header's `alg` against its vendor's documented list (RS384 or ES384
  for Practice Fusion, MEDHOST and Nextech; TruBridge's advertised
  four; Veradigm's RSA-only pair; RS384 elsewhere), so a client that
  signs everything RS384 fails here, not against a practice.
- **Cerner demands explicit system scopes at the token endpoint** —
  a scope-less request (Epic's normal shape, since Epic's backend token
  request takes no scope parameter at all) or a wildcard scope gets
  `invalid_scope`, exactly as Oracle Health documents. It also accepts
  the secret in an RFC 2617 Basic Authorization header, Oracle's
  primary documented system-account mode, alongside the JWT assertion.
  ModMed, Altera, Greenway, Practice Fusion, TruBridge, Netsmart and
  Nextech demand explicit scopes too, because each documents `scope` in
  its token request; the wildcard refusal that rides on the same flag is
  Oracle Health's rule and is stricter than those vendors, which their
  chapters say.
- **NextGen refuses `$export`** with an `OperationOutcome`, not an
  empty result — a caller must not be able to mistake "unsupported" for
  "no data". (eClinicalWorks used to be modelled this way too; their
  portal now documents bulk FHIR APIs, so its emulator serves
  `$export`.) NextGen is the only profiled vendor recorded without
  `$export`; every other emulator serves the async handshake.
- **Epic advertises `create` for almost nothing**, and the read-only
  surfaces — **MEDITECH, eClinicalWorks, ModMed, Altera, Greenway,
  Veradigm, Practice Fusion, TruBridge and MEDHOST** — advertise it for
  nothing at all (MEDITECH's public surface is view-only; eCW's Create
  APIs are a contracted add-on the emulator models as absent; the rest
  document their FHIR API as read-only), so the delivery capability
  check has something real to refuse. Netsmart advertises it for
  DocumentReference and DiagnosticReport, and Nextech for
  DocumentReference, so it also has something real to admit.
- **Only Cerner honours `If-None-Exist`.** The others return `412`, which
  is what makes duplicate-prevention testable.
- **Pagination is 2 per page regardless of `_count`**, so a client that
  mishandles the `next` link fails here, not against a large practice.
- **Bulk export is genuinely async** — the first status poll returns
  `202`. A client assuming the first poll is ready breaks against every
  real implementation.

---

## In-context launch, end to end

Open `http://127.0.0.1:9101/emulator/launch` (Epic's port; every
vendor's emulator serves the same page on its own `DEFAULT_PORTS`
entry) — this stands in for a
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
is the majority of integration defects. `tests/test_e2e_matrix.py` runs
every emulator as a source and as a delivery target in one matrix — see
`docs/EMR_CONNECTORS.md`, "Proof of integration".

**It is not certification.** A particular customer's build may behave
differently — every profile in `core/fhir/emr_profiles.py` still says to
confirm against the instance's own `CapabilityStatement`, and that stands.
