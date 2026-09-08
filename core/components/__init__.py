# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Components: the third System screen, and the machinery under it.

The screen keeps the core components of PHI AI current. Every part of the
platform that can go out of date declares itself ONCE through the
interface in core/components/registry.py - key, group, kind, how to read
what is running and what was built, where "latest known" comes from, its
cadence, and its five-step update procedure - and the screen enumerates
that registry. Nothing about a component is typed into a template.

Packages:

  registry   the Component interface, the five steps, the five states,
             the registry and the readers' context (THE CONTRACT)
  members    every component declared, in group order A to E
  build      the release stamp (one RELEASE source) and BUILD.json
  manifest   components.manifest.json written by the workstation CLI
  ledger     the migration ledger (schema_migrations) and its backfill
  journal    the update journal, the job lock, and the five-step engine
  updater    the separate service that holds the docker socket

House rules this package is written under (private-notes proposal of
2026-09-07, all decisions taken): derived, never hand-listed; two modes,
one procedure; no step without a way back; the demo shows, the platform
does; aggregates drill down; role dictates visibility; never assert
unverified state; a PHI host never phones home; dark System section; no
inline script.
"""

from core.components.registry import (  # noqa: F401
    GROUPS, MODES, STATES, STEPS, Component, Context, Evidence, Fact,
    Reading, Step, component, read_all, registry, summary,
)
