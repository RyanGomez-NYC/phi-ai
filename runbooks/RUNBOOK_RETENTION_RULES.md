# Runbook: Maintaining the retention ruleset

Audience: the Health Information Manager (or whoever your organization
designates to own retention schedules) responsible for deciding, and
periodically re-confirming, how long retained records must be kept.

> **Read this first: what this system does and does not do.**
>
> This software correctly **applies** whatever retention rule you enter
> into the ruleset file below - it does not, and cannot, **determine**
> whether that rule is legally correct for your organization. Building
> something that made that determination automatically would create
> exactly the wrong kind of confidence: it would look authoritative
> right up until it was wrong, and by then it would already have been
> relied on. See `docs/COMPLIANCE.md` for the full reasoning.
>
> Your professional judgment - drawing on whatever your organization's
> normal process already is (your own research, your state's medical or
> hospital licensing board, AHIMA's published guidance, your
> organization's counsel if that's part of your process) - is what
> makes an entry in this file correct. This tool's job is narrower and
> more mechanical: apply it consistently, keep a clear record of where
> it came from, and flag when it hasn't been looked at in a while.

---

## Step 1 - Determine the correct figures

Before touching the file itself, confirm what your state actually
requires. A few starting points, not a substitute for your own
verification:

- **AHIMA** (American Health Information Management Association)
  maintains ongoing professional guidance on health information
  retention, including state-by-state considerations - a legitimate,
  actively-maintained reference, unlike a one-time internet search.
- **Your state's medical board and hospital licensing agency.** These
  are frequently two SEPARATE rules with two separate periods - most
  states run a physician-records regime and a hospital-licensing regime
  that can specify different lengths. Confirm which one (or both)
  applies to your organization.
- **HIPAA does not set a retention period for patient medical records.**
  Its own retention requirement (45 CFR §164.316(b)(2)(i), 6 years) is
  for required *documentation* - policies, risk assessments, training
  records - not clinical records themselves. Don't use that 6-year
  figure as a medical-record retention period; it answers a different
  question.
- **Check whether the rule has changed recently.** This is a genuinely
  live area of law - as one concrete example, Washington's hospital
  record retention period changed in 2025, and Texas added a new
  electronic-records rule effective 2026. Confirm you're looking at the
  current version, not an older summary.

If a specific resource type in the object store needs a different period
than your state's general rule - immunization records are commonly
retained longer than general clinical records in several states - note
that separately; you'll enter it as its own rule in Step 2.

## Step 2 - Fill in the ruleset file

Copy the template:

```bash
cp config/retention_ruleset.example.yaml config/retention_ruleset.yaml
```

Open it in any text editor. Every field marked `REPLACE` needs your
actual information. The file is YAML, which is meant to be readable
without a programming background - each rule is just a labeled list of:

- `retention_years` - the number of years to retain
- `citation` - the specific statute or regulation section (specific
  enough that someone could look it up themselves - "state law" is not
  a citation)
- `reviewed_by` / `reviewed_on` - who confirmed this figure and when
- `source_note` / `regime` - optional context (e.g., "hospital" vs
  "physician" regime)

The `default_rule` applies to everything. Only add an entry under
`resource_type_rules` for a specific FHIR resource type (e.g.
`Immunization`, `DocumentReference`) if it genuinely needs a different
period than the default - most deployments won't need any.

## Step 3 - Validate before using it

```bash
python -m core.config.retention_rules_check config/retention_ruleset.yaml
```

This checks the file is well-formed and prints a plain-language summary
of exactly what it will apply - review this output yourself before
moving on. If anything is missing or malformed, the error names the
specific field and file location, not a technical error message.

The unedited template will fail this check on purpose - a 0-year
placeholder retention period is deliberately rejected, not silently
accepted.

## Step 4 - Put it into use

```bash
PHI_AI_RETENTION_RULESET_PATH=config/retention_ruleset.yaml
```

Add that line to `.env`. Once set, this file becomes the source of
retention configuration - the older
`PHI_AI_RETENTION_YEARS`/`PHI_AI_RETENTION_YEARS_OVERRIDES`
variables, if also present, are ignored in favor of it (a warning is
logged if both are set, so it's never silently ambiguous which one is
actually in effect - remove whichever you're not using).

If the file has a problem when the application starts, it fails
immediately with a clear error rather than silently falling back to a
default that might be wrong - so a mistake here is caught at startup,
not discovered later.

## Step 5 - Review periodically

Retention law changes - not often, but it does, and a figure that was
correct when entered doesn't stay correct on its own. Re-run the check
periodically:

```bash
python -m core.config.retention_rules_check config/retention_ruleset.yaml
```

Any rule not reviewed within 2 years (adjustable with
`--warn-after-years`) is flagged with a notice, not an error - it
doesn't block anything, it's a prompt to take a fresh look and confirm
the figure and citation are still current, then update `reviewed_by` /
`reviewed_on` once you have.

---

## What this file does not cover

- **Different retention for minors' records.** Several states specify a
  longer period for minors (commonly "until age of majority plus N
  years"), which this format doesn't currently support - doing so
  correctly would require the ingest process to know patient date of
  birth, which conflicts with this project's design of never handling
  identifiable demographics at the ingest/index layer (see
  `core/db/schema.sql`). If your organization needs this, it's a real,
  separate design question worth raising - not something to work around
  by inflating the general retention figure as a substitute.
- **Multiple jurisdictions in one file.** One ruleset file covers one
  state. An organization operating across multiple states needs a
  separate file (and most likely a separate deployment) per state.
- **Enforcement of any kind.** There is no Object Lock mode setting
  anymore, and nothing in storage enforces the periods in this file. A
  reviewed, cited ruleset is a strong *documentary* artifact - it records
  what the retention period should be and who decided it - but the
  object store will neither prevent an early deletion nor perform a late
  one. Disposal happens when someone runs
  `runbooks/RUNBOOK_DISPOSITION.md`.
  See `docs/COMPLIANCE.md` → "Retention and integrity".
