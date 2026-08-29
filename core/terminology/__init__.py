# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Terminology loading (SPEC §7.4): references, not content.

Licensing dictates this architecture. Some vocabularies may be
committed to this repository (LOINC with its notice, ICD-10-CM/PCS from
CMS/CDC, CVX, NDC); some may only ever be fetched at install time under
the deployment's own licence (SNOMED CT US, RxNorm full release, VSAC
expansions); one is disabled by default and gated on an
operator-attested licence ID (CPT). The loader in this package enforces
those classes and FAILS LOUD on missing credentials — no silent
fallback, per invariant — and reports which query expansions (5.1c) are
therefore available, because expansion coverage is a deployment-time
property, not a platform constant."""
# Made by Ryan Gomez & Co. Inc.
