# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Governance enforcement kernel — the code behind docs/SPEC.md Invariants
13–19 and cross-cutting subsystems §6.1–§6.6.

Every module in this package implements a *gate*: a decision point that
refuses rather than degrades. The spec's repeated design rule — no
silent fallback on security-relevant misconfiguration — is applied
uniformly here: an unknown jurisdiction denies capture, an unregistered
model does not execute, an unclassifiable resource is excluded and
counted, an unsigned draft never commits. There is deliberately no
"permissive mode" flag anywhere in this package.

Module map (spec section in parentheses):

- ``segmentation``    — sensitive-category segmentation engine (§6.1),
                        including the AB 352 requester-geography gate.
- ``registry``        — model registry and execution gate (§6.2,
                        Invariant 14).
- ``fairness``        — 45 CFR 92.210 made structural (§6.3).
- ``writeback``       — staged-draft-plus-signature protocol and the
                        verified Epic R4 write surface (§6.4,
                        Invariant 13).
- ``consent_gate``    — ambient capture consent gate keyed on
                        jurisdiction AND modality (§6.5, Invariant 15).
- ``source_attributes`` — HTI-1 predictive source attributes and IRM
                        summary artifact (§6.6).
- ``action_space``    — constrained action space for operational
                        predictions (Invariant 18).
- ``release_gate``    — human release gate on patient-directed output
                        (Invariant 17).

Audit integration: each gate accepts an optional ``core.audit.log
.AuditLog`` and records its verdicts through it (§6.8 extends audit
coverage to "every registration decision, every consent evaluation,
every signature event, every operator override, and every geo-gate
refusal"). The gates function without one so they stay unit-testable,
but production wiring must pass the deployment's audit log — a gate
whose refusal leaves no audit event satisfies the letter of the
invariant and none of its purpose.
"""
# Made by Ryan Gomez & Co. Inc.
