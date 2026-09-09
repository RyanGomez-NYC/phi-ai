# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Patient search, the record view, and a document's decrypted source

MOVED OUT OF core/web/app.py's create_app(), UNCHANGED. See
core/web/roi_routes.py's header for why that function was broken up
and what the register() seam is. The handler bodies here are
byte-for-byte what they were inside create_app.

_imaging_studies is passed in rather than moved: the SMART screens need
the same helper, and one copy that both call is the point.
"""

from __future__ import annotations

import logging
import re

from typing import Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web.patient_context import clear_patient, set_patient
from core.web.auth import (
    Identity,
    NotAuthorized,
    purpose_allowed,
    validate_purpose,
)

log = logging.getLogger("phi-ai.web.records")

# The ONLY shape a decrypted source document may be fetched by. Moved
# here with the route that enforces it - it had no other user.
_SOURCE_DOCUMENT_KEY = re.compile(
    r"^documents/source/doc-[0-9a-f]{32}\.(?:pdf|png|jpg|jpeg|tif|tiff|bmp|gif|webp)$"
)


def register(app, page, require, current_identity, record, reader, _imaging_studies) -> None:
    """Attach these screens to `app`."""

    @app.get("/patients", response_class=HTMLResponse)
    def patient_search_form(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "patient:search")
        return page(request, "patients.html", identity, results=None, term="")

    @app.post("/patients", response_class=HTMLResponse)
    def patient_search(
        request: Request,
        term: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        # POST, not GET: a search term must not reach a proxy access log
        # or the browser's history.
        require(identity, "patient:search")
        results = reader.search_patients(term)
        record(identity, "record.search", f"patient_reference~{term}", None)
        return page(request, "patients.html", identity, results=results, term=term)

    @app.post("/patients/{patient_id}/open", response_class=HTMLResponse)
    def patient_record(
        request: Request,
        patient_id: str,
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "patient:read")
        try:
            purpose = validate_purpose(purpose_of_use)
            if not purpose_allowed(identity, purpose):
                # The role dictates the purposes it may
                # assert; refusal is audited like any
                # other denial.
                require(identity, f"purpose:{purpose}")
        except NotAuthorized as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        reference = f"Patient/{patient_id}"
        # Audit BEFORE reading - see resource_detail for the reasoning.
        record(identity, "record.read.patient", reference, purpose)
        resources = reader.resources_for_patient(reference)

        # OPENING A CHART IS WHAT PUTS A PATIENT IN CONTEXT, and it is the
        # only thing that does, apart from a SMART launch. Set AFTER the
        # audit entry and the read, so a refused or failed open leaves the
        # previous context alone rather than moving it to a chart this
        # person was not permitted to see.
        set_patient(request.session, reference,
                    label=(resources[0].get("patient_label") if resources else "") or patient_id)
        return page(
            request,
            "patient.html",
            identity,
            patient_reference=reference,
            resources=resources,
            purpose=purpose,
            imaging_studies=_imaging_studies(identity, reference),
        )

    @app.post("/patients/context/clear")
    def clear_patient_context(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """Stop following this patient.

        EXPLICIT, because the alternative is worse in both directions: a
        context that expires on its own leaves somebody working a chart
        they think is open and is not, and one that never clears follows
        them into work that has nothing to do with that patient. Clearing
        reads nothing and discloses nothing, so it writes no audit entry -
        the reads it prevents are the ones that would have.
        """
        clear_patient(request.session)
        return RedirectResponse(request.headers.get("referer") or "/patients",
                                status_code=303)

    @app.post("/resource", response_class=HTMLResponse)
    def resource_detail(
        request: Request,
        storage_key: str = Form(...),
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "patient:read")
        purpose = validate_purpose(purpose_of_use)
        if not purpose_allowed(identity, purpose):
            # The role dictates the purposes it may
            # assert; refusal is audited like any
            # other denial.
            require(identity, f"purpose:{purpose}")

        row = reader.resource_index_row(storage_key)
        if row is None:
            raise HTTPException(status_code=404, detail="not in the index")

        # AUDIT BEFORE DECRYPTING, not after. If the audit write fails,
        # the request must end having never decrypted the content - the
        # same ordering core/fhir/purge.py uses, where the disposal entry
        # is written before the delete. Recording afterwards means a
        # failed audit still leaves PHI decrypted in this process, which
        # is precisely the unlogged access the trail exists to prevent.
        record(identity, "record.read", storage_key, purpose)
        resource = reader.read_resource(storage_key)

        ocr_text, source_key = None, None
        if resource.get("resourceType") == "DocumentReference":
            from core.fhir.documents import decode_ocr_text

            ocr_text = decode_ocr_text(resource)
            for entry in resource.get("content", []):
                url = (entry.get("attachment") or {}).get("url", "")
                if url.startswith("documents/source/"):
                    source_key = url
                    break

        return page(
            request,
            "resource.html",
            identity,
            row=row,
            resource=resource,
            ocr_text=ocr_text,
            source_key=source_key,
            purpose=purpose,
        )

    @app.post("/document/source")
    def document_source(
        request: Request,
        storage_key: str = Form(...),
        purpose_of_use: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        """Serve the original scan behind an OCR'd DocumentReference.

        The scan is the record of truth - the OCR text is derived - so a
        clinician checking anything consequential needs the original, not
        a transcription that misreads characters. Audited before the
        object is decrypted, exactly like every other clinical read.
        """
        require(identity, "document:read")
        try:
            purpose = validate_purpose(purpose_of_use)
            if not purpose_allowed(identity, purpose):
                # The role dictates the purposes it may
                # assert; refusal is audited like any
                # other denial.
                require(identity, f"purpose:{purpose}")
        except NotAuthorized as exc:
            # 400, not 500: a bad purpose is a malformed request, and
            # letting NotAuthorized escape produced an opaque server error.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not _SOURCE_DOCUMENT_KEY.match(storage_key):
            # This route decrypts and returns raw bytes AND echoes the
            # key's final segment into a Content-Disposition filename, so
            # it must not become a general object-fetch endpoint and the
            # key must not carry characters that do not belong in a header.
            # The full shape is validated, not merely the prefix.
            raise HTTPException(status_code=400, detail="not a source document key")

        record(identity, "record.read.document.source", storage_key, purpose)

        try:
            payload = reader.read_object_bytes(storage_key)
        except Exception as exc:
            log.error("could not read source document %s: %s", storage_key, exc)
            raise HTTPException(status_code=404, detail="source document unavailable") from exc

        from fastapi.responses import Response

        suffix = storage_key.rsplit(".", 1)[-1].lower()
        media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "tif": "image/tiff", "tiff": "image/tiff",
                 "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp"}.get(
                     suffix, "application/octet-stream")
        return Response(
            content=payload,
            media_type=media,
            headers={
                # inline, unlike the ROI production: this is meant to be
                # looked at next to the extracted text, not filed away.
                "Content-Disposition": f'inline; filename="{storage_key.rsplit("/", 1)[-1]}"',
                "Cache-Control": "no-store",
            },
        )

# Made by Ryan Gomez & Co. Inc.
