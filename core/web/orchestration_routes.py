# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The Orchestration screen: wire an exchange, bound its scope, and read
every delivery's decision before anything moves.

Three steps, disclosed as they are earned. Step 1 (wire) is always open;
step 2 (scope) opens when an exchange is saved and STAYS open through
step 3 (preflight), so the reader decides whether to run with the whole
scope on screen rather than a summary of it. A plain page load opens step
1 alone. What opens is a one-shot flash in the session, set by the action
that earned it - never derived from stored state, which is what put the
preflight on screen for anyone who happened to have a scope saved.

Every decision shown here is core.orchestration.decide.decide_delivery;
the run (increment 2) will call the same function, so the preflight cannot
promise what the run will not do.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.orchestration import (CADENCES, DELIVERY_MODES, SET_MAX, Run, Scope, Selection,
                                audit_run, categories_for_chart, category_label,
                                decide_delivery, decided_not_written, execute,
                                normalise_scope, normalise_selection, scope_has_selectors,
                                scope_label, selection_chosen, systems, withheld_prose)
from fastapi import HTTPException
from core.orchestration.consents import parse_consent_key
from core.web.auth import Identity, role_allowed_purposes
from core.web.platform_state import PlatformState, _now

log = logging.getLogger("phi_ai.web.orchestration")

#: How many of a chart's records the screen classifies before it stops. A
#: chart is small; a store that is not a chart is not what this screen is
#: for, and a bound here beats a page that never comes back.
CHART_RESOURCE_CAP = 200
#: Charts the "work these per-chart" hand-off scans for heightened
#: categories before giving up. The set holds SET_MAX; scanning the whole
#: store to fill three slots would be the wrong trade.
TAKE_SCAN_CAP = 50


def register(app, page, require, current_identity, record, reader) -> None:

    def state() -> PlatformState:
        return app.state.platform_state

    def current_reader():
        # Read at request time, not bound at registration: tests hand the
        # app a different store, and the deployment may replace its reader.
        return getattr(app.state, "reader", reader)

    # ---- state as dataclasses ------------------------------------------

    def selection() -> Selection:
        raw = state().orch_selection_get()
        return Selection(**{k: (tuple(v) if isinstance(v, list) else v)
                            for k, v in raw.items() if k in Selection.__dataclass_fields__})

    def scope() -> Scope:
        raw = state().orch_scope_get()
        return Scope(**{k: v for k, v in raw.items() if k in Scope.__dataclass_fields__})

    def purposes_for(identity: Identity) -> list[tuple[str, str]]:
        return list(role_allowed_purposes(identity))

    def purpose_or_current(identity: Identity, posted: Optional[str], current: str) -> str:
        allowed = {code for code, _ in purposes_for(identity)}
        if posted in allowed:
            return posted
        if current in allowed:
            return current
        return next(iter(allowed), "treatment")

    # ---- the session flash: which step this action just opened ----------

    def flash_set(request: Request, step: str) -> None:
        if "session" in request.scope:
            request.session["orch_open"] = step

    def flash_take(request: Request) -> str:
        if "session" in request.scope:
            return str(request.session.pop("orch_open", "") or "")
        return ""

    def notice_set(request: Request, text: str) -> None:
        if "session" in request.scope:
            request.session["orch_notice"] = text

    def notice_take(request: Request) -> str:
        if "session" in request.scope:
            return str(request.session.pop("orch_notice", "") or "")
        return ""

    def land(step: str) -> RedirectResponse:
        return RedirectResponse(f"/orchestration#{step}", status_code=303)

    # ---- charts and their heightened categories -------------------------

    def chart_row(ref: str) -> dict:
        """One chart: its reference, a display name, and the heightened
        categories it carries, from the platform's own classifier over the
        records the store holds for it."""
        rd = current_reader()
        rows = rd.resources_for_patient(ref) or []
        resources = []
        name = ref
        for r in rows[:CHART_RESOURCE_CAP]:
            key = r.get("storage_key") if isinstance(r, dict) else None
            if not key:
                continue
            try:
                res = rd.read_resource(key)
            except Exception:  # one unreadable object must not hide the chart
                continue
            if isinstance(res, dict):
                resources.append(res)
                if res.get("resourceType") == "Patient" and name == ref:
                    nm = (res.get("name") or [{}])[0] or {}
                    parts = [" ".join(nm.get("given") or []), nm.get("family") or ""]
                    label = " ".join(p for p in parts if p).strip()
                    name = label or ref
        return {"ref": ref, "name": name, "categories": categories_for_chart(resources)}

    def charts_in_set(sel: Selection) -> list[dict]:
        return [chart_row(ref) for ref in sel.patients]

    def scope_count(sel: Selection, sc: Scope) -> int:
        if sc.mode == "chart":
            return len(sel.patients)
        try:
            return int(current_reader().stats().distinct_patients)
        except Exception as exc:
            # A store that cannot count is not an empty store. Zero is what
            # the screen can honestly show, but never silently.
            log.warning("orchestration: scope count unavailable from the reader: %s", exc)
            return 0

    # ---- the plan: every target x every chart, decided ------------------

    def plan(sel: Selection, sc: Scope, charts: list[dict]) -> list[dict]:
        sysmap = systems()
        cons = state().orch_consents
        rows = []
        for tk in sel.targets:
            target = sysmap.get(tk)
            if target is None:
                continue
            if sc.mode != "chart" or not charts:
                d = decide_delivery(target, patient=None, held={}, purpose=sel.purpose,
                                    consents=cons, scope=sc)
                rows.append({"target": target, "chart": None, "decision": d})
                continue
            for ch in charts:
                # The same identity bound the run consults: a per-chart delivery
                # is decided against the chart's VERIFIED link at the target.
                linked = state().orch_links.verified_for(ch["ref"], tk) is not None
                d = decide_delivery(target, patient=ch["ref"], held=ch["categories"],
                                    purpose=sel.purpose, consents=cons, scope=sc,
                                    who=ch["name"], link_verified=linked)
                rows.append({"target": target, "chart": ch, "decision": d})
        return rows

    def consent_rows(sel: Selection, charts: list[dict]) -> list[dict]:
        """The matrix: system x chart x category, each with whether a consent
        is on file. Only categories the chart actually carries - nothing to
        consent where there is nothing to release."""
        sysmap = systems()
        cons = state().orch_consents
        out = []
        for tk in sel.targets:
            target = sysmap.get(tk)
            if target is None:
                continue
            for ch in charts:
                for cat, n in ch["categories"].items():
                    out.append({"target": target, "chart": ch, "category": cat,
                                "label": category_label(cat), "records": n,
                                "key": f"{tk}|{ch['ref']}|{cat}",
                                "on_file": cons.has(tk, ch["ref"], cat)})
        return out

    # ---- the page -------------------------------------------------------

    @app.get("/orchestration", response_class=HTMLResponse)
    def orchestration(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        sel = selection()
        sc = scope()
        sysmap = systems()
        wired = selection_chosen(sel)
        charts = charts_in_set(sel) if wired else []
        rows = plan(sel, sc, charts) if wired else []
        withheld: dict[str, int] = {}
        for r in rows:
            for cat, n in r["decision"].withheld.items():
                withheld[cat] = max(withheld.get(cat, 0), int(n))
        heightened_total = sum(withheld.values())
        matrix = consent_rows(sel, charts) if wired else []
        on_file = sum(1 for m in matrix if m["on_file"])
        n_scope = scope_count(sel, sc) if wired else 0
        return page(
            request, "orchestration.html", identity, active="orchestration",
            systems=sysmap, sel=sel, scope=sc, wired=wired,
            scoped=wired and (sc.mode != "chart" or bool(sel.patients)),
            n_scope=n_scope, scope_text=scope_label(sc, max(1, len(sel.sources))),
            has_selectors=scope_has_selectors(sc),
            charts=charts, plan=rows, matrix=matrix, matrix_on_file=on_file,
            withheld=withheld, withheld_total=heightened_total,
            withheld_text=withheld_prose(withheld),
            exchanges=state().orch_exchange_list(),
            purposes=purposes_for(identity),
            delivery_modes=DELIVERY_MODES, cadences=CADENCES, set_max=SET_MAX,
            flash=flash_take(request), notice=notice_take(request),
            can_choose=identity.can("patient:search"),
            can_hold=identity.can("patient:read"),
            can_consent=identity.can("roi:create"),
            can_execute=identity.can("integration:export"),
            runs=state().orch_run_list()[:8],
        )

    # ---- actions --------------------------------------------------------

    @app.post("/orchestration/select", response_class=HTMLResponse)
    async def orchestration_select(request: Request,
                                   identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        form = await request.form()
        cur = selection()
        sel = normalise_selection(
            form.getlist("sources"), form.getlist("targets"), cur.patients,
            purpose_or_current(identity, form.get("purpose"), cur.purpose),
            name=form.get("name") if "name" in form else None,
            delivery=form.get("delivery"), cadence=form.get("cadence"), current=cur)
        state().orch_selection_set(asdict(sel))
        record(identity, "integration.selection_changed",
               f"orch/selection sources={len(sel.sources)} targets={len(sel.targets)}",
               sel.purpose)
        # A lane change that applies itself stays put: the reader is still
        # wiring, and opening the next step on every tick is the page
        # bouncing. The platform's own screen never posts lane_apply - it
        # serves script-src 'none', so a tick waits for Save - but the
        # contract is kept for a deployment that allows a script to apply
        # the lanes on change, which is what the demonstration does.
        if form.get("lane_apply"):
            return land("wired")
        if not selection_chosen(sel):
            # The same system on both sides moves nothing: the step is not
            # finished, and the page says why instead of opening the scope.
            notice_set(request, "Nothing crosses yet: the same system is on both sides. "
                                "Pick a different target and this exchange is wired.")
            return land("wired")
        flash_set(request, "scope")
        return land("scope")

    @app.post("/orchestration/scope", response_class=HTMLResponse)
    async def orchestration_scope(request: Request,
                                  identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        form = await request.form()
        if form.get("mode", "chart") != "chart":
            # A selector that finds people is an identity search: the
            # roster's gate applies here too.
            require(identity, "patient:search")
        sel = selection()
        sc = normalise_scope(form, sel.purpose, by=identity.username, at=_now())
        state().orch_scope_set(sc.as_dict())
        n = scope_count(sel, sc)
        record(identity, "integration.scope_changed",
               f"scope/{sc.mode} matched={n} — {scope_label(sc, max(1, len(sel.sources)))}",
               sel.purpose)
        flash_set(request, "preflight" if n > 0 else "scope")
        return land("preflight" if n > 0 else "scope")

    @app.post("/orchestration/consent", response_class=HTMLResponse)
    async def orchestration_consent(request: Request,
                                    identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        require(identity, "patient:read")
        require(identity, "roi:create")
        form = await request.form()
        sel = selection()
        grant = parse_consent_key(form.get("grant", ""))
        revoke = parse_consent_key(form.get("revoke", ""))
        if grant and grant[0] in sel.targets and grant[1] in sel.patients:
            system, patient, cat = grant
            state().orch_consent_grant(system, patient, cat, sel.purpose,
                                       by=identity.username, at=_now())
            record(identity, "consent.disclosure_granted",
                   f"orch/consent {system} {patient} {cat}", sel.purpose)
        elif revoke:
            system, patient, cat = revoke
            if state().orch_consent_revoke(system, patient, cat):
                record(identity, "consent.disclosure_revoked",
                       f"orch/consent {system} {patient} {cat}", sel.purpose)
        flash_set(request, "preflight")
        return land("preflight")

    @app.post("/orchestration/heightened", response_class=HTMLResponse)
    async def orchestration_heightened_take(request: Request,
                                            identity: Identity = Depends(current_identity)):
        """The way out of the dead end. "N heightened records stay behind" is
        true and, on a population scope, has nothing behind it: consent is
        per chart and a population names none. This puts the charts that
        actually carry those categories into the set and switches to the
        per-chart path - the only path that can carry them."""
        require(identity, "integration:view")
        require(identity, "patient:search")   # naming charts is choosing people
        require(identity, "patient:read")
        sel = selection()
        sc = scope()
        if sc.exclude_sensitive:
            notice_set(request, "Nothing to work per-chart while this scope excludes "
                                "heightened records.")
            return land("scope")
        found: list[str] = list(sel.patients)
        try:
            candidates = current_reader().search_patients("", limit=TAKE_SCAN_CAP) or []
        except Exception:
            candidates = []
        for row in candidates:
            ref = row.get("patient_reference") if isinstance(row, dict) else None
            if not ref or ref in found or len(found) >= SET_MAX:
                continue
            if chart_row(ref)["categories"]:
                found.append(ref)
        if not found:
            record(identity, "orchestration.run_refused",
                   "orch/scope no chart in this scope carries a heightened category",
                   sel.purpose)
            notice_set(request, "No chart in this scope carries a heightened category.")
            return land("scope")
        sel = normalise_selection(sel.sources, sel.targets, found, sel.purpose, current=sel)
        state().orch_selection_set(asdict(sel))
        sc = normalise_scope({"mode": "chart", "exclude_sensitive": sc.exclude_sensitive},
                             sel.purpose, by=identity.username, at=_now())
        state().orch_scope_set(sc.as_dict())
        record(identity, "integration.scope_changed",
               f"scope/chart heightened-charts={len(sel.patients)}", sel.purpose)
        n = len(sel.patients)
        notice_set(request, f"{n} chart{'' if n == 1 else 's'} carrying heightened categories "
                            f"{'is' if n == 1 else 'are'} now the scope. Record a disclosure "
                            "consent per category below, then execute — the records cross "
                            "on the next run.")
        flash_set(request, "preflight")
        return land("preflight")

    # ---- the run -----------------------------------------------------------

    def chart_resources(ref: str) -> list[dict]:
        rd = current_reader()
        out = []
        for r in (rd.resources_for_patient(ref) or [])[:CHART_RESOURCE_CAP]:
            key = r.get("storage_key") if isinstance(r, dict) else None
            if not key:
                continue
            try:
                res = rd.read_resource(key)
            except Exception:
                continue
            if isinstance(res, dict):
                out.append(res)
        return out

    def mover_for(identity: Identity):
        """The deployment's mover. Writing needs a target URL, a token and a
        verified identity map - the delivery service's job (core.fhir.delivery);
        this screen never guesses at one. Until a deployment wires that in,
        every allowed step is decided and not written, with the seam named."""
        return decided_not_written

    @app.post("/orchestration/execute", response_class=HTMLResponse)
    async def orchestration_execute(request: Request,
                                    identity: Identity = Depends(current_identity)):
        require(identity, "integration:export")
        sel = selection()
        sc = scope()
        if not selection_chosen(sel):
            notice_set(request, "Wire at least one source and one target before executing.")
            return land("systems")
        if sc.mode == "chart" and not sel.patients:
            notice_set(request, "Put a chart in the set, or choose a population, before executing.")
            flash_set(request, "scope")
            return land("scope")
        if sc.mode != "chart":
            require(identity, "patient:search")
        charts = charts_in_set(sel) if sc.mode == "chart" else []
        try:
            stored = int(current_reader().stats().total_resources)
        except Exception:
            stored = 0
        run_id = state().orch_run_next_id()
        started = _now()
        run = execute(run_id=run_id, sel=sel, scope=sc, charts=charts, systems=systems(),
                      consents=state().orch_consents, resources_for=chart_resources,
                      mover=mover_for(identity), population_stored=stored,
                      links=state().orch_links,
                      started_at=started, finished_at=_now(), by=identity.username)
        state().orch_run_add(run.as_dict())
        # Every step on the trail, before the run is reported: a refusal lands
        # as a refusal, a decided-not-written step as exactly that.
        for step in run.steps:
            record(identity, "orchestration.step",
                   f"orch/run/{run.id}/{step.seq} {step.direction} {step.system} "
                   f"{step.decision['decision']} moved={step.moved}", sel.purpose)
        record(identity, "orchestration.run",
               f"orch/run/{run.id} {run.status} moved={run.moved} withheld={run.withheld} "
               f"released={run.released}" + (" scope-excluded" if run.scope_excluded else ""),
               sel.purpose)
        return RedirectResponse(f"/orchestration/run/{run.id}", status_code=303)

    @app.get("/orchestration/run/{run_id}", response_class=HTMLResponse)
    def orchestration_run(request: Request, run_id: int,
                          identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        raw = state().orch_run_get(run_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="no such run")
        run = Run.from_dict(raw)
        sysmap = systems()
        findings = audit_run(run, state().orch_consents, sysmap)
        return page(request, "orch_run.html", identity, active="orchestration",
                    run=run, systems=sysmap, findings=findings)

    @app.post("/orchestration/exchange", response_class=HTMLResponse)
    async def orchestration_exchange(request: Request,
                                     identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        form = await request.form()
        action = form.get("action", "")
        s = state()
        if action == "save":
            sel = selection()
            name = str(form.get("name", "") or sel.name).strip()
            if not name or not selection_chosen(sel):
                notice_set(request, "Wire at least one source and one target, and name "
                                    "the exchange, before saving it.")
                return land("systems")
            sel = normalise_selection(sel.sources, sel.targets, sel.patients, sel.purpose,
                                      name=name, current=sel)
            s.orch_selection_set(asdict(sel))
            s.orch_exchange_save(name, {"selection": asdict(sel), "scope": scope().as_dict()},
                                 by=identity.username, at=_now())
            record(identity, "integration.exchange_saved", f"orch/exchange {name}", sel.purpose)
            flash_set(request, "scope")
            return land("scope")
        try:
            xid = int(form.get("id", "0"))
        except ValueError:
            xid = 0
        row = s.orch_exchange_get(xid)
        if action == "load" and row:
            v = row["value"]
            sel = Selection(**{k: (tuple(x) if isinstance(x, list) else x)
                               for k, x in (v.get("selection") or {}).items()
                               if k in Selection.__dataclass_fields__})
            sel = normalise_selection(sel.sources, sel.targets, sel.patients,
                                      purpose_or_current(identity, sel.purpose, sel.purpose),
                                      name=sel.name, delivery=sel.delivery, cadence=sel.cadence)
            s.orch_selection_set(asdict(sel))
            sc = Scope(**{k: x for k, x in (v.get("scope") or {}).items()
                          if k in Scope.__dataclass_fields__})
            s.orch_scope_set(sc.as_dict())
            record(identity, "integration.exchange_loaded", f"orch/exchange {row['name']}",
                   sel.purpose)
            flash_set(request, "scope")
            return land("scope")
        if action == "delete" and row and s.orch_exchange_delete(xid):
            record(identity, "integration.exchange_deleted", f"orch/exchange {row['name']}",
                   selection().purpose)
        return land("exchanges")
