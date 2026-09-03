# Cutting a public release

Run before publishing, and again before any release that touches
configuration or deployment.

## 1. Nothing secret ships

```bash
# Nothing sensitive tracked
git ls-files | grep -iE "\.pem$|\.key$|^\.env$|terraform\.tfvars$|backend\.hcl$|\.tfstate"

# No credentials in tracked content.
#
# Matches assigned VALUES, not variable names. An earlier version of this
# check matched on "aws_secret_access_key" alone and flagged two files
# that merely set the environment variable from STS - a check that cries
# wolf is one people learn to skip, which is worse than not having it.
git ls-files -z | xargs -0 grep -linE \
  "AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|(secret|password|token)\w*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9/+=-]{24,}"

# No personal or account identifiers. Substitute your own before release.
# (This checklist names the placeholders in its own text, so it excludes
# itself; any other file matching is a failure.)
git ls-files -z -- . ':!docs/RELEASE_CHECKLIST.md' | xargs -0 grep -linE "<your-github-account>|<your-aws-account-id>"
```

The first and third must return nothing.

The credential scan currently returns **three known-good matches**, all
self-documenting synthetic values. Confirm they are still these three and
nothing else:

| File | Value |
|---|---|
| `scripts/mock_epic_server.py` | `"mock-access-token-not-a-real-epic-token"` |
| `tests/test_smart_launch.py` | `"test-secret-not-for-production"` |
| `tests/test_local_auth.py` | `"test-secret-not-a-real-one"` |

A scanner that flags anything secret-SHAPED and leaves a human to clear
obvious test values is the right trade. One tuned until it returns zero
is one that has been tuned until it finds nothing. `.gitignore` covers `.env`, `*.pem`,
`*.key`, `terraform.tfvars`, `backend.hcl` and `*.tfstate` — but
`.gitignore` does not untrack a file already committed, so check rather
than assume.

**If this repository has history from development**, a secret removed in
a later commit is still in the history and still public. Either scrub it
(`git filter-repo`) or publish from a fresh repository with no history.
A fresh repository is the safer option and the one to prefer.

## 2. Identifiers are yours, not the author's

Two settings are written **into stored data and into partner EMRs**, so
they must be namespaced to the deploying organisation:

| Setting | Why |
|---|---|
| `PHI_AI_CANONICAL_BASE` | FHIR extension and CodeSystem URLs minted by this project |
| JWKS URL in `deploy/aws/README_EPIC_JWKS.md` | Epic fetches it to verify your client assertions — whoever controls that URL controls which keys Epic accepts |

Set the canonical base **before ingesting**. Changing it later does not
rewrite existing resources; they keep referencing the old namespace, so a
search on the new one will not find them.

## 3. It builds and passes

```bash
python -m pytest tests/ -q
python -m compileall -q core emulators scripts
for d in deploy/aws deploy/gcp deploy/azure deploy/aws/bootstrap; do
  (cd $d && terraform validate)
done
docker build -t phi-ai-platform:release .
```

For a release (not every push), additionally run the §10 validation
metrics over the synthetic corpus — the two target-zero metrics fail
the command's exit code:

```bash
python scripts/generate_corpus.py   # once per machine; regenerates testdata/layer2/
python scripts/eval_metrics.py
```

Silent omission and status inversion must be zero and attribution false
negatives must be zero; recall is reported per expansion configuration
(docs/TESTDATA.md records the current figures). These numbers are lower
bounds on error — synthetic only, per docs/SPEC.md §7.7 — and do not
substitute for the §10 real-data pilot that gates general availability.

The Python gates also run automatically before every push, locally:
this project runs **no GitHub Actions** (docs/SPEC.md §7.1 R5 - hosted
CI is a project constraint, and metered runner minutes are a bill).
`scripts/pre_push_gates.sh` is the hook; install it once per clone:

```bash
ln -s ../../scripts/pre_push_gates.sh .git/hooks/pre-push
```

Terraform validate and the docker build stay manual-only, exactly as
they were when a workflow existed - they need tooling a quick pre-push
should not assume.

The image tag is a local build artefact with nothing persisted behind it,
and nothing else in this repository consumes it: `docker-compose.yml`
builds every service from `Dockerfile` through `build:` and never names
an image tag, and `install/install.sh` shells out to
`docker compose build` and likewise names none. This checklist is its
only consumer, so changing it is a one-line change with no coordination
cost - which is worth knowing before anyone treats it as load-bearing.

## 4. The deployment shape is stated

`PHI_AI_PROFILE` decides the storage layout and cannot be changed on
a populated object store. Under `large`, apply
`core/db/schema_partitioned.sql` **before** ingesting — converting a
populated table rewrites it in place. See `docs/SCALING.md`.

## 5. What the README must not overstate

This project has real limits, and a public README that hides them costs
more than it gains. Keep these visible:

- **No storage-level immutability.** Retention is recorded, not enforced.
  Integrity is detective, not preventive — see `docs/COMPLIANCE.md`.
- **No live EMR validation.** Every integration is exercised against the
  emulators in `emulators/`, not a real instance of any vendor profiled in
  `core/fhir/emr_profiles.py`. Registration is per customer and still
  required.
- **Not a compliance determination.** The software implements controls; it
  does not certify anyone against HIPAA.
- **OCR is printed text only.** Tesseract is not built for handwriting.

## 6. Licence and attribution

`LICENSE` is present and correct, third-party dependencies in
`requirements.txt` are compatible with it, and no vendor's name is used
in a way implying endorsement or certification.
