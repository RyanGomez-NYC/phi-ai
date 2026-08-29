# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tamper-evident audit logging (HIPAA §164.312(b) audit controls).

Every event is hash-chained to the previous event: each record's hash
includes the previous record's hash, so altering or deleting a past
record breaks the chain for every record after it. This doesn't make
tampering *impossible* for someone with storage-admin access, but it
makes it *detectable*, which is the standard bar for audit controls, and
it produces a strong forensic trail for breach-notification scoping.

Records are appended to the same storage tier used for stored PHI (see
core/storage). That tier is NOT WORM-locked - S3 Object Lock and the
equivalent Azure/GCP retention locks have been removed on every cloud, so
an audit record can be deleted by any principal holding delete permission
(see core/audit/sink.py's module docstring). The hash chain therefore
makes such a deletion DETECTABLE - the chain breaks at that point, object
versioning keeps the superseded copy, and CloudTrail records the call -
but nothing PREVENTS it. That is what makes verifying the chain on a
schedule (core/audit/verify.py), not only during an incident, the control
that actually does the work now.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    actor: str  # who (user id, service account, or system identity)
    action: str  # e.g. "record.write", "record.read", "record.export", "access.denied"
    resource_key: str  # what was acted on
    purpose_of_use: Optional[str]  # required for reads per minimum-necessary standard
    timestamp: str
    prev_hash: str
    event_hash: str = field(init=False)

    def __post_init__(self):
        # `action` is part of the committed payload: an event's action
        # string cannot be edited after the fact without breaking the
        # chain from that event onward, which is the point.
        payload = {
            "actor": self.actor,
            "action": self.action,
            "resource_key": self.resource_key,
            "purpose_of_use": self.purpose_of_use,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        object.__setattr__(self, "event_hash", digest)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChainDiagnostics:
    """Full result of AuditLog.diagnose_chain() - `verify_chain()` below
    reduces this to the single bool most callers want, but
    core/audit/verify.py's CLI uses the extra fields to tell an operator
    *why* a check passed or failed, particularly to distinguish a
    legitimate concurrent-writer fork from real tampering - both involve
    more than one event at some point in the record set, and only this
    breakdown can tell them apart."""

    ok: bool
    total_events: int
    fork_points: int  # prev_hash values shared by 2+ events - concurrent writers, NOT a failure
    root_events: int  # events whose prev_hash is GENESIS_HASH
    corrupted_event_hashes: list[str]  # this event's own hash doesn't recompute - real tampering, always a failure
    unresolved_prev_hashes: list[str]  # prev_hash isn't GENESIS and matches no event_hash in this set - a
    # failure when `closed=True` (a full read: the parent should be right here); expected at the start of a
    # `closed=False` partial-range read, since that range begins mid-chain by construction


class AuditLog:
    """
    In-memory/append-only-sink audit log builder. `sink` is any callable
    that durably persists a serialized event (typically a write into
    object storage, and/or a forward to the health system's SIEM).

    THAT STORAGE IS NOT IMMUTABLE, exactly as this module's docstring
    above states: no S3 Object Lock and no equivalent Azure/GCP retention
    lock is provisioned on any cloud, so a persisted audit record can be
    deleted or overwritten by any principal holding delete permission on
    the bucket. This class had described the sink as writing into
    "immutable object storage," which contradicted that module docstring
    within the same file and would have led a reader - an auditor most of
    all - to the wrong conclusion about what the chain guarantees.

    What the hash chain built here provides is tamper EVIDENCE, not
    tamper PREVENTION. Deleting or altering a record breaks the chain at
    that point and is therefore DETECTABLE (alongside object versioning
    and the provider's own access log), but nothing here or at the
    storage layer PREVENTS it. Scheduled verification
    (core/audit/verify.py) is the control that turns that evidence into
    something anyone actually notices.
    """

    def __init__(self, sink, last_known_hash: str = GENESIS_HASH):
        self._sink = sink
        self._last_hash = last_known_hash

    def record(
        self,
        actor: str,
        action: str,
        resource_key: str,
        purpose_of_use: Optional[str] = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            resource_key=resource_key,
            purpose_of_use=purpose_of_use,
            timestamp=datetime.now(timezone.utc).isoformat(),
            prev_hash=self._last_hash,
        )
        self._sink(event.to_dict())
        self._last_hash = event.event_hash
        return event

    @staticmethod
    def diagnose_chain(events: list[dict], *, closed: bool = True) -> ChainDiagnostics:
        """
        FOUND AND FIXED (2026-08-17 audit, MEDIUM, "audit chain forks
        under concurrent writers"): the previous implementation walked
        `events` as one strict linear sequence - `prev` tracked the
        immediately-preceding list entry's hash, and any event whose
        `prev_hash` didn't match that single running value failed the
        whole check. That assumption breaks under this system's actual
        write pattern: core/fhir/scheduler.py and
        core/fhir/bulk_scheduler.py are two independent processes, each
        building its own AuditLog and each calling sink.last_hash() to
        resume the chain on startup (see core/audit/sink.py's module
        docstring - "all three sinks therefore resume the chain from the
        last persisted record on startup"). If both happen to read the
        same tip close enough together - both starting up around the
        same time, or two replicas of either process - they append two
        genuine, unaltered events sharing one `prev_hash`: a fork,
        structurally identical to two git commits sharing a parent.
        core.audit.sink's `read_all()` returns every event sorted by
        object key (a timestamp-prefixed string), which interleaves the
        two branches into one list with no branch information - the old
        linear walk reached the second branch's event, found its
        `prev_hash` didn't equal "whatever the previous list entry was",
        and reported CHAIN BROKEN. Nothing was tampered with; that is a
        false positive, and it previously would have sent an operator to
        runbooks/RUNBOOK_INCIDENT_RESPONSE.md over routine concurrent
        writes rather than an actual incident. Proven locally (this
        sandbox has no live cloud credentials) against a reconstruction
        of the OLD linear algorithm: a two-writer fork built exactly as
        described above reported `False` under it, `True` under this
        one - see tests/test_audit_log.py's
        test_concurrent_writers_fork_is_not_reported_as_broken.

        This method instead models `events` as a forest of hash-linked
        records rather than one line: every event must (a) recompute to
        its own claimed `event_hash` - proves that specific record's
        content wasn't altered after being written - and (b) carry a
        `prev_hash` equal to GENESIS_HASH or to some OTHER event's
        stored `event_hash` in this same set - proves it wasn't spliced
        onto a hash that doesn't actually exist in the record. Multiple
        events sharing a `prev_hash` (a fork) or multiple GENESIS-rooted
        events (concurrent first-ever writers - e.g. the very first
        record write and the very first bulk export both racing to
        produce either process's first-ever event) are expected and are
        NOT failures. What IS still a failure, exactly as before: any
        event whose own hash doesn't recompute (the record's content was
        altered after being written, with its stored `event_hash` left
        stale), or - on a `closed=True` (full) read - any event whose
        `prev_hash` names a hash that exists nowhere in this set and
        isn't GENESIS_HASH (the record it claims to follow was deleted,
        or this record was fabricated/spliced in). Those are the two
        things a hash chain exists to catch, and a legitimate fork
        produces neither.

        This check is entirely independent of what any event's `action`
        string says - the chain commits to the action but never
        interprets it, so no action filtering happens here at all.

        `closed=False` is for core/audit/verify.py's `--prefix` partial
        reads, which by construction begin mid-chain: an unresolved,
        non-GENESIS `prev_hash` there is an expected boundary artifact,
        not evidence of tampering, matching this tool's pre-existing,
        still-accurate caveat that "a partial verification cannot prove
        the subset's first event is genuinely the successor of whatever
        precedes it outside the range." Self-hash corruption is still a
        failure in both modes - a partial range can't excuse an event
        whose own content doesn't match its own claimed hash.

        Known, disclosed limitation: this detects a broken *link* -
        either endpoint's hash content not matching, or a link that
        points nowhere in the set - but does not attempt to detect a
        fabricated closed loop (event A's prev_hash pointing to event B
        and vice versa), which no purely per-event, order-independent
        check can rule out. Reaching that scenario requires rewriting
        every hash pointer across the affected records with full
        knowledge of this scheme, at which point the same adversary
        controls the store enough that the independent CloudTrail/
        provider-log cross-check documented in
        runbooks/RUNBOOK_INDEX_MAINTENANCE.md - not this in-band check -
        is the control actually relied on to catch them.
        """
        known_hashes = {raw["event_hash"] for raw in events}

        corrupted: list[str] = []
        for raw in events:
            reconstructed = AuditEvent(
                actor=raw["actor"],
                action=raw["action"],
                resource_key=raw["resource_key"],
                purpose_of_use=raw["purpose_of_use"],
                timestamp=raw["timestamp"],
                prev_hash=raw["prev_hash"],
            )
            if reconstructed.event_hash != raw["event_hash"]:
                corrupted.append(raw["event_hash"])

        fork_counts: dict[str, int] = {}
        unresolved: list[str] = []
        root_events = 0
        for raw in events:
            prev = raw["prev_hash"]
            fork_counts[prev] = fork_counts.get(prev, 0) + 1
            if prev == GENESIS_HASH:
                root_events += 1
            elif prev not in known_hashes:
                unresolved.append(prev)

        fork_points = sum(1 for count in fork_counts.values() if count > 1)
        ok = not corrupted and (not closed or not unresolved)

        return ChainDiagnostics(
            ok=ok,
            total_events=len(events),
            fork_points=fork_points,
            root_events=root_events,
            corrupted_event_hashes=corrupted,
            unresolved_prev_hashes=unresolved,
        )

    @staticmethod
    def verify_chain(events: list[dict]) -> bool:
        """Recompute hashes and confirm a FULL event set's chain hasn't
        been altered, tolerating legitimate concurrent-writer forks
        (see diagnose_chain() above for the full reasoning and the
        distinction between a fork and real tampering). Equivalent to
        `AuditLog.diagnose_chain(events, closed=True).ok` - kept as its
        own method since it's the shape every existing caller
        (tests/test_audit_log.py, tests/test_envelope.py,
        scripts/smoke_test_aws.py) already expects."""
        return AuditLog.diagnose_chain(events, closed=True).ok
# Made by Ryan Gomez & Co. Inc.
