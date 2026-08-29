# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/ocr/ and core/fhir/documents.py.

Two layers, deliberately separated:

  - The ingestion logic (patient linkage, id derivation, FHIR shape,
    storage ordering) is tested against a FAKE OCR engine, so it is
    deterministic and runs anywhere. These are the tests that guard the
    safety properties.

  - The Tesseract wrapper itself is tested against the REAL binary, and
    skipped when it isn't installed. A fake proves nothing about whether
    the wrapper drives the real engine correctly.

Reuses the fake storage/KMS/audit from test_client_store.py rather than
reimplementing them, so document ingestion is exercised through the same
real AES-GCM encryption and the same integrity accounting as every other
stored resource.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.index import extract_patient_reference  # noqa: E402
from core.fhir.documents import (  # noqa: E402
    DocumentIngestionError,
    DocumentIngestor,
    build_document_reference,
    decode_ocr_text,
    derive_document_id,
    validate_patient_reference,
)
from core.ocr.base import OCRError, OCRPage, OCRResult  # noqa: E402
from test_client_store import _make_client  # noqa: E402


def _ocr_result(text="extracted text", confidence=90.0, source_bytes=b"scan-bytes", pages=1):
    import hashlib

    return OCRResult(
        text=text,
        pages=tuple(
            OCRPage(page_number=n, text=text, mean_confidence=confidence)
            for n in range(1, pages + 1)
        ),
        mean_confidence=confidence,
        engine="fake",
        engine_version="0.0",
        language="eng",
        source_sha256_hex=hashlib.sha256(source_bytes).hexdigest(),
    )


class _FakeOCR:
    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def extract(self, document, content_type, language="eng", timeout_seconds=300):
        self.calls.append((document, content_type, language))
        if self._raises:
            raise self._raises
        return self._result or _ocr_result(source_bytes=document)


# ---------------------------------------------------------------------------
# Patient linkage - the safety-critical part
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    ["", "   ", "eAB12cd3", "Observation/eAB12cd3", "Patient/", "patient/eAB12cd3",
     "Patient/eAB12cd3/extra", "Patient/has spaces"],
)
def test_invalid_patient_references_are_refused(bad):
    """Anything that isn't exactly Patient/<id> is refused rather than
    coerced. A document stored against a malformed or absent reference
    is invisible to restore-by-patient, and that is discovered when
    someone requests those records - the worst possible time."""
    with pytest.raises(DocumentIngestionError):
        validate_patient_reference(bad)


def test_valid_patient_reference_is_accepted_and_stripped():
    assert validate_patient_reference("  Patient/eAB12cd3  ") == "Patient/eAB12cd3"


def test_ingest_refuses_a_document_with_no_patient():
    client, _ = _make_client()
    ingestor = DocumentIngestor(client=client, ocr_engine=_FakeOCR())
    with pytest.raises(DocumentIngestionError):
        ingestor.ingest(b"scan", "image/png", patient_reference="")


# ---------------------------------------------------------------------------
# Deterministic ids - idempotent re-ingestion
# ---------------------------------------------------------------------------

def test_same_document_and_patient_yields_the_same_id():
    """Re-running a batch that failed halfway must not create a second
    copy of every document it already stored."""
    a = derive_document_id("Patient/eAB12cd3", b"scan-bytes")
    b = derive_document_id("Patient/eAB12cd3", b"scan-bytes")
    assert a == b


def test_same_document_for_different_patients_yields_different_ids():
    """The same blank consent form scanned for two patients is two
    genuinely different records and must not collapse into one."""
    a = derive_document_id("Patient/eAB12cd3", b"scan-bytes")
    b = derive_document_id("Patient/eXYz9999", b"scan-bytes")
    assert a != b


def test_different_documents_for_one_patient_yield_different_ids():
    a = derive_document_id("Patient/eAB12cd3", b"scan-one")
    b = derive_document_id("Patient/eAB12cd3", b"scan-two")
    assert a != b


# ---------------------------------------------------------------------------
# FHIR shape, and the index integration that depends on it
# ---------------------------------------------------------------------------

def _built(**kwargs):
    defaults = dict(
        document_id="doc-abc",
        patient_reference="Patient/eAB12cd3",
        ocr=_ocr_result(),
        source_storage_key="documents/source/doc-abc.png",
        source_content_type="image/png",
        source_size_bytes=1234,
    )
    defaults.update(kwargs)
    return build_document_reference(**defaults)


def test_document_reference_is_linked_to_the_patient_by_the_index():
    """THE integration that makes this feature work at all.

    core/db/index.py links stored objects to patients solely through
    extract_patient_reference(), which reads `subject` and `patient`. If
    the built resource carried its link anywhere else, every OCR'd
    document would index with a NULL patient reference and silently drop
    out of restore-by-patient."""
    assert extract_patient_reference(_built()) == "Patient/eAB12cd3"


def test_ocr_text_round_trips_through_the_attachment():
    resource = _built(ocr=_ocr_result(text="Chief complaint: routine follow up"))
    assert decode_ocr_text(resource) == "Chief complaint: routine follow up"


def test_source_document_is_referenced_with_its_digest():
    """The text must stay traceable to the exact scan it came from."""
    ocr = _ocr_result(source_bytes=b"scan-bytes")
    resource = _built(ocr=ocr)
    source = [
        c["attachment"] for c in resource["content"]
        if not c["attachment"]["contentType"].startswith("text/plain")
    ][0]
    assert source["url"] == "documents/source/doc-abc.png"
    assert base64.b64decode(source["hash"]).hex() == ocr.source_sha256_hex


def test_low_confidence_is_marked_preliminary_not_final():
    """A garbled transcription entering the record set indistinguishable
    from a clean one is the failure this prevents - a reader years later
    has no other signal that the text was uncertain."""
    assert _built(ocr=_ocr_result(confidence=30.0))["docStatus"] == "preliminary"
    assert _built(ocr=_ocr_result(confidence=95.0))["docStatus"] == "final"


def test_empty_extraction_is_marked_preliminary():
    assert _built(ocr=_ocr_result(text="   "))["docStatus"] == "preliminary"


def test_ocr_provenance_is_recorded():
    """Recorded so the trustworthiness of stored text is answerable
    years later, and so a defective engine version can be found and
    re-OCR'd selectively."""
    resource = _built(ocr=_ocr_result(confidence=88.0, pages=3))
    fields = {
        e["url"]: e
        for e in resource["extension"][0]["extension"]
    }
    assert fields["engine"]["valueString"] == "fake"
    assert fields["engineVersion"]["valueString"] == "0.0"
    assert fields["pageCount"]["valueInteger"] == 3
    assert fields["meanConfidence"]["valueDecimal"] == 88.0


# ---------------------------------------------------------------------------
# Ingestion behaviour
# ---------------------------------------------------------------------------

def test_ingest_stores_both_the_source_and_the_document_reference():
    client, storage = _make_client()
    result = DocumentIngestor(client=client, ocr_engine=_FakeOCR()).ingest(
        b"scan-bytes", "image/png", "Patient/eAB12cd3"
    )
    assert result.source_storage_key in storage.list_keys("documents/source/")
    assert result.document_reference_storage_key in storage.list_keys("fhir/DocumentReference/")


def test_source_is_stored_even_when_ocr_fails():
    """Order matters: a document whose OCR fails must already be safely
    stored. A stored source with no text is recoverable by re-running;
    a document dropped because a derived step failed is not."""
    client, storage = _make_client()
    ingestor = DocumentIngestor(
        client=client, ocr_engine=_FakeOCR(raises=OCRError("corrupt PDF"))
    )
    with pytest.raises(OCRError):
        ingestor.ingest(b"scan-bytes", "image/png", "Patient/eAB12cd3")

    assert storage.list_keys("documents/source/"), "source was lost when OCR failed"
    assert not storage.list_keys("fhir/DocumentReference/")


def test_digest_mismatch_between_source_and_ocr_is_refused():
    """Guards against text from one document being attached to another's
    scan - the one bug that would silently misattribute clinical text."""
    client, _ = _make_client()
    wrong = _ocr_result(source_bytes=b"a-completely-different-document")
    ingestor = DocumentIngestor(client=client, ocr_engine=_FakeOCR(result=wrong))
    with pytest.raises(DocumentIngestionError, match="does not match"):
        ingestor.ingest(b"scan-bytes", "image/png", "Patient/eAB12cd3")


def test_unsupported_content_type_is_refused_before_anything_is_stored():
    client, storage = _make_client()
    ingestor = DocumentIngestor(client=client, ocr_engine=_FakeOCR())
    with pytest.raises(DocumentIngestionError):
        ingestor.ingest(b"data", "application/msword", "Patient/eAB12cd3")
    assert not storage.list_keys("")


def test_stored_source_decrypts_back_to_the_original_bytes():
    """The source is the record of truth, so it has to come back
    byte-identical - through the same envelope encryption every other
    stored object uses."""
    client, storage = _make_client()
    original = b"\x89PNG\r\n\x1a\n" + b"synthetic-scan-payload" * 10
    result = DocumentIngestor(client=client, ocr_engine=_FakeOCR()).ingest(
        original, "image/png", "Patient/eAB12cd3"
    )
    stored = storage.get_object(result.source_storage_key)
    nonce, ciphertext = stored[:12], stored[12:]
    meta = storage.get_metadata(result.source_storage_key)
    assert client.encryptor.decrypt(ciphertext, nonce, meta.wrapped_dek_b64) == original


def test_source_write_is_audited():
    client, _ = _make_client()
    DocumentIngestor(client=client, ocr_engine=_FakeOCR()).ingest(
        b"scan-bytes", "image/png", "Patient/eAB12cd3"
    )
    actions = [action for _, action, _, _ in client.audit.records]
    assert "record.document.source" in actions


# ---------------------------------------------------------------------------
# The real Tesseract engine
# ---------------------------------------------------------------------------

def _tesseract_available() -> bool:
    try:
        from core.ocr.tesseract import TesseractOCR

        TesseractOCR().version()
        return True
    except Exception:
        return False


requires_tesseract = pytest.mark.skipif(
    not _tesseract_available(),
    reason="tesseract binary not installed (see Dockerfile); wrapper tests need the real engine",
)


def _png(lines):
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 80 + 60 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 40 + 60 * index), line, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@requires_tesseract
def test_real_tesseract_extracts_text_from_an_image():
    from core.ocr.tesseract import TesseractOCR

    data = _png(["SYNTHETIC TEST DOCUMENT", "Chief Complaint: cough"])
    result = TesseractOCR().extract(document=data, content_type="image/png")

    assert "SYNTHETIC" in result.text.upper()
    assert result.page_count == 1
    assert result.engine == "tesseract"
    assert result.mean_confidence is not None
    assert not result.is_empty


@requires_tesseract
def test_real_tesseract_records_the_source_digest():
    import hashlib

    from core.ocr.tesseract import TesseractOCR

    data = _png(["SYNTHETIC TEST DOCUMENT"])
    result = TesseractOCR().extract(document=data, content_type="image/png")
    assert result.source_sha256_hex == hashlib.sha256(data).hexdigest()


@requires_tesseract
def test_real_tesseract_reports_a_blank_page_as_empty_not_an_error():
    """A blank fax cover sheet is a legitimate document, not a failure -
    and its source still has to be storable."""
    import io

    from PIL import Image

    from core.ocr.tesseract import TesseractOCR

    blank = Image.new("RGB", (600, 400), "white")
    buffer = io.BytesIO()
    blank.save(buffer, format="PNG")

    result = TesseractOCR().extract(document=buffer.getvalue(), content_type="image/png")
    assert result.is_empty


def test_unsupported_content_type_is_refused_by_the_engine():
    from core.ocr.tesseract import TesseractOCR

    with pytest.raises(OCRError, match="unsupported content type"):
        TesseractOCR().extract(document=b"whatever", content_type="application/msword")


def test_oversized_document_is_refused_before_the_engine_sees_it():
    from core.ocr.base import MAX_DOCUMENT_BYTES, DocumentTooLarge, guard_document_size

    with pytest.raises(DocumentTooLarge):
        guard_document_size(b"\x00" * (MAX_DOCUMENT_BYTES + 1))


def test_empty_document_is_refused():
    from core.ocr.base import guard_document_size

    with pytest.raises(OCRError):
        guard_document_size(b"")
# Made by Ryan Gomez & Co. Inc.
