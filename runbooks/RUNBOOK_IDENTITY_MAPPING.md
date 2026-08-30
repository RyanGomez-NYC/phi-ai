# Runbook: mapping your users and roles to permissions

**This is the step that turns "SSO works" into "the right people see the
right things."** Getting identity to arrive — a proxy, a SMART launch,
local accounts — is covered by `runbooks/RUNBOOK_WEB_UI.md`,
`runbooks/RUNBOOK_SMART_LAUNCH.md` and `runbooks/RUNBOOK_LOCAL_USERS.md`.
This runbook picks up where those stop: a signed-in person who carries no
role can reach **nothing**, and deciding who carries which role is the
control that determines who can open a chart.

Do not defer it to after go-live. Until the mapping exists, either nobody
can work or somebody has been given far more than their job requires.

---

## The chain, and the one link you own

    your directory group   →   a PHI AI role   →   enumerated permissions   →   enforcement
    (or EMR population)        (9, fixed)          (fixed, in software)        route · tool · option
      ^ you map this           ^ you assign        ^ not editable              ^ not bypassable

Roles and their permission sets are defined in `core/web/auth.py` and are
deliberately **not configurable**. A screen can never be reached by a role
that was quietly granted something extra in a config file. What you own is
which of your people land in which role.

The role strings are also a database constraint: `users_schema.sql`'s
`ck_local_user_roles_role` lists exactly the same nine. Adding a role in
one place and not the other either makes it ungrantable or makes the grant
confer nothing — change both in the same commit.

## The failure mode to design against is silence

An authenticated person whose groups match no role gets **no permissions
at all**. The platform logs a warning (`authenticated user %r has no
recognised role groups`) and lets them in to a system where nothing is
reachable. They will report this as "the app is broken." It is the mapping
being incomplete. Step H is how you catch it before they do.

---

## Step A — decide what carries the role, per identity mode

| Identity mode | Who asserts identity | Where roles come from | What you configure |
|---|---|---|---|
| **Reverse proxy / SSO** (recommended) | Your IdP, in front of the app. `PHI_AI_WEB_TRUST_PROXY_AUTH` has no default — an operator must set it deliberately, because trusting headers on a directly-exposed port means anyone is whoever they type. | Directory group membership, forwarded in `X-Auth-Request-Groups` (identity in `X-Auth-Request-User`, `X-Auth-Request-Email`). | Your proxy must set those three headers **and strip any client-supplied copies**. Rename them with `PHI_AI_WEB_USER_HEADER` / `PHI_AI_WEB_EMAIL_HEADER` / `PHI_AI_WEB_GROUPS_HEADER` if your proxy uses its own convention. |
| **SMART-on-FHIR launch** | The EMR, at launch, from the issuer allowlist (`PHI_AI_WEB_SMART_REDIRECT_URI`). | Still your directory. The launch establishes *who* and the patient context; it does not carry PHI AI roles, because EMR security classes and these permissions are not the same vocabulary. | The same group mapping, keyed on the identity the launch asserts. Plan for a person who launches from inside the EMR but is grouped in your directory. |
| **Local accounts** (fallback only) | The platform itself — the one mode where it holds a credential (`PHI_AI_WEB_LOCAL_ACCOUNTS`, mutually exclusive with proxy trust). | `authn.local_user_roles`, constrained by the CHECK above. | Roles per account, by hand (`runbooks/RUNBOOK_LOCAL_USERS.md`). No directory to inherit from, so recertification becomes a manual duty you must schedule. |

## Step B — inventory the EMR populations that will use this

Start from your EMR: that is where your workforce is already segmented,
and the segmentation is usually right. The concept has a different name
per vendor — Epic: user **Template / SubTemplate** and security classes;
Oracle Health: **position** and its privileges; athenahealth,
eClinicalWorks, MEDITECH, NextGen: **role** or **permission group**.

Two rules keep this tractable:

- **Map only the populations who will use this platform.** An EMR has
  hundreds of templates; you need the handful whose people open this app.
- **Map job function, not seniority.** A department chair who treats
  patients is a clinician here. The title changes nothing about which
  permissions the work requires.

## Step C — create one directory group per role, named for the role

Make the group the join key — one per role. The matcher is built for it:
group names arrive comma- or semicolon-separated, are compared
case-insensitively, and match on the **trailing segment** after the last
`-` or `:`. So `PHI-AI-HIM`, `phi-ai-him` and `role:him` all resolve to
`him`, and your IdP's naming convention does not have to change. Anything
resolving to no role is ignored, so unrelated groups in the same claim are
harmless.

Do not map EMR templates straight to roles even though it looks like a
shortcut. The directory group is what your access reviews enumerate, what
joiner/mover/leaver automation writes to, and what an auditor can be
handed as evidence. An EMR template is none of those things for this
system.

## Step D — the crosswalk

Assign each group exactly one role; compose people who genuinely do two
jobs by putting them in two groups (Step E). These are the **effective**
sets the platform resolves, including `assistant:use`, which every role
holds — the assistant is granted broadly precisely because it is not a way
to see anything: each of its tools declares the same permission the
equivalent screen requires.

| Who, in your organization | Role | Effective permissions | Deliberately withheld |
|---|---|---|---|
| Treating clinicians, residents, nursing — anyone who opens a chart to provide care | `viewer` | `patient:search` `patient:read` `document:read` `imaging:read` `identity:search` `assistant:use` | Cohort queries — a clinician finding the right patient is doing lookup, not research. Also the audit trail, psychotherapy notes, configuration. |
| Health information management, release-of-information staff | `him` | `patient:search` `patient:read` `document:read` `document:ingest` `imaging:read` `identity:search` `analytics:query` `roi:create` `roi:export` `integration:view` `report:read` `assistant:use` | Audit read — add `auditor` if the role reviews the trail. Also psychotherapy notes and configuration. |
| Privacy and compliance officers, internal audit | `auditor` | `audit:read` `audit:verify` `report:read` `assistant:ops` `assistant:use` | **All clinical content.** An auditor is not a viewer with extras — they read the record of disclosures, never the records. |
| Population health and quality analysts | `analyst` | `analytics:query` `report:read` `assistant:use` | `patient:read` and `identity:search` — counts without the ability to turn a cohort into a list of names. That gap is the whole distinction between analytics and disclosure, and closing it is the most common over-grant. |
| Research staff working record-level across many charts, under IRB or privacy-board approval | `researcher` | `analytics:query` `identity:search` `research:search` `patient:search` `patient:read` `document:read` `report:read` `assistant:use` | Imaging, audit, configuration. Granted and reviewed as one unit with its own purpose code — searching every chart at once is research, not care or counting. |
| Behavioral health clinicians (**read Step F before granting**) | `psychotherapy` | `psychotherapy:read` `assistant:use` | Everything else, including ordinary chart access. Held alone it opens nothing else, by design. |
| Platform operations and configuration owners | `admin` | `admin:config` `admin:users` `analytics:query` `integration:view` `audit:read` `report:read` `assistant:ops` `assistant:use` | **Every clinical read.** `admin:users` administers local accounts only: this person can create the user who reads a chart and cannot read one themselves. |
| Records-retention and disposition decision-makers | `disposition` | `retention:read` `retention:dispose` `retention:certificate` `audit:read` `report:read` `assistant:use` | Clinical content. Disposal decisions run on holdings figures and the trail, not on reading the records being disposed. |
| Platform administrators — the smallest group you can defend | `sysadmin` | `*` `assistant:use` (the second is redundant under the wildcard; it is set literally because every role gets it) | Nothing. The only wildcard, and the only role carrying `system:admin` (the control panel). Full control is not exemption: every action lands on the audit trail under that person's own name. |

## Step E — compose roles; never invent one

People who do two jobs get two groups, and permissions union:

- Behavioral-health clinician = `viewer` + `psychotherapy`
- Records professional who also reviews the trail = `him` + `auditor`
- Operations lead who also decides retention = `admin` + `disposition`

If someone seems to need a role that does not exist, the answer is almost
always a composition you have not tried — or a job that should be split
between two people. It is never a new permission: permissions are
enumerated in software, and screens check permissions rather than role
names.

## Step F — psychotherapy is a separate approval, always

No other role includes `psychotherapy:read`, and this is the mapping's
most important property. Psychotherapy notes carry their own authorization
regime (45 CFR §164.508(a)(2)) and their own storage boundary — a separate
bucket under a separate key (`runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md`).

The role is therefore its own grant holding that one permission and
nothing else, deliberately not folded into `viewer`, `him` or
`researcher`, so that granting any breadth of general clinical access
never quietly includes the one record class the law treats differently.

Give it its own directory group, its own approval step and its own
reviewer. **Never fold it into the clinician group** because
behavioral-health staff are clinicians too.

## Step G — set purpose-of-use expectations per role

Reading clinical content requires a stated reason, recorded in the audit
entry. The permissions that demand one are `patient:read`,
`document:read`, `imaging:read`, `roi:export`, `research:search` and
`psychotherapy:read`.

A role may assert only the purposes its work plausibly involves — the
select offers only these, and the record-opening routes refuse a posted
purpose outside them:

| Role | May assert |
|---|---|
| `viewer` | treatment, operations |
| `psychotherapy` | treatment |
| `him` | payment, operations, patient request, legal |
| `researcher` | research, operations |
| `auditor`, `analyst`, `admin` | operations |
| `disposition` | operations, legal |

Train people on what their purposes mean before go-live. The purpose lands
on every audit event, and a workforce that picks one at random makes the
accounting of disclosures worth less. Note that the `research` purpose
records *which kind* of access a read was — it does not establish that the
IRB approval exists, which stays an organizational control.

## Step H — verify the mapping before anyone relies on it

Do all four. The first three can pass while the fourth fails.

1. **Read the matrix.** The Control panel (sysadmin only) shows every
   profile with its roles and the permissions those roles resolve to,
   live, from the same table the enforcement reads. It is an enforcement
   view, not a user directory — nobody is provisioned there.
2. **Sign in as a real person from each group**, not as an administrator
   imagining them. Confirm the navigation shows what that role should have
   — and confirm it is **not empty**, which is the signature of a group
   that matched no role.
3. **Test the direct URL, not just the menu.** A screen a role should not
   have must refuse the URL, not merely omit it from the nav. Hiding a
   link is presentation; the route check is the control. Paste a forbidden
   URL for each role and confirm the refusal.
4. **Read the audit trail.** Every refusal writes `access.denied` with the
   permission that was missing. A mapping error shows up there as a
   pattern — one person refused the same permission repeatedly is usually
   a group they were never added to.

## Step I — hand the lifecycle back to your directory

Once the map is in place, stop administering people here. Joiner / mover /
leaver processing, access recertification and emergency revocation all
happen in your identity system. The platform reads roles from the request
rather than caching a local copy, so it reflects the result on the next
request and there is no second role store to drift.

In local-accounts mode none of that is true, which is the real reason it
is a fallback rather than a choice.

---

## The five mistakes this step exists to prevent

1. **Everyone starts as `sysadmin` "just to get going."** Pilots become
   production. Map the real roles on day one.
2. **Psychotherapy folded into the clinician group.** It hands the one
   category of record the law treats separately to every clinician at
   once.
3. **Analysts given `patient:read`** because a cohort screen felt
   incomplete without it. That single grant converts counting into
   disclosure.
4. **EMR templates mapped straight to roles**, leaving access reviews with
   nothing to enumerate and auditors with nothing to be handed.
5. **Go-live with an unmapped group.** The people in it can sign in and
   reach nothing, and will report it as an outage rather than a
   permissions gap.

## Where this is enforced, if you want to read the code

| Concern | File |
|---|---|
| Roles, permission sets, the group matcher | `core/web/auth.py` |
| Purpose-of-use codes and per-role purposes | `core/web/auth.py` (`PURPOSES_OF_USE`, `ROLE_PURPOSES`) |
| Route enforcement and `access.denied` | `core/web/app.py` |
| Local-account role constraint | `core/db/users_schema.sql` |
| Assistant tool permissions | `core/assistant/tools.py` |
| Parallel infrastructure roles | `deploy/aws/iam.tf` |
