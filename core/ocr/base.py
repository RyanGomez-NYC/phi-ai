# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
OCR abstraction for document ingestion.

WHAT THIS IS FOR: a clinical record platform that only holds what an
EMR's FHIR API returns is incomplete. Scanned paper records, faxed
referrals, outside-hospital records and signed forms routinely live as
images, and an EMR retirement has to carry them across too. This module
turns those into text that can be stored, searched, and tied to a
patient.

THREE RULES THIS MODULE AND ITS CALLERS ENFORCE. Each exists because the
obvious alternative is actively unsafe in a system holding clinical
records:

1. OCR OUTPUT IS PHI. Extracted text from a clinical document contains
   names, MRNs, dates of birth - by definition. It is encrypted and
   stored exactly like any other stored resource, and it NEVER goes
   into the Postgres index. core/db/index.py's "no clinical content,
   ever" rule is not relaxed for OCR text; the index gets the same
   structural facts it gets for every other resource (type, id, storage
   key, hash, and the opaque patient reference) and nothing more.

2. OCR NEVER DECIDES WHICH PATIENT A DOCUMENT BELONGS TO. It would be
   easy to parse a name or MRN out of the extracted text and file the
   document against the matching patient. That is deliberately not done
   anywhere in this codebase. OCR misreads characters routinely -
   "0"/"O", "1"/"l", "5"/"S" - and a misread digit in an MRN files a
   patient's record under a DIFFERENT patient. That is a clinical safety
   incident and a HIPAA disclosure at once, and it is silent: nothing
   downstream would flag it. The patient linkage is always supplied by
   the caller and validated structurally; see
   core/fhir/documents.py.

3. THE ORIGINAL IS THE RECORD; OCR TEXT IS DERIVED. OCR is lossy and its
   error rate on real scanned clinical documents is not close to zero.
   The extracted text is a convenience for search and review, never a
   replacement for the source. CMS requires hospital records be retained
   "in their original or legally reproduced form" (42 CFR 482.24(b)(1)),
   so callers store the source bytes alongside the text and link them.

UNTRUSTED INPUT. Every byte handed to an OCR engine here came from
outside the system - a scanner, a fax gateway, another organization's
records department - and is fed to a native C/C++ library. That is a
real attack surface, so the limits below are enforced BEFORE any engine
touches the bytes, not left to the engine's own robustness.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

# Ceilings applied to untrusted document input before an engine sees it.
# Deliberately conservative: a legitimate scanned clinical document is
# very rarely near any of these, and something that is says more about
# the input than about anything this platform needs to store.
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_PAGES = 500
DEFAULT_TIMEOUT_SECONDS = 300

# Mean per-word confidence below which extracted text is flagged rather
# than trusted. Tesseract reports 0-100. This is NOT a pass/fail gate -
# low-confidence text is still stored, because a poor scan of a real
# record is still that record - it is a marker so a human reviews it
# instead of a garbled page silently entering the record set as if it
# were clean. See OCRResult.is_low_confidence.
LOW_CONFIDENCE_THRESHOLD = 60.0


class OCRError(RuntimeError):
    """Raised when extraction fails. Never raised for a document that
    simply contains no text - that is an empty result, not an error."""


class OCREngineUnavailable(OCRError):
    """The engine's native binary or Python binding is missing.

    Distinct from OCRError specifically so callers can tell a deployment
    problem ("tesseract is not installed in this image") apart from a
    document problem ("this PDF is corrupt"), and surface the right one.
    """


class DocumentTooLarge(OCRError):
    """Input exceeded MAX_DOCUMENT_BYTES or MAX_PAGES."""


@dataclass(frozen=True)
class OCRPage:
    page_number: int  # 1-based, matching how a human refers to a page
    text: str
    mean_confidence: Optional[float]


@dataclass(frozen=True)
class OCRResult:
    """
    Extracted text plus everything needed to judge how much to trust it.

    The provenance fields are not decoration. Stored OCR text may be
    read years from now, during a records request or a dispute, by
    someone who needs to know whether a given line is a faithful
    transcription or an artifact. Recording which engine and version
    produced it, at what confidence, is what makes that answerable
    later - and lets a stored record set be re-OCR'd selectively if an
    engine version is ever found to have a systematic defect.
    """

    text: str
    pages: tuple[OCRPage, ...]
    mean_confidence: Optional[float]
    engine: str
    engine_version: str
    language: str
    source_sha256_hex: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def is_empty(self) -> bool:
        """True when the engine found no text at all.

        A legitimate outcome, not a failure: a photograph, a blank fax
        cover sheet, or a page of handwriting Tesseract cannot read all
        produce this. Callers should still store the source - an
        unreadable page is still part of the record - but must not treat
        the absence of text as evidence the document was blank.

        Reads the PAGES, not `self.text`. `text` carries "--- page N ---"
        separators for human readability, so it is never blank even when
        every page is - checking it would have reported a blank scan as
        having content, and marked it docStatus=final rather than
        preliminary. Caught by a test against the real engine, not
        theorised.
        """
        return not any(page.text.strip() for page in self.pages)

    @property
    def is_low_confidence(self) -> bool:
        if self.mean_confidence is None:
            return False
        return self.mean_confidence < LOW_CONFIDENCE_THRESHOLD


class OCREngine(abc.ABC):
    """
    Interface a document-text extractor must implement.

    Kept as an abstraction with one implementation for the same reason
    core/storage/base.py and core/fhir/emr_profiles.py are: the engine is
    a swappable detail, and a clinical deployment may be required to use
    a specific validated one. Tesseract is the default because it is
    genuinely open source (Apache 2.0), runs entirely on-premises, and
    sends no document anywhere - which matters more than raw accuracy
    here, since a cloud OCR API would mean transmitting PHI to a third
    party and needing a BAA to cover it.
    """

    @abc.abstractmethod
    def extract(
        self,
        document: bytes,
        content_type: str,
        language: str = "eng",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> OCRResult:
        """Extract text from a document's raw bytes.

        `content_type` is the caller's declared MIME type; implementations
        must not infer it from the file extension alone, and must reject
        types they cannot handle rather than guessing.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def version(self) -> str:
        """Engine version string, recorded into every OCRResult for
        provenance. Raises OCREngineUnavailable if the engine is not
        installed - which makes this usable as a startup health check."""
        raise NotImplementedError


def guard_document_size(document: bytes) -> None:
    """Size ceiling, checked before any native library sees the bytes."""
    if len(document) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge(
            f"document is {len(document)} bytes, over the {MAX_DOCUMENT_BYTES}-byte limit. "
            "Split it or raise MAX_DOCUMENT_BYTES deliberately - this ceiling exists because "
            "document bytes are untrusted input to a native library."
        )
    if not document:
        raise OCRError("document is empty (zero bytes)")
# Made by Ryan Gomez & Co. Inc.
