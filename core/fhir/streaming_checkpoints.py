# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Streaming-plane checkpoint discipline (SPEC §6.2, "Streaming plane").

Real-time feeds for hosted models have no clean-run boundary against
which to advance a watermark, so streaming ingestion NEVER shares the
batch ETL watermark (core/fhir's schedulers own that; nothing here
touches it). Instead: per-partition offsets with EXPLICIT gap
accounting. A gap is surfaced loudly, not interpolated — the consumer
records exactly which offset ranges it never saw, and that record
survives until an operator resolves it, because a monitoring model fed
a silently gappy stream produces confident scores about a patient
state it never observed.

Pure in-memory core over an injected persistence callable, the same
sink pattern as core/audit/log.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class StreamingCheckpointError(Exception):
    pass


@dataclass(frozen=True)
class Gap:
    partition: str
    from_offset: int  # first missing offset, inclusive
    to_offset: int  # last missing offset, inclusive

    def __str__(self) -> str:
        return f"{self.partition}[{self.from_offset}..{self.to_offset}]"


@dataclass
class PartitionState:
    next_offset: int = 0  # the offset we expect to see next
    gaps: list[Gap] = field(default_factory=list)


class StreamCheckpoints:
    """`persist` is called with (partition, state-dict) after every
    accepted record and every gap detection, so a restart resumes from
    durable state — and resumes the GAP LIST too: a crash must not
    launder a gap."""

    def __init__(self, persist=None):
        self._partitions: dict[str, PartitionState] = {}
        self._persist = persist

    def restore(self, partition: str, *, next_offset: int, gaps: list[tuple[int, int]]) -> None:
        self._partitions[partition] = PartitionState(
            next_offset=next_offset,
            gaps=[Gap(partition, a, b) for a, b in gaps],
        )

    def _save(self, partition: str) -> None:
        if self._persist is not None:
            state = self._partitions[partition]
            self._persist(
                partition,
                {
                    "next_offset": state.next_offset,
                    "gaps": [(g.from_offset, g.to_offset) for g in state.gaps],
                },
            )

    def observe(self, partition: str, offset: int) -> Gap | None:
        """
        Records one received offset. In-order delivery advances the
        checkpoint; a skip RECORDS THE GAP and then advances past it
        (the stream has moved on; pretending otherwise blocks the
        partition forever); a duplicate or late offset raises unless it
        exactly fills a recorded gap boundary — regression without a
        matching gap means the feed replayed, which the operator must
        know about, not the checkpoint absorb.
        """
        state = self._partitions.setdefault(partition, PartitionState())

        if offset == state.next_offset:
            state.next_offset = offset + 1
            self._save(partition)
            return None

        if offset > state.next_offset:
            gap = Gap(partition, state.next_offset, offset - 1)
            state.gaps.append(gap)
            state.next_offset = offset + 1
            self._save(partition)
            return gap

        # offset < next_offset: late arrival. Legitimate only if it
        # lands inside a recorded gap - then it shrinks that gap.
        for i, gap in enumerate(state.gaps):
            if gap.from_offset <= offset <= gap.to_offset:
                remaining = []
                if gap.from_offset < offset:
                    remaining.append(Gap(partition, gap.from_offset, offset - 1))
                if offset < gap.to_offset:
                    remaining.append(Gap(partition, offset + 1, gap.to_offset))
                state.gaps[i : i + 1] = remaining
                self._save(partition)
                return None
        raise StreamingCheckpointError(
            f"Partition {partition!r} regressed to offset {offset} with no "
            f"matching gap (next expected {state.next_offset}); the feed "
            "replayed or forked - operator attention required"
        )

    def open_gaps(self, partition: str | None = None) -> tuple[Gap, ...]:
        if partition is not None:
            return tuple(self._partitions.get(partition, PartitionState()).gaps)
        return tuple(
            gap
            for name in sorted(self._partitions)
            for gap in self._partitions[name].gaps
        )

    def assert_gapless(self) -> None:
        """The loud surface: callers that require a complete stream
        (e.g. before certifying a monitoring window) call this and get
        every open gap in the error."""
        gaps = self.open_gaps()
        if gaps:
            raise StreamingCheckpointError(
                "Stream has unresolved gaps: "
                + ", ".join(str(g) for g in gaps)
                + " - surfaced loudly, never interpolated (SPEC §6.2)"
            )
# Made by Ryan Gomez & Co. Inc.
