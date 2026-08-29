# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Temporal weighting (SPEC §5.1e): weight retrieved chunks by effective
date against the question's time anchor. The sentence this module
exists to make true: a resolved 2019 problem must never outrank the
active list.

Two multiplicative factors on top of whatever relevance score the
retriever produced:

- **recency**: exponential decay in the distance between the chunk's
  effective date and the anchor. Half-life defaults to a year — labs
  and encounters age fast; the default is a starting point, not a
  clinical claim, and callers with a better-informed profile pass
  their own.
- **status**: active content keeps its score; resolved/inactive content
  is heavily discounted; refuted / entered-in-error content scores
  ZERO for any "what is current" ranking — it remains retrievable when
  the question is about history (rank with `include_negated=True`),
  but it can never outrank anything by default.

A chunk with NO effective date gets the neutral factor 1.0 rather than
a penalty: absence of a date is absence of data, not evidence of age
(§7.3's absence rule, applied to time).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from core.rag.serialization import (
    INACTIVE_CLINICAL,
    NEGATING_VERIFICATIONS,
    Chunk,
)

#: Score multiplier for resolved / inactive / remission content.
INACTIVE_FACTOR = 0.2


def _parse_date(value: str) -> date | None:
    """FHIR dates come as YYYY, YYYY-MM, YYYY-MM-DD, or full datetimes;
    take the date part and be tolerant — an unparseable date is treated
    like no date (neutral), never like an old one."""
    raw = value[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(raw[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


def recency_factor(
    effective: str | None, anchor: date, *, half_life_days: float = 365.0
) -> float:
    if not effective:
        return 1.0
    parsed = _parse_date(effective)
    if parsed is None:
        return 1.0
    distance = abs((anchor - parsed).days)
    return math.pow(0.5, distance / half_life_days)


def status_factor(chunk: Chunk, *, include_negated: bool = False) -> float:
    if chunk.verification_status in NEGATING_VERIFICATIONS:
        return 1.0 if include_negated else 0.0
    if chunk.clinical_status in INACTIVE_CLINICAL:
        return INACTIVE_FACTOR
    return 1.0


@dataclass(frozen=True)
class RankedChunk:
    chunk: Chunk
    score: float


def rank(
    scored_chunks: list[tuple[Chunk, float]],
    anchor: date,
    *,
    half_life_days: float = 365.0,
    include_negated: bool = False,
) -> list[RankedChunk]:
    """`scored_chunks` is (chunk, retriever_relevance_score). Output is
    sorted best-first by relevance × recency × status, with storage key
    as the deterministic tiebreak so identical inputs always rank
    identically."""
    ranked = [
        RankedChunk(
            chunk=chunk,
            score=base
            * recency_factor(chunk.effective, anchor, half_life_days=half_life_days)
            * status_factor(chunk, include_negated=include_negated),
        )
        for chunk, base in scored_chunks
    ]
    ranked.sort(key=lambda r: (-r.score, r.chunk.storage_key))
    return ranked
# Made by Ryan Gomez & Co. Inc.
