# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The DICOMweb API the embedded viewer talks to, with this project's rules
attached.

WHY THIS IS A SEPARATE MODULE FROM core/dicom/dicomweb.py. That one turns
index rows and bytes into DICOM JSON and knows nothing about who is
asking. This one is where the three rules core/web/app.py's header states
are enforced for imaging: every read of clinical content is audited with
a stated purpose of use, authorisation is checked per route, and no PHI
goes in a URL that was not already an opaque identifier.

PURPOSE OF USE COMES FROM THE SESSION, NOT THE REQUEST. A DICOMweb client
has nowhere to put one - PS3.18 defines no such parameter, and OHIF would
not send it if it did. So this platform's own interface establishes it: a
user opens a study from the record page, states a purpose exactly as they
do to open any other clinical content, and that choice is stored in the
signed session before the viewer is launched. Every DICOMweb request then
inherits it. A request arriving with no purpose in the session is refused
rather than defaulted - a default here would be a disclosure recorded
under a reason nobody chose.

AUDITING IS PER STUDY, NOT PER REQUEST, and for imaging that is the only
granularity that produces a usable trail. Opening one CT in a viewer is
thousands of HTTP requests - metadata, then a frame per slice, then more
as the user scrolls. core/audit/sink.py writes one object per event, so
auditing each would turn a single study view into thousands of audit
objects, make chain verification materially slower, and bury the entries
that matter. What is recorded is what a human did: this user opened this
study, at this time, for this stated purpose. The individual frame
fetches that follow are the mechanics of that one disclosure, and the
study entry accounts for it under 45 CFR 164.528.

THE VIEWER RUNS ON A DIFFERENT ORIGIN, deliberately - see
runbooks/RUNBOOK_DICOM_IMAGING.md. That keeps `script-src 'none'` intact
on every page of this application that displays PHI, and confines the
scripting a medical image viewer genuinely requires to an origin that
holds no session of its own. The cost is CORS, handled below with an
exact-origin allowlist: `*` is not merely discouraged here, it is
forbidden by the browser alongside credentialed requests, which these
are.

BOTH VARIABLES BELOW ARE READ THROUGH env_var(), never os.environ.get().
viewer_origin() in particular fails INVISIBLY when it reads the wrong
name: it returns None, core/web/app.py's CORS middleware then treats
every viewer request as disallowed, and the browser discards responses
the server logged as ordinary 200s. The operator sees a blank viewer and
nothing anywhere says why.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from core.config.settings import env_var
from core.dicom import dicomweb, index as imaging_index
from core.web.auth import Identity, NotAuthorized, validate_purpose

log = logging.getLogger("phi-ai.web.dicomweb")

# Session keys set by this platform's own "open in viewer" action.
PURPOSE_KEY = "imaging_purpose"

# DICOM UIDs are dotted digits (PS3.5 §9.1) and nothing else. Validated
# on the way in so a UID can never carry a path traversal, a SQL
# metacharacter or a header injection into anything downstream - the
# queries are parameterised regardless, but a value that cannot be
# malformed is better than one that is only handled carefully.
_UID = re.compile(r"^[0-9]+(\.[0-9]+)*$")

# PS3.18 requires DICOM JSON responses carry this type.
_DICOM_JSON = "application/dicom+json"


def _valid_uid(value: str, label: str) -> str:
    if not value or len(value) > 64 or not _UID.match(value):
        raise HTTPException(status_code=400, detail=f"malformed {label}")
    return value


def viewer_origin() -> Optional[str]:
    """The exact origin the viewer is served from, or None if unset."""
    return (env_var("IMAGING_VIEWER_ORIGIN") or "").strip() or None


def build_router(reader, connection_factory, audit, require, current_identity) -> APIRouter:
    """Mount the DICOMweb API.

    `require` and `current_identity` are passed in from create_app rather
    than re-implemented, so imaging authorisation cannot drift from the
    rest of the interface - it is the same function that guards the
    patient record page.
    """
    router = APIRouter(prefix="/dicomweb", tags=["dicomweb"])

    # Ceiling on how many instances a single study-level metadata request
    # will decrypt. A viewer that asks for a whole 4,000-slice study's
    # metadata would otherwise hold a worker for minutes; past this it is
    # told to fetch series by series, which is the cheaper path anyway.
    max_study_metadata = int(
        env_var("IMAGING_MAX_STUDY_METADATA", "500") or "500"
    )

    def _purpose(request: Request) -> str:
        purpose = (request.session or {}).get(PURPOSE_KEY) if "session" in request.scope else None
        try:
            return validate_purpose(purpose)
        except NotAuthorized as exc:
            raise HTTPException(
                status_code=403,
                detail="No purpose of use is recorded for this imaging session. Open the "
                "study from this platform's patient record page, which asks for one, "
                "rather than navigating to the viewer directly.",
            ) from exc

    def _conn():
        if connection_factory is None:
            raise HTTPException(
                status_code=503,
                detail="The imaging index is not configured "
                "(PHI_AI_IMAGING_DB_USERNAME). See "
                "runbooks/RUNBOOK_DICOM_IMAGING.md.",
            )
        return connection_factory()

    def _audit_study(identity: Identity, study_uid: str, purpose: str, action: str) -> None:
        """Record the disclosure. Failure to audit FAILS the request.

        Identical posture to core/web/app.py's record(): serving clinical
        content without an audit entry produces exactly the undetectable
        access the trail exists to prevent, and imaging is the largest
        single disclosure this platform can make.
        """
        if audit is None:
            raise HTTPException(
                status_code=503,
                detail="Audit logging is not configured. Imaging is not served without "
                "an audit trail.",
            )
        audit.record(
            actor=identity.username,
            action=action,
            resource_key=f"dicom/{study_uid}/",
            purpose_of_use=purpose,
        )

    def _json(payload) -> JSONResponse:
        return JSONResponse(content=payload, media_type=_DICOM_JSON)

    # ---- QIDO-RS ---------------------------------------------------

    @router.get("/studies")
    def search_studies(
        request: Request,
        limit: int = imaging_index.DEFAULT_LIMIT,
        offset: int = 0,
        identity: Identity = Depends(current_identity),
    ):
        """Study-level search - the viewer's worklist.

        Filters arrive as query parameters named by DICOM keyword, which
        is what PS3.18 specifies. They are identifying (PatientName,
        AccessionNumber), which is why this route needs `patient:search`
        and is audited as a search exactly as this platform's own patient
        search is.
        """
        require(identity, "imaging:read")
        require(identity, "patient:search")
        purpose = _purpose(request)

        filters = {
            key: value
            for key, value in request.query_params.items()
            if key not in ("limit", "offset", "includefield", "fuzzymatching")
        }

        conn = _conn()
        try:
            rows = imaging_index.search_studies(conn, filters, limit=limit, offset=offset)
        finally:
            conn.close()

        _audit_study(identity, "search", purpose, "record.search.imaging")
        return _json([dicomweb.study_json(row) for row in rows])

    @router.get("/studies/{study_uid}/series")
    def search_series(
        request: Request, study_uid: str, identity: Identity = Depends(current_identity)
    ):
        require(identity, "imaging:read")
        _purpose(request)
        study_uid = _valid_uid(study_uid, "StudyInstanceUID")
        conn = _conn()
        try:
            rows = imaging_index.series_for_study(conn, study_uid)
        finally:
            conn.close()
        return _json([dicomweb.series_json(row) for row in rows])

    @router.get("/studies/{study_uid}/series/{series_uid}/instances")
    def search_instances(
        request: Request,
        study_uid: str,
        series_uid: str,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "imaging:read")
        _purpose(request)
        study_uid = _valid_uid(study_uid, "StudyInstanceUID")
        series_uid = _valid_uid(series_uid, "SeriesInstanceUID")
        conn = _conn()
        try:
            rows = imaging_index.instances_for_series(conn, study_uid, series_uid)
        finally:
            conn.close()
        return _json([dicomweb.instance_json(row) for row in rows])

    # ---- WADO-RS: metadata -----------------------------------------

    def _metadata_for(rows) -> list[dict]:
        out = []
        for row in rows:
            try:
                raw = reader.read_object_bytes(row["storage_key"])
            except Exception as exc:
                # One unreadable instance must not fail a whole study.
                # Logged, skipped, and visible to the viewer as a missing
                # image rather than as a blank screen.
                log.error("could not read %s: %s", row["storage_key"], exc)
                continue
            out.append(dicomweb.instance_metadata(raw))
        return out

    @router.get("/studies/{study_uid}/metadata")
    def study_metadata(
        request: Request, study_uid: str, identity: Identity = Depends(current_identity)
    ):
        require(identity, "imaging:read")
        purpose = _purpose(request)
        study_uid = _valid_uid(study_uid, "StudyInstanceUID")

        conn = _conn()
        try:
            rows = imaging_index.instances_for_study(conn, study_uid)
        finally:
            conn.close()

        if not rows:
            raise HTTPException(status_code=404, detail="no such study")
        if len(rows) > max_study_metadata:
            raise HTTPException(
                status_code=413,
                detail=f"This study has {len(rows)} instances, above the "
                f"{max_study_metadata} this deployment will decrypt for one "
                "study-level metadata request. Fetch series metadata instead, or raise "
                "PHI_AI_IMAGING_MAX_STUDY_METADATA.",
            )

        # AUDIT BEFORE DECRYPTING, the same ordering core/web/app.py uses
        # before serving a resource: a failed audit must end the request
        # having never decrypted anything.
        _audit_study(identity, study_uid, purpose, "record.read.imaging.study")
        return _json(_metadata_for(rows))

    @router.get("/studies/{study_uid}/series/{series_uid}/metadata")
    def series_metadata(
        request: Request,
        study_uid: str,
        series_uid: str,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "imaging:read")
        purpose = _purpose(request)
        study_uid = _valid_uid(study_uid, "StudyInstanceUID")
        series_uid = _valid_uid(series_uid, "SeriesInstanceUID")

        conn = _conn()
        try:
            rows = imaging_index.instances_for_series(conn, study_uid, series_uid)
        finally:
            conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="no such series")

        _audit_study(identity, study_uid, purpose, "record.read.imaging.study")
        return _json(_metadata_for(rows))

    # ---- WADO-RS: the objects themselves ----------------------------

    def _instance_row(study_uid: str, series_uid: str, sop_uid: str) -> dict:
        conn = _conn()
        try:
            row = imaging_index.get_instance(conn, study_uid, series_uid, sop_uid)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="no such instance")
        return row

    @router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
    def retrieve_instance(
        request: Request,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        identity: Identity = Depends(current_identity),
    ):
        """One instance as application/dicom.

        Not audited individually - the study-level entry written when the
        viewer opened this study accounts for the disclosure. See this
        module's docstring.
        """
        require(identity, "imaging:read")
        _purpose(request)
        row = _instance_row(
            _valid_uid(study_uid, "StudyInstanceUID"),
            _valid_uid(series_uid, "SeriesInstanceUID"),
            _valid_uid(sop_uid, "SOPInstanceUID"),
        )
        raw = reader.read_object_bytes(row["storage_key"])
        return Response(
            content=raw,
            media_type="application/dicom",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata")
    def instance_metadata_route(
        request: Request,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "imaging:read")
        _purpose(request)
        row = _instance_row(
            _valid_uid(study_uid, "StudyInstanceUID"),
            _valid_uid(series_uid, "SeriesInstanceUID"),
            _valid_uid(sop_uid, "SOPInstanceUID"),
        )
        raw = reader.read_object_bytes(row["storage_key"])
        return _json([dicomweb.instance_metadata(raw)])

    @router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frames}")
    def retrieve_frames(
        request: Request,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        frames: str,
        identity: Identity = Depends(current_identity),
    ):
        """Pixel frames, as multipart/related per PS3.18 §10.4.

        The frames are returned in whatever encoding the instance is
        stored in - this platform never transcodes. Decoding is the
        viewer's job and cornerstone does it in a web worker; transcoding
        server-side would need codec libraries this project does not
        ship, and would silently alter data this system exists to
        preserve.
        """
        require(identity, "imaging:read")
        _purpose(request)
        row = _instance_row(
            _valid_uid(study_uid, "StudyInstanceUID"),
            _valid_uid(series_uid, "SeriesInstanceUID"),
            _valid_uid(sop_uid, "SOPInstanceUID"),
        )

        try:
            numbers = [int(part) for part in frames.split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="malformed frame list") from exc
        if not numbers:
            raise HTTPException(status_code=400, detail="no frames requested")

        raw = reader.read_object_bytes(row["storage_key"])
        try:
            payloads = list(dicomweb.frame_bytes(raw, numbers))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        boundary = "phiaiframeboundary"
        transfer_syntax = row.get("transfer_syntax_uid") or "1.2.840.10008.1.2.1"
        body = bytearray()
        for payload in payloads:
            body += f"--{boundary}\r\n".encode()
            body += (
                f'Content-Type: application/octet-stream; '
                f'transfer-syntax={transfer_syntax}\r\n\r\n'
            ).encode()
            body += payload
            body += b"\r\n"
        body += f"--{boundary}--".encode()

        return Response(
            content=bytes(body),
            media_type=f'multipart/related; type="application/octet-stream"; boundary={boundary}',
            headers={"Cache-Control": "no-store"},
        )

    return router
# Made by Ryan Gomez & Co. Inc.
