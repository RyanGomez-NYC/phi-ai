# Runbook: in-context SSO from the EMR (SMART on FHIR)

A clinician viewing a patient in their EMR clicks through to the platform
and arrives **already signed in, already on that patient's record**. No
second login, no re-searching for the patient they were just looking at.

That is the difference between a system people use and one they avoid,
and it is the same standard — SMART App Launch — across all six target
EMRs: **Epic, Oracle Health (Cerner), athenahealth, eClinicalWorks,
MEDITECH and NextGen Healthcare**. One implementation, not six.

---

## Before anything else: the issuer allowlist

`config/smart_issuers.yaml` is a **security control**, not a convenience.

A launch arrives with an `iss` parameter naming the FHIR server to trust
for authentication. It comes from a redirect this application does not
control. Without an allowlist, someone who can get a clinician to click a
crafted link could name **their own** authorization server — and this
application would fetch their discovery document, redirect there, and
accept their token as proof of who the user is.

An issuer that is not listed cannot launch. **A missing file disables
launch entirely** rather than allowing anything, because that is the
correct failure direction for an allowlist.

Copy `config/smart_issuers.example.yaml` and edit. Never commit real
client secrets — use `env:VARIABLE_NAME` and keep the value in your
secret store.

---

## What the platform asks the EMR for

Deliberately almost nothing:

```
launch  openid  fhirUser  patient/Patient.read
```

- `launch` is what makes this an EHR launch — it tells the authorization
  server to resolve the opaque launch token into patient context. Without
  it the clinician would be asked to pick a patient, defeating the point.
- `openid fhirUser` identifies the clinician. This platform audits every
  PHI view against a named user, and "someone launched from Epic" is not
  a name.
- `patient/Patient.read` is the **only** clinical scope, and it is
  read-only.

**No `offline_access`.** A refresh token would let this application reach
into the EMR after the clinician has gone home, and it has no background
work to do there.

**No broad clinical scopes.** The platform supplies the records; the only
thing it needs from the EMR is *which patient*. Asking for more would
expand what a compromise of this application yields, for nothing — and it
is the kind of request that stalls a security review for good reason.

---

## Registration, per EMR

Each vendor is federated: a client id is issued **per customer
organisation**, not globally, so every EMR instance is a separate entry
in the allowlist.

| EMR | Where to register |
|---|---|
| Epic | [fhir.epic.com](https://fhir.epic.com/Documentation?docId=oauth2) — then each customer enables the app in their environment |
| Oracle Health (Cerner) | Oracle Health developer console — register as a **provider**-facing app (docs moved: `fhir.cerner.com` now redirects to [docs.oracle.com](https://docs.oracle.com/en/industries/health/millennium-platform-apis/)) |
| athenahealth | [Marketplace / developer portal](https://docs.athenahealth.com/api/guides/smart-fhir) — enabled per practice |
| eClinicalWorks | [Developer programme](https://fhir.eclinicalworks.com/ecwopendev) |
| MEDITECH | [Greenfield Workspace](https://ehr.meditech.com/ehr-solutions/greenfield-workspace-resources) — no self-service sandbox; MEDITECH issues credentials and endpoints, and provider-facing launch support should be confirmed with them per site |
| NextGen | [Developer portal](https://www.nextgen.com/api-and-developer-portal) |

Register the redirect URI as **exactly** the value of
`PHI_AI_WEB_SMART_REDIRECT_URI`. Authorization servers reject a
mismatch, by design — that check is what stops an attacker redirecting a
code to themselves.

---

## Patient ids only resolve within their own EMR

This is the failure mode most likely to surface in a real deployment.

In-context launch works by taking the patient id the EMR sends and
looking it up here. **Patient ids are opaque and instance-specific.** An
id from a Cerner tenant means nothing in an object store populated from
Epic.

Set `record_source: false` for any EMR that may launch the platform but
did not populate it. A launch from such an EMR lands the clinician on a
**search page with an explanation** rather than an empty record — because
an empty record reads as *"this patient has no history"*, which is false
and clinically misleading.

> **⚠️ Spell `record_source` exactly. A typo here fails silently.** The
> loader matches keys literally and treats an unrecognised key as
> forward-compatible rather than fatal, so a misspelling raises no error
> — the key is ignored and the issuer stops being treated as a source.
> The visible effect is the exact inversion of what this section is for:
> a launch from the EMR that actually populated this deployment lands the
> clinician on a search page reading as though no history exists. This is
> the one setting in this file that degrades quietly instead of loudly;
> check it by launching, not by reading the YAML.

---

## Configuration

| Variable | Required for launch | Meaning |
|---|:--:|---|
| `PHI_AI_WEB_SMART_REDIRECT_URI` | yes | Must exactly match what is registered with each EMR |
| `PHI_AI_WEB_SESSION_SECRET` | yes | Signs the session cookie carrying a completed launch |
| `PHI_AI_WEB_SESSION_MINUTES` | no | Session lifetime, default 30 |
| `PHI_AI_SMART_ISSUERS_PATH` | no | Default `config/smart_issuers.yaml` |

The application **refuses to start** if issuers are registered without a
redirect URI or session secret, rather than starting into a launch flow
that would fail at the last step.

---

## How this relates to the proxy authentication

Two paths, both delegating the actual authentication elsewhere:

| Who | Path |
|---|---|
| HIM, compliance, records staff | Reverse proxy → your IdP (`RUNBOOK_WEB_UI.md`) |
| Clinicians launching from a chart | SMART launch → the EMR's authorization server |

A SMART launch establishes *who* and the patient context; it does not
carry PHI AI roles. Those still come from your directory group mapping —
`runbooks/RUNBOOK_IDENTITY_MAPPING.md`.

A completed SMART launch wins over proxy headers when both are present:
the clinician explicitly launched in a patient's context, and that is the
more specific statement of who is asking.

Adding a session cookie is the one place this project stores anything
authentication-shaped. It is worth being precise about what it is: the
cookie carries a username, roles and issuer — **no credential, and no
PHI** — signed, `HttpOnly`, `Secure`, `SameSite=Lax`, expiring in 30
minutes. `SameSite=Lax` rather than `Strict` because the OAuth callback
is a cross-site top-level redirect and `Strict` would drop the cookie on
arrival. The payload is signed, not encrypted, so treat the username and
role list as visible to anyone holding the cookie.

Roles come from the allowlist entry, **per issuer**, never a global
default: "authenticated by an EMR" is not the same claim as "authorised
to read this deployment's records."

---

## Verifying a deployment

```bash
# 1. An unregistered issuer must be refused with 403.
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://records.example.org/smart/launch?iss=https://attacker.example.com/fhir&launch=x"

# 2. A registered issuer must redirect (302) to that EMR's authorize endpoint,
#    with code_challenge_method=S256, a state, a nonce, and aud set.
curl -s -o /dev/null -D - \
  "https://records.example.org/smart/launch?iss=<registered-iss>&launch=test" | grep -i location
```

(`records.example.org` above is only a placeholder hostname — substitute
whatever DNS name this deployment actually serves.)

Then do it for real: open a patient in the EMR, launch the platform, and
confirm you land on that patient. Check `/audit` — you should see
`auth.smart.launch` followed by `record.read.patient` with purpose
`treatment`, attributed to your EMR username.

Over an exported log rather than the `/audit` view:

```bash
grep -E '"action": *"(auth\.smart\.launch|record\.read\.patient)"' exported-audit.jsonl
```

See `RUNBOOK_INCIDENT_RESPONSE.md` Step 2 for the same trail used to
scope a breach rather than to verify a launch.

---

## Landing in the encounter

When the EMR sends an `encounter` alongside the patient, the platform
opens that visit rather than the patient's whole history — which is what
"in context" means to a clinician looking at one encounter.

Encounter membership lives **inside** each resource, not in the index: an
encounter id ties a patient to a specific episode on a specific date,
which is the linkage `schema.sql` keeps out. So this filters by reading
resources, exactly as date scoping does.

**The filtered view says it may be incomplete, and that wording is
deliberate.** A resource whose encounter link is absent or recorded
differently will not appear. A clinician who believes they are seeing a
complete visit when they are not is worse off than one who knows the view
is filtered — so the banner states it and links to the full record.

---

## Rendering inside the EHR frame

Set `embedded: true` on an issuer to render the platform in the EMR's
iframe instead of a separate window. The layout switches to a compact
one: shorter header, tighter tables, no wasted margin. Nothing is
removed — a clinician in the frame can still need any of it.

**Enabling this is a deployment-wide decision, not a per-issuer one.** A
session cookie only reaches a cross-site frame with `SameSite=None`, so
turning it on for one EMR changes the cookie policy for every user of
that instance.

**That is why CSRF tokens are enforced unconditionally.** `SameSite=Lax`
was doing real work: it is what stopped another site POSTing to `/roi` or
`/documents` with a logged-in clinician's cookie. Removing it without
replacing it would have traded a clickjacking mitigation for a CSRF hole.
Every state-changing request now carries a per-session token, in every
configuration — a protection that exists in one deployment and not
another is one nobody can reason about.

Worth noting this closes a gap that predates embedding: **header
authentication was never CSRF-resistant**. When identity comes from a
header a proxy adds to every request from that browser, a cross-site form
POST carries it just like a legitimate request. Only the SameSite cookie
was protecting session users, and nothing was protecting proxy users.

`frame-ancestors` is an **allowlist** built from issuers with
`embedded: true`, never `*`. The set of origins permitted to frame the
platform is exactly the set of EMRs already trusted for authentication.
The rest of the policy is tight because it can be — this interface serves
no JavaScript at all, so `script-src 'none'` removes XSS as a delivery
route entirely.

If no issuer opts in, the platform sends `frame-ancestors 'none'` and
`X-Frame-Options: DENY`.

---

## Launching back into the EMR

Once a clinician has launched from an EMR, the platform offers a
**"← Back to <EMR>"** link that returns them to that patient's chart —
and to the specific encounter, when the launch carried one.

Configure `chart_url` on the issuer:

```yaml
chart_url: "https://epic.example-hospital.org/Chart?pat={patient}&csn={encounter}"
chart_label: "Epic"
```

**This is site-specific and cannot be inferred.** Unlike launching *in* —
which is SMART App Launch, a specification every target implements —
there is no standard interaction meaning "open this patient's chart in
your UI". The URL shape differs per vendor *and* per install. Ask whoever
administers the EMR for the URL that opens a patient chart, and the one
that opens a specific encounter if you want encounter-level return.

Placeholders are `{patient}` and `{encounter}`, both percent-encoded. A
template needing `{encounter}` renders **no link at all** when the session
has no encounter context — a partially-substituted URL either 404s or
opens the wrong record, and no link is better than a wrong one.

Templates are validated **at startup**, not at click time: https only, no
embedded credentials, no unknown placeholders. A clinician mid-shift
trying to get back to a chart is the worst possible moment to discover a
malformed link. A bad template fails the whole issuer file.

The link uses `target="_top"`, so from inside an EHR frame it navigates
the whole browser rather than trying to load the EHR inside its own
iframe — which would be nonsense, and which the EHR's own
`frame-ancestors` would block anyway.

Leave `chart_url` unset to offer no link.

### Why not SMART Web Messaging

HL7 defines SMART Web Messaging for this: an embedded app posts
`ui.launchActivity` or `ui.done` to the host EHR frame. It is the
standards-track answer and genuinely better for the embedded case —
closing a panel is something only the host can do.

It is not implemented, for two reasons worth recording rather than
leaving as an unexplained omission:

- **It requires JavaScript.** This interface ships `script-src 'none'`,
  which removes script injection as a delivery route outright. Trading
  that for a return button is a poor exchange on a PHI interface.
- **Support across the six targets is uneven**, and the capability must
  be negotiated at launch rather than assumed. **This runbook does not
  record which of the six implement it** - that has not been established
  here, and a per-vendor tally nobody has verified would be worse than
  admitting the gap. MEDITECH is the clearest illustration of why: per
  the registration table above it has no self-service sandbox, so its
  support cannot be looked up and has to be confirmed with them per site.
  The shape of the risk does not depend on the exact split. A return
  mechanism that works on some EMRs and silently does nothing on the rest
  is worse than a plain link that works on all six, because the failure
  is invisible to the person hitting it: the button is there, they press
  it, and nothing happens.

If a deployment needs true panel-close behaviour, that is a deliberate
decision to relax the CSP for one narrow script. Take it explicitly, and
confirm with that specific vendor that the messaging handshake is
supported before building against it.

---

## Known gaps

- **No token refresh**, deliberately — see `offline_access` above. A
  clinician whose session expires launches again from the chart.
- **The id_token audience check assumes one client id per issuer.** An
  organisation running two registrations against the same issuer needs a
  separate entry per client id.
- **Per-vendor SMART Web Messaging support is unrecorded.** See "Why not
  SMART Web Messaging" above. The feature is not implemented, so this
  costs nothing today, but the sentence "support is uneven" is currently
  a general claim rather than a checked one, and it should not be cited
  as though it were a matrix.
- **An unrecognised key in `smart_issuers.yaml` is ignored, not
  rejected.** Deliberate - the loader treats unknown keys as
  forward-compatible - but it means a misspelled `record_source` silently
  disables source treatment for that issuer rather than failing the file.
  See the warning above. This is the one place in this configuration
  where a mistake does not announce itself.
