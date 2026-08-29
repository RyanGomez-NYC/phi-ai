# Changelog

## 1.0.0 — 2026-08-29

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
Terraform for AWS, GCP, and Azure. Twenty-four runbooks, an installer
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
