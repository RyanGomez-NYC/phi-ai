# Runbook: deploying the web interface

`core/web/` serves an HTML interface and a JSON API over the object store.

> **Read this section before exposing it anywhere.**
>
> **This application performs no authentication of its own.** It reads an
> identity from HTTP headers set by an authenticating reverse proxy. If it
> is reachable without that proxy in front of it, **anyone who can reach it
> is whoever they claim to be in a header** — with full access to PHI.
>
> This is the standard pattern (oauth2-proxy, OIDC-enabled ALB, Azure App
> Service Authentication), and it was chosen because hand-rolling password
> storage, sessions, MFA and recovery for a PHI system would be strictly
> worse than delegating to an IdP your organization already runs and
> audits. Every competitor in `docs/COMPETITIVE_ANALYSIS.md` integrates
> with SSO for the same reason. But the failure mode is total, so it gets
> a warning box rather than a footnote.


An **Assistant** tab also appears when the optional AI assistant is
enabled — see `runbooks/RUNBOOK_AI_ASSISTANT.md`. It is off by default,
available to every role, and cannot reach anything a user's own role does
not already permit. Whether it can reach clinical content at all is a
per-deployment setting that is off by default.

---

## Configuration

The app refuses to start unless you declare the deployment shape. There
is no default, because the safe value differs by deployment and guessing
either way is wrong.

| Variable | Meaning |
|---|---|
| `PHI_AI_WEB_TRUST_PROXY_AUTH` | `true` only when an authenticating proxy is the **only** route to the app |
| `PHI_AI_WEB_DEV_IDENTITY` | `user:role[,role]` — local development against synthetic data only |
| `PHI_AI_WEB_USER_HEADER` | default `X-Auth-Request-User` |
| `PHI_AI_WEB_GROUPS_HEADER` | default `X-Auth-Request-Groups` |
| `PHI_AI_WEB_HOST` | default `127.0.0.1` — binding elsewhere must be deliberate |
| `PHI_AI_WEB_PORT` | default `8080` |

Setting both `TRUST_PROXY_AUTH=true` and a dev identity is refused: the
dev identity bypasses authentication entirely, and having it available in
a proxied deployment would silently defeat the proxy.

```bash
python -m core.web
```

---

## Appearance

The interface is built on one stylesheet, `core/web/static/app.css`, with
no build step and no JavaScript. To retune the look, edit the token block
at the top of that file — `docs/DESIGN_SYSTEM.md` explains the tokens and
the component classes that read from them.

Two things about it are operational rather than cosmetic:

- **The typefaces are vendored**, in `core/web/static/fonts/` (SIL OFL,
  ~176 KB). Nothing is fetched from a font CDN, so the interface looks
  the same on an air-gapped or egress-restricted network as it does on
  the open internet, and no page a clinician opens announces itself to a
  third party. **Do not widen the CSP to load a webfont** — the policy is
  `default-src 'self'` and the whole interface is served from it.
- **`script-src 'none'` still holds.** There is no client-side
  JavaScript anywhere in this interface; the one interactive affordance,
  the ask-the-assistant drawer, is a native `<details>` element.

The landing page is **Platform overview**, the assistant drawer reads
**Ask the platform**, and the index table is headed **Stored resources**.
The wordmark is `φ(ai)` — vector artwork at
`core/web/static/brand/wordmark.svg`, drawn as paths so it renders
identically without reaching the vendored typefaces, which an `<img>`-
loaded SVG cannot do. See that directory's own README for how it is
sized and why the mark is narrower than the sub-line beneath it.

---

## Clinician access from the EMR

Staff reach the platform through the proxy above. Clinicians reach it
from inside their EMR, in a patient's context, via SMART on FHIR — Epic,
Cerner, athenahealth, eClinicalWorks, MEDITECH and NextGen. See
`RUNBOOK_SMART_LAUNCH.md`.

---

## Roles

Mapped from the IdP group claim in the groups header. Group names may be
namespaced (`phi-ai-him`, `role:him`) — the trailing segment is
matched, so your IdP's naming convention does not have to change. (Those
are only illustrations of arbitrary namespacing; whatever prefix your
directory already uses works unchanged, because only the trailing segment
is matched. There is no need to rename groups in your IdP to suit this
application, and no benefit to doing so.)

| Role | Can |
|---|---|
| `viewer` | Search patients, read records and documents |
| `him` | Viewer, plus document ingestion and release of information |
| `auditor` | Audit trail and integrity verification — **no clinical read** |
| `disposition` | Retention review and disposal |
| `admin` | Configuration |

**An auditor is not a viewer with extras.** They verify the trail; they
have no business reading the records it describes. A levels-based model
expresses that badly and gets it wrong by default.

**An authenticated user whose groups map to no role gets nothing.** There
is no fallback — a default of `viewer` would mean any successful SSO login
could read PHI.

---

## What is audited, and what happens if auditing fails

Every clinical read is written to the hash-chained audit log with the
authenticated username and a declared purpose of use, **before** the
content is decrypted. If the audit write fails, the request fails and
nothing is decrypted — the same ordering `core/fhir/purge.py` uses, where
the disposal entry is written before the delete.

Refused requests are audited too (`access.denied`). A pattern of denials
is a security signal, and a trail that records only successes cannot show
one.

Clinical reads are recorded as `record.read`. See
`RUNBOOK_INCIDENT_RESPONSE.md` Step 2 for how these entries are used to
scope a breach notification.

Purpose of use is a fixed vocabulary — treatment, payment, operations,
patient request, legal. Free text is refused: a purpose that is written
but never compared is not auditable, and an "other" option collects
everything within a month.

---

## Privacy properties built into the interface

- **Search terms are POSTed, never GET.** Query strings land in proxy
  access logs, browser history and referrer headers.
- **Uvicorn's access log is disabled**, because paths carry patient
  references.
- **`Referrer-Policy: no-referrer`** on every page.
- **The index holds no names, MRNs or dates of birth**, so search is by
  the EMR's opaque patient id. That is a real usability cost, stated in
  the UI rather than hidden, and it is the price of an index that cannot
  disclose identities if it is ever exposed.

---

## Deployment sketch (oauth2-proxy)

```
Browser → oauth2-proxy (OIDC to your IdP) → 127.0.0.1:8080
```

The app must not be reachable except through the proxy. Bind it to
localhost, or to a private interface with a security group that admits
only the proxy. Configure the proxy to **set** — not pass through — the
user, email and groups headers, so a client cannot inject its own.

Verify before going live:

```bash
# From a host that can reach the app directly, bypassing the proxy.
# Expect 401, not a page.
curl -s -o /dev/null -w '%{http_code}\n' http://<app-host>:8080/patients
```

If that returns `200`, stop: the app is exposed and header-spoofable.

---

## Release of information

> The web interface connects as the **reader** role for this, and its
> grants on `roi_requests` are `SELECT, INSERT, UPDATE` - never `DELETE`.
> A fulfilled row is part of the accounting of disclosures under 45 CFR
> §164.528, and a disclosure record that can be erased is not an
> accounting. Close a superseded request by changing its status, never by
> removing the row. Its grants on `stored_resources` (the index table)
> remain SELECT-only, so the index still cannot be modified from the
> interface.

`/roi`. A request records the patient, a requester **type** (patient,
attorney, payer, employer, provider, government), the requester's
identity, an optional authorization reference, and a purpose of use.

**Identifying detail never reaches the index.** The requester's name and
authorization reference go into an encrypted object in the object store;
the Postgres row holds only the type code and storage keys. The same rule
that keeps patient names out of the index applies to the people asking
for records.

**Requests can be scoped by date and record type.** The date bounds the
**date of service**, not when the record was ingested — a retired EHR's
whole history is ingested within days, so ingest dates would scope
nothing useful. Filtering therefore reads and decrypts each candidate
resource rather than filtering in SQL, because the clinical date lives in
the resource and cannot live in the index: under Safe Harbor (45 CFR
164.514(b)(2)) dates tied to an individual are identifiers. Slower,
correct, and bounded by one patient's record count.

The end date includes its whole day. A record whose date cannot be
determined is **included** and flagged in the manifest for review —
under-producing a legal record set is the error with consequences;
over-producing is one a reviewer can see and set aside. The `Patient`
resource is always included, since a bundle of observations with no
patient identifies nobody.

**Releasing assembles and stores the record set.** Fulfilment produces
a FHIR R4 Bundle, stored encrypted as its own object. It is not
regenerated on demand, deliberately: resources get disposed of, retention
elapses, the index gets rebuilt — a disclosure you cannot reproduce is
not really accounted for. When someone asks years later "what exactly did
you release?", the answer is an object in the object store.

Creating a request and releasing one are **separate grants**
(`roi:create` vs `roi:export`). A fulfilled request cannot be re-fulfilled;
open a new one so each disclosure is accounted for separately. Denials
record a reason — a refusal is as much a part of the accounting as a
release.

**Two artifacts are produced, both stored**, from the same filtered set
in one operation so they cannot disagree:

| Artifact | Key | For |
|---|---|---|
| FHIR R4 Bundle | `roi/export/<id>.json` | Machine-readable; what another system ingests |
| Production PDF | `roi/production/<id>.pdf` | Paginated, Bates-numbered, for legal review |

The production document contains a cover sheet, a certification with a
signature block for the custodian of records, a manifest of every record
considered — **including those withheld, with the reason** — the records
themselves, and an integrity appendix.

Every page carries a sequential Bates number, which is what makes a
produced set citable: both sides mean the same page. The default prefix
is `PHIAI-` (`PHIAI-000042`). Choose it before the first production and
leave it alone afterwards — Bates numbering's only job is that a citation
resolves to the same page for everyone holding the set, so changing the
prefix mid-matter means two different pages can be cited by the same
number, which cannot be fixed after the fact.

**What the certification asserts, and does not.** It states retrieval and
integrity facts the system can stand behind. It explicitly disclaims
being an affidavit, makes no representation that the records are complete
as a matter of law, and makes none as to clinical accuracy. Those are the
custodian's determinations, and they sign for them. The integrity
appendix says whether digests were **re-verified during production** or
merely reported as recorded — a document asserting a check that never ran
would be worse than one making no claim.

Downloading a production is audited separately from fulfilment: producing
a record set and later handing someone a copy are different disclosures,
and an accounting recording only the first understates how many times the
records left the system.

The fulfilled rows, joined to the stored exports, are the accounting of
disclosures an individual may request under 45 CFR 164.528. `/reports`
renders it.

---

## Certificates of destruction

`core/fhir/disposal_certificate.py`. A one-page, plain-text artifact
recording that a specific record existed and the reason, date and time it
was destroyed.

The audit log already records every disposal and is stronger evidence —
but it is the wrong *shape* for the question actually asked. "Prove you
destroyed this record" cannot be answered with "here is our entire audit
trail, search it." A certificate is one document, about one record,
readable by someone who knows nothing about this system.

**It is checkable, not merely printable.** It embeds the SHA-256 of the
destroyed object and the hash of the disposal's audit event. Verifying it
means confirming that event is present in the chain and that the chain
still verifies. A certificate whose audit event is absent is not evidence
of destruction — it is evidence of a forgery, or of a removed audit
entry, and `verify_certificate()` says so rather than failing quietly.

Re-issuing a lost certificate produces an identical document; a second,
differently-numbered one would read as a second destruction.

---

## Break-the-glass: why there isn't one

314e publishes break-the-glass access. This deliberately does not
implement it, and the reason is worth stating rather than leaving as an
unexplained gap.

Roles here come from the IdP. An in-application emergency override would
be a **second, parallel privilege system** that grants access the identity
provider did not — bypassing the very directory the organization audits,
reviews and terminates accounts in. That is a large amount of
security-critical machinery (who may invoke it, for how long, with what
approval, revoked how) duplicating something the IdP already does well.

**Emergency access should be an emergency role grant in the IdP**, which
is auditable there, expires there, and shows up in the organization's
existing access reviews. What this application should contribute is the
audit distinction — and it does: the access is recorded with the granted
role and a stated purpose of use like any other.

If a deployment genuinely needs in-app elevation, that is a design
discussion, not a feature to add quietly.

---

## Known gaps

Stated plainly rather than left for discovery.

- **No per-requester-type print templates.** One production format is
  used for every requester. Harmony varies formatting by requester type
  (patient, attorney, payer, employer).
- **Scope is by date and record type only.** Not by encounter, episode of
  care, or clinical category.
- **Certificates are not yet generated automatically by `purge.py`.** The
  module builds and verifies them, and the disposition UI can produce
  one, but the CLI disposal path does not emit them as it runs.
- **No session timeout** — the proxy owns session lifetime, so configure
  it there.
- **No document viewer for source scans.** OCR text renders inline; the
  original scan is retained but not displayed in the browser.
