# Runbook: model governance and the enforcement gates

`core/governance/` is the enforcement kernel for `docs/SPEC.md`
Invariants 13–19: the model registry and execution gate, the 45 CFR
92.210 fairness screen, the sensitive-category segmentation engine, the
ambient-capture consent gate, the staged-draft write-back queue, the
patient-output release gate, and the constrained action space.

One sentence to hold onto while operating any of it: **every module in
that package is a gate that refuses rather than degrades.** When a gate
refuses, the fix is never a bypass flag — no such flag exists anywhere
in the package, on purpose — it is supplying the artifact or resolving
the condition the refusal names.

---

## Registering a model (§6.2, Invariant 14)

Any model producing a patient-specific score, ranking, or
classification that could influence care must have an accepted
registration before the gateway will run it. That includes models your
organization labels "operational": readmission risk and length-of-stay
estimation influence discharge planning and resource allocation, which
makes them patient care decision support tools under 92.210 regardless
of the label. There is no operational bypass.

A registration needs, and is refused without:

1. **Intended use** and **validated population** — free text, but it is
   the text your compliance office signs, so write it as such.
2. **A declared input-variable schema.** Every variable the model
   consumes, by name. This is what the fairness screen parses; an
   empty schema is refused, not waved through.
3. **PHI-eligibility basis** — required whenever the model may receive
   PHI (typically your BAA reference for the model target).
4. **Training-corpus provenance** — required for any fine-tuned
   artifact. Fine-tuning is permitted on de-identified or public
   corpora only (SPEC §5.1); the corpus recorded here is the basis.
5. **A fairness report** disaggregating performance across race,
   color, national origin, sex, age, and disability, plus a
   **mitigation record**. Every category must appear with at least one
   subgroup measurement.

### What the fairness screen refuses

- Any schema naming a **protected-class variable** — race, ethnicity,
  sex/gender, age or date of birth, disability — under common
  spellings and casings. Fix: remove the variable; there is no
  justification path for a directly protected variable.
- Any **declared proxy candidate** — ZIP in isolation, payer class,
  primary language, interpreter need — **without an operator
  justification carrying a stated basis.** Fix: record who decided and
  why via a `ProxyJustification`; the basis is persisted with the
  registration and lands in the audit trail.

### Honest limit (read this, it is the boundary of the claim)

Excluding *declared* protected-class variables is mechanical.
Identifying *undeclared* proxies is not, and this platform does not
claim to have solved it. What the screen guarantees is narrower and
still substantial: the question is forced to be asked, recorded, and
justified at registration time, and the disaggregated fairness report
gives your organization the evidence it needs to discharge its own
ongoing 92.210 identification-and-mitigation duty. A screen pass is
not a fairness certification.

---

## The action space (Invariant 18)

An operational prediction may trigger supportive interventions — an
additional reminder, a transport or telehealth offer, an outreach
call — with no ceremony. It may **not** trigger double-booking,
deprioritization, or denial without an explicit operator override
carrying a stated basis, which is recorded in the hash-chained audit
log. An action string the vocabulary has never heard of is refused,
not guessed at; adding a new supportive action is a code change, where
someone has to argue the classification in review.

---

## Ambient capture consent (§6.5, Invariant 15)

Capture is **deny-by-default in every jurisdiction**, keyed on state
AND modality. Operationally:

- **Unresolved jurisdiction refuses capture.** Resolve the encounter's
  state before the microphone opens; there is nothing else to fix.
- **Michigan refuses capture, full stop.** Its recording law is
  unsettled and the platform treats it as deny. No override exists.
- **All-party states** (CA, DE, FL, IL, MD, MA, MT, NH, PA, WA; OR in
  person; CT and NV by telehealth) additionally require the
  **visit-level verbal attestation captured at the head of the
  recording**. A registration-packet checkbox alone never satisfies
  the gate — the pending CIPA litigation theory is precisely that a
  buried checkbox is not consent to a specific recorded encounter.
- **A state absent from the policy table is treated as all-party**,
  not one-party: the table transcribes only what the spec verified.
- **Revocation** stops capture and obligates deletion of the encounter
  audio and the derived transcript under the retention schedule; the
  refusal result carries the obligation flag and the deletion is
  audited.

The policy table (`core/governance/consent_gate.py`) applies law; it
does not determine it. It is the same division of labor as the
retention ruleset: counsel review of the table is open dependency #7
in `docs/SPEC.md` §11 and is your organization's to close.

---

## Write-back (§6.4, Invariant 13)

Nothing the platform generates enters the legal medical record without
a recorded human signature. The flow is stage → sign → commit:

1. `stage()` validates the target against **Epic's verified R4 write
   surface** before anything else. A write Epic does not support —
   most notably any MedicationRequest create, which exists only as CDS
   Hooks unsigned-order suggestions — fails here, with the reason,
   before a clinician has signed something that can never land.
2. `sign()` records the signature event over a hash of exactly the
   content the human saw.
3. `commit()` re-hashes and refuses if the content changed after
   signature, then — and only then — calls the EMR writer.

**Why the queue is platform-side:** whether an Epic DocumentReference
create lands unsigned in a clinician's signing queue or lands
committed is unverified from public documentation (open dependency
#1). Until Epic confirms, drafts are signed *here* and written only
after. This degrades gracefully to Epic's native workflow once
confirmed — no design change, just a shorter queue.

---

## Segmentation and the AB 352 geo-gate (§6.1)

The segmentation engine decides at **serialization time** what never
enters the embedding corpus. Three operational facts:

- **Absence of a label is never absence of sensitivity.** Your corpus
  will arrive with `meta.security` overwhelmingly empty; the engine's
  primary signals are the curated per-category value sets and the
  department map, both supplied at deployment via the terminology
  loader. Curating them is the operator work that determines recall.
- **Watch `excluded_unclassifiable`.** A rising count means resources
  the engine cannot place — each excluded fail-closed. That is the
  signal your value sets or department map need attention, surfaced
  loudly rather than interpolated away.
- **The AB 352 geo-gate fails closed on requester location.** An
  out-of-state or unresolvable requester is refused access to
  reproductive/gender-affirming categories on California-governed
  records, and every refusal is audited.

---

## Known gaps and asymmetries

Recorded here per project documentation discipline; none is a defect
to be fixed, each is a boundary to be known:

- **Undeclared proxy variables** are not detected (see the honest
  limit above).
- **GCP speech data-logging enrollment exposes no queryable
  property**; operator attestation stands in where AWS is
  machine-checkable.
- **Microsoft publishes no citable BAA scope list** where AWS and GCP
  do; the evidence burden shifts to the operator on Azure alone.
- **HTI-1 per-category attribute counts are unverified** (open
  dependency #6): `core/governance/source_attributes.py` validates the
  nine categories and deliberately does not code to "31 items" until
  the eCFR text is confirmed; `REQUIRED_ITEMS` is the named landing
  spot for that confirmation.
- **Illinois/Texas segmentation statutes and the minor-consent
  matrix** rest on secondary sources or are unresearched; the affected
  categories are excluded regardless, which is the conservative
  direction.
