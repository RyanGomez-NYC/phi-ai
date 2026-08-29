# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The grounded-assistant pipeline (SPEC §5.1, end to end).

One function, `ask()`, strings the kernel together in the only order
the spec permits:

    resources → serialize (segmentation-excluded, 5.1b)
              → grant-scoped hybrid retrieval (5.1c/d)
              → temporal weighting (5.1e)
              → structured spine for summarization-shaped questions (5.1g)
              → model composes claims (the ONLY model-shaped step)
              → answer contract: citations, attribution gate, abstention
                (5.1h) — or the Invariant 19 refusal (5.1i)

The model boundary is one injected callable, `compose`: it receives the
evidence (ranked chunks plus the spine) and returns Claims. It cannot
widen scope, cannot mint citations that were never retrieved, and
cannot prevent abstention — everything it returns passes through
assemble_answer(), which enforces all three. A deployment wires
`compose` to its registered model target through the gateway; tests
wire it to a stub. Either way this module never sees a provider SDK.

Differential-diagnosis requests are refused BEFORE retrieval: the
question classifier here is deliberately conservative and lexical
(explicit differential-request phrasings), because a false negative
just means the model gets a question its own instructions also refuse,
while the structural refusal documents the supported alternative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Mapping, Optional, Sequence

from core.governance.segmentation import CategoryValueSets, SegmentationStats
from core.rag.answer_contract import (
    Abstention,
    Answer,
    Claim,
    PurposeOfUse,
    assemble_answer,
    refuse_differential,
)
from core.rag.retriever import GrantScope, retrieve
from core.rag.serialization import Chunk, serialize_resource
from core.rag.spine import Spine, build_spine
from core.rag.temporal import RankedChunk, rank


@dataclass(frozen=True)
class Refusal:
    reason: str


#: Conservative, explicit differential-request phrasings (5.1i /
#: Invariant 19). Hypothesis-directed questions ("anything relevant to
#: amyloidosis?") deliberately do NOT match.
_DIFFERENTIAL = re.compile(
    r"\bdifferential(\s+diagnos\w*)?\b|\bddx\b|what (could|might) (this|the patient) have\b"
    r"|\bmost likely diagnos\w*\b|\brank\w* (the )?diagnos\w*",
    re.IGNORECASE,
)


def is_differential_request(question: str) -> bool:
    return bool(_DIFFERENTIAL.search(question))


#: Question shapes that get the structured spine alongside retrieval
#: (5.1g): summarization-shaped asks, where chunk-level recall silently
#: drops what mattered.
_SUMMARY_SHAPED = re.compile(
    r"\bsummar\w+\b|\boverview\b|\bhistory\b|\btell me about\b|\bwhat.s going on\b"
    r"|\bmedication list\b|\bproblem list\b|\ball (of )?(the |their |her |his )?(medication|problem|allerg)\w*",
    re.IGNORECASE,
)


def wants_spine(question: str) -> bool:
    return bool(_SUMMARY_SHAPED.search(question))


#: The compose callable: evidence in, claims out. Kept as a plain type
#: alias so core/assistant can wire any registered model target to it.
ComposeFn = Callable[[str, Sequence[RankedChunk], Optional[Spine]], Sequence[Claim]]


def serialize_corpus(
    resources_by_key: Mapping[str, Mapping],
    value_sets: CategoryValueSets,
    *,
    departments_by_key: Optional[Mapping[str, str]] = None,
    stats: Optional[SegmentationStats] = None,
) -> list[Chunk]:
    """Storage-key → resource dict, out come the chunks that may exist.
    Exclusions are counted into `stats` when given — the operator's
    §6.1 signal — and produce nothing else. ETL and pipeline share this
    one entry so there is exactly one place resources become text."""
    departments_by_key = departments_by_key or {}
    chunks: list[Chunk] = []
    for key in sorted(resources_by_key):
        result = serialize_resource(
            resources_by_key[key],
            key,
            value_sets,
            source_department=departments_by_key.get(key),
        )
        if stats is not None:
            if result.chunk is not None:
                stats.included += 1
            elif result.excluded is not None:
                stats.observe(result.excluded)
        if result.chunk is not None:
            chunks.append(result.chunk)
    return chunks


def ask(
    question: str,
    chunks: Iterable[Chunk],
    *,
    scope: GrantScope,
    purpose: PurposeOfUse,
    compose: ComposeFn,
    resources_by_key: Optional[Mapping[str, Mapping]] = None,
    anchor: Optional[date] = None,
    k: int = 10,
    expand=None,
    dense_scores=None,
    include_negated: bool = False,
    audit=None,
    actor: str = "assistant",
) -> Answer | Abstention | Refusal:
    """
    The whole 5.1 flow over an already-serialized chunk corpus.

    `include_negated` is for history-shaped questions where refuted /
    entered-in-error content is the subject — it changes RANKING only;
    the negation banners in chunk text are unconditional either way.
    """
    if is_differential_request(question):
        if audit is not None:
            audit.record(
                actor=actor,
                action="answer.refused_differential",
                resource_key=scope.patient_reference,
                purpose_of_use=purpose.value,
            )
        return Refusal(reason=refuse_differential())

    hits = retrieve(
        question, chunks, scope, k=k, expand=expand, dense_scores=dense_scores
    )
    ranked = rank(
        hits,
        anchor=anchor or date.today(),
        include_negated=include_negated,
    )
    ranked = [r for r in ranked if r.score > 0.0]

    spine: Optional[Spine] = None
    spine_keys: frozenset[str] = frozenset()
    if wants_spine(question):
        in_scope = [c for c in chunks if scope.admits(c)]
        spine = build_spine(in_scope, dict(resources_by_key or {}))
        spine_keys = spine.citation_keys()

    if not ranked and not spine_keys:
        # assemble_answer produces the audited abstention; hand it an
        # empty evidence set rather than short-circuiting so there is
        # exactly one abstention path.
        return assemble_answer(
            [],
            [],
            patient_reference=scope.patient_reference,
            purpose=purpose,
            encounter_reference=scope.encounter_reference,
            audit=audit,
            actor=actor,
        )

    claims = compose(question, ranked, spine)
    return assemble_answer(
        claims,
        [r.chunk for r in ranked],
        patient_reference=scope.patient_reference,
        purpose=purpose,
        spine_keys=spine_keys,
        encounter_reference=scope.encounter_reference,
        audit=audit,
        actor=actor,
    )


def evidence_package(
    question: str,
    resources_by_key: Mapping[str, Mapping],
    *,
    scope: GrantScope,
    value_sets: CategoryValueSets,
    anchor: Optional[date] = None,
    k: int = 10,
    include_negated: bool = False,
) -> str:
    """
    The 5.1 flow rendered as an EVIDENCE TEXT for a tool-calling
    assistant (core/assistant/tools.py's grounded tool), where the
    model composes prose from tool results rather than returning
    structured Claims. Everything structurally enforceable still
    happens here — segmentation exclusion, grant-bounded retrieval,
    temporal and status ranking, the spine, the differential refusal,
    and abstention on empty evidence — and every evidence line carries
    its [cite: storage-key] so the composing model has no reason to
    assert anything uncited. The full claim-level contract binds when a
    caller uses ask() with a structured compose; this entry point is
    the honest tool-shaped rendering of the same kernel.
    """
    if is_differential_request(question):
        return refuse_differential()

    stats = SegmentationStats()
    chunks = serialize_corpus(resources_by_key, value_sets, stats=stats)

    hits = retrieve(question, chunks, scope, k=k)
    ranked = [
        r
        for r in rank(hits, anchor=anchor or date.today(), include_negated=include_negated)
        if r.score > 0.0
    ]

    spine_text = ""
    if wants_spine(question):
        # Imported here: capabilities sits above this package in the
        # layering; a module-level import would invert it.
        from core.capabilities.summarization import render_summary

        in_scope = [c for c in chunks if scope.admits(c)]
        spine_text = render_summary(build_spine(in_scope, dict(resources_by_key)))

    if not ranked and not spine_text:
        message = (
            "No responsive content was retrieved from this patient's record "
            "for that question. Do not answer it from general knowledge — "
            "say that the record holds nothing responsive."
        )
        if stats.excluded_unclassifiable or stats.excluded_by_category:
            # A record that is empty BECAUSE policy excluded it must not
            # read as an empty record — a clinician told "nothing here"
            # about a chart that is entirely sensitive-category content
            # has been misled by omission.
            message += (
                " Note: some record content is excluded from assistant "
                "retrieval by this deployment's sensitive-category policy, "
                "so absence here is not absence from the record."
            )
        return message

    lines = [
        "Evidence from this patient's record. EVERY claim in your answer "
        "must cite one of the [cite: ...] keys below; content marked "
        "REFUTED or ENTERED-IN-ERROR is recorded as NOT true and must "
        "never be presented as an active finding.",
        "",
    ]
    if spine_text:
        lines += ["=== Structured record (complete, deterministic) ===", spine_text, ""]
    if ranked:
        lines.append("=== Retrieved evidence (most relevant first) ===")
        for r in ranked:
            lines.append(f"- {r.chunk.text} [cite: {r.chunk.storage_key}]")
    if stats.excluded_unclassifiable or stats.excluded_by_category:
        lines += [
            "",
            "(Some record content is excluded from assistant retrieval by "
            "this deployment's sensitive-category policy.)",
        ]
    return "\n".join(lines)
# Made by Ryan Gomez & Co. Inc.
