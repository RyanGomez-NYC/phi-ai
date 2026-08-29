# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Capability cores (docs/SPEC.md §5) built over the core/rag kernel
and the core/governance gates.

Each module is the decision core of one §5 capability — the part that
must be right regardless of which model composes prose around it. The
pattern is uniform: structured inputs, deterministic checks, citations
to storage keys, and refusal rather than degradation. Model calls, web
routes, and EMR wiring consume these; they never re-implement them.

- ``summarization``        — §5.2, rendering the 5.1(g) spine.
- ``patient_instructions`` — §5.3, the no-new-assertions check.
- ``prior_auth``           — §5.4, per-criterion cited evidence packets.
- ``triage``               — §5.6, the recall-biased operating point.
- ``data_quality``         — §5.13, unmapped codes / duplicates / drift.
"""
# Made by Ryan Gomez & Co. Inc.
