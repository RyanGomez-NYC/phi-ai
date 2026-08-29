# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Grounded retrieval kernel — docs/SPEC.md §5.1's retrieval design as
pure, testable decision cores.

This package is NOT core/db/retrieval_* (the cross-record lexical
research index, a different surface with different rules). This is the
per-patient grounded-assistant pipeline: chunk serialization with
status and negation preserved (5.1a/f), sensitive-category exclusion at
serialization time (5.1b, delegated to core/governance/segmentation),
temporal weighting (5.1e), the deterministic structured spine (5.1g),
the attribution hard gate, and the answer contract with mandatory
citations and non-disableable abstention (5.1h) plus the Invariant 19
refusal path (5.1i).

Everything here is pure functions and dataclasses over decrypted FHIR
resource dicts — the same discipline as core/db/retrieval_text.py: the
rules that decide what the assistant can say are testable without a
database, a bucket, an embedding model, or a network, and auditable by
reading these files. Wiring to the vector index and the model gateway
consumes these cores; it never re-implements their decisions.

Module map:
- ``serialization``  — 5.1(a)(b)(f): versioned deterministic chunk
                       templates; status/negation survive into text.
- ``attribution``    — the hard gate: wrong-patient or wrong-encounter
                       chunks refuse the answer, they don't tune it.
- ``temporal``       — 5.1(e): effective-date weighting against the
                       question's time anchor; a resolved 2019 problem
                       must never outrank the active list.
- ``spine``          — 5.1(g): the deterministic structured timeline;
                       the defense against silent omission.
- ``answer_contract``— 5.1(h)(i): every claim cited, abstention on
                       empty retrieval, purpose-of-use bounds, and the
                       Invariant 19 refusal that names the supported
                       alternative.
"""
# Made by Ryan Gomez & Co. Inc.
