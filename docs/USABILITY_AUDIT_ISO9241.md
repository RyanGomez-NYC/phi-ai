# Usability audit — ISO 9241

**Scope:** the public web demo (all screens, six roles) ahead of the live-web
cut. Evaluated against ISO 9241-110:2020 (interaction principles),
ISO 9241-11:2018 (usability framework), and ISO 9241-171 (accessibility),
with ISO 9241-125 (visual presentation) notes. Method: heuristic walkthrough
of the main journeys (home → persona → assistant → revenue screens → control
panel → docs) plus mechanical DOM audits (labels, names, target sizes,
heading structure, contrast, reflow) across home, assistant, claims,
control panel, and the merged documentation page, at 1440px and 375px.
Date: 2026-08-29.

**Remediation status (same day):** all High and Medium findings fixed and
verified — H1 (labels associated, 0 unlabeled fields remain), H2/H3
(deferred-disable submit guard + "Asking…" busy state on both assistants),
M1 (2,000-char cap, client and server), M2 (persona menu discloses the
fresh-thread behavior), M3 (32px minimum targets on prompt controls, toc
and jump chips, composer buttons), M4 (walkthrough headings reordered - 0
heading skips remain), M5 (mono type floor raised to 11px). Low findings
L1 (required on the clinical composer) landed with H2; L2-L4 remain open
by choice.

## Summary

The demo conforms well to the 110 principles that are hardest to retrofit —
self-descriptiveness, conformity with expectations, and use-error robustness
are carried by the product's own design (structured refusals that name their
rule, the 403 page that teaches, post/redirect/get everywhere, empty states
on every surface). The material findings cluster in two places: **form-label
programmatic association on the Control panel** and **feedback/protection
around slow model calls**. Nothing found blocks launch; H1–H3 are
recommended before it.

## Findings

### High

- **H1 · Unlabeled form controls on the Control panel** (171 §8.2; WCAG
  1.3.1/3.3.2). 12 inputs in the configuration editor (source_*/target_*
  vendor, base URL, client ID, group ID, assistant_max_tokens, example_*)
  have visible text labels in adjacent table cells but no `label for=` /
  `aria-label` association. A screen reader announces "edit text" with no
  name. Fix: associate labels; ~20 minutes.
- **H2 · No double-submit protection on model asks** (110 §4.7 robustness
  against use error). The clinical and documentation assistants take 2–8 s
  per live call; the Ask button stays active and the page shows no busy
  state. A second click sends a second model call — duplicate spend,
  duplicate audit events, duplicated answers. Fix: disable-on-submit with an
  "Asking…" label (one inline handler; the demo's page executes script).
- **H3 · No progress feedback during model latency** (110 §4.3
  self-descriptiveness). During the same 2–8 s the only signal is the
  browser's tab spinner; on mobile it reads as a hang. Same fix as H2
  (busy state on the composer).

### Medium

- **M1 · Clinical assistant question is unbounded** (110 §4.7). The docs
  assistant caps at 600 chars; the clinical one has no maxlength, so an
  accidental large paste goes to the model whole. Fix: maxlength (~2,000)
  plus the existing server-side trim.
- **M2 · Persona switch silently discards the conversation** (110 §4.6
  controllability). Justified — the thread is role-scoped — but the menu
  does not say it. Fix: one line in the persona menu ("switching roles
  starts a fresh thread").
- **M3 · Sub-minimum touch targets** (171; WCAG 2.5.8 24px min / 44px
  ideal). Prompt-history star/remove (~20px), doc-toc and doc-jump chips
  (~24–26px; 48 instances on the merged docs page). Desktop-fine,
  thumb-hostile. Fix: min-height 32–44px via padding on those controls.
- **M4 · One heading-level skip on the merged docs page** (171). The
  "Walkthrough" h3s under the data-flow diagrams follow an h1 directly.
  Fix: h2-with-visual-h3-styling or restructure; cosmetic to sighted users,
  ordering matters to screen-reader navigation.
- **M5 · Micro-typography** (125 legibility). Mono labels at 9.5–11px
  (audit line, chips, footer badge, citation sources). Acceptable for
  glanceable metadata, below comfortable threshold for anything a user must
  read; keep the idiom but hold the floor at 11px.

### Low

- **L1 · Empty question submit is a silent no-op** (110 §4.3) — redirects
  with no message. Fix: `required` attribute.
- **L2 · CSRF failure page is unstyled plain text** with no navigation
  back. Rare path; still off-brand.
- **L3 · No Escape-key close on hover/focus dropdown menus** (110 §4.6).
  Blur closes them; Escape is the expected idiom.
- **L4 · GA consent banner has no keyboard focus trap or aria-live**;
  buttons are reachable and operable, so this is polish, not a blocker.

## What conforms well (keep doing this)

- **Suitability for the task** (110 §4.2): each role's surface carries
  exactly its work; purpose-of-use is asserted per action, not as mode.
- **Self-descriptiveness** (110 §4.3): refusals name their rule and the
  switch that caused them; empty states explain themselves; the 403 page
  names the missing permission and records the refusal; degraded modes are
  labeled (scripted fallback, disabled AI cores).
- **Conformity with user expectations** (110 §4.4): post/redirect/get on
  every mutation (back button is safe), links look like links, the header
  navigation follows platform conventions, clean URLs.
- **Learnability** (110 §4.5): example questions on both assistants, the
  homepage's guided "see it working" path, starter chips, docs jump nav.
- **Accessibility mechanics** (171): `lang` set, focus-visible outlines,
  keyboard-operable menus (tabindex + focus-within) and native
  details/summary, labeled SVG diagrams (`role="img"` + aria-label),
  prefers-reduced-motion honored by the animated diagrams, no horizontal
  overflow at 375px (reflow), th on every data table, striped rows.
- **Use-error robustness** (110 §4.7): cookie gate explains itself; rate
  limits refuse with the reason; nothing destructive is one click deep.

## ISO 9241-11 framing

- **Effectiveness:** all audited journeys completable in every role tested;
  refusal paths leave the user knowing why and what to do.
- **Efficiency:** any screen is ≤ 2 interactions from anywhere via the
  grouped header nav; PRG prevents rework; the largest costs found are the
  unindicated model waits (H2/H3).
- **Satisfaction:** consistent visual language, honest system status, and
  the demo's "watch the audit watch you" loop; the findings above are the
  main satisfaction risks (perceived freeze, thumb-target misses).

*Made by Ryan Gomez & Co. Inc.*
