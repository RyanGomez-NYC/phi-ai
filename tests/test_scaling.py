# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for the paths that decide whether a large deployment is workable.

Everything here is about the same property: cost must track how much has
CHANGED, not how much EXISTS. A deployment whose verification and
reconciliation get slower every day eventually stops being verified at
all, which is worse than never having claimed to verify it.
"""

import bisect
import sys
import tracemalloc
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.checkpoint import (  # noqa: E402
    VerificationCheckpoint,
    verify_incremental,
)
from core.audit.log import GENESIS_HASH, AuditEvent  # noqa: E402
from core.db.reconcile import build_report, build_report_streaming  # noqa: E402


# ---------------------------------------------------------------------------
# Reconciliation: memory tracks discrepancies, not stored volume
# ---------------------------------------------------------------------------

def _keys(n, prefix="fhir/Observation/o"):
    return [f"{prefix}{i:09d}.json" for i in range(n)]


class _StreamStorage:
    def __init__(self, keys, extra=()):
        self._keys = list(keys) + list(extra)

    def iter_keys(self, prefix=""):
        for key in self._keys:
            yield key

    def list_keys(self, prefix=""):
        return list(self._keys)


class _StreamConn:
    def __init__(self, keys):
        self._keys = sorted(keys)

    def cursor(self):
        keys = self._keys

        class _Cursor:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params):
                last, limit = params
                start = bisect.bisect_right(keys, last)
                self._rows = [(k,) for k in keys[start:start + limit]]

            def fetchall(self):
                return self._rows

            def close(self):
                pass

        return _Cursor()


def test_streaming_reconcile_finds_the_same_drift_as_the_set_version():
    """The optimisation must not change the answer."""
    import core.db.index as index_module

    storage_keys = _keys(500) + ["fhir/Observation/zMISSING.json"]
    index_keys = _keys(500) + ["fhir/Observation/zORPHAN.json"]

    original = index_module.list_indexed_keys
    index_module.list_indexed_keys = lambda conn: set(index_keys)
    try:
        legacy = build_report(_StreamStorage(storage_keys), _StreamConn(index_keys))
    finally:
        index_module.list_indexed_keys = original

    streamed = build_report_streaming(_StreamStorage(storage_keys), _StreamConn(index_keys))

    assert set(streamed.missing_index_rows) == set(legacy.missing_index_rows)
    assert set(streamed.orphaned_index_rows) == set(legacy.orphaned_index_rows)
    assert streamed.total_storage_objects == legacy.total_storage_objects


class _GeneratedStorage:
    """Yields keys arithmetically, holding none.

    The earlier version of this fake stored the whole key list, so the
    measurement below was of the FAKE's memory rather than the function's
    - and reported growth that the production path does not have, since
    it streams from the S3 paginator. A fake that is not itself bounded
    cannot be used to prove boundedness."""

    def __init__(self, count):
        self.count = count

    def iter_keys(self, prefix=""):
        for i in range(self.count):
            yield f"fhir/Observation/o{i:09d}.json"


class _GeneratedConn:
    """Keyset pagination computed arithmetically, holding no rows."""

    def __init__(self, count, batch=10_000):
        self.count = count
        self.batch = batch

    def cursor(self):
        count, batch = self.count, self.batch

        class _Cursor:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params):
                last, limit = params
                start = 0 if not last else int(last.split("/o")[1].split(".")[0]) + 1
                self._rows = [
                    (f"fhir/Observation/o{i:09d}.json",)
                    for i in range(start, min(start + min(limit, batch), count))
                ]

            def fetchall(self):
                return self._rows

            def close(self):
                pass

        return _Cursor()


def test_reconcile_memory_does_not_grow_with_stored_volume():
    """THE scaling property. A set-based diff holds every key on both
    sides; a merge join holds one key from each plus the discrepancies.

    Both fakes generate keys rather than storing them, so what is measured
    is the function and not the harness."""
    def peak_for(n):
        tracemalloc.start()
        report = build_report_streaming(_GeneratedStorage(n), _GeneratedConn(n))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert report.total_storage_objects == n
        return peak

    # Both sizes are ABOVE the 10,000-row pagination batch, which is the
    # meaningful comparison: memory rises to one batch and then plateaus.
    # Comparing across that threshold would measure the batch filling up,
    # not growth with stored volume - which is what an earlier version of
    # this assertion did, and it failed for the wrong reason.
    base, ten_times = peak_for(100_000), peak_for(1_000_000)
    assert ten_times < base * 1.5, (
        f"memory grew with stored volume past the batch: {base} -> {ten_times}"
    )


def test_discrepancy_examples_are_capped_but_counts_stay_exact():
    """Ten million orphaned keys in a report is not a finding a human can
    act on; the count carries the magnitude."""
    storage_keys = _keys(3_000)
    report = build_report_streaming(_StreamStorage(storage_keys), _StreamConn([]),
                                    sample_limit=100)
    assert len(report.missing_index_rows) == 100
    assert report.total_storage_objects == 3_000


# ---------------------------------------------------------------------------
# Audit verification: cost tracks what was written, not what exists
# ---------------------------------------------------------------------------

def _chain(n, start_prev=GENESIS_HASH, actor="scheduler"):
    """A linear run of n valid events."""
    events, prev = [], start_prev
    for i in range(n):
        event = AuditEvent(
            actor=actor, action="record.write", resource_key=f"fhir/Observation/o{i}.json",
            purpose_of_use="scheduled_ingestion",
            timestamp=f"2026-08-18T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00",
            prev_hash=prev,
        )
        events.append(event.to_dict())
        prev = event.event_hash
    return events


class _Sink:
    def __init__(self, events):
        self._events = events
        self.reads = 0

    def iter_events(self, prefix="audit/", after_key=None):
        for i, event in enumerate(self._events):
            key = f"audit/2026/08/18/{i:09d}-{event['event_hash'][:12]}.json"
            if after_key and key <= after_key:
                continue
            self.reads += 1
            yield key, event


def test_a_first_verification_reads_the_whole_chain():
    sink = _Sink(_chain(50))
    diagnostics, checkpoint = verify_incremental(sink, None)

    assert diagnostics.ok
    assert diagnostics.total_events == 50
    assert sink.reads == 50
    assert checkpoint.events_verified == 50


def test_a_second_verification_reads_only_what_is_new():
    """THE scaling property for audit. Daily verification must cost what
    was written that day, not what exists in total."""
    events = _chain(50)
    sink = _Sink(events)
    _, checkpoint = verify_incremental(sink, None)

    events.extend(_chain(5, start_prev=events[-1]["event_hash"]))
    resumed = _Sink(events)
    diagnostics, _ = verify_incremental(resumed, checkpoint)

    assert diagnostics.ok
    assert diagnostics.total_events == 5, "re-read already-verified history"
    assert resumed.reads == 5


def test_tampering_after_the_checkpoint_is_still_caught():
    """Incremental must not mean less thorough for new events."""
    events = _chain(20)
    _, checkpoint = verify_incremental(_Sink(events), None)

    events.extend(_chain(3, start_prev=events[-1]["event_hash"]))
    events[-1] = dict(events[-1], actor="mallory")  # hash no longer recomputes

    diagnostics, _ = verify_incremental(_Sink(events), checkpoint)
    assert not diagnostics.ok
    assert diagnostics.corrupted_event_hashes


def test_an_event_continuing_from_before_the_checkpoint_is_not_a_false_alarm():
    """A writer mid-chain when the checkpoint was taken continues from a
    hash the incremental pass never sees. Seeding the checkpoint's tips is
    what stops that legitimate continuation reading as a splice."""
    events = _chain(10)
    _, checkpoint = verify_incremental(_Sink(events), None)

    # A second writer extends the same tip the checkpoint recorded.
    events.extend(_chain(2, start_prev=events[-1]["event_hash"], actor="bulk-scheduler"))
    diagnostics, _ = verify_incremental(_Sink(events), checkpoint)

    assert diagnostics.ok, diagnostics.unresolved_prev_hashes


def test_a_checkpoint_stays_small_regardless_of_chain_length():
    """It has to be storable and cheap to read. Tips are one per writer,
    not one per event."""
    _, checkpoint = verify_incremental(_Sink(_chain(2_000)), None)
    assert len(checkpoint.tip_hashes) <= 64
    assert len(checkpoint.to_json()) < 8_000


def test_an_unreadable_checkpoint_falls_back_to_full_verification():
    """The safe direction is more checking, not less."""
    assert VerificationCheckpoint.from_json("{not json") is None
    assert VerificationCheckpoint.from_json('{"unexpected": true}') is None


def test_a_checkpoint_round_trips():
    original = VerificationCheckpoint(
        verified_through_key="audit/2026/08/18/x.json",
        tip_hashes=("a" * 64,), events_verified=10, verified_at="2026-08-18T00:00:00+00:00",
    )
    restored = VerificationCheckpoint.from_json(original.to_json())
    assert restored == original
# Made by Ryan Gomez & Co. Inc.
