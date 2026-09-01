# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Release of information: request, fulfil, disclose.

The workflow products in this category are built around, and the one HIM
staff actually spend their day in. Retrieving a
record is not the same thing as releasing one: a release has a requester,
a stated authority, a defined scope, a produced record set, and a
disclosure that must remain accountable for six years under 45 CFR
164.528.

THREE PROPERTIES THIS ENFORCES:

1. IDENTIFYING DETAIL NEVER REACHES THE INDEX. A requester's name and
   authorization reference go into an encrypted stored object; the
   Postgres row holds a requester TYPE code and storage keys. See
   core/db/schema.sql - the same rule that keeps patient names out of the
   index applies to the people asking for records.

2. THE PRODUCED RECORD SET IS ITSELF STORED. Not regenerated on demand.
   A custodian asked years later "what exactly did you release?" can
   answer from the stored copy rather than re-running a query whose
   results may have changed - resources get disposed of, retention
   elapses, the index gets rebuilt. A disclosure that cannot be
   reproduced is not really accounted for.

3. FULFILMENT IS AUDITED BEFORE IT HAPPENS. Same ordering as
   core/fhir/purge.py and core/web/app.py: the audit entry is written
   first, so a failure leaves a record of the attempt rather than a
   silent disclosure.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("phi-ai.fhir.roi")

# Requester categories. Codes, not names - the name lives in the
# encrypted detail object. These are the categories ROI print templates
# in this category are organised around, because what a requester is
# entitled to differs by category.
REQUESTER_TYPES = (
    ("patient", "Patient — the individual's own right of access (45 CFR 164.524)"),
    ("attorney", "Attorney — with a valid authorization or court order"),
    ("payer", "Payer — insurer or health plan"),
    ("employer", "Employer — with a valid authorization"),
    ("provider", "Provider — continuity of care"),
    ("government", "Government or regulator — mandated report or investigation"),
)
VALID_REQUESTER_TYPES = frozenset(code for code, _ in REQUESTER_TYPES)


class ROIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ROIRequest:
    request_id: str
    patient_reference: str
    requester_type: str
    purpose_of_use: str
    status: str
    created_by: str
    created_at: datetime
    fulfilled_by: Optional[str] = None
    fulfilled_at: Optional[datetime] = None
    denied_reason: Optional[str] = None
    detail_storage_key: Optional[str] = None
    export_storage_key: Optional[str] = None
    production_storage_key: Optional[str] = None
    record_count: Optional[int] = None
    withheld_count: Optional[int] = None
    scope_start: Optional[datetime] = None
    scope_end: Optional[datetime] = None
    scope_resource_types: Optional[str] = None

    def scope_description(self) -> str:
        from core.fhir.legal_export import ExportScope

        types = (
            frozenset(t.strip() for t in self.scope_resource_types.split(",") if t.strip())
            if self.scope_resource_types
            else None
        )
        return ExportScope(
            patient_reference=self.patient_reference,
            scope_start=self.scope_start,
            scope_end=self.scope_end,
            resource_types=types,
        ).describe()


def new_request_id() -> str:
    """Opaque, unguessable request id.

    Random rather than sequential on purpose: a sequential id leaks how
    many records requests an organisation receives, and lets anyone
    holding one id enumerate its neighbours.
    """
    return f"roi-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(8)}"


def validate_requester_type(requester_type: str) -> str:
    if requester_type not in VALID_REQUESTER_TYPES:
        raise ROIError(
            f"requester_type {requester_type!r} is not one of: "
            f"{', '.join(sorted(VALID_REQUESTER_TYPES))}. What a requester is entitled to "
            "differs by category, so this is a fixed vocabulary rather than free text."
        )
    return requester_type


class ROIService:
    """
    Creates and fulfils release-of-information requests.

    Takes its storage/encryption/audit collaborators rather than building
    them, matching core/fhir/client.py, so the workflow is testable
    without Postgres, S3 or a KMS.
    """

    def __init__(self, connection_factory, storage, encryptor, audit, reader,
                 actor: str = "phi-ai-roi"):
        self._connect = connection_factory
        self._storage = storage
        self._encryptor = encryptor
        self._audit = audit
        self._reader = reader
        self.actor = actor

    # -- persistence -------------------------------------------------

    def _execute(self, sql: str, params: tuple = (), fetch: bool = False):
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                if fetch:
                    columns = [d[0] for d in cur.description]
                    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
                    conn.commit()
                    return rows
                conn.commit()
                return None
            finally:
                cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- encrypted side-objects --------------------------------------

    def _put_encrypted(self, key: str, payload: dict, content_type: str) -> str:
        """Store a JSON object through this platform's own encryption.

        Reuses the nonce-prefix convention and digest accounting from
        core/fhir/client.py so these objects verify, restore and dispose
        exactly like clinical resources - an ROI export is PHI and has no
        business being a special case.
        """
        from core.fhir.client import _stored_sha256_hex

        plaintext = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        envelope = self._encryptor.encrypt(plaintext)
        storage_bytes = envelope.nonce + envelope.ciphertext

        self._storage.put_object(
            key=key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=envelope.wrapped_dek_b64,
            sha256_hex=_stored_sha256_hex(envelope.nonce, envelope.ciphertext),
            content_type=content_type,
        )
        return key

    # -- workflow ----------------------------------------------------

    def create(
        self,
        patient_reference: str,
        requester_type: str,
        requester_detail: str,
        purpose_of_use: str,
        authorization_reference: Optional[str],
        created_by: str,
        scope_start: Optional[str] = None,
        scope_end: Optional[str] = None,
        scope_resource_types: Optional[str] = None,
    ) -> ROIRequest:
        """Open a request. Does NOT release anything.

        `scope_start`/`scope_end` are YYYY-MM-DD strings bounding DATES OF
        SERVICE, not the dates records were ingested - see
        core/fhir/clinical_dates.py. The end bound is inclusive of its
        whole day: a request "through 2021-12-31" means everything that
        day.
        """
        from core.fhir.documents import validate_patient_reference
        from core.web.auth import validate_purpose

        reference = validate_patient_reference(patient_reference)
        validate_requester_type(requester_type)
        purpose = validate_purpose(purpose_of_use)

        if not (requester_detail or "").strip():
            raise ROIError(
                "requester detail is required - who asked for these records. It is stored "
                "encrypted, not in the index, but a disclosure with no identified requester "
                "cannot be accounted for under 45 CFR 164.528."
            )

        from core.fhir.clinical_dates import coerce_scope_bound

        try:
            start = coerce_scope_bound(scope_start)
            end = coerce_scope_bound(scope_end, end_of_day=True)
        except ValueError as exc:
            raise ROIError(str(exc)) from exc

        if start and end and start > end:
            raise ROIError(
                f"the requested period starts ({start.date()}) after it ends ({end.date()})"
            )

        types = ",".join(
            sorted({t.strip() for t in (scope_resource_types or "").split(",") if t.strip()})
        ) or None

        request_id = new_request_id()
        detail_key = self._put_encrypted(
            f"roi/request/{request_id}.json",
            {
                "request_id": request_id,
                "patient_reference": reference,
                "requester_type": requester_type,
                "requester_detail": requester_detail.strip(),
                "authorization_reference": (authorization_reference or "").strip() or None,
                "purpose_of_use": purpose,
                "created_by": created_by,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scope_start": start.isoformat() if start else None,
                "scope_end": end.isoformat() if end else None,
                "scope_resource_types": types,
            },
            "application/json",
        )

        self._execute(
            "INSERT INTO roi_requests (request_id, patient_reference, requester_type, "
            "purpose_of_use, status, created_by, detail_storage_key, "
            "scope_start, scope_end, scope_resource_types) "
            "VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)",
            (request_id, reference, requester_type, purpose, created_by, detail_key,
             start, end, types),
        )

        self._audit.record(
            actor=created_by,
            action="roi.request.created",
            resource_key=f"{request_id} for {reference}",
            purpose_of_use=purpose,
        )

        return self.get(request_id)

    def fulfil(
        self,
        request_id: str,
        fulfilled_by: str,
        organization: Optional[str] = None,
        bates_prefix: str = "PHIAI",
    ) -> ROIRequest:
        """Assemble, store and disclose the record set.

        Produces TWO artifacts, both stored:

          roi/export/<id>.json      FHIR R4 Bundle - machine-readable,
                                    what another system would ingest
          roi/production/<id>.pdf   paginated, Bates-numbered production
                                    for legal review

        Both, not one. The Bundle is the correct interoperable artifact
        and the wrong thing to hand an attorney; the PDF is citable in a
        deposition and useless to a downstream system. They are generated
        from the same filtered set in the same operation, so they cannot
        disagree about what was released.

        Scope filtering reads and decrypts each candidate resource,
        because the clinical date lives in the resource and cannot live in
        the index - see core/fhir/clinical_dates.py. Slower than SQL,
        correct, and bounded by one patient's record count.

        The audit entry is written BEFORE any record is read, so a failure
        mid-way leaves evidence of an attempted disclosure rather than
        none at all.
        """
        request = self.get(request_id)
        if request is None:
            raise ROIError(f"no such request: {request_id}")
        if request.status != "open":
            raise ROIError(
                f"request {request_id} is already {request.status}. A fulfilled request is "
                "not re-fulfilled - open a new one, so each disclosure is accounted for "
                "separately."
            )

        self._audit.record(
            actor=fulfilled_by,
            action="roi.disclosure",
            resource_key=f"{request_id} for {request.patient_reference}",
            purpose_of_use=request.purpose_of_use,
        )

        from core.fhir.legal_export import ExportScope, LegalExportBuilder

        types = (
            frozenset(t.strip() for t in request.scope_resource_types.split(",") if t.strip())
            if request.scope_resource_types
            else None
        )
        scope = ExportScope(
            patient_reference=request.patient_reference,
            scope_start=request.scope_start,
            scope_end=request.scope_end,
            resource_types=types,
        )

        rows = self._reader.resources_for_patient(request.patient_reference)
        candidates = [(row, self._reader.read_resource(row["storage_key"])) for row in rows]

        # Read the detail object back for the requester's identity rather
        # than passing it around: it is only ever needed at production
        # time, and keeping it in one place means one thing to secure.
        detail = self._read_detail(request)

        production = LegalExportBuilder(bates_prefix=bates_prefix).build(
            scope=scope,
            resources=candidates,
            request_id=request_id,
            requester_type=request.requester_type,
            requester_detail=detail.get("requester_detail", "not recorded"),
            purpose_of_use=request.purpose_of_use,
            produced_by=fulfilled_by,
            organization=organization,
            authorization_reference=detail.get("authorization_reference"),
            verify_integrity=getattr(self._reader, "verify_object_integrity", None),
        )

        produced_keys = {e.storage_key for e in production.manifest if e.included}
        entries = [
            {"fullUrl": row["storage_key"], "resource": resource}
            for row, resource in candidates
            if row["storage_key"] in produced_keys
        ]

        bundle = {
            "resourceType": "Bundle",
            "id": request_id,
            "type": "collection",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "meta": {
                "tag": [
                    {"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
                     "code": request.purpose_of_use},
                ]
            },
            "entry": entries,
        }

        export_key = self._put_encrypted(
            f"roi/export/{request_id}.json", bundle, "application/fhir+json"
        )
        production_key = self._put_encrypted_bytes(
            f"roi/production/{request_id}.pdf", production.pdf_bytes, "application/pdf"
        )

        self._execute(
            "UPDATE roi_requests SET status = 'fulfilled', fulfilled_by = %s, "
            "fulfilled_at = now(), export_storage_key = %s, production_storage_key = %s, "
            "record_count = %s, withheld_count = %s WHERE request_id = %s",
            (fulfilled_by, export_key, production_key, production.included_count,
             production.excluded_count, request_id),
        )

        if production.integrity_failures:
            # Loud, because a production containing a record whose digest
            # no longer matches is a document that should not be relied
            # upon - and it has already been handed over by the time
            # anyone reads a log line.
            log.error(
                "ROI %s produced %d record(s) that FAILED integrity verification - the "
                "production document reports them, but investigate before relying on it",
                request_id, len(production.integrity_failures),
            )

        log.info(
            "ROI %s fulfilled by %s: %d produced, %d withheld, %d pages (%s..%s)",
            request_id, fulfilled_by, production.included_count, production.excluded_count,
            production.page_count, production.bates_first, production.bates_last,
        )
        return self.get(request_id)

    def _read_detail(self, request: ROIRequest) -> dict:
        """Decrypt the request's detail object."""
        if not request.detail_storage_key:
            return {}
        try:
            stored = self._storage.get_object(request.detail_storage_key)
            meta = self._storage.get_metadata(request.detail_storage_key)
            plaintext = self._encryptor.decrypt(stored[12:], stored[:12], meta.wrapped_dek_b64)
            return json.loads(plaintext)
        except Exception as exc:
            log.error("could not read ROI detail %s: %s", request.detail_storage_key, exc)
            return {}

    def _put_encrypted_bytes(self, key: str, payload: bytes, content_type: str) -> str:
        from core.fhir.client import _stored_sha256_hex

        envelope = self._encryptor.encrypt(payload)
        self._storage.put_object(
            key=key,
            ciphertext=envelope.nonce + envelope.ciphertext,
            wrapped_dek_b64=envelope.wrapped_dek_b64,
            sha256_hex=_stored_sha256_hex(envelope.nonce, envelope.ciphertext),
            content_type=content_type,
        )
        return key

    def read_production(self, request_id: str) -> Optional[bytes]:
        """Decrypt and return the production PDF for download."""
        request = self.get(request_id)
        if request is None or not request.production_storage_key:
            return None
        stored = self._storage.get_object(request.production_storage_key)
        meta = self._storage.get_metadata(request.production_storage_key)
        return self._encryptor.decrypt(stored[12:], stored[:12], meta.wrapped_dek_b64)

    def deny(self, request_id: str, denied_by: str, reason: str) -> ROIRequest:
        """Refuse a request, with a recorded reason.

        A denial is as much a part of the accounting as a release -
        "we refused this, on this date, because" is exactly what an
        organisation needs when the refusal is later challenged.
        """
        if not (reason or "").strip():
            raise ROIError("a denial reason is required")

        self._execute(
            "UPDATE roi_requests SET status = 'denied', fulfilled_by = %s, "
            "fulfilled_at = now(), denied_reason = %s WHERE request_id = %s AND status = 'open'",
            (denied_by, reason.strip(), request_id),
        )
        self._audit.record(
            actor=denied_by,
            action="roi.request.denied",
            resource_key=request_id,
            purpose_of_use=None,
        )
        return self.get(request_id)

    # -- reads -------------------------------------------------------

    _COLUMNS = (
        "request_id, patient_reference, requester_type, purpose_of_use, status, "
        "created_by, created_at, fulfilled_by, fulfilled_at, denied_reason, "
        "detail_storage_key, export_storage_key, production_storage_key, "
        "record_count, withheld_count, scope_start, scope_end, scope_resource_types"
    )

    def get(self, request_id: str) -> Optional[ROIRequest]:
        rows = self._execute(
            f"SELECT {self._COLUMNS} FROM roi_requests WHERE request_id = %s",
            (request_id,),
            fetch=True,
        )
        return ROIRequest(**rows[0]) if rows else None

    def list_requests(self, status: Optional[str] = None, limit: int = 100) -> list[ROIRequest]:
        if status:
            rows = self._execute(
                f"SELECT {self._COLUMNS} FROM roi_requests WHERE status = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (status, limit),
                fetch=True,
            )
        else:
            rows = self._execute(
                f"SELECT {self._COLUMNS} FROM roi_requests ORDER BY created_at DESC LIMIT %s",
                (limit,),
                fetch=True,
            )
        return [ROIRequest(**r) for r in rows]

    def disclosures_for_patient(self, patient_reference: str) -> list[ROIRequest]:
        """The 45 CFR 164.528 accounting for one individual."""
        rows = self._execute(
            f"SELECT {self._COLUMNS} FROM roi_requests WHERE patient_reference = %s "
            "AND status = 'fulfilled' ORDER BY fulfilled_at DESC",
            (patient_reference,),
            fetch=True,
        )
        return [ROIRequest(**r) for r in rows]
# Made by Ryan Gomez & Co. Inc.
