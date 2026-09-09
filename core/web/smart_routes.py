# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
SMART on FHIR: the in-context EHR launch, its callback, and the record it lands on

MOVED OUT OF core/web/app.py's create_app(), UNCHANGED. See
core/web/roi_routes.py's header for why that function was broken up
and what the register() seam is. The handler bodies here are
byte-for-byte what they were inside create_app.

/smart/launch and /smart/callback are the two routes with no
authenticated identity yet - the launch establishes one. That is why they
read as exceptions to the 'every route checks authorization' rule in
app.py's docstring, and why they are worth reading as a group.
"""

from __future__ import annotations

import logging

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web.auth import Identity, PURPOSES_OF_USE
from core.web.patient_context import clear_patient, set_patient
from core.web.templating import TEMPLATES

log = logging.getLogger("phi-ai.web.smart")


def register(app, page, require, current_identity, record, reader, _imaging_studies) -> None:
    """Attach these screens to `app`."""

    def smart_service():
        service = getattr(app.state, "smart", None)
        if service is None:
            raise HTTPException(
                status_code=503,
                detail="SMART launch is not configured. Register an EMR in "
                "config/smart_issuers.yaml - see runbooks/RUNBOOK_SMART_LAUNCH.md.",
            )
        return service

    @app.get("/smart/launch")
    def smart_launch(request: Request, iss: str = "", launch: str = ""):
        """Entry point the EMR opens. Unauthenticated BY DEFINITION - the
        whole purpose is to establish who the user is."""
        from fastapi.responses import RedirectResponse

        from core.web.smart.launch import IssuerNotAllowed, SMARTError

        try:
            return RedirectResponse(smart_service().begin(iss, launch), status_code=302)
        except IssuerNotAllowed as exc:
            # 403 rather than 400: this is a refusal to trust, not a
            # malformed request, and the distinction matters when reading
            # logs for a crafted-launch attempt.
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except SMARTError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/smart/callback")
    def smart_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        from fastapi.responses import RedirectResponse

        from core.web.auth import Identity, store_identity_in_session, _parse_roles
        from core.web.smart.launch import SMARTError

        if error:
            raise HTTPException(
                status_code=400,
                detail=f"the EMR refused the launch: {error} {error_description or ''}".strip(),
            )
        if not code or not state:
            raise HTTPException(status_code=400, detail="incomplete callback from the EMR")

        try:
            context = smart_service().complete(state=state, code=code)
        except SMARTError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        identity = Identity(
            username=context.username,
            email=None,
            roles=_parse_roles(",".join(context.roles)),
        )
        if "session" in request.scope:
            store_identity_in_session(
                request.session, identity, issuer=context.issuer, fhir_user=context.fhir_user
            )
        else:
            raise HTTPException(
                status_code=503,
                detail="PHI_AI_WEB_SESSION_SECRET is not set, so a completed SMART "
                "launch cannot be carried across requests. Set it to enable EHR launch.",
            )

        if app.state.audit is not None:
            app.state.audit.record(
                actor=identity.username,
                action="auth.smart.launch",
                resource_key=f"{context.issuer} patient={context.patient_id or 'none'}",
                purpose_of_use="treatment",
            )

        # Remember the presentation choice for the rest of the session:
        # subsequent navigation inside the EHR frame must stay compact,
        # and the redirect below is the only place that knows.
        registered = smart_service().resolve_issuer(context.issuer)
        request.session["embedded"] = bool(registered.embedded)
        # Remember where "back" is. Held in the session rather than
        # recomputed per page so a clinician who navigates deeper into the
        # platform can still return to the chart they came from.
        request.session["chart_url_template"] = registered.chart_url or ""
        request.session["chart_label"] = registered.chart_label or "the EMR"
        request.session["launch_patient"] = context.patient_id or ""
        # The launch's patient becomes the patient in context, so every
        # screen after the landing chart follows the EMR's choice - which
        # is the whole point of launching in context.
        if context.patient_id:
            set_patient(request.session, f"Patient/{context.patient_id}",
                        label=context.patient_id)
        else:
            clear_patient(request.session)
        request.session["launch_encounter"] = context.encounter_id or ""

        # Land in context. Treatment is the correct purpose for a
        # clinician launching from a patient's chart, and it is recorded
        # as such rather than inferred later.
        if context.patient_id and context.record_source:
            target = f"/smart/patient/{context.patient_id}"
            if context.encounter_id:
                # Land on the VISIT they launched from, not the whole
                # record - that is what "in context" means to a clinician
                # who is looking at one encounter.
                target += f"?encounter={context.encounter_id}"
            return RedirectResponse(target, status_code=302)

        reason = None
        if context.patient_id and not context.record_source:
            reason = (
                f"This platform's records did not come from {context.issuer}, so patient "
                f"identifiers from it do not resolve here. Search for the patient by their "
                "identifier in the source system this platform was populated from."
            )
        elif not context.patient_id:
            reason = (
                "The EMR did not send a patient context with this launch, so there is no "
                "record to open. Search for the patient below."
            )
        return TEMPLATES.TemplateResponse(
            request=request, name="smart_no_context.html",
            context={"identity": identity, "purposes": PURPOSES_OF_USE, "reason": reason},
        )

    @app.get("/smart/patient/{patient_id}", response_class=HTMLResponse)
    def smart_patient(
        request: Request,
        patient_id: str,
        encounter: Optional[str] = None,
        identity: Identity = Depends(current_identity),
    ):
        """The in-context landing page.

        A GET, unlike the ordinary patient view which is POSTed with a
        chosen purpose - the EMR redirect cannot POST. Purpose is
        `treatment`, which is what a clinician launching from a patient's
        open chart is doing, and it is recorded in the audit entry exactly
        as an explicitly chosen one would be.
        """
        require(identity, "patient:read")
        reference = f"Patient/{patient_id}"

        record(identity, "record.read.patient", reference, "treatment")
        rows = reader.resources_for_patient(reference)

        encounter_total = None
        if encounter:
            # Encounter membership lives inside the resource, not the
            # index - an encounter id links a patient to a specific
            # episode on a specific date, which schema.sql keeps out. So
            # this reads the resources, like date scoping does.
            from core.fhir.encounter_context import resource_in_encounter

            encounter_total = len(rows)
            filtered = []
            for row in rows:
                try:
                    resource = reader.read_resource(row["storage_key"])
                except Exception as exc:
                    # A resource that cannot be read is SHOWN, not hidden:
                    # dropping it would quietly narrow a clinical view.
                    log.error("could not read %s for encounter filter: %s",
                              row["storage_key"], exc)
                    filtered.append(row)
                    continue
                if resource_in_encounter(resource, encounter):
                    filtered.append(row)
            rows = filtered

        return page(
            request, "patient.html", identity,
            patient_reference=reference, resources=rows, purpose="treatment",
            launched_in_context=True, encounter_id=encounter,
            encounter_total=encounter_total,
            imaging_studies=_imaging_studies(identity, reference),
        )

# Made by Ryan Gomez & Co. Inc.
