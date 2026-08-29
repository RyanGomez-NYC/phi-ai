# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Encounter and longitudinal summarization (SPEC §5.2): built on the
5.1(g) structured spine, not on top-k text.

The renderer below is DETERMINISTIC: given a spine, the summary's
factual skeleton — every active problem, every active medication,
every allergy, every negated assertion shown AS negated — is complete
by construction, because it is a straight rendering of a structure the
silent-omission metric already holds to zero. A model may then wrap
narrative AROUND this skeleton (through core/rag/pipeline.ask, where
the answer contract applies); it never replaces it. That division is
the §5.2 sentence "the failure mode instrumented is silent omission,
not fabrication" turned into an architecture.

Every line carries its citation key inline, in the same
claim→storage-key form the answer contract enforces, so a rendered
summary is audit-resolvable line by line.
"""

from __future__ import annotations

from core.rag.spine import Spine, SpineEntry


def _lines(header: str, entries: tuple[SpineEntry, ...]) -> list[str]:
    if not entries:
        return []
    out = [f"{header}:"]
    for entry in entries:
        out.append(f"  - {entry.label} [cite: {entry.storage_key}]")
    return out


def render_summary(spine: Spine) -> str:
    """The complete factual skeleton, sectioned, cited, deterministic.
    Sections with nothing to say are omitted entirely rather than
    rendered as 'None' — an empty section reads as a clinical negative,
    and absence is never a negative (§7.3)."""
    lines: list[str] = []
    lines += _lines("Active problems", spine.active_problems)
    lines += _lines("Active medications", spine.active_medications)
    lines += _lines("Allergies and intolerances", spine.allergies)

    if spine.labs:
        lines.append("Recent labs:")
        for series in spine.labs:
            points = ", ".join(f"{v} ({d})" for d, v in series.points[-3:])
            keys = ", ".join(series.storage_keys[-3:])
            lines.append(
                f"  - {series.display}: {points} — {series.trend} [cite: {keys}]"
            )

    lines += _lines("Resolved / inactive problems", spine.resolved_problems)
    lines += _lines("Inactive / stopped medications", spine.inactive_medications)
    lines += _lines(
        "Recorded as NOT true (refuted / entered-in-error)",
        spine.negated_assertions,
    )
    lines += _lines("Procedures and other events", spine.procedures)
    lines += _lines("Encounters", spine.encounters)
    return "\n".join(lines)
# Made by Ryan Gomez & Co. Inc.
