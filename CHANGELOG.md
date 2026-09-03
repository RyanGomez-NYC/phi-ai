# Changelog

## 1.1.0 — 2026-09-03

### AI-Native Data Orchestration Designed for Compliance

Orchestration in 1.1 exists to hydrate the PHI AI store, and the store is
what the AI is allowed to read. Data moves out of each EMR, through the
categories and permissions that govern it, into one place — and retrieval
answers from that place or refuses. The compliance boundary and the
retrieval boundary are the same boundary.

- **Nine more EMR connectors** — ModMed, Altera Digital Health, Greenway
  Health, Veradigm, Practice Fusion, TruBridge, MEDHOST, Netsmart and
  Nextech join the six already shipped. Every profile is written from
  that vendor's own documentation; where a vendor documents nothing on a
  point, the profile says so and defaults conservatively. Nothing is
  carried over from Epic.
- **Per-vendor assertion signing** — `EMRProfile.assertion_algorithm`
  (RS384 by default, ES384 where the vendor documents only that).
- **Store-bound retrieval** — `GrantScope.permitted_sources` and
  `require_provenance` are checked before the subject, so a chunk with no
  recorded origin cannot be retrieved at all. Every serialized resource
  carries a `Provenance` record naming the system it came from.
- **Connector extension guide** — `docs/EXTENDING_CONNECTORS.md`.
- **Removed from the public repository**: references to the demonstration
  tree. The public repository is the software; the demonstration is not
  part of it and is no longer named by it.

## 1.1.0-rc1 — 2026-09-02

### Nine more EMRs, one client, no new special cases

```text
 YOUR EMR ──▶ CLASSIFY ──▶ ENCRYPT ──▶ SYSTEM OF RECORD ──▶ GOVERNED AI
  FHIR R4     sensitive     per-object   your cloud, your     bounded by
  15 vendors  categories    data keys,   keys — storage       the asker's
              fail closed   your KMS     always wins          own role
```

- **Nine new EMR connectors** — ModMed, Altera Digital Health, Greenway
  Health, Veradigm, Practice Fusion, TruBridge, MEDHOST, Netsmart and
  Nextech join Epic, Oracle Health (Cerner), athenahealth,
  eClinicalWorks, MEDITECH and NextGen Healthcare: every entry in
  `core/fhir/emr_profiles.py` `PROFILES` is written from that vendor's
  own documentation — auth, keys, scopes, consent, bulk scope, writes,
  registration, limits — with a chapter in `docs/EMR_CONNECTORS.md`
  that separates what the vendor documents, what its own public
  endpoints returned, and what must be confirmed on the instance. Where
  a vendor documents nothing on a point the chapter says "not
  documented by the vendor" and the profile defaults conservatively;
  nothing is carried over from Epic.
- **Per-vendor assertion signing algorithm** —
  `EMRProfile.assertion_algorithm` (RS384 by default; ES384 where the
  vendor documents only that - the profile says which). The ingestion
  client signs with the profile's algorithm rather than a hard-coded
  RS384, so an EC P-384 key works where the vendor requires one;
  `Settings.from_env()` refuses a key of the wrong family at startup.
  The delivery CLI (`python -m core.fhir.delivery`) now builds its
  destination token request on the DESTINATION's profile too - its
  algorithm, its grant, its `kid`, and one `system/{Type}.write` scope
  per writable type where the profile requires explicit scopes - and
  refuses a `PHI_AI_DELIVERY_CLIENT_SECRET` set for a vendor whose
  profile takes none, instead of silently using it.
- **Emulators enforce the algorithm, and verify the assertion** —
  `EmulatorVendor.assertion_algorithms` lists what each emulator's token
  endpoint accepts; an assertion signed with anything else is refused as
  `invalid_client`, so a client that signs everything RS384 fails
  against the ModMed and Greenway emulators, not against a practice.
  Every assertion's audience, expiry, required claims and `iss == sub`
  are verified; its signature is verified when the client's public JWK
  Set is registered (`build_server(client_jwks=...)`, `python -m
  emulators --client-jwks PATH` - the integration tests and the e2e
  matrix both register one), and without one the emulator logs a
  WARNING that signatures are unverified. A client secret is checked
  against registered credentials the same way (`--client-secret
  ID:SECRET`). Wildcard-scope refusal is now its own per-vendor flag
  (`refuses_wildcard_scope`), true only for Oracle Health, which
  documents it. Malformed input (a non-integer Content-Length, a
  non-UTF-8 body, a non-string `id`, a negative `_offset`) is a 400
  with a body, never a dropped connection. Both-grant token endpoints
  (TruBridge, Netsmart, alongside Oracle Health), scope-required token
  requests and read-only write refusals are modelled the same way, each
  from the vendor's own documentation.
- **Ports 9107–9115** — one emulator per new vendor in
  `emulators/vendors.py` `DEFAULT_PORTS`; the earlier vendors keep the
  ports they had.
- **The end-to-end matrix** — `tests/test_e2e_matrix.py` and
  `scripts/e2e_matrix.py` drive every emulator as a source
  (authenticate with its real grant and algorithm, read its
  CapabilityStatement, ingest by paged search and by `$export` where
  supported, with the refusal asserted where not) and every vendor as a
  delivery target through `core/fhir/delivery/writer.py` (success where
  the CapabilityStatement advertises `create`, a structured refusal
  where it does not), across the full source-by-target matrix on
  synthetic, non-PHI data. Before each source's real grant the matrix
  sends that vendor's documented refusals (wrong algorithm, unsigned,
  unregistered key, missing scope, wrong grant) and asserts each 400,
  and the proof records what was refused per source. The proof table
  lands in `private-notes/e2e-proof.md` beside the checkout, never in
  the repository. The matrix delivers through `writer.py` with tokens
  it mints itself; the delivery CLI's own token request is covered by
  `tests/test_delivery.py`, parametrised over every profile.
- **Setup runbooks** — every vendor chapter ends with "Setting it up":
  register with the vendor, generate the key pair and JWKS (RSA or EC
  P-384 as the vendor documents), configure the environment, pre-flight
  the instance, first ingest, first delivery (and why it is refused on a
  read-only surface), local rehearsal against the emulator, and known
  limits with where to confirm them.
- **Enumerations derived, not maintained** — the README, ARCHITECTURE,
  the runbooks and the installer text now point at `PROFILES` and
  `DEFAULT_PORTS` instead of repeating a vendor count that had already
  drifted in places.

## 1.0.0-rc1 — 2026-08-29

```text
                         φ(ai)

    ____    __  __  ______      ______  ______
   /\  _`\ /\ \/\ \/\__  _\    /\  _  \/\__  _\
   \ \ \L\ \ \ \_\ \/_/\ \/    \ \ \L\ \/_/\ \/
    \ \ ,__/\ \  _  \ \ \ \     \ \  __ \ \ \ \
     \ \ \/  \ \ \ \ \ \_\ \__   \ \ \/\ \ \_\ \__
      \ \_\   \ \_\ \_\/\_____\   \ \_\ \_\/\_____\
       \/_/    \/_/\/_/\/_____/    \/_/\/_/\/_____/

                        1 . 0
   The AI-native platform for protected health data
```

**Health care runs on the most sensitive data there is — and the AI
revolution keeps happening somewhere else.** Locked demos, vendor
clouds, black boxes your compliance officer can't sign off on.

PHI AI 1.0 ends the standoff. It is a platform you deploy into your
own cloud, connect to your own EMR, and run under your own keys —
where a frontier model becomes a governed, first-class consumer of the
clinical record, and every safeguard the HIPAA Security Rule names is
enforced in code, not in a policy binder.

```text
┌────────────────┬────────────────┬────────────────┬────────────────┐
│  COMPLIANT BY  │   YOUR CLOUD   │    MINIMUM     │  HASH-CHAINED  │
│  CONSTRUCTION  │   YOUR KEYS    │   NECESSARY    │  AUDIT TRAIL   │
└────────────────┴────────────────┴────────────────┴────────────────┘
```

**Open Source. Free for All.** See it running on synthetic data right
now: <https://ryangomez.nyc/phi-ai/>

### One pipeline, from EMR to governed AI

```text
 YOUR EMR ──▶ CLASSIFY ──▶ ENCRYPT ──▶ SYSTEM OF RECORD ──▶ GOVERNED AI
  FHIR R4     sensitive     per-object   your cloud, your     bounded by
  6 vendors   categories    data keys,   keys — storage       the asker's
              fail closed   your KMS     always wins          own role
```

- **Six EMR connectors, one data-driven client** — Epic, Oracle Health
  (Cerner), athenahealth, eClinicalWorks, MEDITECH, NextGen — each
  speaking its vendor's real auth model, each testable end-to-end
  against a bundled emulator before you ever touch a live system.
- **An encrypted system of record** — one envelope-encrypted object
  per FHIR resource. Every index, every analytics layer, every answer
  traces back to those bytes; if a derived store ever disagrees, the
  object store wins.
- **Segmentation at the door** — 42 CFR Part 2, psychotherapy notes,
  state-law categories: classified on ingestion, withheld fail-closed,
  released only through their own consent lanes.

### An assistant that must show its work

Ask it about a patient and every claim comes back cited to stored
bytes. Nothing retrieved means nothing asserted — it abstains rather
than invents. Retrieval is bounded by *your* role grants, so the
assistant can never become a way to see what you couldn't open
yourself. And it is the only component in the platform with a network
path out of the deployment — a path that, on AWS and GCP, never leaves
your own account.

### AI that cannot touch the record on its own

```text
   model drafts ──▶ SIGNATURE QUEUE ──▶ human signs ──▶ the record

        Nothing an AI writes reaches the chart without a
              human signature. No exceptions.
```

The governance kernel treats every model as untrusted by default:
registry and execution gates, fairness screening, ambient consent
gating, patient-output release gates, a constrained action space.
Every gate refuses rather than degrades.

### And the rest of the platform

Population analytics that count patients, not rows. An optional OMOP
CDM layer for standard tooling. DICOM imaging with the upstream OHIF
viewer, pinned and unmodified. Release-of-information productions
where every withheld record is itemized, never silent. Complete
Terraform for AWS, GCP, and Azure. Twenty-five runbooks, an installer
chatbot, and a healthcheck that verifies compliance posture — not just
connectivity.

### What 1.0 is not

```text
   ╔═════════════════════[ THE HONESTY BOX ]═════════════════════╗
   ║  Software that manages PHI must not overstate itself.       ║
   ╚═════════════════════════════════════════════════════════════╝
```

- **No storage-level immutability.** Retention is recorded, not
  enforced; integrity is detective, not preventive.
- **No live EMR validation.** Everything is exercised against the
  emulators; Epic alone has also run against a live sandbox.
- **Not a compliance determination.** The software implements
  controls; operating on real PHI lawfully remains yours.
- **Not yet audited.** Run your own HIPAA security risk assessment —
  and get review from someone other than the code's own author —
  before any production workload.

### Try it in the next five minutes

```bash
git clone https://github.com/RyanGomez-NYC/phi-ai && cd phi-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q     # the whole platform proves itself, no cloud needed
```

The bundled emulators let you exercise every connector locally before
you provision a single cloud resource. The README's "Getting started,
in detail" takes it from there.

### Keep the code, keep the credit

```text
        ╔═══════════════════════════════════════════════════╗
        ║   APACHE-2.0  ·  KEEP THE CODE, KEEP THE CREDIT   ║
        ╠═══════════════════════════════════════════════════╣
        ║   PHI AI — Copyright 2026 Ryan Gomez & Co. Inc.   ║
        ║   Created by Ryan Gomez  ·  www.ryangomez.nyc     ║
        ╚═══════════════════════════════════════════════════╝
```

Apache 2.0, chosen for its explicit patent grant. Per Section 4(d),
the notices in `NOTICE` and the source-file headers travel with every
redistribution and derivative work.

---

Built by one person working with a frontier model. To err is human; to
completely blow stuff up is to AI. See something, say something —
report an issue. <https://www.ryangomez.nyc>
