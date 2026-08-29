# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Tests for the hash-chained audit log — the tamper-evidence guarantee
is the whole point of this module, so it needs to actually be verified."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog, GENESIS_HASH  # noqa: E402


def test_chain_links_correctly():
    events = []
    log = AuditLog(sink=events.append)

    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/1.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")
    log.record(actor="user-b", action="record.read", resource_key="fhir/Patient/1.json",
               purpose_of_use="records request")

    assert events[0]["prev_hash"] == GENESIS_HASH
    assert events[1]["prev_hash"] == events[0]["event_hash"]
    assert events[2]["prev_hash"] == events[1]["event_hash"]

    assert AuditLog.verify_chain(events) is True


def test_tampering_is_detected():
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/1.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")

    # Simulate an attacker editing a past event's resource_key without
    # recomputing the chain.
    tampered = [dict(e) for e in events]
    tampered[0]["resource_key"] = "fhir/Patient/999.json"

    assert AuditLog.verify_chain(tampered) is False


def test_deleting_an_event_breaks_the_chain():
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/1.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/3.json")

    with_deletion = [events[0], events[2]]  # silently drop the middle event
    assert AuditLog.verify_chain(with_deletion) is False


def test_concurrent_writers_fork_is_not_reported_as_broken():
    """FOUND AND FIXED (2026-08-17 audit, MEDIUM, "audit chain forks
    under concurrent writers"): core/fhir/scheduler.py and
    core/fhir/bulk_scheduler.py are two independent processes that each
    build their own AuditLog and each resume from sink.last_hash() on
    startup (core/audit/sink.py). If both read the same tip before
    either one's write lands - both starting near-simultaneously, or two
    replicas of either process - they append two genuine, unaltered
    events sharing one prev_hash: a fork, not tampering. Proven against
    a reconstruction of the OLD (pre-fix) linear implementation that
    this false-positived: False on exactly this input, where the fixed
    AuditLog.verify_chain() below correctly reports True."""
    all_events = []

    seed_events = []
    AuditLog(sink=seed_events.append).record(
        actor="scheduler", action="record.write", resource_key="fhir/Patient/1.json"
    )
    tip = seed_events[-1]["event_hash"]
    all_events.extend(seed_events)

    writer_a_events = []
    AuditLog(sink=writer_a_events.append, last_known_hash=tip).record(
        actor="scheduler", action="record.write", resource_key="fhir/Patient/2.json"
    )

    writer_b_events = []
    AuditLog(sink=writer_b_events.append, last_known_hash=tip).record(
        actor="bulk-scheduler", action="record.write", resource_key="fhir/Encounter/9.json"
    )

    # read_all() sorts by object key (timestamp-prefixed), interleaving
    # both branches into one list with no branch information - the same
    # shape core/audit/sink.py's read_all() would hand to this check.
    combined = all_events + sorted(
        writer_a_events + writer_b_events, key=lambda e: e["timestamp"] + e["event_hash"]
    )

    assert AuditLog.verify_chain(combined) is True

    diag = AuditLog.diagnose_chain(combined)
    assert diag.ok is True
    assert diag.fork_points == 1
    assert diag.corrupted_event_hashes == []
    assert diag.unresolved_prev_hashes == []


def test_two_first_ever_writers_both_rooted_at_genesis_is_not_broken():
    """The fork case above, but at the very start of the chain: two
    processes that have each never written before (e.g. the very first
    incremental run and the very first bulk export, racing) both
    resume from GENESIS_HASH independently. Two GENESIS-rooted events is
    a legitimate double root, not a broken chain."""
    a_events, b_events = [], []
    AuditLog(sink=a_events.append).record(
        actor="scheduler", action="record.write", resource_key="fhir/Patient/1.json"
    )
    AuditLog(sink=b_events.append).record(
        actor="bulk-scheduler", action="record.write", resource_key="fhir/Encounter/1.json"
    )
    combined = a_events + b_events

    assert AuditLog.verify_chain(combined) is True
    diag = AuditLog.diagnose_chain(combined)
    assert diag.root_events == 2


def test_diagnose_chain_partial_range_boundary_is_not_tampering():
    """core/audit/verify.py's --prefix reads a sub-range that begins
    mid-chain by construction. The first event in that slice references
    a parent outside the queried range - diagnose_chain(closed=False)
    must not treat that as tampering, matching this tool's documented
    "cannot prove the subset's first event is genuinely the successor of
    whatever precedes it outside the range" limitation."""
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/1.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/3.json")

    sub_range = events[1:]  # starts mid-chain
    diag = AuditLog.diagnose_chain(sub_range, closed=False)

    assert diag.ok is True
    assert len(diag.unresolved_prev_hashes) == 1  # recorded, but not held against ok


def test_diagnose_chain_partial_range_still_catches_real_tampering():
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/1.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/3.json")

    sub_range = [dict(e) for e in events[1:]]
    sub_range[1]["resource_key"] = "fhir/Patient/999.json"  # tamper within the slice

    diag = AuditLog.diagnose_chain(sub_range, closed=False)
    assert diag.ok is False
    assert len(diag.corrupted_event_hashes) == 1


def test_an_action_is_committed_to_the_chain_and_cannot_be_rewritten():
    """`action` is one of the six fields hashed into event_hash, so an
    in-place edit of an action string - however well-intentioned -
    corrupts the chain from that event onward and is reported as
    tampering, which is exactly what it looks like from the outside."""
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="him-user", action="record.read", resource_key="fhir/Patient/1.json",
               purpose_of_use="records request")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")

    edited = [dict(e) for e in events]
    edited[0]["action"] = "record.export"

    assert AuditLog.verify_chain(edited) is False
    diag = AuditLog.diagnose_chain(edited)
    assert diag.ok is False
    assert len(diag.corrupted_event_hashes) == 1


def test_an_action_query_matches_exactly_one_vocabulary():
    """There is one spelling of every action - `record.*`. A reader
    filtering on an action string compares it directly; there is no
    alias table, and nothing to expand through."""
    events = []
    log = AuditLog(sink=events.append)
    log.record(actor="him-user", action="record.read", resource_key="fhir/Patient/1.json",
               purpose_of_use="records request")
    log.record(actor="svc-a", action="record.write", resource_key="fhir/Patient/2.json")
    log.record(actor="him-user", action="record.read", resource_key="fhir/Patient/3.json",
               purpose_of_use="records request")

    matched = [e["resource_key"] for e in events if e["action"] == "record.read"]

    assert matched == ["fhir/Patient/1.json", "fhir/Patient/3.json"]


if __name__ == "__main__":
    test_chain_links_correctly()
    test_tampering_is_detected()
    test_deleting_an_event_breaks_the_chain()
    test_concurrent_writers_fork_is_not_reported_as_broken()
    test_two_first_ever_writers_both_rooted_at_genesis_is_not_broken()
    test_diagnose_chain_partial_range_boundary_is_not_tampering()
    test_diagnose_chain_partial_range_still_catches_real_tampering()
    test_an_action_is_committed_to_the_chain_and_cannot_be_rewritten()
    test_an_action_query_matches_exactly_one_vocabulary()
    print("All audit log tests passed.")
# Made by Ryan Gomez & Co. Inc.
