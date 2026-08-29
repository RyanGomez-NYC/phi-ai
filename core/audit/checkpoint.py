# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Verification checkpoints, so audit verification stays constant-time.

THE PROBLEM. AuditLog.diagnose_chain() needs every event's hash in memory
to prove that each prev_hash resolves to a real event - that is inherent
to the check, not an implementation shortcut. At a hundred million events
that is tens of gigabytes, and it grows forever: a deployment that
verified in a minute on day one takes an hour on day one thousand, and
eventually does not complete at all. Verification that gets slower until
it stops is verification that quietly stops happening.

THE FIX. Record how far the chain has been verified, then verify only
what is new. A checkpoint holds the last verified object key, the chain
TIPS at that point (hashes a later event may legitimately reference), and
a running count. Routine verification reads from that key forward, so its
cost tracks how much was WRITTEN since the last run rather than how much
exists.

TIPS ARE SMALL. A tip is a hash no later event has yet built on - in
practice one per concurrent writer, so a handful. That is what keeps the
checkpoint bounded while still letting a new event legitimately reference
something from before it.

WHERE A CHECKPOINT LIVES, AND WHY IT IS NOT EVIDENCE. It goes in the
Postgres index, NOT the audit bucket. An attacker who can write to the
audit bucket could otherwise forge a checkpoint claiming the tampered
range was already verified, and incremental verification would skip
exactly the events they altered. Putting it in a different system with
different credentials means defeating incremental verification requires
compromising both.

That is defence in depth, not proof. A checkpoint is an OPTIMISATION and
this module treats it as one: full verification remains available, is
what --deep runs, and is the authoritative answer. Incremental
verification tells you nothing new has been tampered with since the last
full check; only a full check tells you the whole chain is sound. Run one
periodically, and after any incident.

ACTION NAMES. Like core/audit/verify.py, nothing here filters on an
event's `action`: the `prefix` argument below is a storage KEY prefix,
and every event in the resumed key range is verified whatever it is
called.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("phi-ai.audit.checkpoint")

CHECKPOINT_KEY = "audit_verification_checkpoint"


@dataclass(frozen=True)
class VerificationCheckpoint:
    verified_through_key: str
    tip_hashes: tuple[str, ...]
    events_verified: int
    verified_at: str

    def to_json(self) -> str:
        return json.dumps({
            "verified_through_key": self.verified_through_key,
            "tip_hashes": list(self.tip_hashes),
            "events_verified": self.events_verified,
            "verified_at": self.verified_at,
        }, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Optional["VerificationCheckpoint"]:
        try:
            data = json.loads(raw)
            return cls(
                verified_through_key=data["verified_through_key"],
                tip_hashes=tuple(data.get("tip_hashes") or ()),
                events_verified=int(data.get("events_verified") or 0),
                verified_at=data.get("verified_at", ""),
            )
        except Exception as exc:
            # A malformed checkpoint must not fail the run - it should
            # fall back to a full verification, which is the safe
            # direction: more work, not less checking.
            log.warning("ignoring unreadable verification checkpoint: %s", exc)
            return None


def load_checkpoint(conn) -> Optional[VerificationCheckpoint]:
    """Read the checkpoint from the index, or None."""
    if conn is None:
        return None
    from core.db.index import read_index_state

    raw = read_index_state(conn, CHECKPOINT_KEY)
    return VerificationCheckpoint.from_json(raw) if raw else None


def save_checkpoint(conn, checkpoint: VerificationCheckpoint) -> None:
    if conn is None:
        log.info("no index configured - verification cannot checkpoint and will re-read "
                 "the whole chain each run")
        return
    from core.db.index import write_index_state

    write_index_state(conn, CHECKPOINT_KEY, checkpoint.to_json())


def verify_incremental(sink, checkpoint: Optional[VerificationCheckpoint],
                       prefix: str = "audit/"):
    """Verify events written since `checkpoint`. Returns (diagnostics, new_checkpoint).

    Memory is bounded by how much is NEW, not by the size of the log.

    An event after the checkpoint may reference a hash from before it - a
    writer that was mid-chain when the checkpoint was taken. The
    checkpoint's tips are therefore seeded as known-good ancestors, which
    is exactly what stops a legitimate continuation being reported as a
    splice onto a non-existent hash.

    `prefix` is an object storage key prefix, not an action name filter -
    see this module's docstring.
    """
    from core.audit.log import AuditEvent, GENESIS_HASH, ChainDiagnostics

    known_hashes: set[str] = set(checkpoint.tip_hashes) if checkpoint else set()
    seeded = set(known_hashes)

    corrupted: list[str] = []
    unresolved: list[str] = []
    prev_hash_counts: dict[str, int] = {}
    roots = 0
    total = 0
    last_key = checkpoint.verified_through_key if checkpoint else ""

    after = checkpoint.verified_through_key if checkpoint else None
    events: list[dict] = []

    for key, event in sink.iter_events(prefix=prefix, after_key=after):
        total += 1
        last_key = key
        events.append(event)

        try:
            recomputed = AuditEvent(
                actor=event["actor"], action=event["action"],
                resource_key=event["resource_key"],
                purpose_of_use=event["purpose_of_use"],
                timestamp=event["timestamp"], prev_hash=event["prev_hash"],
            ).event_hash
        except KeyError:
            corrupted.append(event.get("event_hash", "<no hash>"))
            continue

        if recomputed != event.get("event_hash"):
            corrupted.append(event.get("event_hash", "<no hash>"))
            continue

        known_hashes.add(event["event_hash"])
        prev = event["prev_hash"]
        prev_hash_counts[prev] = prev_hash_counts.get(prev, 0) + 1
        if prev == GENESIS_HASH:
            roots += 1

    # Resolve after the pass: an event may legitimately reference one
    # written later in key order than itself, since key order is
    # timestamp order and clocks across writers are not perfectly aligned.
    for event in events:
        prev = event.get("prev_hash")
        if prev and prev != GENESIS_HASH and prev not in known_hashes:
            unresolved.append(prev)

    diagnostics = ChainDiagnostics(
        ok=not corrupted and not unresolved,
        total_events=total,
        fork_points=sum(1 for count in prev_hash_counts.values() if count > 1),
        root_events=roots,
        corrupted_event_hashes=corrupted,
        unresolved_prev_hashes=unresolved,
    )

    # New tips: hashes nothing built on. Small by construction - one per
    # writer that has not yet been extended.
    referenced = set(prev_hash_counts)
    tips = sorted((known_hashes - referenced) - seeded) or sorted(known_hashes)[-8:]

    new_checkpoint = VerificationCheckpoint(
        verified_through_key=last_key,
        # Bounded: a pathological log should not grow the checkpoint
        # without limit, and more than a few dozen live tips means
        # something is wrong with the writers, not with this cap.
        tip_hashes=tuple(tips[:64]),
        events_verified=(checkpoint.events_verified if checkpoint else 0) + total,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
    return diagnostics, new_checkpoint
# Made by Ryan Gomez & Co. Inc.
