# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Produce a records package suitable for legal review.

A FHIR Bundle is the right machine-readable artifact and the wrong thing
to hand an attorney. This produces the other one: a paginated,
Bates-numbered PDF with a cover sheet, a custodian certification, a
manifest of what was and was not included, the records themselves, and an
integrity appendix.

WHAT THIS DOES AND DOES NOT ASSERT. The certification page states facts
this system can actually stand behind - that each record was retrieved
from the record store at a stated time, that its SHA-256 matches the
digest recorded when it was stored, and who produced the package. It does
NOT declare itself an affidavit, does not assert the records are complete
as a matter of law, and does not claim the underlying clinical content is
accurate. Those are determinations for the human custodian of records,
who signs the block this page leaves for them. Software generating a
document that asserts its own legal sufficiency would be worse than
useless - it would be misleading in a setting where that matters.

BATES NUMBERING. Every page carries a sequential identifier
(PREFIX-000001). It is what makes a produced set citable - an attorney
refers to "PHIAI-000042", and both sides mean the same page. Numbering is
continuous across the whole package including the cover and manifest, so
no page in the production is unnumbered.

THE MANIFEST RECORDS EXCLUSIONS, NOT JUST INCLUSIONS. A date-scoped
release necessarily leaves records out, and a production that silently
omits them invites the question of what else was omitted. Every resource
considered appears, with the reason it was included or excluded and the
date field that decided it.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("phi-ai.fhir.legal_export")

DEFAULT_BATES_PREFIX = "PHIAI"


@dataclass
class ManifestEntry:
    resource_type: str
    resource_id: str
    storage_key: str
    included: bool
    reason: str
    sha256_hex: Optional[str] = None
    integrity_ok: Optional[bool] = None
    first_bates: Optional[str] = None


@dataclass
class ExportScope:
    patient_reference: str
    scope_start: Optional[datetime] = None
    scope_end: Optional[datetime] = None
    resource_types: Optional[frozenset[str]] = None
    encounter_id: Optional[str] = None

    def describe(self) -> str:
        if self.scope_start and self.scope_end:
            period = (
                f"{self.scope_start.date().isoformat()} through "
                f"{self.scope_end.date().isoformat()}"
            )
        elif self.scope_start:
            period = f"on or after {self.scope_start.date().isoformat()}"
        elif self.scope_end:
            period = f"on or before {self.scope_end.date().isoformat()}"
        else:
            period = "the complete stored record (no date limitation)"

        types = (
            ", ".join(sorted(self.resource_types))
            if self.resource_types
            else "all record types"
        )
        scope = f"{period}; {types}"
        if self.encounter_id:
            scope += f"; encounter {self.encounter_id} only"
        return scope


@dataclass
class LegalExport:
    pdf_bytes: bytes
    manifest: list[ManifestEntry] = field(default_factory=list)
    page_count: int = 0
    bates_first: Optional[str] = None
    bates_last: Optional[str] = None

    @property
    def included_count(self) -> int:
        return sum(1 for e in self.manifest if e.included)

    @property
    def excluded_count(self) -> int:
        return sum(1 for e in self.manifest if not e.included)

    @property
    def integrity_failures(self) -> list[ManifestEntry]:
        return [e for e in self.manifest if e.included and e.integrity_ok is False]


def _render_resource_lines(resource: dict) -> list[str]:
    """Flatten a FHIR resource into readable lines.

    Deliberately a readable rendering rather than raw JSON: the audience
    is a paralegal or attorney, not a developer. The full JSON is not
    lost - the machine-readable Bundle export carries it, and the
    integrity appendix ties both to the same digest.
    """
    lines: list[str] = []

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("resourceType", "id"):
                    continue
                walk(value, f"{prefix}{key}." if not prefix else f"{prefix}{key}.")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{prefix}{index}.")
        else:
            label = prefix.rstrip(".")
            if node is not None and str(node).strip():
                lines.append(f"{label}: {node}")

    walk(resource)
    return lines


class LegalExportBuilder:
    """Builds the PDF. Separated from ROIService so it can be tested, and
    reused for productions that did not originate as an ROI request."""

    def __init__(self, bates_prefix: str = DEFAULT_BATES_PREFIX):
        self.bates_prefix = bates_prefix

    def build(
        self,
        scope: ExportScope,
        resources: list[tuple[dict, dict]],
        request_id: str,
        requester_type: str,
        requester_detail: str,
        purpose_of_use: str,
        produced_by: str,
        organization: Optional[str] = None,
        authorization_reference: Optional[str] = None,
        verify_integrity=None,
    ) -> LegalExport:
        """
        `resources` is a list of (index_row, resource) pairs - every
        candidate considered, in scope or not. Scope filtering happens
        here rather than upstream so the manifest can report exclusions.

        `verify_integrity` is an optional callable taking a storage key
        and returning True/False. When supplied - normally the object
        store's verify_integrity - each produced record is checked
        against the digest recorded when it was stored and the appendix
        reports the result. When NOT supplied, the appendix says the
        digest is reported as recorded and was not re-verified, rather
        than claiming a check that did not happen. A production document
        asserting an integrity check it never performed would be worse
        than one making no claim at all.
        """
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas

        from core.fhir.clinical_dates import resource_in_scope

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=LETTER)
        width, height = LETTER
        margin = 0.9 * inch
        produced_at = datetime.now(timezone.utc)

        state = {"page": 0}

        def bates(page_number: int) -> str:
            return f"{self.bates_prefix}-{page_number:06d}"

        def new_page(title: Optional[str] = None) -> float:
            state["page"] += 1
            pdf.setFont("Helvetica", 7.5)
            pdf.setFillGray(0.35)
            pdf.drawRightString(width - margin, 0.55 * inch, bates(state["page"]))
            pdf.drawString(margin, 0.55 * inch,
                           f"{scope.patient_reference} · request {request_id}")
            pdf.setFillGray(0)
            y = height - margin
            if title:
                pdf.setFont("Helvetica-Bold", 12)
                pdf.drawString(margin, y, title)
                y -= 0.32 * inch
            return y

        def field_row(y: float, label: str, value: str) -> float:
            """Two-column label/value.

            Drawn at explicit x positions rather than padded with spaces:
            Helvetica is proportional, so string padding produces a ragged
            column. This is a document that gets read across a table in a
            deposition - alignment is not cosmetic.
            """
            if y < 1.05 * inch:
                pdf.showPage()
                y = new_page()
            pdf.setFont("Helvetica", 10)
            pdf.setFillGray(0.35)
            pdf.drawString(margin, y, label)
            pdf.setFillGray(0)
            pdf.drawString(margin + 1.85 * inch, y, str(value)[:78])
            return y - 13.5

        def line(y: float, text: str, font: str = "Helvetica", size: float = 9.5,
                 indent: float = 0.0) -> float:
            if y < 1.05 * inch:
                pdf.showPage()
                y = new_page()
            pdf.setFont(font, size)
            pdf.drawString(margin + indent, y, text[:118])
            return y - (size + 3.2)

        # ---- cover ------------------------------------------------
        y = new_page()
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(margin, y, "PRODUCTION OF MEDICAL RECORDS")
        y -= 0.42 * inch
        if organization:
            y = line(y, organization, "Helvetica-Bold", 11)
            y -= 0.08 * inch

        for label, value in (
            ("Request identifier", request_id),
            ("Patient reference", scope.patient_reference),
            ("Requester type", requester_type),
            ("Requester", requester_detail),
            ("Authorization reference", authorization_reference or "not recorded"),
            ("Purpose of use", purpose_of_use),
            ("Scope of production", scope.describe()),
            ("Produced by", produced_by),
            ("Produced at", produced_at.isoformat()),
        ):
            y = field_row(y, label, value)

        y -= 0.2 * inch
        y = line(y, "CERTIFICATION", "Helvetica-Bold", 11)
        y -= 0.05 * inch
        for text in (
            "The records reproduced in this package were retrieved from the record store",
            "identified above at the time stated. The SHA-256 digest recorded for each",
            "record when it was stored is reported in the Integrity Appendix, which",
            "also states whether those digests were re-verified during this production.",
            "",
            "This package was generated by an automated records platform. It certifies",
            "the retrieval and integrity facts stated above and nothing further. It is",
            "not an affidavit, makes no representation that the records are complete",
            "as a matter of law, and makes no representation as to the accuracy of the",
            "clinical content the records contain. Those determinations rest with the",
            "custodian of records signing below.",
        ):
            y = line(y, text, "Helvetica", 9.5)

        y -= 0.35 * inch
        y = line(y, "_" * 52, "Helvetica", 10)
        y = line(y, "Custodian of records (signature)", "Helvetica", 8.5)
        y -= 0.16 * inch
        y = line(y, "_" * 52, "Helvetica", 10)
        y = line(y, "Printed name and title", "Helvetica", 8.5)
        y -= 0.16 * inch
        y = line(y, "_" * 52, "Helvetica", 10)
        y = line(y, "Date", "Helvetica", 8.5)
        pdf.showPage()

        # ---- scope filtering + manifest ---------------------------
        manifest: list[ManifestEntry] = []
        included: list[tuple[dict, dict]] = []
        for row, resource in resources:
            keep, reason = resource_in_scope(
                resource, scope.scope_start, scope.scope_end, scope.resource_types
            )
            if keep and scope.encounter_id:
                # Encounter membership lives inside the resource, like the
                # clinical date - see core/fhir/encounter_context.py for
                # why it cannot be an index column.
                from core.fhir.encounter_context import (
                    encounter_reference,
                    resource_in_encounter,
                )

                if resource.get("resourceType") not in ("Patient",):
                    if not resource_in_encounter(resource, scope.encounter_id):
                        keep = False
                        found = encounter_reference(resource)
                        reason = (
                            f"belongs to {found}" if found
                            else "carries no encounter link, and this request is scoped "
                                 "to one encounter"
                        )
            entry = ManifestEntry(
                resource_type=resource.get("resourceType", "?"),
                resource_id=str(resource.get("id", "?")),
                storage_key=row.get("storage_key", ""),
                included=keep,
                reason=reason,
                sha256_hex=row.get("sha256_hex"),
            )
            if keep:
                # Only claim a verification that actually ran. The digest
                # in the index covers the STORED bytes (nonce +
                # ciphertext), not this parsed resource, so it cannot be
                # recomputed from the JSON here - the real check is the
                # object store's verify_integrity, passed in by the
                # caller. Absent that, integrity_ok stays None and the
                # appendix says so.
                if verify_integrity is not None:
                    try:
                        entry.integrity_ok = bool(verify_integrity(entry.storage_key))
                    except Exception as exc:
                        log.error("integrity check failed for %s: %s", entry.storage_key, exc)
                        entry.integrity_ok = False
                included.append((row, resource))
            manifest.append(entry)

        y = new_page("MANIFEST OF RECORDS CONSIDERED")
        y = line(y, f"{len(manifest)} record(s) considered · {len(included)} produced · "
                    f"{len(manifest) - len(included)} withheld as out of scope",
                 "Helvetica-Oblique", 9)
        y -= 0.12 * inch
        y = line(y, "Records withheld are listed with the reason. Nothing considered is",
                 "Helvetica", 8.5)
        y = line(y, "omitted from this manifest.", "Helvetica", 8.5)
        y -= 0.14 * inch

        for entry in manifest:
            marker = "PRODUCED" if entry.included else "WITHHELD"
            y = line(y, f"[{marker}] {entry.resource_type}/{entry.resource_id}",
                     "Helvetica-Bold", 9)
            y = line(y, entry.reason, "Helvetica", 8.5, indent=0.22 * inch)
        pdf.showPage()

        # ---- the records ------------------------------------------
        for row, resource in included:
            rtype = resource.get("resourceType", "?")
            rid = resource.get("id", "?")
            y = new_page(f"{rtype} · {rid}")

            for manifest_entry in manifest:
                if manifest_entry.storage_key == row.get("storage_key"):
                    manifest_entry.first_bates = bates(state["page"])
                    break

            y = line(y, f"Storage key: {row.get('storage_key','')}", "Helvetica-Oblique", 8)
            y -= 0.1 * inch

            if rtype == "DocumentReference":
                from core.fhir.documents import decode_ocr_text

                text = decode_ocr_text(resource)
                if resource.get("docStatus") == "preliminary":
                    y = line(y, "NOTE: text below was extracted by OCR at low confidence or",
                             "Helvetica-Bold", 9)
                    y = line(y, "not at all, and has NOT been verified against the source scan.",
                             "Helvetica-Bold", 9)
                    y -= 0.08 * inch
                if text:
                    for raw_line in text.splitlines():
                        y = line(y, raw_line, "Courier", 8.5)
                    y -= 0.1 * inch

            for rendered in _render_resource_lines(resource):
                y = line(y, rendered, "Helvetica", 8.5)
            pdf.showPage()

        # ---- integrity appendix -----------------------------------
        y = new_page("INTEGRITY APPENDIX")
        y = line(y, "SHA-256 digest recorded when each produced record was stored.",
                 "Helvetica", 9)
        if verify_integrity is not None:
            y = line(y, "Each record below was re-read from storage during this production",
                     "Helvetica", 9)
            y = line(y, "and its digest compared. A mismatch is reported as FAILURE and the",
                     "Helvetica", 9)
            y = line(y, "record must not be relied upon without investigation.",
                     "Helvetica", 9)
        else:
            y = line(y, "These digests are reported AS RECORDED WHEN STORED. They were NOT",
                     "Helvetica-Bold", 9)
            y = line(y, "re-verified during this production. They can be checked",
                     "Helvetica-Bold", 9)
            y = line(y, "independently against the stored objects.", "Helvetica-Bold", 9)
        y -= 0.16 * inch
        for entry in manifest:
            if not entry.included:
                continue
            status = {True: "VERIFIED", False: "FAILURE", None: "NOT RE-VERIFIED"}[entry.integrity_ok]
            y = line(y, f"{entry.resource_type}/{entry.resource_id}  [{status}]  "
                        f"first page {entry.first_bates or '—'}", "Helvetica-Bold", 8.5)
            y = line(y, entry.sha256_hex or "no digest recorded", "Courier", 7.5,
                     indent=0.22 * inch)
        pdf.showPage()

        pdf.save()
        return LegalExport(
            pdf_bytes=buffer.getvalue(),
            manifest=manifest,
            page_count=state["page"],
            bates_first=bates(1),
            bates_last=bates(state["page"]),
        )
# Made by Ryan Gomez & Co. Inc.
