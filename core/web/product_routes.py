# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The v1 product screens (docs/SPEC.md §5, §6) as web routes.

Five screens are BESPOKE and wired to the real decision cores:

- /consent      → core/governance/consent_gate.py  (the actual gate runs)
- /preflight    → the egress evidence matrix per cloud + the model registry
- /signature    → staged drafts; the metformin row's refusal comes from
                  core/governance/writeback.py's real Epic write-surface
                  assertion, not from copy
- /cohort       → core/analytics/cohort.py over the live OMOP layer
- /assistant    → already a first-class route in core/web/app.py

Everything else under /product/<key> renders from
core/web/product_content.py through one template - screen copy from the
adopted v1 design, labelled as the spec's worked examples where the
workflow behind it is not yet live.

GET carries screen state (jurisdiction, modality, cloud) in query
strings deliberately: none of it is PHI, and links are what make a
no-JavaScript interface's toggles work. Anything that could carry PHI
still POSTs, per the rule at the top of core/web/app.py.
"""

from __future__ import annotations

import logging

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web import nav as product_nav
from core.web import product_content as content
from core.web.auth import Identity

log = logging.getLogger("phi-ai.web.product")

_SIGNED_KEY = "signature_signed"


def register(app, page, require, current_identity, record) -> None:

    def _flags() -> dict:
        return {
            "assistant_enabled": getattr(app.state, "assistant", None) is not None,
            "local_accounts": getattr(app.state, "local_accounts", None) is not None,
            "imaging_enabled": getattr(app.state, "imaging_connection_factory", None) is not None,
        }

    def _require_screen(identity: Identity, key: str) -> None:
        """403 unless this identity's navigation includes the screen.

        The navigation table is the product's statement of who each
        screen is for; enforcing it here keeps the sidebar and the
        routes from ever disagreeing.
        """
        for group in product_nav.NAV:
            for item in group.items:
                if item.key == key:
                    if item.visible(identity, _flags()):
                        return
                    raise HTTPException(
                        status_code=403,
                        detail=f"your role does not include the {item.label} screen",
                    )
        raise HTTPException(status_code=404, detail="no such screen")

    # ---- generic spec screens --------------------------------------

    @app.get("/product/{key}", response_class=HTMLResponse)
    def product_page(
        key: str, request: Request, identity: Identity = Depends(current_identity)
    ):
        spec = content.PAGES.get(key)
        if spec is None:
            raise HTTPException(status_code=404, detail="no such screen")
        _require_screen(identity, key)

        sections = list(spec["sections"])
        live_note = None
        if key == "ingest":
            # The one generic screen with live numbers behind it today:
            # the index's own holdings, shown ahead of the worked example.
            try:
                stats = app.state.reader.stats()
                sections.insert(0, content.stats("This deployment — live index", [
                    {"v": f"{stats.total_resources:,}", "k": "resources in the encrypted store (indexed)", "cls": ""},
                    {"v": f"{stats.distinct_patients:,}", "k": "distinct patients", "cls": ""},
                    {"v": str(len(stats.resource_type_counts)), "k": "resource types held", "cls": ""},
                    {"v": stats.latest_stored_at.strftime("%Y-%m-%d") if stats.latest_stored_at else "—",
                     "k": "most recent stored resource", "cls": ""},
                ]))
                live_note = "The first row of figures is live from this deployment's index."
            except Exception as exc:  # index optional / unreachable
                log.warning("ingest live stats unavailable: %s", exc)

        return page(request, "product_page.html", identity,
                    active=key, spec=spec, sections=sections, live_note=live_note)

    # ---- 6.5 ambient consent gate ----------------------------------

    @app.get("/consent", response_class=HTMLResponse)
    def consent_screen(
        request: Request,
        j: str = "CA",
        m: str = "in_person",
        att: str = "0",
        identity: Identity = Depends(current_identity),
    ):
        _require_screen(identity, "consent")
        from core.governance.consent_gate import (
            ConsentRecord, ConsentStatus, Modality, consent_standard, evaluate_capture,
        )

        modality = Modality.TELEHEALTH if m == "telehealth" else Modality.IN_PERSON
        attested = att == "1"
        codes = {code for _, code in content.CONSENT_JURISDICTIONS}
        jurisdiction = j if j in codes else "CA"

        consent = ConsentRecord(
            status=ConsentStatus.GRANTED,
            timestamp="2026-08-26T14:00:00Z",
            obtained_by=identity.username,
            verbal_attestation_captured=attested,
        )
        decision = evaluate_capture(
            jurisdiction or None, modality, consent,
            audit=app.state.audit, actor=identity.username,
            encounter_key="ambient/consent-screen-evaluation",
        )
        standard = (
            consent_standard(jurisdiction, modality).value.replace("_", "-")
            if jurisdiction else None
        )

        audit_line = (
            f"consent.evaluated — verdict={'allow' if decision.allowed else 'deny'}"
            + (f" — required={standard}" if standard else " — basis=unresolved_jurisdiction")
            + (" — attestation=" + ("captured" if attested else "absent"))
        )

        return page(request, "consent.html", identity, active="consent",
                    jurisdiction=jurisdiction, modality=m, attested=attested,
                    jurisdictions=content.CONSENT_JURISDICTIONS,
                    decision=decision, standard=standard,
                    citation=content.CONSENT_CITATIONS.get(jurisdiction, ""),
                    layers=content.CONSENT_LAYERS,
                    footnote=content.CONSENT_FOOTNOTE,
                    audit_line=audit_line)

    # ---- 6.2 registry & preflight ----------------------------------

    @app.get("/preflight", response_class=HTMLResponse)
    def preflight_screen(
        request: Request, cloud: str = "aws",
        identity: Identity = Depends(current_identity),
    ):
        _require_screen(identity, "preflight")
        selected = cloud if cloud in content.PREFLIGHT else "aws"
        return page(request, "preflight.html", identity, active="preflight",
                    cloud=selected, clouds=content.PREFLIGHT,
                    data=content.PREFLIGHT[selected],
                    registry=content.REGISTRY_ROWS,
                    registry_footnote=content.REGISTRY_FOOTNOTE)

    # ---- 5.16 signature queue --------------------------------------

    def _draft_rows(request: Request) -> list[dict]:
        from core.governance.writeback import WritebackError, assert_epic_writable

        signed = set(request.session.get(_SIGNED_KEY, []))
        rows = []
        for d in content.SIGNATURE_DRAFTS:
            row = dict(d)
            try:
                # The REAL write-surface gate: refusal text comes from
                # core/governance/writeback.py, not from copy.
                assert_epic_writable(d["resource_type"], d["interaction"])
                row["writable"] = True
                row["blocked_reason"] = ""
            except WritebackError as exc:
                row["writable"] = False
                row["blocked_reason"] = str(exc)
            row["signed"] = d["id"] in signed
            rows.append(row)
        return rows

    @app.get("/signature", response_class=HTMLResponse)
    def signature_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        _require_screen(identity, "signature")
        return page(request, "signature.html", identity, active="signature",
                    drafts=_draft_rows(request),
                    facts=content.WRITEBACK_FACTS,
                    dependency=content.SIGNATURE_DEPENDENCY)

    @app.get("/signature/{draft_id}", response_class=HTMLResponse)
    def signature_detail(
        request: Request, draft_id: str,
        identity: Identity = Depends(current_identity),
    ):
        """The full draft, reviewed before the sign control appears.

        Nothing is signed from a list row: the reviewer reads exactly
        what signing would commit - the complete text, its citations,
        and the write path it would take - and only then the control.
        """
        _require_screen(identity, "signature")
        draft = next((d for d in _draft_rows(request) if d["id"] == draft_id), None)
        if draft is None:
            raise HTTPException(status_code=404, detail="no such draft")
        return page(request, "signature_detail.html", identity,
                    active="signature", d=draft)

    @app.post("/signature", response_class=HTMLResponse)
    def signature_sign(
        request: Request,
        draft_id: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        _require_screen(identity, "signature")
        from core.governance.writeback import WritebackError, assert_epic_writable

        draft = next((d for d in content.SIGNATURE_DRAFTS if d["id"] == draft_id), None)
        if draft is None:
            raise HTTPException(status_code=404, detail="no such draft")
        try:
            assert_epic_writable(draft["resource_type"], draft["interaction"])
        except WritebackError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        signed = set(request.session.get(_SIGNED_KEY, []))
        signed.add(draft_id)
        request.session[_SIGNED_KEY] = sorted(signed)
        # The signature event IS the record; audited before the redirect.
        record(identity, "signature.committed",
               f"draft/{draft_id}/{draft['resource_type']}", "treatment")
        return RedirectResponse("/signature", status_code=303)

    # ---- 5.11 cohort builder ---------------------------------------

    def _omop_connection():
        """A live OMOP analyst connection, or None with the reason why."""
        from core.config.settings import Settings
        from core.db.connection import connect

        try:
            settings = Settings.from_env()
        except Exception as exc:
            return None, f"platform settings did not load: {exc}"
        if not getattr(settings, "omop_analyst_username", None):
            return None, (
                "the OMOP analytics layer is not configured on this deployment "
                "(PHI_AI_OMOP_ANALYST_USERNAME is unset) — see "
                "runbooks/RUNBOOK_OMOP_SETUP.md"
            )
        try:
            return connect(settings, settings.omop_analyst_username), None
        except Exception as exc:
            return None, f"the OMOP layer is configured but unreachable: {exc}"

    def _cohort_context(result=None, term="", error=None, sql=None):
        return {
            "active": "cohort", "badges": content.COHORT_BADGES,
            "result": result, "term": term, "error": error, "sql": sql,
            "shortcuts": None, "demographics": None, "facilities": None,
        }

    @app.get("/cohort", response_class=HTMLResponse)
    def cohort_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        require(identity, "analytics:query")
        from core.analytics.cohort import CONDITION_SHORTCUTS, population_demographics

        ctx = _cohort_context()
        ctx["shortcuts"] = sorted(CONDITION_SHORTCUTS)
        conn, reason = _omop_connection()
        if conn is None:
            ctx["error"] = reason
        else:
            try:
                ctx["demographics"] = population_demographics(conn)
            except Exception as exc:
                ctx["error"] = f"the OMOP layer answered with an error: {exc}"
            finally:
                conn.close()
        return page(request, "cohort.html", identity, **ctx)

    @app.post("/cohort", response_class=HTMLResponse)
    def cohort_run(
        request: Request,
        term: str = Form(""),
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "analytics:query")
        from core.analytics.cohort import CONDITION_SHORTCUTS, count_patients_with_condition

        term = (term or "").strip()
        ctx = _cohort_context(term=term)
        ctx["shortcuts"] = sorted(CONDITION_SHORTCUTS)
        if not term:
            ctx["error"] = "state a condition to count — a shortcut name or a code prefix."
            return page(request, "cohort.html", identity, **ctx)

        # Recorded verbatim BEFORE it runs — the same ordering every
        # clinical read uses. A failed audit write means no query.
        record(identity, "analytics.cohort", f"cohort/{term[:120]}", "operations")

        conn, reason = _omop_connection()
        if conn is None:
            ctx["error"] = reason
            return page(request, "cohort.html", identity, **ctx)
        try:
            result = count_patients_with_condition(conn, term)
            ctx["result"] = result
            ctx["sql"] = (
                "SELECT COUNT(DISTINCT co.person_id)\n"
                "FROM cdm.condition_occurrence co\n"
                "WHERE  -- matched on: " + "; ".join(result.matched_on)
            )
        except Exception as exc:
            log.warning("cohort query failed: %s", exc)
            ctx["error"] = f"the OMOP layer answered with an error: {exc}"
        finally:
            conn.close()
        return page(request, "cohort.html", identity, **ctx)
# Made by Ryan Gomez & Co. Inc.
