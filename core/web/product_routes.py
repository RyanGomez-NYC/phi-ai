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

    def _cohort_args(term: str, exact: str, sex: str, band: str) -> tuple[str, bool, str, str]:
        """The definition's parts as every cohort route accepts them: the
        term (a condition name, a shortcut or a code prefix - never PHI,
        so it rides in the query string), whether it names a concept
        exactly (a pick from the search list), the sex value and the age
        band. Any part alone is a cohort; nothing set is every person."""
        from core.analytics.cohort import BANDS

        return ((term or "").strip()[:120],
                (exact or "").strip().lower() in ("1", "true", "on", "yes"),
                (sex or "").strip().lower()[:40],
                band if band in BANDS else "")

    def _cohort_context(result=None, term="", error=None, sql=None, exact=False, sex="", band=""):
        from core.analytics.cohort import BANDS, cohort_definition
        from core.analytics.cohort_report import PAYER_SELECTOR_NOTE

        return {
            "active": "cohort", "badges": content.COHORT_BADGES,
            "result": result, "term": term, "exact": exact, "sex": sex, "band": band,
            "definition": cohort_definition(term, sex, band),
            "error": error, "sql": sql,
            "demographics": None, "sexes": [], "bands": BANDS,
            "matches": None, "matches_note": None,
            # The sentence for text that names no condition the corpus
            # holds; the cohort is then the selectors alone.
            "unresolved": None,
            "suggest_url": "/cohort/suggest",
            "payer_note": PAYER_SELECTOR_NOTE,
        }

    def _value_sets():
        """The deployment's sensitive-category value sets, or None."""
        from core.terminology.loader import configured_value_sets

        return configured_value_sets()

    def _resolve_condition(conn, term: str, is_exact: bool, value_sets, matches=None):
        """What the typed term counts as, for every cohort route alike:
        the corpus's own condition name, a contains match, or nothing at
        all (core/analytics/cohort.resolve_cohort_term). A vocabulary
        lookup, not a count - it writes no audit row, and it runs BEFORE
        the row the count writes, because the row must name the cohort
        that will actually be counted."""
        from core.analytics.cohort import ResolvedCondition, resolve_cohort_term
        from core.analytics.cohort_report import excluded_codes

        try:
            return resolve_cohort_term(conn, term, exact=is_exact,
                                       excluded=excluded_codes(value_sets), matches=matches)
        except Exception as exc:
            # A lookup that could not run is not evidence that the corpus
            # has no such condition: the term is left as the reader asked
            # for it, and the count that follows says what went wrong.
            log.warning("condition resolution failed: %s", exc)
            return ResolvedCondition(typed=(term or "").strip(), term=(term or "").strip(),
                                     exact=is_exact, checked=False)

    def _refused_term(request: Request, identity: Identity, term: str, category: str,
                      exact: bool = False, sex: str = "", band: str = ""):
        """The refusal a protected term takes, on the cohort screen and on
        the report alike: the reason on the screen, nothing counted, a
        denial on the trail. A cohort over "depression" IS a mental-health
        cohort, and an operations purpose does not admit the category."""
        record(identity, "access.denied", f"cohort/{term[:120]} heightened:{category}", "operations")
        ctx = _cohort_context(term=term, exact=exact, sex=sex, band=band, error=(
            f"“{term}” falls inside a heightened category "
            f"({category.replace('_', ' ')}). A cohort over it is a disclosure of the "
            "category, and an operations purpose does not admit it; nothing was counted."
        ))
        return page(request, "cohort.html", identity, **ctx)

    @app.get("/cohort", response_class=HTMLResponse)
    def cohort_screen(
        request: Request,
        q: str = "",
        exact: str = "",
        sex: str = "",
        band: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The builder. A cohort is any combination of its variables and
        each alone is one: the condition is a SEARCH over the CDM's own
        concept names, sex and age band are selectors, nothing set is
        every person. GET, so a definition is a URL: the term is a
        condition name, a shortcut or a code prefix - never PHI (the same
        call the report makes) - and links are what let the search list
        set a condition exactly with no script. The definition is built
        the moment any part of it is on the query string, an emptied
        search box included; a bare /cohort is the screen with nothing
        asked yet."""
        require(identity, "analytics:query")
        from core.analytics.cohort import (
            cohort_definition,
            count_cohort,
            population_demographics,
            protected_term,
            search_conditions,
            sex_values,
        )
        from core.analytics.cohort_report import WITHHELD_NO_VALUE_SETS, excluded_codes

        term, is_exact, sex, band = _cohort_args(q, exact, sex, band)
        building = any(k in request.query_params for k in ("q", "exact", "sex", "band"))
        ctx = _cohort_context(term=term, exact=is_exact, sex=sex, band=band)
        value_sets = _value_sets()
        if term:
            category = protected_term(term, value_sets)
            if category:
                return _refused_term(request, identity, term, category, is_exact, sex, band)

        conn, reason = _omop_connection()
        if conn is None:
            # Nothing can be resolved or counted; the attempt is still on
            # the trail, named as the reader asked for it.
            if building:
                record(identity, "analytics.cohort", f"cohort/{ctx['definition'][:120]}",
                       "operations")
            ctx["error"] = reason
            return page(request, "cohort.html", identity, **ctx)
        resolved = None
        if building:
            try:
                # The list is a code-level breakdown - condition by
                # persons - so it takes the report's own exclusion, and
                # withholds itself the same way without the value sets.
                excluded = excluded_codes(value_sets)
                if len(term) >= 2 and excluded is None:
                    ctx["matches_note"] = WITHHELD_NO_VALUE_SETS
                elif len(term) >= 2:
                    ctx["matches"] = search_conditions(conn, term, excluded=excluded)
            except Exception as exc:
                log.warning("condition search failed: %s", exc)
            # Only KNOWN conditions count. The resolution is the
            # vocabulary lookup the suggestion panel makes, made once
            # more on the server so a URL typed by hand cannot count by a
            # name the corpus does not hold.
            resolved = _resolve_condition(conn, term, is_exact, value_sets, ctx["matches"])
            ctx["term"] = resolved.term if resolved.known else term
            ctx["exact"] = resolved.exact
            ctx["unresolved"] = resolved.note
            ctx["definition"] = cohort_definition(resolved.term, sex, band)
            # Recorded verbatim BEFORE the count runs - the definition the
            # count will use, in the same ordering every clinical read
            # uses. A failed audit write means no query, and still closes
            # the connection.
            try:
                record(identity, "analytics.cohort", f"cohort/{ctx['definition'][:120]}",
                       "operations")
            except Exception:
                conn.close()
                raise
        try:
            if resolved is not None:
                result = count_cohort(conn, resolved.term, sex, band, exact=resolved.exact)
                ctx["result"] = result
                if ctx["matches"] is not None and not ctx["matches"] and not resolved.checked:
                    ctx["matches_note"] = (
                        "This deployment has no vocabulary loaded, so there are no condition "
                        "names to search; the term counts as a shortcut or a code prefix - see "
                        "runbooks/RUNBOOK_OMOP_SETUP.md."
                    )
                ctx["sql"] = (
                    "SELECT COUNT(DISTINCT p.person_id)\n"
                    "FROM cdm.person p\n"
                    "WHERE  -- matched on: " + "; ".join(result.matched_on)
                )
            ctx["demographics"] = population_demographics(conn)
            ctx["sexes"] = sex_values(conn)
        except Exception as exc:
            log.warning("cohort query failed: %s", exc)
            ctx["error"] = f"the OMOP layer answered with an error: {exc}"
        finally:
            conn.close()
        return page(request, "cohort.html", identity, **ctx)

    @app.get("/cohort/suggest")
    def cohort_suggest(q: str = "", identity: Identity = Depends(current_identity)):
        """The condition box's suggestions: the KNOWN condition names that
        match, the twelve with the most persons, each with its count
        (the contract's addendum, item 3).

        The same permission the count takes and the same derived list the
        page renders below it (cohort.search_conditions - a GROUP BY over
        the CDM's own concept names, never a typed list), with the same
        heightened-category exclusion; with no value sets configured it
        withholds itself exactly as that list does. NO AUDIT ROW: this is
        a vocabulary lookup, not a count, and it never returns a person.

        Answers `{"q", "conditions": [{"name", "persons", "label"}],
        "withheld", "note"}`. The label carries the small-cell rule
        already ("< 11 persons"), so the client never formats a
        suppressed count, and `persons` is null when it is suppressed.
        """
        require(identity, "analytics:query")
        from fastapi.responses import JSONResponse

        from core.analytics.cohort import protected_term, search_conditions
        from core.analytics.cohort_report import WITHHELD_NO_VALUE_SETS, excluded_codes

        term = (q or "").strip()
        payload: dict = {"q": term, "conditions": [], "withheld": None, "note": None}
        headers = {"Cache-Control": "no-store"}
        if len(term) < 2 or len(term) > 200:
            return JSONResponse(payload, headers=headers)
        value_sets = _value_sets()
        category = protected_term(term, value_sets)
        if category:
            # The refusal the screen gives the same term: a heightened
            # category is not suggested either.
            payload["note"] = (
                f"“{term}” falls inside a heightened category "
                f"({category.replace('_', ' ')}); it is not suggested."
            )
            return JSONResponse(payload, headers=headers)
        excluded = excluded_codes(value_sets)
        if excluded is None:
            payload["withheld"] = WITHHELD_NO_VALUE_SETS
            return JSONResponse(payload, headers=headers)
        conn, reason = _omop_connection()
        if conn is None:
            payload["note"] = reason
            return JSONResponse(payload, headers=headers)
        try:
            for match in search_conditions(conn, term, 12, excluded=excluded):
                payload["conditions"].append({
                    "name": match["name"],
                    "persons": match["persons"],
                    "label": f"{match['persons_text']} persons",
                })
        except Exception as exc:
            log.warning("condition suggestions failed: %s", exc)
            payload["note"] = f"the OMOP layer answered with an error: {exc}"
        finally:
            conn.close()
        return JSONResponse(payload, headers=headers)

    # ---- 5.11 cohort report ----------------------------------------
    #
    # Every population metric at the cohort level, over the same predicate
    # the cohort count uses (core/analytics/cohort_report.py). Reached from
    # the cohort result's Report button; no navigation entry of its own.
    # GET carries the term and the selectors: a condition name is not PHI,
    # and links are what make a no-script report's selectors work.

    def _report_helpers() -> dict:
        from core.web import charts

        return {"bars": charts.bars, "hist": charts.hist, "line": charts.line, "tile": charts.tile}

    def _report_stores(refs: list[str], report: dict) -> None:
        """The stores outside the CDM, joined on the cohort's opaque
        references: the PHI AI store, the run ledger, the imaging index.
        Each says "not connected" rather than inventing a figure."""
        from core.analytics import cohort_report as cr

        report["store"] = cr.store_holdings_for(getattr(app.state, "reader", None), refs)
        pstate = getattr(app.state, "platform_state", None)
        runs = pstate.orch_run_list() if pstate is not None and hasattr(pstate, "orch_run_list") else []
        try:
            from core.orchestration.systems import systems

            catalogue = systems()
        except Exception as exc:  # the catalogue is prose over profiles; never fatal
            log.warning("systems catalogue unavailable: %s", exc)
            catalogue = {}
        report["lives"] = cr.ledger_holdings_for(runs, refs, catalogue,
                                                 report["store"]["charts"], len(refs))
        factory = getattr(app.state, "imaging_connection_factory", None)
        if factory is None:
            report["imaging"] = {"rows": [], "note": "This deployment has no imaging index connected."}
            return
        try:
            iconn = factory()
        except Exception as exc:
            report["imaging"] = {"rows": [], "note": f"the imaging index is unreachable: {exc}"}
            return
        try:
            report["imaging"] = {"rows": cr.imaging_modalities_for(iconn, refs), "note": None}
        except Exception as exc:
            report["imaging"] = {"rows": [], "note": f"the imaging index answered with an error: {exc}"}
        finally:
            iconn.close()

    @app.get("/cohort/report", response_class=HTMLResponse)
    def cohort_report(
        request: Request,
        term: str = "",
        exact: str = "",
        sex: str = "",
        band: str = "",
        identity: Identity = Depends(current_identity),
    ):
        require(identity, "analytics:query")
        from core.analytics import cohort_report as cr
        from core.analytics.cohort import (
            cohort_definition,
            protected_term,
            sex_values,
            unresolved_condition_note,
        )
        from core.analytics.cohort_report import PAYER_SELECTOR_NOTE

        term, is_exact, sex, band = _cohort_args(term, exact, sex, band)
        value_sets = _value_sets()
        if term:
            category = protected_term(term, value_sets)
            if category:
                return _refused_term(request, identity, term, category, is_exact, sex, band)
        definition = cohort_definition(term, sex, band)

        ctx = {"active": "cohort", "term": term, "exact": is_exact, "sex": sex, "band": band,
               "definition": definition, "selectors": " · ".join(p for p in (sex, band) if p),
               "report": None, "error": None, "unresolved": None,
               # The report's own selector bar carries the builder's
               # condition box (templates/_condition_box.html), so it needs
               # what the builder's needs: the endpoint that suggests the
               # corpus's own names, and the text the box holds - which is
               # what the reader TYPED when that named no condition, so
               # their own words survive the redraw to be fixed.
               "typed": term, "suggest_url": "/cohort/suggest",
               # Payer is not a control here: this CDM has no payer table,
               # and the bar says so rather than growing a selector that
               # would filter nothing.
               "payer_note": PAYER_SELECTOR_NOTE,
               # The role dictates what a number links to: rows for a
               # role that may read charts, the section for one that may not.
               "can_rows": identity.can("patient:read"),
               "can_holdings": identity.can("integration:view"),
               **_report_helpers()}
        conn, reason = _omop_connection()
        if conn is None:
            # ONE audit row, with the purpose, BEFORE any query - the whole
            # definition, "all persons" included, and written whether or
            # not there is a layer to answer. A failed audit write means
            # no report.
            record(identity, "analytics.cohort_report", f"cohort/{definition[:120]}", "operations")
            ctx["error"] = reason
            return page(request, "cohort_report.html", identity, **ctx)
        # Only KNOWN conditions count, here as on the builder: the report
        # never claims a condition that resolved to nothing. A vocabulary
        # lookup, so it writes no row of its own; the report's ONE audit
        # row follows it and names the definition the report will draw.
        # An audit sink that refuses the row still closes the connection.
        try:
            resolved = _resolve_condition(conn, term, is_exact, value_sets)
            term, is_exact = resolved.term, resolved.exact
            definition = cohort_definition(term, sex, band)
            ctx["term"], ctx["exact"], ctx["definition"] = term, is_exact, definition
            # The box keeps the corpus's own spelling once a term resolves,
            # and the reader's own words when it resolved to nothing.
            ctx["typed"] = term if resolved.known else resolved.typed
            ctx["unresolved"] = (
                unresolved_condition_note(resolved.typed, report=True) if resolved.note else None
            )
            # The bar's sex options come from the CDM, not from this cohort's
            # own breakdown: a cohort with no men must still offer "male", or
            # the reader cannot cut the other way from here.
            ctx["sexes"] = sex_values(conn)
            record(identity, "analytics.cohort_report", f"cohort/{definition[:120]}", "operations")
        except Exception:
            conn.close()
            raise
        try:
            report = cr.build_report(conn, term, sex, band, exact=is_exact,
                                     value_sets=value_sets, with_references=True)
        except Exception as exc:
            log.warning("cohort report failed: %s", exc)
            ctx["error"] = f"the OMOP layer answered with an error: {exc}"
            return page(request, "cohort_report.html", identity, **ctx)
        finally:
            conn.close()
        refs = report.pop("references", [])
        _report_stores(refs, report)
        ctx["report"] = report
        return page(request, "cohort_report.html", identity, **ctx)

    @app.get("/cohort/report/rows", response_class=HTMLResponse)
    def cohort_report_rows(
        request: Request,
        term: str = "",
        exact: str = "",
        sex: str = "",
        band: str = "",
        cell: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The persons behind one number: `patient:read` on top of
        `analytics:query`, because a list of references is the thing an
        analyst is precisely not given."""
        require(identity, "analytics:query")
        require(identity, "patient:read")
        from core.analytics import cohort_report as cr
        from core.analytics.cohort import cohort_definition, protected_term

        term, is_exact, sex, band = _cohort_args(term, exact, sex, band)
        cell = (cell or "").strip()[:160]
        value_sets = _value_sets()
        if term:
            category = protected_term(term, value_sets)
            if category:
                return _refused_term(request, identity, term, category, is_exact, sex, band)
        definition = cohort_definition(term, sex, band)
        ctx = {"active": "cohort", "term": term, "exact": is_exact, "sex": sex, "band": band,
               "definition": definition, "selectors": " · ".join(p for p in (sex, band) if p),
               "cell": cell, "rows": None, "error": None}
        conn, reason = _omop_connection()
        if conn is None:
            record(identity, "analytics.cohort_rows", f"cohort/{definition[:120]}/{cell[:80]}",
                   "operations")
            ctx["error"] = reason
            return page(request, "cohort_report_rows.html", identity, **ctx)
        # The rows are the report's own cohort, so they take the report's
        # own resolution: an unresolved condition is the selectors' rows,
        # never a contains match nobody asked for.
        try:
            resolved = _resolve_condition(conn, term, is_exact, value_sets)
            term, is_exact = resolved.term, resolved.exact
            definition = cohort_definition(term, sex, band)
            ctx["term"], ctx["exact"], ctx["definition"] = term, is_exact, definition
            record(identity, "analytics.cohort_rows", f"cohort/{definition[:120]}/{cell[:80]}",
                   "operations")
        except Exception:
            conn.close()
            raise
        try:
            ctx["rows"] = cr.build_rows(conn, term, sex, band, cell, exact=is_exact,
                                        value_sets=value_sets)
        except cr.CellError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.warning("cohort rows failed: %s", exc)
            ctx["error"] = f"the OMOP layer answered with an error: {exc}"
        finally:
            conn.close()
        return page(request, "cohort_report_rows.html", identity, **ctx)
# Made by Ryan Gomez & Co. Inc.
