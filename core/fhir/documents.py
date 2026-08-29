# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Document ingestion: scanned documents in, stored FHIR resources out.

THE GAP THIS FILLS. Everything else in this project stores what an
EMR's FHIR API returns. Real clinical records are not all in there:
scanned paper charts, faxed referrals, outside-hospital records, signed
consent forms. An EMR retirement has to carry those across too, and a
scanned page nobody can search is barely usable at all. This module
runs documents through OCR (core/ocr/), stores the result, and ties it
to a patient.

HOW A DOCUMENT IS MODELLED. One ingestion produces TWO stored objects:

  1. The source bytes, encrypted, at `documents/source/<id>.<ext>`.
     This is the record. OCR is lossy; the scan is what a records
     request, an audit, or a dispute is actually entitled to, and CMS
     requires hospital records be kept "in their original or legally
     reproduced form" (42 CFR 482.24(b)(1)).

  2. A FHIR R4 DocumentReference at `fhir/DocumentReference/<id>.json`,
     carrying the extracted text plus a pointer to (1), written through
     the ordinary store_resource() path so it is encrypted, audited,
     indexed and patient-linked exactly like every other resource. No
     special-case handling downstream: restore, reconcile, purge and
     the index all treat it as the DocumentReference it genuinely is.

Two objects rather than one because inlining a 50 MB scan as base64
inside its own JSON resource inflates it by a third and forces the whole
thing into memory to read one line of text.

PATIENT LINKAGE IS SUPPLIED, NEVER INFERRED. `patient_reference` is a
required argument and is validated structurally. Nothing here reads the
OCR text to work out whose document it is, and nothing should be added
that does - see core/ocr/base.py, rule 2, for why: OCR routinely
confuses 0/O and 1/l, and one misread MRN digit files a patient's record
under a different patient, silently. Deciding whose record a scan is
belongs to the person or system that had the document, not to a
character recogniser.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.ocr.base import OCRResult

log = logging.getLogger("phi-ai.fhir.documents")

# FHIR ids are [A-Za-z0-9-.]{1,64}; an EMR-assigned Patient id sits well
# inside that. Anchored, because the point is to reject anything that is
# not exactly a patient reference - including a bare id with no
# "Patient/" prefix, which would otherwise sail through and produce a
# resource linked to nothing.
PATIENT_REFERENCE_PATTERN = re.compile(r"^Patient/[A-Za-z0-9\-.]{1,64}$")

# Canonical namespace, configurable per deployment - see
# core/config/canonical.py for why this must not be a repository URL.
def _extension_base() -> str:
    from core.config.canonical import canonical_base

    return f"{canonical_base()}/StructureDefinition"

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/tiff": "tif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/webp": "webp",
}


class DocumentIngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentIngestionResult:
    document_id: str
    patient_reference: str
    source_storage_key: str
    source_sha256_hex: str
    document_reference_storage_key: str
    page_count: int
    mean_confidence: Optional[float]
    low_confidence: bool
    text_empty: bool
    warnings: tuple[str, ...]


def validate_patient_reference(patient_reference: str) -> str:
    """Structural validation of the caller-supplied patient link.

    Structural is all this can be: whether `Patient/eAB12cd3` is the
    RIGHT patient for this document is not knowable here, and pretending
    otherwise would be worse than not checking. What this does catch is
    the whole class of "linked to nothing" errors - an empty string, a
    bare id, an `Observation/...` reference pasted by mistake - which
    would otherwise produce a document stored against no patient and
    invisible to every restore-by-patient query, discovered only when
    someone requests those records.
    """
    if not patient_reference or not patient_reference.strip():
        raise DocumentIngestionError(
            "patient_reference is required. A document stored without a patient link is "
            "invisible to restore-by-patient and effectively lost - see "
            "runbooks/RUNBOOK_DATA_RESTORE.md."
        )

    reference = patient_reference.strip()
    if not PATIENT_REFERENCE_PATTERN.match(reference):
        raise DocumentIngestionError(
            f"patient_reference {reference!r} is not a FHIR Patient reference. Expected "
            "'Patient/<id>' using the source EMR's own id, e.g. 'Patient/eAB12cd3'. A bare "
            "id or another resource type is refused rather than coerced - guessing here "
            "risks filing a record against the wrong patient."
        )
    return reference


def derive_document_id(patient_reference: str, source_bytes: bytes) -> str:
    """Deterministic id from the patient link plus the document bytes.

    Makes re-ingesting the same scan for the same patient IDEMPOTENT: the
    same id, the same storage keys, and a duplicate index write that
    core/db/index.py already absorbs. Document ingestion is frequently
    re-run - a batch that failed halfway, a directory scanned twice - and
    the alternative (a random id per run) silently accumulates duplicate
    copies of the same page, each looking like a distinct record.

    Includes the patient reference, not just the bytes: the same blank
    consent form scanned for two patients is two genuinely different
    records, and must not collapse into one.
    """
    digest = hashlib.sha256()
    digest.update(patient_reference.encode("utf-8"))
    digest.update(b"\x00")  # separator; keeps the two fields unambiguous
    digest.update(source_bytes)
    # 32 hex chars is 128 bits - far past collision concerns here, and
    # short enough to stay readable in a storage key or a log line.
    return f"doc-{digest.hexdigest()[:32]}"


def build_document_reference(
    document_id: str,
    patient_reference: str,
    ocr: OCRResult,
    source_storage_key: str,
    source_content_type: str,
    source_size_bytes: int,
    title: Optional[str] = None,
    document_type: Optional[dict] = None,
    created_at: Optional[datetime] = None,
) -> dict:
    """Assemble the FHIR R4 DocumentReference carrying the OCR output.

    Spec-shaped on purpose rather than a convenient in-house structure.
    The whole premise of this platform is that it outlives the EMR the
    data came from and, likely, this codebase - so a future FHIR-aware
    tool being able to read these resources without bespoke knowledge is
    worth more than saving a base64 decode. Text goes in
    Attachment.data, which the spec defines as base64Binary, not as raw
    inline text.
    """
    now = created_at or datetime.now(timezone.utc)

    # docStatus is the standard-vocabulary way to say "not verified".
    # Low-confidence OCR entering the record set indistinguishable from a
    # clean transcription is the failure worth preventing here: a reader
    # years later has no other way to know the text was uncertain.
    doc_status = "preliminary" if (ocr.is_low_confidence or ocr.is_empty) else "final"

    text_attachment = {
        "contentType": "text/plain; charset=utf-8",
        "data": base64.b64encode(ocr.text.encode("utf-8")).decode("ascii"),
        "title": "OCR-extracted text (derived, not the source of record)",
        "creation": now.isoformat(),
    }

    source_attachment = {
        "contentType": source_content_type,
        # A storage key, not a URL: this platform is the resolution
        # context, and an http(s) URL here would imply a fetchable
        # endpoint that deliberately does not exist for PHI.
        "url": source_storage_key,
        "size": source_size_bytes,
        # Attachment.hash is base64 of the raw digest per the spec, not
        # the hex form used elsewhere in this codebase.
        "hash": base64.b64encode(bytes.fromhex(ocr.source_sha256_hex)).decode("ascii"),
        "title": title or "Source document (record of truth)",
        "creation": now.isoformat(),
    }

    resource = {
        "resourceType": "DocumentReference",
        "id": document_id,
        "status": "current",
        "docStatus": doc_status,
        "subject": {"reference": patient_reference},
        "date": now.isoformat(),
        "description": title or "Scanned document ingested via OCR",
        "content": [{"attachment": text_attachment}, {"attachment": source_attachment}],
        # Provenance, so the trustworthiness of the text is answerable
        # years later without re-running anything - and so a specific
        # engine version can be found and re-OCR'd if one is ever shown
        # to have a systematic defect.
        "extension": [
            {
                "url": f"{_extension_base()}/ocr-provenance",
                "extension": [
                    {"url": "engine", "valueString": ocr.engine},
                    {"url": "engineVersion", "valueString": ocr.engine_version},
                    {"url": "language", "valueString": ocr.language},
                    {"url": "pageCount", "valueInteger": ocr.page_count},
                    {"url": "sourceSha256", "valueString": ocr.source_sha256_hex},
                ]
                + (
                    [{"url": "meanConfidence", "valueDecimal": round(ocr.mean_confidence, 2)}]
                    if ocr.mean_confidence is not None
                    else []
                ),
            }
        ],
    }

    if document_type:
        resource["type"] = document_type

    return resource


class DocumentIngestor:
    """
    Ingests documents into the platform.

    Composed rather than inherited: it holds a FHIRIngestionClient and an
    OCREngine, and reuses the client's own store_resource() for the
    DocumentReference so encryption, audit, indexing and retention all
    behave identically to every other stored resource. Nothing about a
    document should need a parallel implementation of any of that.
    """

    def __init__(self, client, ocr_engine, actor: str = "phi-ai-document-ingestion"):
        self.client = client
        self.ocr_engine = ocr_engine
        self.actor = actor

    def ingest(
        self,
        source_bytes: bytes,
        content_type: str,
        patient_reference: str,
        title: Optional[str] = None,
        document_type: Optional[dict] = None,
        language: str = "eng",
    ) -> DocumentIngestionResult:
        """
        Store one document: source bytes first, then OCR, then the
        DocumentReference.

        ORDER IS DELIBERATE. The source is stored BEFORE OCR runs, so a
        document whose OCR fails - an unsupported format, a corrupt PDF,
        a missing language pack - is still safely stored rather than
        lost because a derived step failed. A stored source with no
        DocumentReference is recoverable (re-run ingestion, or re-OCR
        later with a better engine); a document dropped on the floor
        because OCR errored is not.
        """
        reference = validate_patient_reference(patient_reference)
        if not source_bytes:
            raise DocumentIngestionError("source document is empty (zero bytes)")

        content_type = (content_type or "").split(";")[0].strip().lower()
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if extension is None:
            raise DocumentIngestionError(
                f"unsupported document content type {content_type!r}. Supported: "
                f"{', '.join(sorted(CONTENT_TYPE_EXTENSIONS))}."
            )

        document_id = derive_document_id(reference, source_bytes)
        source_storage_key = f"documents/source/{document_id}.{extension}"

        source_sha256_hex = self._store_source(
            source_bytes=source_bytes,
            storage_key=source_storage_key,
            content_type=content_type,
        )

        ocr = self.ocr_engine.extract(
            document=source_bytes, content_type=content_type, language=language
        )

        # Cheap, but it is the one check that would catch the source
        # bytes and the OCR input having diverged - a bug that would
        # attach one document's text to another document's scan.
        if ocr.source_sha256_hex != source_sha256_hex:
            raise DocumentIngestionError(
                "OCR result digest does not match the stored source document - refusing "
                "to link text to a document it may not have come from."
            )

        resource = build_document_reference(
            document_id=document_id,
            patient_reference=reference,
            ocr=ocr,
            source_storage_key=source_storage_key,
            source_content_type=content_type,
            source_size_bytes=len(source_bytes),
            title=title,
            document_type=document_type,
        )

        store_result = self.client.store_resource(resource)

        if ocr.is_empty:
            log.warning(
                "%s produced no extractable text; source is stored and the "
                "DocumentReference is marked preliminary",
                source_storage_key,
            )
        if ocr.is_low_confidence:
            log.warning(
                "%s OCR mean confidence %.1f is below the review threshold; marked "
                "preliminary pending human review",
                source_storage_key,
                ocr.mean_confidence,
            )

        return DocumentIngestionResult(
            document_id=document_id,
            patient_reference=reference,
            source_storage_key=source_storage_key,
            source_sha256_hex=source_sha256_hex,
            document_reference_storage_key=store_result.storage_key,
            page_count=ocr.page_count,
            mean_confidence=ocr.mean_confidence,
            low_confidence=ocr.is_low_confidence,
            text_empty=ocr.is_empty,
            warnings=ocr.warnings,
        )

    def _store_source(self, source_bytes: bytes, storage_key: str, content_type: str) -> str:
        """Encrypt and store the source document.

        Goes through the same envelope encryptor and storage backend the
        client uses for FHIR resources, including the nonce-prefixing
        convention - so the object store's verify_integrity() and
        core/audit/verify.py work on these objects unchanged, and a
        restore decrypts them with the same code path.
        """
        from core.fhir.client import _stored_sha256_hex, _retention_until

        payload = self.client.encryptor.encrypt(source_bytes)
        storage_bytes = payload.nonce + payload.ciphertext
        stored_digest = _stored_sha256_hex(payload.nonce, payload.ciphertext)

        retention_years = self.client._retention_years_for("DocumentReference")
        retention_until = _retention_until(datetime.now(timezone.utc), retention_years)

        self.client.storage.put_object(
            key=storage_key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=payload.wrapped_dek_b64,
            sha256_hex=stored_digest,
            retention_until=retention_until,
            content_type=content_type,
        )

        self.client.audit.record(
            actor=self.actor,
            action="record.document.source",
            resource_key=storage_key,
            purpose_of_use="document_ingestion",
        )

        return hashlib.sha256(source_bytes).hexdigest()


def decode_ocr_text(document_reference: dict) -> Optional[str]:
    """Pull the OCR text back out of a stored DocumentReference.

    The counterpart to build_document_reference(), for restore tooling
    and for anyone reading a stored resource years from now. Returns
    None rather than raising when there is no text attachment, since a
    DocumentReference from another source may legitimately have none.
    """
    for entry in document_reference.get("content", []):
        attachment = entry.get("attachment", {})
        if not attachment.get("contentType", "").startswith("text/plain"):
            continue
        data = attachment.get("data")
        if not data:
            continue
        try:
            return base64.b64decode(data).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            log.error("DocumentReference carries undecodable text: %s", exc)
            return None
    return None


# ---------------------------------------------------------------------------
# CLI: python -m core.fhir.documents --file <path> --patient Patient/<id>
#
# Same shape as core/fhir/purge.py's CLI - a dry run by default, real work
# only on an explicit flag. Ingestion is not destructive, but it does write
# PHI into the platform under a patient link the operator asserted, and an
# accidental bulk run against the wrong --patient produces records
# misfiled under one patient that then have to be disposed of individually.
# ---------------------------------------------------------------------------

CLI_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _build_ingestor():
    """Wire an ingestor from environment configuration.

    Mirrors how the schedulers build their own clients - through
    core/storage/factory.py and Settings.from_env() - so a document
    ingested from the CLI lands in the same buckets, under the same
    encryption and audit trail, as one stored by the scheduler.
    """
    from core.audit.log import AuditLog
    from core.config.settings import Settings
    from core.crypto.envelope import EnvelopeEncryptor
    from core.fhir.client import FHIRIngestionClient
    from core.config.scale_profile import profile_from_env
    from core.fhir.emr_profiles import EPIC
    from core.ocr.tesseract import TesseractOCR
    from core.storage.factory import build_audit_sink, build_kms, build_storage

    settings = Settings.from_env()
    storage = build_storage(settings)
    encryptor = EnvelopeEncryptor(kms=build_kms(settings))
    audit_sink = build_audit_sink(settings)
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())

    client = FHIRIngestionClient(
        base_url=settings.fhir_base_url,
        profile=EPIC,
        storage=storage,
        encryptor=encryptor,
        audit=audit,
        retention_years=settings.retention_years,
        retention_years_overrides=settings.retention_years_overrides,
        profile_config=profile_from_env(),
    )
    return DocumentIngestor(client=client, ocr_engine=TesseractOCR())


def main(argv: Optional[list] = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="python -m core.fhir.documents",
        description="OCR a scanned clinical document and store it against a patient.",
    )
    parser.add_argument("--file", required=True, help="path to the document (PDF or image)")
    parser.add_argument(
        "--patient",
        required=True,
        help="FHIR patient reference, e.g. Patient/eAB12cd3. Never inferred from the "
        "document's own contents - see this module's docstring.",
    )
    parser.add_argument("--title", default=None, help="human-readable document title")
    parser.add_argument("--language", default="eng", help="tesseract language code (default: eng)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually store the document. Without this, the document is OCR'd and the "
        "result summarised, but nothing is written.",
    )
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    content_type = CLI_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        print(
            f"Unsupported file extension {path.suffix!r}. Supported: "
            f"{', '.join(sorted(CLI_CONTENT_TYPES))}",
            file=sys.stderr,
        )
        return 2

    try:
        reference = validate_patient_reference(args.patient)
    except DocumentIngestionError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    source_bytes = path.read_bytes()

    if not args.confirm:
        # Dry run still performs real OCR - the point is to show the
        # operator what would be stored and at what quality, which a
        # simulated result could not.
        from core.ocr.tesseract import TesseractOCR

        ocr = TesseractOCR().extract(
            document=source_bytes, content_type=content_type, language=args.language
        )
        document_id = derive_document_id(reference, source_bytes)
        print(f"DRY RUN - nothing stored. Re-run with --confirm.\n")
        print(f"  file            {path}")
        print(f"  content type    {content_type}")
        print(f"  patient         {reference}")
        print(f"  document id     {document_id}")
        print(f"  pages           {ocr.page_count}")
        print(
            "  confidence      "
            + (f"{ocr.mean_confidence:.1f}" if ocr.mean_confidence is not None else "unknown")
            + (" (LOW - would be marked preliminary)" if ocr.is_low_confidence else "")
        )
        print(f"  extracted chars {len(ocr.text)}")
        if ocr.is_empty:
            print("  WARNING: no text extracted; source would still be stored")
        for warning in ocr.warnings:
            print(f"  WARNING: {warning}")
        # Deliberately does not print the extracted text: it is PHI, and
        # stdout is frequently redirected to a file or a CI log.
        print("\n  (extracted text not shown - it is PHI)")
        return 0

    result = _build_ingestor().ingest(
        source_bytes=source_bytes,
        content_type=content_type,
        patient_reference=reference,
        title=args.title,
        language=args.language,
    )

    print(f"Stored {path}")
    print(f"  document id     {result.document_id}")
    print(f"  patient         {result.patient_reference}")
    print(f"  source          {result.source_storage_key}")
    print(f"  DocumentReference {result.document_reference_storage_key}")
    print(f"  pages           {result.page_count}")
    if result.low_confidence:
        print("  REVIEW NEEDED: low OCR confidence; marked docStatus=preliminary")
    if result.text_empty:
        print("  REVIEW NEEDED: no text extracted; marked docStatus=preliminary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
