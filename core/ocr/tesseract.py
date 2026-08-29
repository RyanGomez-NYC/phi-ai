# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tesseract OCR engine (https://github.com/tesseract-ocr/tesseract).

Chosen for a PHI platform specifically because it runs ENTIRELY LOCALLY.
Every hosted OCR API - AWS Textract, Google Document AI, Azure Document
Intelligence - would mean transmitting scanned clinical documents to a
third party, which needs a Business Associate Agreement under 45 CFR
164.502(e) and expands the disclosure surface of the deployment for the
sake of accuracy nothing here actually depends on (OCR text is derived
and reviewable, never the record of truth - see core/ocr/base.py).
Tesseract is Apache 2.0, ships in Debian, and no document byte leaves the
container.

TWO NATIVE DEPENDENCIES, neither pip-installable, both in the Dockerfile:
  - `tesseract-ocr` plus a language pack (`tesseract-ocr-eng`)
  - `poppler-utils`, only for PDFs - Tesseract reads images, not PDFs, so
    pages are rasterised first via pdf2image, which shells out to poppler

Both are imported lazily and reported through OCREngineUnavailable rather
than a bare ImportError, so a deployment that has not installed them gets
a message naming the missing package instead of a stack trace - the same
deferred-import discipline core/storage/aws_s3.py uses for boto3.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Optional

from core.ocr.base import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PAGES,
    DocumentTooLarge,
    OCREngine,
    OCREngineUnavailable,
    OCRError,
    OCRPage,
    OCRResult,
    guard_document_size,
)

log = logging.getLogger("phi-ai.ocr.tesseract")

# Raster formats Pillow opens and Tesseract reads directly. PDFs are
# handled separately (rasterised first); anything else is refused rather
# than guessed at.
IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/gif",
    "image/webp",
}
PDF_CONTENT_TYPES = {"application/pdf"}
SUPPORTED_CONTENT_TYPES = IMAGE_CONTENT_TYPES | PDF_CONTENT_TYPES

# Rasterisation density for PDF pages. 300 DPI is the usual floor for
# reliable OCR of body text; below roughly 200 accuracy on small print
# degrades sharply, and above 300 costs memory and time for little gain
# on documents that were themselves scanned at 200-300.
PDF_RENDER_DPI = 300


class TesseractOCR(OCREngine):
    def __init__(self, tesseract_cmd: Optional[str] = None, render_dpi: int = PDF_RENDER_DPI):
        # Lets a deployment point at a specific validated binary rather
        # than whatever is first on PATH - some clinical environments
        # require a pinned, verified build.
        self.tesseract_cmd = tesseract_cmd
        self.render_dpi = render_dpi

    def _pytesseract(self):
        try:
            import pytesseract
        except ImportError as exc:
            raise OCREngineUnavailable(
                "pytesseract is not installed. Add it (see requirements.txt) - note it is "
                "only a wrapper: the tesseract binary itself must also be present."
            ) from exc

        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        return pytesseract

    def version(self) -> str:
        pytesseract = self._pytesseract()
        try:
            return str(pytesseract.get_tesseract_version())
        except Exception as exc:
            raise OCREngineUnavailable(
                "the tesseract binary is not installed or not on PATH. On Debian/Ubuntu: "
                "`apt-get install tesseract-ocr tesseract-ocr-eng`. The Dockerfile in this "
                "repo already does this; a bare virtualenv will not."
            ) from exc

    def _images_from(self, document: bytes, content_type: str) -> list:
        """One Pillow image per page, rasterising PDFs as needed."""
        if content_type in PDF_CONTENT_TYPES:
            try:
                from pdf2image import convert_from_bytes
            except ImportError as exc:
                raise OCREngineUnavailable(
                    "pdf2image is not installed, so PDFs cannot be rasterised for OCR. "
                    "Images still work. Add pdf2image (see requirements.txt) AND the "
                    "poppler-utils system package it shells out to."
                ) from exc

            try:
                # Page cap enforced during conversion rather than after:
                # rasterising a 10,000-page PDF to check its length would
                # already have spent the memory the cap exists to avoid.
                images = convert_from_bytes(
                    document, dpi=self.render_dpi, first_page=1, last_page=MAX_PAGES
                )
            except Exception as exc:
                if "poppler" in str(exc).lower():
                    raise OCREngineUnavailable(
                        "poppler is not installed - pdf2image needs it to rasterise PDFs. "
                        "On Debian/Ubuntu: `apt-get install poppler-utils`."
                    ) from exc
                raise OCRError(f"could not read PDF: {exc}") from exc

            if not images:
                raise OCRError("PDF contained no renderable pages")
            return images

        try:
            from PIL import Image, ImageSequence
        except ImportError as exc:
            raise OCREngineUnavailable(
                "Pillow is not installed; it is required to decode images for OCR."
            ) from exc

        try:
            image = Image.open(io.BytesIO(document))
            # Multi-page TIFF is common for faxed clinical records, so
            # every frame is read, not just the first.
            frames = []
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                if index >= MAX_PAGES:
                    raise DocumentTooLarge(
                        f"image has more than {MAX_PAGES} frames"
                    )
                frames.append(frame.convert("RGB"))
            return frames or [image.convert("RGB")]
        except DocumentTooLarge:
            raise
        except Exception as exc:
            raise OCRError(f"could not decode image: {exc}") from exc

    def extract(
        self,
        document: bytes,
        content_type: str,
        language: str = "eng",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> OCRResult:
        guard_document_size(document)

        content_type = (content_type or "").split(";")[0].strip().lower()
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise OCRError(
                f"unsupported content type {content_type!r}. Supported: "
                f"{', '.join(sorted(SUPPORTED_CONTENT_TYPES))}. The declared type is used "
                "rather than sniffed from the bytes, deliberately - guessing the format of "
                "untrusted input is how a decoder gets handed something it did not expect."
            )

        pytesseract = self._pytesseract()
        engine_version = self.version()
        # Digest of the SOURCE bytes, carried into the result so the
        # stored text can always be traced back to the exact document
        # it came from - including after a re-OCR with a later engine.
        source_sha256_hex = hashlib.sha256(document).hexdigest()

        images = self._images_from(document, content_type)

        pages: list[OCRPage] = []
        warnings: list[str] = []
        confidences: list[float] = []

        for page_number, image in enumerate(images, start=1):
            try:
                text = pytesseract.image_to_string(
                    image, lang=language, timeout=timeout_seconds
                )
            except RuntimeError as exc:
                # pytesseract raises RuntimeError specifically on timeout.
                raise OCRError(
                    f"OCR timed out after {timeout_seconds}s on page {page_number}"
                ) from exc
            except Exception as exc:
                message = str(exc)
                if "Failed loading language" in message or "tessdata" in message.lower():
                    raise OCREngineUnavailable(
                        f"tesseract has no language data for {language!r}. On Debian/Ubuntu "
                        f"install the matching pack, e.g. `tesseract-ocr-{language}`."
                    ) from exc
                raise OCRError(f"OCR failed on page {page_number}: {exc}") from exc

            page_confidence = self._page_confidence(pytesseract, image, language)
            if page_confidence is not None:
                confidences.append(page_confidence)

            if not text.strip():
                warnings.append(f"page {page_number} produced no text")

            pages.append(
                OCRPage(page_number=page_number, text=text, mean_confidence=page_confidence)
            )

        mean_confidence = sum(confidences) / len(confidences) if confidences else None

        # Page separators are explicit rather than a bare newline join:
        # someone reading this text years later during a records request
        # needs to know where one page ended, and OCR output frequently
        # ends mid-sentence at a page break.
        text = "\n\n".join(
            f"--- page {page.page_number} ---\n{page.text.strip()}" for page in pages
        )

        result = OCRResult(
            text=text,
            pages=tuple(pages),
            mean_confidence=mean_confidence,
            engine="tesseract",
            engine_version=engine_version,
            language=language,
            source_sha256_hex=source_sha256_hex,
            warnings=tuple(warnings),
        )

        if result.is_low_confidence:
            # Logged without any extracted text: this logger is not a PHI
            # sink, and the whole point of this project's separation is
            # that clinical content lives in encrypted storage only.
            log.warning(
                "OCR mean confidence %.1f is below the review threshold across %d page(s) "
                "- extracted text should be reviewed before being relied on",
                mean_confidence,
                len(pages),
            )

        return result

    @staticmethod
    def _page_confidence(pytesseract, image, language: str) -> Optional[float]:
        """Mean per-word confidence for one page, or None if unavailable.

        Deliberately non-fatal: confidence is a quality signal, and losing
        it must never cost the extracted text itself. A page whose
        confidence cannot be computed is recorded as None - honestly
        unknown - rather than as a fabricated number or a silent zero,
        which would read as "certainly wrong" instead of "not measured".
        """
        try:
            data = pytesseract.image_to_data(
                image, lang=language, output_type=pytesseract.Output.DICT
            )
        except Exception as exc:
            log.debug("confidence unavailable for a page: %s", exc)
            return None

        # -1 marks entries Tesseract did not score (layout blocks rather
        # than recognised words); averaging them in would drag every page
        # toward a meaningless number.
        scores = [float(c) for c in data.get("conf", []) if float(c) >= 0]
        return sum(scores) / len(scores) if scores else None
# Made by Ryan Gomez & Co. Inc.
