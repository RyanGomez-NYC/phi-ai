# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Hybrid retrieval with grant-bounded pre-filters (SPEC §5.1 c, d).

Two spec rules shape everything here:

**5.1(d): the pre-filters run INSIDE the scan, before scoring.** The
GrantScope — patient, encounter, date range, resource types, all bounded
by the caller's grant — is applied to the chunk set before any score is
computed. This is a relevance mechanism as much as an access-control
one: a top-k drawn from the whole corpus and then filtered returns
fewer usable results than a top-k drawn from the permitted scope. It is
also why this module refuses to score an unscoped query at all — there
is no "search everything" entry point.

**5.1(c): hybrid, because clinical queries carry exact tokens.** Drug
names, LOINC codes, "A1c", "EF" — tokens dense embeddings blur. The
lexical arm here is a BM25 scorer over chunk text (pure Python: a
per-patient permitted scope is hundreds of chunks, not millions, so an
index server buys nothing). The dense arm is an OPTIONAL injected
callable — the platform never requires an embedding model to answer,
and a deployment without one still gets exact-token retrieval. When
both arms run, fusion is reciprocal-rank (RRF), which needs no score
normalization between arms. Query expansion (RxNorm / SNOMED / LOINC)
is likewise an injected callable, because expansion coverage is a
deployment-time property of the terminology loader (§7.4), not a
platform constant.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from core.rag.serialization import Chunk


class RetrievalScopeError(Exception):
    """Raised on any attempt to retrieve without a patient-bounded
    scope. There is deliberately no way to search all patients through
    this module — cohort questions belong to the de-identified plane
    (SPEC §5.11)."""


@dataclass(frozen=True)
class GrantScope:
    """The caller's permitted slice, applied as predicates before
    scoring (5.1d). `patient_reference` is mandatory — see
    RetrievalScopeError."""

    patient_reference: str
    encounter_reference: Optional[str] = None
    date_from: Optional[str] = None  # ISO, inclusive, on chunk.effective
    date_to: Optional[str] = None
    resource_types: Optional[frozenset[str]] = None

    def admits(self, chunk: Chunk) -> bool:
        if chunk.subject_reference != self.patient_reference:
            return False
        if (
            self.encounter_reference is not None
            and chunk.encounter_reference is not None
            and chunk.encounter_reference != self.encounter_reference
        ):
            return False
        if self.resource_types is not None and chunk.resource_type not in self.resource_types:
            return False
        if chunk.effective is not None:
            if self.date_from is not None and chunk.effective < self.date_from:
                return False
            if self.date_to is not None and chunk.effective > self.date_to:
                return False
        # A chunk with no effective date is never excluded by a date
        # filter: absence of a date is absence of data (§7.3), and
        # dropping undated active medications on a date-scoped question
        # is exactly the silent omission §10 measures.
        return True


_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def bm25_scores(
    query_terms: Sequence[str], chunks: Sequence[Chunk], *, k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """Plain BM25 over chunk text. Deterministic, no state, no index —
    the permitted scope is small by construction."""
    docs = [_tokens(c.text) for c in chunks]
    if not docs:
        return []
    avgdl = sum(len(d) for d in docs) / len(docs)
    n = len(docs)
    scores = [0.0] * n
    for term in query_terms:
        df = sum(1 for d in docs if term in d)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, d in enumerate(docs):
            tf = d.count(term)
            if tf:
                scores[i] += idf * (tf * (k1 + 1)) / (
                    tf + k1 * (1 - b + b * len(d) / (avgdl or 1))
                )
    return scores


def _rrf(rankings: list[list[int]], n: int, *, k: int = 60) -> list[float]:
    """Reciprocal-rank fusion over index rankings (best first)."""
    fused = [0.0] * n
    for ranking in rankings:
        for rank_pos, idx in enumerate(ranking):
            fused[idx] += 1.0 / (k + rank_pos + 1)
    return fused


def retrieve(
    query: str,
    chunks: Iterable[Chunk],
    scope: GrantScope,
    *,
    k: int = 10,
    expand: Optional[Callable[[str], Sequence[str]]] = None,
    dense_scores: Optional[Callable[[str, Sequence[Chunk]], Sequence[float]]] = None,
) -> list[tuple[Chunk, float]]:
    """
    Returns up to k (chunk, score) pairs from the PERMITTED scope,
    best first, deterministic (storage-key tiebreak).

    `expand` returns extra query terms (already-loaded vocabulary
    expansions); `dense_scores` returns one score per chunk from an
    embedding arm. Both optional; absent, retrieval is lexical-only and
    says nothing about it — that is a configuration the conformance
    report surfaces (§7.4), not a silent degradation of a promised
    capability.
    """
    if not scope.patient_reference or not scope.patient_reference.strip():
        raise RetrievalScopeError(
            "Retrieval requires a patient-bounded GrantScope; there is no "
            "all-patients search on the identified plane (SPEC §5.1d, §5.11)"
        )

    admitted = [c for c in chunks if scope.admits(c)]
    if not admitted:
        return []

    terms = _tokens(query)
    if expand is not None:
        for extra in expand(query):
            terms.extend(_tokens(extra))

    lexical = bm25_scores(terms, admitted)
    order = sorted(range(len(admitted)), key=lambda i: (-lexical[i], admitted[i].storage_key))

    if dense_scores is not None:
        dense = list(dense_scores(query, admitted))
        dense_order = sorted(
            range(len(admitted)), key=lambda i: (-dense[i], admitted[i].storage_key)
        )
        fused = _rrf([order, dense_order], len(admitted))
        order = sorted(
            range(len(admitted)), key=lambda i: (-fused[i], admitted[i].storage_key)
        )
        scores = fused
    else:
        scores = lexical

    top = [i for i in order if scores[i] > 0.0][:k]
    return [(admitted[i], scores[i]) for i in top]
# Made by Ryan Gomez & Co. Inc.
