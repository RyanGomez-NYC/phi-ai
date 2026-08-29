# Synthetic test data

The deliverable docs/SPEC.md §7.6 requires. **The platform is developed
with no access to real patient data at any point** — this corpus is the
entire evidence base behind every acceptance claim in SPEC §10, so its
provenance is held to the same standard as regulatory claims: every
calibration figure cites a primary source or carries an [UNVERIFIED]
label, and the method is this document.

Read SPEC §7.7 before quoting any number produced against this corpus:
gate behavior — refusals, exclusions, attribution, preflight — is fully
testable synthetically and is where this corpus carries real weight.
Retrieval quality on real clinical narrative is not, and every §10
number produced here is a lower bound on error.

## Rules (SPEC §7.1, restated operationally)

- **R1 — no real patient data, ever, including "de-identified".** There
  is no exception path. MIMIC-IV full is excluded on exactly this rule
  (see Exclusions).
- **R2 — every fixture carries the synthetic marker**: `meta.tag` =
  `HTEST` from `http://terminology.hl7.org/CodeSystem/v3-ActReason`.
  Enforced by `scripts/check_fixtures.py`; a fixture without it fails
  the gate no matter how obviously fake it is.
- **R3 — reproducible from (generator, pinned version, seed).** Synthea
  defaults its seed to the wall clock [VERIFIED], so `-s` is always
  passed explicitly and the release is pinned. Hand-authored fixtures
  are "reproduced" by being committed verbatim.
- **R4 — every fixture set carries `MANIFEST.json`**: generator,
  version, seed, exact command line, calibration sources, and per
  fixture the invariant or acceptance criterion it exercises. Enforced
  by the same script.
- **R5 — no GitHub Actions (project constraint).** The gates run as a
  test target (`tests/test_fixtures.py`) and inside
  `scripts/pre_push_gates.sh` (installed as the `pre-push` git hook —
  see docs/RELEASE_CHECKLIST.md §3).

Run the gates by hand any time:

```bash
python scripts/check_fixtures.py
```

## Layer map

| Layer | Source | Status in repo | Use for | Never use for |
|---|---|---|---|---|
| 1 | Epic FHIR sandbox (named test patients: Anna Cadence, Henry Clin Doc, John Grand Central, Omar Optime, Kyle Nelson; MyChart: Derrick Lin, Camilla Lopez, Desiree Powell, Olivia Roberts) [VERIFIED] | not committed — live endpoint | conformance, auth, search-parameter, citation-key plumbing | longitudinal retrieval, cohort, recall/precision, temporal reasoning (per-patient volume [UNVERIFIED], demo-scale) |
| 2 | Synthea v4.0.0 (5 Mar 2026), Apache-2.0, FHIR R4 + US Core IG (targets 3.1.1–7.0.0, default 6.1.0) [VERIFIED] | **generated, not committed** — `scripts/generate_corpus.py`, seed 20260821, 25-patient Massachusetts run → `testdata/layer2/` (gitignored; provenance manifest committed at `tests/fixtures/layer2.MANIFEST.json`) | volume and structure; `scripts/eval_metrics.py` runs the §10 metrics over it | any epidemiological or quantitative claim — calibration is directional only [VERIFIED: the Synthea paper reports ~4000× national diabetes-amputation rates] |
| 3 | NHANES (lab distributions), NAMCS/NHAMCS (encounter shape), CDC WONDER (prevalence priors; **no publishing statistics from counts ≤ 9**), CMS DE-SynPUF (**format only, never calibration** — CMS states co-occurrence was deliberately perturbed) | not yet fetched | distribution calibration of Layer 2 output | — |
| 4 | Hand-authored adversarial fixtures | **committed: `tests/fixtures/layer4/`** (16 fixtures + manifest) | testing the invariants and gates — the things Synthea cannot produce [VERIFIED: no `meta.security`, verificationStatus hardcoded `confirmed`, no mental-health modules, no Part 2 semantics, no unmapped codes, single note template] | clinical-distribution claims |

### Layer 2 generation (when generated)

```bash
java -jar synthea-with-dependencies.jar -s 20260821 -cs 20260821 -p 100 \
  --exporter.fhir.use_us_core_ig=true Massachusetts
```

Pin the release in the command recorded in that fixture set's
`MANIFEST.json`; post-process to add the R2 `meta.tag` marker (Synthea
does not emit `meta.tag` [VERIFIED] — its only de facto markers are the
narrative text and `Patient.identifier.system`). Synthea's `meta.profile`
conformance assertion is not validator-proven [VERIFIED]; our own
conformance check runs independently (open dependency #13 tracks
running Inferno/HAPI against pinned output).

## Layer 4 fixture classes

The committed set (`tests/fixtures/layer4/MANIFEST.json` is the
authoritative per-file record):

| Fixture class | Exercises | Test |
|---|---|---|
| `refuted` allergy / `entered-in-error` condition | Status-inversion rate, target zero (§10) | `tests/test_rag_serialization.py` |
| Resolved-2019 vs active condition, same code | Temporal weighting (5.1e) | `tests/test_rag_gates.py` |
| HIV category, `meta.security` populated AND stripped | §6.1 recall; the stripped variant is the production condition | `tests/test_rag_serialization.py`, `tests/test_segmentation.py` |
| Part 2 SUD record | §6.1 provenance retention | same |
| AB 352 category | Geo-gate refusal | `tests/test_segmentation.py` |
| Cross-patient near-duplicates (same name/DOB) | Attribution-gate false negatives (§10: any pass is a bug) | `tests/test_rag_gates.py` |
| Encounter-reference mismatch | Attribution gate, encounter arm | same |
| `medication[x]` as CodeableConcept AND Reference | Consumer must-support — both forms handled | `tests/test_rag_serialization.py` |
| "No known allergy" (SNOMED 716186003) vs "not asked" (1631000175102) | Semantically different; must not collapse | same |
| Must-support elements absent | Processed without error; absence rendered "not recorded", never a clinical negative (§7.3) | same |

Also committed: psychotherapy-note DocumentReference (labeled AND
stripped variants), narrative with embedded synthetic identifiers
(de-identification pass — the pass itself is future work; the fixture
waits for it), a multi-year 1998-onset condition, and the
local/unmapped-code and duplicate-patient records for 5.13.

## Terminology licence matrix (SPEC §7.4)

Committed to this repo: **LOINC** (with its notice), **ICD-10-CM/PCS**
(from CMS/CDC, never the UMLS copy), **CVX** (public domain,
attribution), **NDC** (CC0). Prohibited in this repo: **CPT** (AMA
paid licence; also prohibits training/fine-tuning against the data
file), **SNOMED CT US** raw content (each deployment needs its own
Affiliate licence — this project cannot confer it), **RxNorm full
release**, **VSAC expansions** (OIDs and metadata only are safe).
The install-time loader fails loud on missing credentials; expansion
coverage is a deployment-time property reported by the §6.7 probe.

## Exclusions

- **HCUP NIS**: purchase + signed DUA + mandatory training —
  incompatible with an open-source repository. Free HCUPnet aggregates
  only, if inpatient marginals are ever needed. [VERIFIED]
- **MIMIC-IV (full)**: real de-identified patient data under the
  PhysioNet credentialed DUA — R1 excludes it, and the DUA's
  derived-works clause independently does [VERIFIED]. Private sanity
  checks by a credentialed team member only; never the cited
  provenance for a committed fixture. Any MIMIC-derived number in a
  public repo requires a written determination.
- **MIMIC-IV Demo** (100 patients, ODbL v1.0, redistributable):
  usable in principle, but ODbL share-alike vs this project's OSS
  licence is an open licensing decision (open dependency #11) — no
  Demo-derived fixture is committed until it is made.

## Known gaps

- Layer 3 calibration data (NHANES / NAMCS distributions) is specified
  but not yet fetched or applied to Layer 2 output.
- The embedded-identifiers fixture exists; the de-identification pass
  it exercises is not yet implemented against it.
- Synthea US Core output validator status is open (#13).
- Per SPEC §7.7: nothing here validates retrieval quality on real
  clinical narrative; that requires the §10 pilot (#10). Current
  synthetic figures (2026-08-21, 25-patient corpus, lexical-only
  retrieval): silent omission 0, status inversion 0, attribution
  false negatives 0, recall@10 ≈ 0.92 — lower bounds on error, per
  expansion configuration (none loaded).
