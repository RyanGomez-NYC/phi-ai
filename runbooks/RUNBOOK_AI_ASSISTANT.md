# Runbook: the AI assistant (optional add-on)

`core/assistant/` is an assistant that helps a human implement, operate
and use this platform. It answers from this project's own documentation
and from this deployment's own configuration, for both technical staff
standing the system up and non-technical staff working in it.

It is **off by default**, additive, and removable: a deployment that
never enables it behaves exactly as it did before the package existed.

> **Read this before enabling it.**
>
> This is the only component of the PHI AI Platform that talks to
> anything outside the deployment. Everything else runs entirely inside
> infrastructure you own — that is the "bring your own infrastructure"
> promise in `README.md`, and it is why `docs/COMPLIANCE.md` can say this
> software never processes data as a hosted service.
>
> **By default no PHI is sent to the model**, enforced structurally
> rather than by instruction — see "How the boundary is enforced" below.
> **Your organisation can change that**; see "Letting the assistant read
> PHI". Either way the network path is real and new, and **which provider
> you choose decides whether it leaves your cloud account at all.** Those
> decisions belong to whoever owns this deployment's risk assessment, not
> to whoever installs it.

---

## Choose a provider first

This is the first decision, and it is a compliance decision.

| Provider | Where the model runs | Covered by | Choose it when |
|---|---|---|---|
| `bedrock` | Amazon Bedrock, **in your own AWS account** | the AWS BAA you already rely on for S3 and KMS | you deployed with `deploy/aws/` |
| `vertex` | Vertex AI, **in your own GCP project** | the Google Cloud BAA you already rely on for GCS and Cloud KMS | you deployed with `deploy/gcp/` |
| `anthropic` | Anthropic's API, **outside your cloud account** | a separate agreement with Anthropic, if your organization wants one | you deployed on Azure, or you have deliberately chosen the direct API |

On AWS and GCP, `bedrock` and `vertex` largely dissolve the question this
runbook opens with: the model runs inside the same account, under the
same agreement, reached with the same workload identity the platform
already uses for storage and KMS. There is no new vendor, no new
credential, and nothing crosses the account boundary. **Prefer them.**

Azure has no in-cloud Claude offering, so an Azure deployment that wants
the assistant uses `anthropic`. That is a genuine third-party egress
path. Anthropic offers a Business Associate Agreement; whether you need
one is a question for your privacy officer, and the honest input to that
question is: at the default tier no PHI is sent on this path by
construction, so the decision is about whether a PHI-holding system may
reach an external API at all, not about what it would disclose if it did.
If you intend to enable a PHI tier as well, the BAA stops being
belt-and-braces and becomes load-bearing — read the next section.

---

## Configuration

| Variable | Meaning |
|---|---|
| `PHI_AI_ASSISTANT_ENABLED` | `true` to enable. Absent or false means the feature does not exist. |
| `PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED` | Required alongside `ENABLED`, no default. See below. |
| `PHI_AI_ASSISTANT_PROVIDER` | `bedrock` \| `vertex` \| `anthropic` |
| `PHI_AI_ASSISTANT_MODEL` | default `claude-sonnet-5` |
| `PHI_AI_ASSISTANT_AWS_REGION` | `bedrock` only; falls back to `PHI_AI_STORAGE_REGION` |
| `PHI_AI_ASSISTANT_GCP_PROJECT` | `vertex` only; falls back to `PHI_AI_GCP_PROJECT` |
| `PHI_AI_ASSISTANT_API_KEY_PATH` | `anthropic` only; a **mounted file path**, not the key itself |
| `PHI_AI_ASSISTANT_EFFORT` | `low`…`max`, default `medium` |
| `PHI_AI_ASSISTANT_MAX_TOKENS` | default `8192` |
| `PHI_AI_ASSISTANT_MAX_TOOL_ITERATIONS` | default `6` |
| `PHI_AI_ASSISTANT_DOCS_ROOT` | where to read documentation from; defaults to the repository root |

**Why there are two switches.** `ENABLED` says you want the feature.
`EGRESS_ACKNOWLEDGED` says you know what it does. The application refuses
to start with the first set and the second missing — the same shape as
`PHI_AI_WEB_TRUST_PROXY_AUTH`, and for the same reason: no default
this project could pick would be right for every organization, and a
network path out of a PHI environment should not be something a deployer
discovers from a firewall log.

**The API key is a file path.** `anthropic` is the only provider that
needs a credential at all, and it is mounted read-only exactly like the
EMR private key, because key material in an environment variable ends up
in process listings and log capture. `ANTHROPIC_API_KEY` also works and
is warned about.

### Model access has to be granted before the first call

- **Bedrock:** request access to the model in the Bedrock console for
  your account and region, and grant `bedrock:InvokeModel` to the role
  the platform runs as. Until both are done, calls fail with a permission
  error — `core/assistant/provider.py` translates it into that sentence
  rather than surfacing the raw SDK message.
- **Vertex:** enable the model in Model Garden and grant the service
  account the Vertex AI User role.
- **Anthropic:** no step beyond a valid key.

---

## Turning it on

```bash
# AWS deployment, model inside your own account
cat >> .env <<'ENV'
PHI_AI_ASSISTANT_ENABLED=true
PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED=true
PHI_AI_ASSISTANT_PROVIDER=bedrock
ENV

python -m core.assistant --check     # confirms the model is reachable
python -m core.assistant             # interactive
```

The web interface picks it up on restart and shows an **Assistant** tab
to every role.

---

## Two ways to use it

**`python -m core.assistant`** — a terminal session, and the one to use
while installing. It works with **no object store configured at all**,
which is the state an operator is in at step 1 of
`RUNBOOK_AWS_SETUP.md`: it falls back to documentation-only and says so.
It keeps a conversation for the life of the process.

- `--ask "..."` answers one question and exits, for scripts.
- `--check` verifies model reachability and exits.

**The web interface** — for HIM staff and operators already working in
the platform, and reachable two ways:

- **From any page**, via the *Ask the assistant about this page* drawer
  at the foot of it. The question carries which page you were on, so
  "why is this list empty?" works without restating it, and the answer
  arrives with a link straight back to where you were.
- **The `/assistant` page**, from the nav, for a longer conversation.

Both are the same conversation. It is **multi-turn** — follow-up
questions work, *Start over* clears it — and the transcript lives in
worker memory keyed by an id in your signed session cookie, expiring on
the same clock as the session itself. Nothing is written to disk or to
the database. See `core/assistant/conversations.py` for why an in-memory,
session-lifetime store is a different proposition from a durable one, and
for what it costs (a process restart, or a second replica without session
affinity, starts a fresh conversation).

**No JavaScript is involved.** The drawer is a native `<details>`
element and every exchange is an ordinary form post, because this
interface runs under `script-src 'none'` (`core/web/security.py`) and the
assistant — the one feature that renders model-generated text into pages
that also display PHI — is the last place to spend that guarantee. The
cost is a page navigation per question instead of an in-place update.

---

## Population questions

Separate from the record-reading tiers below, and gated by its own
permissions and its own read-only database roles: the assistant can
answer questions about this deployment's population as a whole - cohort
counts, facility breakdowns, name search - over the optional OMOP layer.

This is deliberately NOT tied to `PHI_AI_ASSISTANT_PHI_ACCESS`. They
are different questions with different answers: an analyst counting
cohorts has no business opening a chart, and a clinician reading one
chart has no business running population queries. A new `analyst` role
holds `analytics:query` and neither `patient:read` nor `identity:search`.

Full setup, permissions, the guarded-SQL design and the counting trap
that makes population numbers plausibly wrong: **`runbooks/RUNBOOK_ANALYTICS.md`**.

---

## Cross-record research

The `researcher` role's capability: `search_clinical_records`, a
full-text search over the extracted prose of every indexed record -
"which records mention insulin pump failure" - answered from the
clinical retrieval index (`core/db/retrieval_schema.sql`; **read its
header first**, it is the broadest derived store in this system).

To enable it, in order:

1. Apply `core/db/retrieval_schema.sql`, then your cloud's
   `core/db/retrieval_bootstrap_<cloud>.sql` (three roles: ETL writer,
   general search, psychotherapy search - the split is the design).
2. Build the index: `python -m core.db.retrieval_etl`. Idempotent;
   schedule it after ingestion runs. `--rebuild` re-extracts everything
   after an extraction-rule change.
3. Set `PHI_AI_RETRIEVAL_SEARCH_USERNAME` (and
   `PHI_AI_RETRIEVAL_ETL_USERNAME` for the ETL).
4. The deployment must be at `PHI_AI_ASSISTANT_PHI_ACCESS=lookup` -
   snippets are clinical text - and the user must hold the `researcher`
   role and state a purpose of use (`research` is the honest code).

Every search is recorded in the audit trail **verbatim, before it
runs** - the guarded-SQL tool's contract. Results are snippets plus
storage keys; opening the full record is a separate, separately-audited
read. The index deliberately holds no names, identifiers or codes
(`core/db/retrieval_text.py` is the complete extraction rulebook), so
it cannot become a second identity index.

### Psychotherapy notes

Off unless ALL of the following hold, and the first two refuse to start
otherwise:

- `PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS=true` **and**
  `PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED=true` - a separate
  acknowledgement from the general PHI one, because §164.508(a)(2) is a
  separate authorization regime. Refused below the `lookup` tier.
- `PHI_AI_RETRIEVAL_PSYCH_USERNAME` names the psychotherapy search
  role, and the ETL was run with `--include-psychotherapy` (an explicit
  per-run flag - indexing this store is never a side effect).
- The asking user holds the `psychotherapy` role - a single-permission
  role granting nothing else, so no breadth of general access ever
  includes it - and states a purpose.

The general search role holds **no database grant** on the
psychotherapy table; the separation is Postgres's, not a prompt's.
Every psychotherapy search and read is audited as a disclosure before
anything is decrypted.

---

## Operations: telemetry and drift

Enabling an AI feature obliges you to be able to say how it is
behaving. Two pieces, both optional, both metrics-only:

**Telemetry.** Apply `core/db/telemetry_schema.sql` and
`core/db/telemetry_bootstrap_<cloud>.sql`, set
`PHI_AI_ASSISTANT_OPS_USERNAME`, and every interaction records who
asked (never what), roles, latency, tokens, tool counts, PHI-read
counts, refusals and errors. `/assistant/ops` (admin and auditor only -
the new `assistant:ops` permission; the rows are staff activity data)
renders usage by day and role, latency percentiles, and which models
have actually served. Questions themselves live only in the audit
trail; the telemetry schema has no content column by design.

**Drift probes.** Copy `config/assistant_probes.example.yaml` to
`config/assistant_probes.yaml` and schedule
`python -m core.assistant.drift` (daily is proportionate). Each probe
asks a fixed question and checks stable expectations - refusal
behaviour, documentation citations, tool use, key phrases - against a
documentation-only session. Results land in telemetry as `drift_probe`
rows and on the ops page next to the models-seen table: a pass-rate
change that coincides with a model change is the drift signal, caught
by the suite rather than by a support ticket. The run exits nonzero on
any failure, so cron alerting works with no further wiring. The
committed starter suite pins the compliance-posture answers this
project can least afford to have drift - keep those probes when you
extend it.

---

## Letting the assistant read PHI

**This is your organisation's decision, not this software's.** The first
version of this feature hard-coded "never", which was the wrong call: an
organisation with a signed BAA covering the model provider, retention
configured per that agreement, and a completed risk assessment is better
placed to make it than the software is. `core/web/auth.py` already
refuses to guess whether a proxy is trusted for exactly the same reason.

Three tiers, set with `PHI_AI_ASSISTANT_PHI_ACCESS`:

| Tier | What it can reach | What it still cannot do |
|---|---|---|
| `none` *(default)* | Documentation and PHI-free aggregates | Read any record. PHI-shaped input is refused rather than sent. |
| `in_context` | The record the user **already has open** | Search for a patient, or open any other record |
| `lookup` | Anything the user's own role permits — search, record lists, contents | Exceed the user's role, or reach psychotherapy notes |

The tiers are ordered, not independent flags: `lookup` contains
`in_context`, so two booleans would make an incoherent combination
representable. Staging a rollout is a one-value change.

`PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED` is required alongside any tier
above `none`, and the application refuses to start without it. Before
setting it, confirm:

- a **Business Associate Agreement** is in place covering the model
  provider you configured — on `bedrock`/`vertex` that is the cloud BAA
  you already hold; on the Anthropic API it is a separate agreement;
- the provider is configured **not to retain or train on** your data, per
  that agreement;
- your **HIPAA security risk assessment** covers this data flow.

### What does not change, at any tier

These are not policy switches. They are rules the rest of the application
already enforces, and an assistant that broke them would silently corrupt
the record:

- **Every clinical read is audit-logged as a disclosure** before the
  object is decrypted, with the object key and a stated purpose of use,
  through the same `record()` path the web routes use. Reads made through
  the assistant are indistinguishable from reads made by clicking, and
  appear in an accounting of disclosures under 45 CFR §164.528. If the
  audit write fails, the read does not happen.
- **Permission still gates access.** An auditor cannot read clinical
  content by asking, because the auditor role never had `patient:read`.
- **Psychotherapy notes are unreachable at every tier by itself.** 45 CFR
  §164.508(a)(2) treats them differently and this project models that as
  a separate bucket and key. The tiers never wire them in; the ONLY path
  is the separately-gated psychotherapy research capability described in
  "Cross-record research" below, which stacks its own deployment
  acknowledgement, its own database role, and a dedicated application
  role on top of the lookup tier.

### Purpose of use

Required before anything is read, and recorded against every read.

- At **`in_context`**, it is inherited from the page. The user already
  chose a purpose to open the record; the drawer carries it forward, so
  the assistant's reads are attributed to the same reason as the view
  they were asked from.
- At **`lookup`**, the assistant page shows a purpose selector. Leave it
  unset and the assistant answers from documentation without opening
  anything — which is the default, and is stated on the page rather than
  failing silently.
- In the CLI, `--purpose` does the same. `in_context` is refused outright
  in a terminal rather than quietly downgraded: that tier means "the
  record on screen", and a terminal has no screen.

### How `in_context` is actually confined

The page sends the patient reference or object key the user has open.
That is a **request** for access, not a grant. `core/assistant/tools.py`
re-derives the permitted object keys from the index and refuses anything
outside them, so a forged form field widens nothing — and the refusal
happens before the audit entry, because nothing was disclosed.

### Minimum necessary

At `lookup` the assistant can read broadly, and 45 CFR §164.502(b) says
it should not. The system prompt tells it to read what the question needs
and to say what it proposes to read before sweeping. That is a prompt,
not a control — treat broad questions at this tier as something to
review in the audit trail, which is where they will show up.

### Grounded retrieval (the §5.1 tool)

At either PHI tier, one more clinical tool appears **only when
`PHI_AI_SENSITIVE_VALUE_SETS` points at your curated copy of
`config/sensitive_value_sets.example.yaml`**: `grounded_patient_evidence`,
the docs/SPEC.md §5.1 pipeline behind a tool call. It differs from
`read_record` in what the model receives — not raw resource JSON but
ranked, citable evidence built by `core/rag/`:

- sensitive-category content is excluded at serialization, fail-closed
  (§6.1) — the model never sees it, and when an entire record is
  policy-excluded the tool says so rather than presenting the chart as
  empty;
- refuted / entered-in-error content arrives banner-first and never
  outranks active content; resolved history never outranks the active
  list;
- summary-shaped questions include the complete deterministic
  structured record (the 5.1(g) spine), which is what makes silent
  omission a measured zero rather than a hope;
- every evidence line carries a `[cite: storage-key]`; empty evidence
  instructs the model to say the record holds nothing responsive
  rather than answer from general knowledge;
- differential-diagnosis requests are refused with the
  hypothesis-directed alternative named (Invariant 19).

Why the tool is absent without the value-sets file: grounded retrieval
with an empty exclusion vocabulary would fail OPEN — every sensitive
resource would serialize. Absent-because-not-configured is the same
path every other unbuilt tool takes, and curating that file is operator
work (see the file's own header). Grounding a question reads the whole
record, so expect one audited disclosure per stored object in the
trail — that is the honest shape of the operation, not noise.

---

## How the boundary is enforced

Four independent mechanisms, in the order they act. **All four describe
the default `none` tier**; where a PHI tier is enabled, mechanisms 1 and
3 relax exactly as much as that tier says and no more, while 2 and 4 hold
unchanged.

**1. There is no tool that can reach clinical content.** Whatever the
model is asked to do, it can only do what a tool permits, so "can the
assistant see PHI?" reduces to "is there a tool that returns PHI?" — and
the list is enumerable by reading `core/assistant/tools.py`. At the
default tier there is no tool that decrypts an object, none that returns
a patient reference, a storage key or an audit entry, and none that
writes anything at all. Above the default, the clinical tools appear in
that same file, permission-gated and audited; nothing else changes.

**2. Holdings facts are aggregates, never rows.** `core/assistant/posture.py`
returns counts, dates, booleans and verdicts. The retention tool reads
the same rows the retention page reads — rows that carry patient
references and object keys — and returns only how many, of which types,
due when. The row never leaves that module.

**3. Outbound text is scanned** (at the `none` tier — where a PHI tier is
enabled, refusing a pasted record would be refusing the feature the
organisation turned on, so the scan is skipped for input while still
guarding documentation and posture results).
`core/assistant/redact.py` refuses
anything that looks like a FHIR resource, an object key, a patient
reference, an SSN, a labelled MRN or date of birth, a phone number or an
email address. A match **refuses the request** rather than stripping the
match, because silent redaction teaches people that pasting PHI is fine
and partial redaction of a clinical note leaves enough to re-identify
while looking safe.

**4. Everything is audited before it is sent.** The audit entry is
written *before* the question leaves, so a failed audit ends the request
having sent nothing — the same ordering `core/web/app.py` uses before
decrypting a resource. Actions recorded: `assistant.query`,
`assistant.tool`, `assistant.refused`. The web interface will not serve
the assistant at all without a working audit sink.

### What the scan cannot catch, stated plainly

A patient's name is a word. *"Margaret Chen was seen on Tuesday"* has no
structure to match and no regular expression will ever flag it. **Free
text typed by a user is not made safe by mechanism 3.** What it catches
is the structured, machine-shaped material that actually turns up in
pasted content — records, keys, identifiers.

The defence against a typed name is mechanisms 1 and 2: there is nothing
the assistant could do with one. It has no patient search, no record
read, and no way to look anybody up. Train staff accordingly, and treat
the scan as the backstop it is rather than the boundary.

---

## It is not a privilege escalation path

Every tool that reports on the live deployment declares the same
permission the equivalent page in the web interface requires.

| Role | Gets |
|---|---|
| `viewer` | documentation; records only if a PHI tier is enabled |
| `him` | documentation, configuration, holdings; records if a PHI tier is enabled |
| `auditor` | documentation, configuration, holdings, audit chain status — **never** clinical content, at any tier |
| `disposition` | documentation, configuration, holdings, retention outlook — **never** clinical content |
| `admin` | documentation, configuration, holdings — **never** clinical content |

The three roles marked "never" are not special-cased. They simply do not
hold `patient:read`, and the clinical tools declare it like every other
route does.

Tools a role does not permit are **never sent** — not refused at call
time — so the model cannot mention a capability the user does not have.
A viewer asking about audit events gets the same nothing they would get
from `/audit`. The deployment-configuration summary in the system prompt
follows the same rule.

---

## What it will not do

- **Answer questions about a specific patient**, unless your organisation
  enabled a PHI tier. At the default it has no access and will say so,
  pointing at the platform's own audited patient view.
- **Decide a compliance question.** It can explain what
  `docs/COMPLIANCE.md` says and what this system does and does not
  enforce. It will not tell you what your retention period should be,
  whether a disclosure is permitted, or whether you satisfy a
  regulation. It reports the retention figure configured; it never
  recommends one.
- **Imply that retention is enforced.** This deployment provisions no
  storage-level immutability on any cloud, and the assistant is
  instructed to say so whenever retention comes up.
- **Change anything.** It has no write tool of any kind.

---

## Cost

Answering one question costs one to three model calls: the question, one
or two documentation searches, and the answer. The system prompt and tool
definitions are cached, so repeated questions in the same process re-read
that prefix at roughly a tenth of the input price rather than re-sending
it. `PHI_AI_ASSISTANT_MAX_TOOL_ITERATIONS` bounds the worst case for
a single question, and `EFFORT=low` cuts spend further at some cost to
answer depth. See `docs/COST.md` for the platform's own costs, which this
does not change.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| App refuses to start naming `EGRESS_ACKNOWLEDGED` | `ENABLED` set without the acknowledgement. Read the top of this runbook. |
| `/assistant` returns 503 | The assistant is not enabled in the running process. Check `.env` reached the container and restart. |
| "Audit logging is not configured" | The web interface will not run the assistant without an audit sink, by design. |
| Answers are vague and cite nothing | The documentation corpus is empty. In a container, confirm `docs/` and `runbooks/` are present (the Dockerfile copies them) or set `PHI_AI_ASSISTANT_DOCS_ROOT`. Startup logs this. |
| Access denied on Bedrock | Model access is granted per-account in the Bedrock console, and the role needs `bedrock:InvokeModel`. Confirm the model exists in your region. |
| Holdings and retention questions cannot be answered | The Postgres index is not configured. Documentation questions still work. |
| A conversation disappeared mid-session | The worker restarted, or the request landed on another replica. Conversations are in-memory and not shared between workers. |

---

## Known gaps

- **Conversations do not survive a restart or span replicas.** They live
  in one worker's memory. A deployment running more than one replica
  without session affinity will drop conversations unpredictably — the
  same caveat the web interface already carries for session secrets.
- **No streaming.** A question that needs two or three documentation
  searches can take ten seconds or more, and the page simply loads for
  that time. Server-sent events would fix it and would need the audited
  tool loop restructured to emit partial output, which has not been done.
- **The drawer knows the page, not the state.** It tells the assistant
  you are on the retention schedule, not that you set the window to 365
  days. Passing a filter value would be safe; passing a patient
  identifier would not, and the allowlist is deliberately coarse enough
  that no future route can leak one by existing.
- **The corpus is this repository's documentation only.** It does not
  include a deployment's own operational notes, ticket history, or the
  retention ruleset file it actually runs (the *example* ruleset is
  included). Adding the real one would be useful and is not done, since
  it is a deployer-owned file whose contents this project cannot assume.
- **No rate limiting.** Nothing bounds how many questions a user may ask.
  The per-question tool budget bounds a single question's cost, not a
  determined user's.
- **The egress scan is regex-based** and cannot catch names or narrative
  clinical text. See "What the scan cannot catch" above. It matters less
  at a PHI tier, where pasting a record is permitted anyway.
- **Minimum necessary at `lookup` is a prompt, not a control.** Nothing
  stops the assistant reading more than a question needed; the audit
  trail records it, and that is the check.
- **Tool results are truncated at 40,000 characters.** A very large
  bundle under the `large` profile will be cut, and the assistant is told
  so — but it is working from a partial record at that point.
- **Not covered by an independent security review**, like the rest of
  this codebase. `README.md`'s Status section applies here too, and this
  component is the one that talks to the outside world.
