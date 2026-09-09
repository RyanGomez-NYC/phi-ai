# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Release of information: the request queue, the review, and the production.

MOVED OUT OF core/web/app.py's create_app(), UNCHANGED. create_app was
1,893 lines - one function holding 52 route handlers as closures over
nine shared locals - and the cost of that was not tidiness. It was that no
route in it could be imported: exercising one meant constructing the whole
application with ten dependencies injected, which is why the route modules
were the least-tested code in the project.

These six routes needed exactly six things from that scope - app, page,
require, current_identity, record, reader - which is the signature
core/web/platform_routes.py, components_routes.py and orchestration_routes.py
already register with. So this is the same seam those three use, applied to
a section that predates it, and the handler bodies are byte-for-byte what
they were inside create_app.

WHY ROI WENT FIRST. It was the section with no dependency on any private
helper of create_app's - no _imaging_studies, no _launch_back - so it
proves the extraction without also moving shared machinery. The sections
that need those follow it.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.fhir.roi import REQUESTER_TYPES
from core.web.auth import Identity


def register(app, page, require, current_identity, record, reader) -> None:
    """Attach the release-of-information screens to `app`.

    Same argument order as every other register() in this package, so the
    call site in create_app reads identically to its neighbours.
    """

    def roi_service():
        service = getattr(app.state, "roi", None)
        if service is None:
            raise HTTPException(status_code=503, detail="release of information is not configured")
        return service

    @app.get("/roi", response_class=HTMLResponse)
    def roi_list(
        request: Request,
        status: Optional[str] = None,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")
        requests = roi_service().list_requests(status=status)
        return page(request, "roi.html", identity, requests=requests, status=status,
                    requester_types=REQUESTER_TYPES, error=None)

    @app.post("/roi", response_class=HTMLResponse)
    def roi_create(
        request: Request,
        patient_reference: str = Form(...),
        requester_type: str = Form(...),
        requester_detail: str = Form(...),
        purpose_of_use: str = Form(...),
        authorization_reference: Optional[str] = Form(None),
        scope_start: Optional[str] = Form(None),
        scope_end: Optional[str] = Form(None),
        scope_resource_types: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")

        error = None
        try:
            roi_service().create(
                patient_reference=patient_reference,
                requester_type=requester_type,
                requester_detail=requester_detail,
                purpose_of_use=purpose_of_use,
                authorization_reference=authorization_reference,
                created_by=identity.username,
                scope_start=scope_start,
                scope_end=scope_end,
                scope_resource_types=scope_resource_types,
            )
        except Exception as exc:
            error = str(exc)

        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

    @app.get("/roi/{request_id}", response_class=HTMLResponse)
    def roi_detail(
        request: Request,
        request_id: str,
        identity: Identity = Depends(current_identity),
    ):
        """The full review before anything is released.

        The production preview is assembled from the index - resource
        types and counts inside and outside the request's scope - so the
        person deciding sees exactly what fulfilment will assemble and
        what the scope filter will exclude, before the release exists.
        Rendering the preview reads no clinical content (the index holds
        none, by design), so the review itself is not a disclosure; the
        disclosure event is written by fulfil, before any record is
        read.
        """
        require(identity, "roi:create")
        roi_request = roi_service().get(request_id)
        if roi_request is None:
            raise HTTPException(status_code=404, detail="no such request")

        rows = reader.resources_for_patient(roi_request.patient_reference)
        types = (
            frozenset(t.strip() for t in
                      roi_request.scope_resource_types.split(",") if t.strip())
            if roi_request.scope_resource_types else None
        )
        by_type: dict[str, int] = {}
        excluded_types: dict[str, int] = {}
        for row in rows:
            rt_name = row["resource_type"]
            if types is not None and rt_name not in types:
                excluded_types[rt_name] = excluded_types.get(rt_name, 0) + 1
            else:
                by_type[rt_name] = by_type.get(rt_name, 0) + 1
        return page(request, "roi_detail.html", identity, active="roi",
                    r=roi_request, by_type=sorted(by_type.items()),
                    excluded_types=sorted(excluded_types.items()),
                    candidate_total=sum(by_type.values()),
                    excluded_total=sum(excluded_types.values()))

    @app.post("/roi/{request_id}/fulfil", response_class=HTMLResponse)
    def roi_fulfil(
        request: Request,
        request_id: str,
        identity: Identity = Depends(current_identity),
    ):
        # Fulfilling a request discloses PHI, so it needs the export
        # permission, not merely the ability to open a request. Creating
        # and releasing are deliberately separate grants.
        require(identity, "roi:export")
        error = None
        try:
            roi_service().fulfil(request_id, fulfilled_by=identity.username)
        except Exception as exc:
            error = str(exc)
        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

    @app.get("/roi/{request_id}/production")
    def roi_production(request_id: str, identity: Identity = Depends(current_identity)):
        """Download the paginated production document.

        A separate audited event from fulfilment: producing the record set
        and later handing a copy to someone are different disclosures, and
        an accounting that recorded only the first would understate how
        many times the records left the system.
        """
        require(identity, "roi:export")
        service = roi_service()
        roi_request = service.get(request_id)
        if roi_request is None or not roi_request.production_storage_key:
            raise HTTPException(status_code=404, detail="no production document for that request")

        record(identity, "roi.production.download", request_id, roi_request.purpose_of_use)
        pdf = service.read_production(request_id)
        if pdf is None:
            raise HTTPException(status_code=404, detail="production document is unreadable")

        from fastapi.responses import Response

        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                # attachment, not inline: a PHI document should be saved
                # deliberately rather than rendered in a browser tab that
                # may be shared, cached or screen-shared.
                "Content-Disposition": f'attachment; filename="{request_id}-production.pdf"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/roi/{request_id}/deny", response_class=HTMLResponse)
    def roi_deny(
        request: Request,
        request_id: str,
        reason: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "roi:create")
        error = None
        try:
            roi_service().deny(request_id, denied_by=identity.username, reason=reason)
        except Exception as exc:
            error = str(exc)
        return page(request, "roi.html", identity,
                    requests=roi_service().list_requests(), status=None,
                    requester_types=REQUESTER_TYPES, error=error)

# Made by Ryan Gomez & Co. Inc.
