# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Certificates of destruction for disposed records.

Proof of purge: a durable record that a record existed at some point in
the system, and the reason, date and time it was destroyed.

WHY IT IS NOT REDUNDANT WITH THE AUDIT LOG. The audit log already records
every disposal, and it is hash-chained, so it is arguably better
evidence. But it is the WRONG SHAPE for the question actually asked. When
an organisation is asked "prove you destroyed this record when you say
you did" - by a regulator, an attorney, or an individual - the answer
cannot be "here is our entire audit trail, search it." A certificate is
one document, about one record, that can be produced on its own and read
by someone who knows nothing about this system.

WHAT MAKES IT EVIDENCE RATHER THAN A CLAIM. Anyone can print a PDF saying
a record was destroyed. Two things make this checkable:

  - It embeds the SHA-256 of the destroyed object as recorded when it was
    stored. The certificate asserts something specific about a specific
    object, not a vague "a record was deleted".
  - It embeds the audit event hash of the disposal entry, which links the
    certificate into the hash chain. Verifying a certificate means finding
    that event in the chain and confirming the chain still verifies -
    which is exactly what core/audit/verify.py already does.

A certificate whose audit hash is absent from the chain is not evidence of
destruction; it is evidence of a forged certificate, and the verify
function below says so rather than failing silently.

NO PHI. A certificate is designed to be handed to someone outside the
organisation, so it carries the opaque resource id and storage key that
already appear in every S3 key, and never clinical content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

CERTIFICATE_VERSION = "1"


@dataclass(frozen=True)
class DisposalCertificate:
    certificate_id: str
    version: str

    # What was destroyed
    resource_type: str
    resource_id: str
    storage_key: str
    stored_sha256_hex: str
    versions_destroyed: int

    # When, why, by whom
    disposed_at: str
    disposal_mode: str          # "expired" | "admin-order"
    disposal_reason: str
    disposed_by: str

    # What ties this to the tamper-evident record
    audit_event_hash: str

    # Retention context, so a reader can judge whether disposal was due
    retention_until: Optional[str] = None

    def fingerprint(self) -> str:
        """Digest over the certificate's own fields.

        Detects a certificate edited after issue. It is NOT a signature -
        anyone can recompute it after changing a field. The audit event
        hash is what makes the certificate checkable against evidence the
        holder cannot rewrite; this only catches careless alteration.
        """
        payload = {k: v for k, v in asdict(self).items() if k != "certificate_fingerprint"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["certificate_fingerprint"] = self.fingerprint()
        return data

    def to_text(self) -> str:
        """Plain-text certificate, for producing to a third party.

        Deliberately plain text rather than PDF: it needs no library to
        generate or read, survives decades without a renderer, and can be
        diffed. A recipient who wants it on letterhead can paste it.
        """
        lines = [
            "CERTIFICATE OF DESTRUCTION",
            "=" * 62,
            "",
            f"Certificate ID     {self.certificate_id}",
            f"Issued             {datetime.now(timezone.utc).isoformat()}",
            "",
            "RECORD DESTROYED",
            f"  Resource type    {self.resource_type}",
            f"  Resource id      {self.resource_id}",
            f"  Storage key      {self.storage_key}",
            f"  Versions removed {self.versions_destroyed}",
            f"  SHA-256 at time  {self.stored_sha256_hex}",
            f"  of storage",
            "",
            "DESTRUCTION",
            f"  Date and time    {self.disposed_at}",
            f"  Mode             {self.disposal_mode}",
            f"  Reason           {self.disposal_reason}",
            f"  Performed by     {self.disposed_by}",
            f"  Retention until  {self.retention_until or 'not recorded'}",
            "",
            "VERIFICATION",
            f"  Audit event      {self.audit_event_hash}",
            f"  Fingerprint      {self.fingerprint()}",
            "",
            "  This certificate is verifiable. The audit event named above is an",
            "  entry in a hash-chained audit log in which each entry commits to",
            "  its predecessor. To verify: confirm that event is present in the",
            "  log and that the chain verifies (core/audit/verify.py). A",
            "  certificate whose audit event is absent from the chain is not",
            "  evidence of destruction.",
            "",
            "  This document contains no clinical content. The identifiers above",
            "  are the source system's own opaque references.",
            "=" * 62,
        ]
        return "\n".join(lines)


def build_certificate(
    resource_type: str,
    resource_id: str,
    storage_key: str,
    stored_sha256_hex: str,
    versions_destroyed: int,
    disposal_mode: str,
    disposal_reason: str,
    disposed_by: str,
    audit_event_hash: str,
    retention_until: Optional[str] = None,
    disposed_at: Optional[str] = None,
) -> DisposalCertificate:
    stamp = disposed_at or datetime.now(timezone.utc).isoformat()
    # Deterministic id: the same disposal always yields the same
    # certificate id, so re-issuing a lost certificate produces an
    # identical document rather than a second, differently-numbered one
    # that looks like a second destruction.
    certificate_id = "cert-" + hashlib.sha256(
        f"{storage_key}\x00{audit_event_hash}".encode("utf-8")
    ).hexdigest()[:24]

    return DisposalCertificate(
        certificate_id=certificate_id,
        version=CERTIFICATE_VERSION,
        resource_type=resource_type,
        resource_id=resource_id,
        storage_key=storage_key,
        stored_sha256_hex=stored_sha256_hex,
        versions_destroyed=versions_destroyed,
        disposed_at=stamp,
        disposal_mode=disposal_mode,
        disposal_reason=disposal_reason,
        disposed_by=disposed_by,
        audit_event_hash=audit_event_hash,
        retention_until=retention_until,
    )


def verify_certificate(certificate: dict, audit_events: list[dict]) -> tuple[bool, str]:
    """Check a certificate against the audit log. Returns (valid, reason).

    Two independent checks, and the second is the one that matters:
    the fingerprint catches an edited document, but a forger would simply
    recompute it. Presence of the named audit event in the chain is the
    claim a holder of the certificate cannot manufacture.
    """
    claimed = certificate.get("certificate_fingerprint")
    payload = {k: v for k, v in certificate.items() if k != "certificate_fingerprint"}
    recomputed = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if claimed != recomputed:
        return (False, "certificate has been altered since it was issued: fingerprint mismatch")

    event_hash = certificate.get("audit_event_hash")
    if not any(e.get("event_hash") == event_hash for e in audit_events):
        return (
            False,
            "the audit event this certificate names is not present in the audit log. This "
            "is not evidence of destruction - either the certificate was fabricated, or "
            "the audit entry it referenced has been removed, which is itself an incident.",
        )

    from core.audit.log import AuditLog

    diagnostics = AuditLog.diagnose_chain(audit_events, closed=True)
    if not diagnostics.ok:
        return (
            False,
            "the certificate's audit event is present, but the audit chain itself does not "
            "verify - so the log cannot currently support any claim. Investigate the chain "
            "before relying on this certificate.",
        )

    return (True, "verified: the disposal event is present in an intact audit chain")
# Made by Ryan Gomez & Co. Inc.
