# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Data access for the web interface.

A thin seam between the HTTP layer and the object store, for two reasons:
the routes stay testable without a live Postgres or S3, and every read
of clinical content passes through one place that records an audit entry.
That second point is the important one - "every PHI view is audited" is
only true if there is exactly one way to view PHI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

log = logging.getLogger("phi-ai.web.data")


@dataclass(frozen=True)
class PlatformStats:
    total_resources: int
    resource_type_counts: dict[str, int]
    distinct_patients: int
    earliest_stored_at: Optional[datetime]
    latest_stored_at: Optional[datetime]


class RecordReader(Protocol):
    """What the web layer needs from the platform.

    Deliberately narrow, and deliberately READ-ONLY apart from the two
    explicit write paths (document ingestion and disposition). The
    interface a UI is given is a good place to make the write surface
    small enough to enumerate.
    """

    def stats(self) -> PlatformStats: ...
    def search_patients(self, term: str, limit: int = 50) -> list[dict]: ...
    def resources_for_patient(self, patient_reference: str) -> list[dict]: ...
    def resource_index_row(self, storage_key: str) -> Optional[dict]: ...
    def read_resource(self, storage_key: str) -> dict: ...
    def read_resources(self, storage_key: str) -> list[dict]: ...
    def read_object_bytes(self, storage_key: str) -> bytes: ...
    def verify_object_integrity(self, storage_key: str) -> bool: ...
    def read_audit_events(self, limit: int = 200, actor: Optional[str] = None) -> list[dict]: ...
    def verify_audit_chain(self) -> tuple[bool, int, Optional[str]]: ...
    def expiring_resources(self, within_days: int = 90) -> list[dict]: ...


class LiveRecordReader:
    """Backed by the real Postgres index and the object store."""

    def __init__(self, connection_factory, storage, encryptor, audit_sink=None):
        self._connect = connection_factory
        self._storage = storage
        self._encryptor = encryptor
        self._audit_sink = audit_sink

    # -- index reads ------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                columns = [d[0] for d in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()

    def stats(self) -> PlatformStats:
        rows = self._query(
            "SELECT resource_type, COUNT(*) AS n FROM stored_resources GROUP BY resource_type"
        )
        counts = {r["resource_type"]: int(r["n"]) for r in rows}
        totals = self._query(
            "SELECT COUNT(DISTINCT patient_reference) AS patients, "
            "MIN(stored_at) AS earliest, MAX(stored_at) AS latest "
            "FROM stored_resources"
        )
        row = totals[0] if totals else {}
        return PlatformStats(
            total_resources=sum(counts.values()),
            resource_type_counts=counts,
            distinct_patients=int(row.get("patients") or 0),
            earliest_stored_at=row.get("earliest"),
            latest_stored_at=row.get("latest"),
        )

    def search_patients(self, term: str, limit: int = 50) -> list[dict]:
        """Search the index by patient reference.

        Searches the OPAQUE EMR reference only - never a name, MRN or date
        of birth, because none of those are in the index and putting them
        there to enable a nicer search would break the rule the index
        exists to enforce (core/db/index.py: no clinical content, ever).

        The practical consequence, which the UI states rather than hides:
        an operator needs the patient's EMR id. That is how a records
        request arrives in practice, and it is the price of an index that
        cannot leak identities if it is ever exposed.
        """
        term = (term or "").strip()
        if not term:
            return []
        return self._query(
            "SELECT patient_reference, COUNT(*) AS resource_count, "
            "MAX(stored_at) AS last_stored "
            "FROM stored_resources WHERE patient_reference ILIKE %s "
            "GROUP BY patient_reference ORDER BY patient_reference LIMIT %s",
            (f"%{term}%", limit),
        )

    def resources_for_patient(self, patient_reference: str) -> list[dict]:
        return self._query(
            "SELECT resource_type, resource_id, storage_key, sha256_hex, stored_at, "
            "retention_until FROM stored_resources WHERE patient_reference = %s "
            "ORDER BY resource_type, stored_at DESC",
            (patient_reference,),
        )

    def resource_index_row(self, storage_key: str) -> Optional[dict]:
        rows = self._query(
            "SELECT resource_type, resource_id, patient_reference, storage_key, "
            "storage_version_id, sha256_hex, stored_at, retention_until "
            "FROM stored_resources WHERE storage_key = %s",
            (storage_key,),
        )
        return rows[0] if rows else None

    def expiring_resources(self, within_days: int = 90) -> list[dict]:
        return self._query(
            "SELECT resource_type, resource_id, patient_reference, storage_key, "
            "retention_until FROM stored_resources "
            "WHERE retention_until IS NOT NULL "
            "AND retention_until <= NOW() + (%s || ' days')::interval "
            "ORDER BY retention_until ASC LIMIT 500",
            (str(within_days),),
        )

    # -- object reads -----------------------------------------------

    def _decrypt(self, storage_key: str) -> bytes:
        """Decrypt one stored object to plaintext bytes.

        The nonce-prefix convention matches core/fhir/client.py exactly;
        a divergence here would surface as a decryption failure rather
        than silent corruption, which is the right direction to fail.
        """
        stored = self._storage.get_object(storage_key)
        meta = self._storage.get_metadata(storage_key)
        return self._encryptor.decrypt(stored[12:], stored[:12], meta.wrapped_dek_b64)

    def read_resource(self, storage_key: str) -> dict:
        """One resource, whichever layout it was written under.

        A `.ndjson` key is a bundle holding many resources. Returning the
        first would be silently wrong, so this returns a synthetic Bundle
        wrapper and callers that want a specific resource use
        read_resources() and select. Callers written for the small
        profile therefore get something structurally honest rather than
        one arbitrary record.
        """
        plaintext = self._decrypt(storage_key)

        if not storage_key.endswith(".ndjson"):
            return json.loads(plaintext)

        from core.storage.layout import parse_bundle

        resources = list(parse_bundle(plaintext))
        return {
            "resourceType": "Bundle",
            "id": storage_key.rsplit("/", 1)[-1].removesuffix(".ndjson"),
            "type": "collection",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }

    def read_resources(self, storage_key: str) -> list[dict]:
        """Every resource in an object: one for JSON, many for a bundle.

        The layout-agnostic read. Anything that processes stored
        content - release of information, delivery, verification - should
        use this rather than read_resource(), so it works unchanged under
        either profile.
        """
        plaintext = self._decrypt(storage_key)
        if not storage_key.endswith(".ndjson"):
            return [json.loads(plaintext)]

        from core.storage.layout import parse_bundle

        return list(parse_bundle(plaintext))

    def verify_object_integrity(self, storage_key: str) -> bool:
        """Re-read an object and confirm its digest still matches.

        Used by legal record production, where the document states
        explicitly whether each record was re-verified. Delegates to
        ObjectStore.verify_integrity so there is one implementation of
        what "intact" means.
        """
        return bool(self._storage.verify_integrity(storage_key))

    def read_object_bytes(self, storage_key: str) -> bytes:
        """Decrypt a stored object and return its raw bytes.

        Separate from read_resource(), which parses JSON: a source
        document is a PDF or an image and must not be run through a JSON
        parser. Same nonce-prefix convention either way.
        """
        stored = self._storage.get_object(storage_key)
        meta = self._storage.get_metadata(storage_key)
        return self._encryptor.decrypt(stored[12:], stored[:12], meta.wrapped_dek_b64)

    # -- audit ------------------------------------------------------

    def read_audit_events(self, limit: int = 200, actor: Optional[str] = None) -> list[dict]:
        """Most recent audit events, bounded.

        NEVER read_all(). This is a page a human loads in a browser, and
        read_all() pulls every event ever recorded into the web worker's
        memory - tens of gigabytes on a large deployment, which is an
        outage triggered by clicking a link rather than a slow batch job.

        Streams newest-first through a bounded ring buffer, so memory is
        `limit` events regardless of how much history exists. Reading
        forward and keeping the tail costs a full listing; that is the
        price of object keys being ordered oldest-first, and it is paid in
        time rather than in memory.
        """
        if self._audit_sink is None:
            return []

        from collections import deque

        if hasattr(self._audit_sink, "recent_events"):
            # Listing-then-tail: one cheap key listing, then GETs for only
            # the newest slice (see S3AuditSink.recent_events). An actor
            # filter searches within a wider recent window rather than the
            # whole history - stated as such, not passed off as complete.
            window = limit * 10 if actor else limit
            events = self._audit_sink.recent_events(limit=window)
            if actor:
                events = [e for e in events if e.get("actor") == actor]
            return list(reversed(events[-limit:]))

        if not hasattr(self._audit_sink, "iter_events"):
            events = self._audit_sink.read_all()
            if actor:
                events = [e for e in events if e.get("actor") == actor]
            return list(reversed(events))[:limit]

        recent: deque = deque(maxlen=limit)
        for _key, event in self._audit_sink.iter_events():
            if actor and event.get("actor") != actor:
                continue
            recent.append(event)
        return list(reversed(recent))

    def verify_audit_chain(self) -> tuple[bool, int, Optional[str]]:
        """(intact, events_checked, explanation_if_not_intact).

        Delegates to AuditLog.diagnose_chain() rather than walking the
        chain here. An earlier version of this method DID walk it, with a
        strict linear algorithm, and that was wrong in a way worth
        recording: concurrent writers legitimately produce a fork - two
        events sharing one prev_hash - and a linear walk reports that as
        tampering. This interface would have shown INTEGRITY FAILURE on a
        perfectly healthy deployment and sent an operator to the incident
        response runbook for nothing. diagnose_chain tells a fork apart
        from real corruption; do not reimplement it here.

        Reduced to a tuple because the UI shows a verdict and a reason,
        not a diagnostics object - but the reason is built from the real
        breakdown, so a fork is never described to an operator as
        tampering.
        """
        if self._audit_sink is None:
            return (True, 0, None)

        from core.audit.log import AuditLog

        if hasattr(self._audit_sink, "recent_events"):
            # A page load verifies the most recent window, as an OPEN
            # chain (`closed=False`): the window starts mid-chain by
            # construction, so its first events' predecessors legitimately
            # live outside it. Full-history verification stays where it
            # belongs - core/audit/verify.py, on a schedule - because a
            # complete read is a batch job, not something a browser click
            # should trigger once the log has real history.
            events = self._audit_sink.recent_events(limit=2000)
            diagnostics = AuditLog.diagnose_chain(events, closed=False)
        else:
            events = self._audit_sink.read_all()
            diagnostics = AuditLog.diagnose_chain(events, closed=True)

        if diagnostics.ok:
            return (True, diagnostics.total_events, None)

        problems = []
        if diagnostics.corrupted_event_hashes:
            problems.append(
                f"{len(diagnostics.corrupted_event_hashes)} event(s) were modified after "
                "being written - their contents no longer match their recorded hash"
            )
        if diagnostics.unresolved_prev_hashes:
            problems.append(
                f"{len(diagnostics.unresolved_prev_hashes)} event(s) reference a "
                "predecessor that is not present - entries appear to have been removed"
            )
        if not problems:
            problems.append("the chain did not verify; see core/audit/verify.py for detail")

        return (False, diagnostics.total_events, "; ".join(problems))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
# Made by Ryan Gomez & Co. Inc.
