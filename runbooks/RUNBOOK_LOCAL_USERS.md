# Runbook: local user accounts

**For deployments with no identity provider.** If your organisation runs
SAML or OIDC — Entra ID, Okta, Google Workspace, Keycloak, anything —
**stop here and read `runbooks/RUNBOOK_WEB_UI.md` instead.** Put this
application behind oauth2-proxy, an OIDC-enabled ALB, or Azure App
Service Authentication and let the directory you already audit decide who
gets in. That remains the recommended deployment and the default, and
everything in this document is the exception to it.

---

**Which role each account should get is not decided here.** The crosswalk
from job function to role, and the reasoning behind each grant, is in
`runbooks/RUNBOOK_IDENTITY_MAPPING.md` — it applies unchanged to local
accounts, minus the directory that would otherwise own the lifecycle.

## Why this exists

Not every organisation with a legal obligation to retain PHI runs an
identity provider. A rural critical-access hospital, a two-physician
specialty practice, a lab, a practice that has closed its doors but must
hold records for another eight years — these have real duties under
45 CFR 164.308(a)(4) (access management) and 164.312(a)(2)(i) (unique
user identification), and nothing to delegate them to.

Before this feature their options were to run the interface with the
fabricated development identity (no authentication at all), to stand up a
proxy and an IdP they had no other use for, or not to deploy. Local
accounts are the fourth option, built carefully and stated honestly.

## What it costs — read this before enabling it

`core/web/auth.py` argues at length that a second credential store on a
PHI system is a liability. Enabling this creates one. Concretely:

- **One more thing to steal.** `authn.local_users` holds a password hash
  and a TOTP secret for every member of staff.
- **One more thing to back up.** `PHI_AI_WEB_LOCAL_AUTH_KEY` is not
  in the database and not in Terraform state. Lose it and every password
  must be reset and every authenticator re-enrolled.
- **One more thing to operate.** Joiners, leavers, lockouts at 2am and
  lost phones become your job rather than your IdP's.
- **No single sign-out.** Disabling somebody in your HR system does
  nothing here. Somebody has to disable them on the accounts page too,
  and that step belongs in your termination checklist.

What it does *not* cost: the platform's other controls are unchanged. Every
PHI read is still audited with a purpose of use, roles still mean exactly
what `core/web/auth.py` says they mean, and the object store is
still the system of record.

---

## What is stored, and how

| Thing | Where | Protection |
| --- | --- | --- |
| Password | `authn.local_users.password_hash` | HMAC-SHA256 under the deployment key (the *pepper*), then scrypt (N=32768, r=8, p=1). A stolen database **without** the key cannot be attacked offline at all. |
| TOTP shared secret | `authn.local_users.mfa_secret` | AES-256-GCM under the same key, with the key's fingerprint as authenticated associated data. |
| Session | `authn.local_sessions` | The browser cookie carries only an opaque 256-bit id. Everything else is a row this application can revoke. |
| Who did what | The hash-chained audit log | Same sink as a PHI read. Not a table anyone here can edit. |

Password policy follows NIST SP 800-63B where it and habit disagree:
minimum length 12, screening against a blocklist, **no** composition
rules, and **no** forced periodic expiry. Length and screening are what
help; "one uppercase, one digit, changed every 90 days" produces
`Summer2026!` and a sticky note. If your own policy nonetheless requires
expiry, `PHI_AI_WEB_LOCAL_AUTH_PASSWORD_MAX_AGE_DAYS` exists.

---

## Configuration

Set these in your `.env` (they are not in `.env.example`, for the same
reason the existing web-interface variables are not — the web variables
are documented here in `runbooks/`, in one place per feature).

```bash
# Turn it on. Mutually exclusive with PHI_AI_WEB_TRUST_PROXY_AUTH and
# with PHI_AI_WEB_DEV_IDENTITY - the application refuses to start if
# you set two of the three.
PHI_AI_WEB_LOCAL_ACCOUNTS=true

# 32 bytes, base64 or hex. Generate with:
#   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
# Put it in AWS Secrets Manager / GCP Secret Manager / Azure Key Vault and
# BACK IT UP. See "If the key is lost" below.
PHI_AI_WEB_LOCAL_AUTH_KEY=

# Required. The sign-in is carried in the signed session cookie, so an
# ephemeral secret would sign everybody out on every restart and no two
# replicas would agree on a session. The application refuses to start
# without it when local accounts are on.
PHI_AI_WEB_SESSION_SECRET=

# Optional, with safe defaults.
PHI_AI_WEB_LOCAL_AUTH_MFA=required        # required | optional | off
PHI_AI_WEB_LOCAL_AUTH_MAX_FAILURES=5
PHI_AI_WEB_LOCAL_AUTH_LOCKOUT_MINUTES=15
PHI_AI_WEB_LOCAL_AUTH_SESSION_MINUTES=480 # absolute session ceiling
PHI_AI_WEB_LOCAL_AUTH_PASSWORD_MAX_AGE_DAYS=0   # 0 = no expiry
PHI_AI_WEB_LOCAL_AUTH_ISSUER="St Elsewhere PHI AI"  # shown in authenticator apps
PHI_AI_WEB_LOCAL_AUTH_BLOCKLIST=/etc/phi-ai/common-passwords.txt
```

The `/etc/phi-ai/` blocklist path above is a conventional filesystem
location rather than part of any name this application matches — put the
file wherever your deployment keeps its configuration and point the
variable at it.

`PHI_AI_WEB_LOCAL_AUTH_MFA=off` is allowed and logs a warning every
start. It means a single-factor login to a system holding PHI, reachable
over a network. If you set it, record the decision and the compensating
control in your risk analysis (45 CFR 164.308(a)(1)(ii)(A)) — this
deployment cannot claim MFA.

A blocklist file is one lowercase password per line, and is *added* to the
built-in list rather than replacing it. A path that cannot be read is a
startup failure, not a silent fallback: a screening control that quietly
shrinks is worse than none. The [SecLists](https://github.com/danielmiessler/SecLists)
`10-million-password-list-top-100000.txt` is a reasonable starting corpus.

---

## Installation

Local accounts live in the same Postgres as the platform's index — there is
nowhere else to keep them. The index must therefore be configured
(`PHI_AI_DB_NAME` plus `PHI_AI_DB_HOST`, or, on GCP,
`PHI_AI_GCP_CLOUD_SQL_INSTANCE_CONNECTION_NAME`).

Run these **after** your cloud's base bootstrap, connected as the same
administrator that ran it:

```bash
# AWS - as the RDS master user
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f core/db/users_schema.sql
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f core/db/users_bootstrap_aws.sql

# GCP - as the Cloud SQL instance administrator. The grants file has a
# {READER_IAM_USER} placeholder, exactly as bootstrap_gcp.sql does:
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f core/db/users_schema.sql
sed 's/{READER_IAM_USER}/"phiai-restore@my-project.iam"/g' \
    core/db/users_bootstrap_gcp.sql | psql "$ADMIN_URL" -v ON_ERROR_STOP=1

# Azure - as the Microsoft Entra administrator
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f core/db/users_schema.sql
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f core/db/users_bootstrap_azure.sql
```

(**Substitute your own project's actual value** in the GCP line — do not
paste the example. `phiai-restore@my-project.iam` illustrates the shape of
what Terraform creates on that cloud; the account ID is built from
`name_prefix` and the project is yours. Read the real value from
`terraform -chdir=deploy/gcp output reader_db_iam_user`. A wrong value
here does not error: the grants land on a principal that does not exist,
the bootstrap reports success, and sign-in then fails for everyone.)

Both files are safe to re-run. `users_bootstrap_*.sql` fails loudly if you
run it before `users_schema.sql`, rather than granting on nothing.

### Create the first administrator

There is no default account and no default password — a default
credential on a PHI system is a published credential. The first account
is created on the host, by somebody who already holds the database
credentials:

```bash
python -m core.web.useradmin create j.okafor --roles admin --generate-password
```

That prints a temporary password once. Hand it over in person. The
account is created with `must_change_password`, so the first thing
`j.okafor` does after signing in is choose a password you never knew —
which is what makes the audit trail's `actor` mean one person.

Omit `--generate-password` to be prompted for one instead (never echoed,
asked twice, checked against the policy).

Then start the interface as usual (`python -m core.web`) and sign in at
`/login`.

---

## Day-to-day

Everything below is on **Accounts** in the navigation, visible to the
`admin` role only.

| Task | Where |
| --- | --- |
| Create an account | Accounts → New account. A temporary password is shown **once**. |
| Change what somebody may do | Accounts → *user* → Roles. Tick the whole set; submitting applies it exactly. |
| Somebody left | Accounts → *user* → **Disable account**. Their live sessions end immediately, not whenever their cookie expires. |
| Forgotten password | Accounts → *user* → **Issue temporary password**. Their sessions end; they must choose a new one. |
| Lost or replaced phone | Accounts → *user* → **Clear second-factor enrolment**. They enrol a new authenticator at next sign-in. |
| Locked out | Accounts → *user* → **Clear lockout**, or wait `LOCKOUT_MINUTES`. |
| Suspected compromise | Accounts → *user* → **End all sessions**, then issue a temporary password. |

Every one of those is also available from the command line
(`python -m core.web.useradmin --help`) for the case where no
administrator can sign in. Both paths call the same functions and write
the same audit entries; a command-line action is recorded with the actor
`cli:<os user>` so it is distinguishable from the same action taken in
the interface.

**Before issuing a password or clearing an enrolment, confirm who you are
talking to.** The page cannot do that for you, and it is the step an
attacker will ask you to skip.

### The last administrator is protected

Removing the `admin` role from the only active administrator, or
disabling them, is refused. So is disabling your own account. A deployment
with no administrator can only be recovered from the command line on the
host, and refusing is kinder than that recovery.

### Roles

Unchanged from `core/web/auth.py`, which is the authority. Briefly:

- `viewer` — search and read records.
- `him` — viewer, plus document ingestion and release of information.
- `analyst` — population counts only. **Cannot open a chart.**
- `auditor` — the audit trail and integrity verification only. **Not a
  viewer with extras**; explicitly no clinical read.
- `disposition` — retention and disposal.
- `admin` — configuration and **these account pages**. Deliberately
  carries no clinical read at all: an account administrator can create
  the person who reads a chart, and cannot read one.

Grant the narrowest set that lets somebody do their job. An account with
no role can sign in and see nothing, which is the correct default rather
than a bug.

---

## What is audited

To the same hash-chained log as a PHI read, with `resource_key` set to
`user/<username>`:

`auth.login`, `auth.login.failed`, `auth.login.locked`,
`auth.login.lockout`, `auth.login.disabled`, `auth.mfa.failed`,
`auth.mfa.enrolled`, `auth.logout`, `auth.password.changed`,
`auth.password.failed`, `admin.user.created`, `admin.user.role.granted`,
`admin.user.role.revoked`, `admin.user.disabled`, `admin.user.enabled`,
`admin.user.password.reset`, `admin.user.mfa.reset`,
`admin.user.unlocked`, `admin.sessions.revoked`.

These sit in the same chain as the `record.*` PHI actions and are read
the same way — see `RUNBOOK_INCIDENT_RESPONSE.md` Step 2, where a run of
failed sign-ins around the time of a suspicious read is exactly the
corroboration that step is looking for.

A sign-in attempt for an account that does **not** exist is logged to the
application log, not the audit trail — writing an attacker-chosen string
into the audit trail as an actor would let an unauthenticated stranger
choose what appears in it.

**No audit sink, no accounts.** Account changes and sign-ins fail with a
503 if audit logging is unavailable, on the same principle that stops PHI
being served without a trail.

Review these as part of your access-management review. A run of
`auth.login.failed` followed by `auth.login.lockout` on one account is
somebody guessing; the same spread thinly across many accounts is
somebody spraying.

---

## Failure modes, and what they look like

**"Sign-in failed" for everybody, all at once.** Almost always
`PHI_AI_WEB_LOCAL_AUTH_KEY`. Sign-in returns **503** with an
explicit message naming both key fingerprints — the one the stored hashes
were made with and the one this deployment has. Restore the original key.

**Every failure says the same thing.** By design. No such user, wrong
password, disabled account and locked account are indistinguishable in the
response and take the same time, so the login page cannot be used to
enumerate your staff. The distinction you need is on the account page and
in the audit log.

**A user is stuck on "Choose a password".** They are on an
administrator-issued password. Nothing else is reachable until they
change it. That is the gate working.

**Codes are rejected but the clock looks right.** TOTP tolerates ±30
seconds. Check NTP on the *server* — a container whose clock has drifted
rejects every correct code. Each code is also single-use: entering the
same one twice, even inside its own window, fails the second time.

### If the key is lost

There is no recovery. Passwords cannot be verified and TOTP secrets
cannot be decrypted. The path back:

1. Set a new `PHI_AI_WEB_LOCAL_AUTH_KEY`.
2. `python -m core.web.useradmin reset-password <user>` for every account.
3. `python -m core.web.useradmin reset-mfa <user>` for every account.

Nothing in the object store is affected — its own KMS key is a different
key entirely, and no PHI is protected by this one. Back it up with the
same seriousness as a KMS key anyway; the outage is real.

---

## How this was verified

Stated because a credential store nobody has proven is a credential store
nobody should trust:

- **The SQL** in `core/db/users.py` and all three
  `users_bootstrap_*.sql` files were run against **live PostgreSQL 16**,
  as a role holding exactly the grants those files issue — including
  confirming that `DELETE FROM authn.local_users` is refused, that the
  role-name CHECK constraint fires on an unknown role, that the lockout
  and the MFA replay guard behave under their real `UPDATE ... WHERE`
  conditions, and that a session stops resolving when it is revoked, when
  it expires, and when its account is disabled.
- **TOTP** is checked against RFC 6238's own Appendix B test vectors, not
  against itself.
- **The routes** are covered by `tests/test_local_auth.py` against the
  real application, including that disabling an account ends a live
  session on the very next request.

---

## Known gaps

Stated rather than left to be discovered.

- **The account store shares the reader's database role.** On GCP it must
  (Cloud SQL IAM ties one identity to one Postgres role name — the same
  constraint that put `roi_requests` there and collapsed `omop_etl` into
  the ingest identity). AWS and Azure could support a dedicated
  `phi_ai_authn` role and do not, so that the three clouds'
  deployment shape and this runbook stay identical. (That is a name for a
  role which does not exist — not one you will find in any database.)
  The separation
  foregone would defend against a SQL-level mistake in this application,
  not against a compromise of it — the web process needs both the index
  and the account store in the same request either way. See
  `core/db/users_bootstrap_aws.sql`.
- **No QR code on the enrolment page.** The interface runs under
  `script-src 'none'` and displays PHI; that is not a good place to spend
  a scripting exemption. The setup key and the full `otpauth://` URI are
  shown for manual entry, which every authenticator app supports.
- **No printed recovery codes.** Recovery is administrator-mediated. A
  sheet of one-time codes is a second credential the user must store
  safely, and in a deployment small enough to have no identity provider it
  will live in the same drawer as the password. The trade is that a lost
  phone needs another person.
- **No email anywhere.** No reset links, no notifications. There is no
  mail path in this system and adding one for credentials would be a
  second credential channel with its own failure modes.
- **No WebAuthn / passkeys.** TOTP only. Passkeys would be a genuine
  improvement, especially for shared clinical workstations, and are not
  built.
- **No password expiry by default**, per NIST SP 800-63B.
  `PASSWORD_MAX_AGE_DAYS` exists for organisations whose own policy
  requires it.
- **No rate limit by source address**, only by account. A distributed
  attempt against many accounts is throttled per account and visible in
  the audit log, but not blocked at the network layer. Put this behind a
  WAF or a rate-limiting proxy if it is reachable from the internet —
  which, for a system holding PHI, it usually should not be.
- **CI does not exercise the SQL.** `tests/test_local_auth.py` runs
  against a realistic in-memory store because CI has no Postgres; the SQL
  is verified manually as described above. A drift-guard test compares the
  stub's function signatures against the real module so the two cannot
  silently diverge.

---

## Turning it off again

If you later stand up an identity provider:

1. Set `PHI_AI_WEB_TRUST_PROXY_AUTH=true` and **remove**
   `PHI_AI_WEB_LOCAL_ACCOUNTS` — the application refuses to start
   with both, deliberately.
2. Map your IdP's groups onto the same role names
   (`runbooks/RUNBOOK_WEB_UI.md`).
3. Leave `authn.*` in place until you are satisfied the new path works.
   Nothing reads those tables once local accounts are off.
4. Then drop the schema, and revoke `PHI_AI_WEB_LOCAL_AUTH_KEY` from
   wherever it is stored. Keeping a credential store you no longer use is
   the liability this runbook opened with.
