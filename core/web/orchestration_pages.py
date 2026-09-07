# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The rest of the Integration group: the patient link set, the
practitioner crosswalk, the permission lattice, what each system holds,
and the connected-systems catalogue.

Each is a view onto state the Orchestration screen already keeps - the
selection, the scope, the consents, the runs - plus two stores of its own
(links, crosswalk) that the run consults. None of them decides anything
the run would not: the lattice calls the one decision function, holdings
reads the reader and the ledger, the catalogue reads the profiles.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.orchestration import (Scope, Selection, categories_for_chart, systems)
from core.orchestration.holdings import delivered_holdings, store_holdings
from core.orchestration.lattice import lattice
from core.orchestration.links import LinkRefusal
from core.web.auth import Identity, role_allowed_purposes
from core.web.platform_state import EMR_VENDORS, PlatformState, _now


def register(app, page, require, current_identity, record, reader) -> None:

    def state() -> PlatformState:
        return app.state.platform_state

    def current_reader():
        return getattr(app.state, "reader", reader)

    def selection() -> Selection:
        raw = state().orch_selection_get()
        return Selection(**{k: (tuple(v) if isinstance(v, list) else v)
                            for k, v in raw.items() if k in Selection.__dataclass_fields__})

    def scope() -> Scope:
        raw = state().orch_scope_get()
        return Scope(**{k: v for k, v in raw.items() if k in Scope.__dataclass_fields__})

    def chart_row(ref: str) -> dict:
        rd = current_reader()
        resources, name = [], ref
        try:
            rows = rd.resources_for_patient(ref) or []
        except Exception:
            rows = []
        for r in rows[:200]:
            key = r.get("storage_key") if isinstance(r, dict) else None
            if not key:
                continue
            try:
                res = rd.read_resource(key)
            except Exception:
                continue
            if isinstance(res, dict):
                resources.append(res)
                if res.get("resourceType") == "Patient" and name == ref:
                    nm = (res.get("name") or [{}])[0] or {}
                    label = " ".join(p for p in [" ".join(nm.get("given") or []),
                                                 nm.get("family") or ""] if p).strip()
                    name = label or ref
        return {"ref": ref, "name": name, "categories": categories_for_chart(resources)}

    def charts_in_set(sel: Selection, identity: Identity) -> list[dict]:
        if not identity.can("patient:read"):
            return []
        return [chart_row(ref) for ref in sel.patients]

    def notice_set(request: Request, text: str) -> None:
        if "session" in request.scope:
            request.session["orch_notice"] = text

    def notice_take(request: Request) -> str:
        if "session" in request.scope:
            return str(request.session.pop("orch_notice", "") or "")
        return ""

    # ---- patient link set ------------------------------------------------

    @app.get("/orchestration/links", response_class=HTMLResponse)
    def links_screen(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        sel = selection()
        charts = charts_in_set(sel, identity)
        st = state().orch_links
        names = {c["ref"]: c["name"] for c in charts}
        rows = [{"link": l, "name": names.get(l.patient, l.patient)} for l in st.all()]
        return page(request, "orch_links.html", identity, active="orch_links",
                    sel=sel, charts=charts, systems=systems(), rows=rows,
                    notice=notice_take(request),
                    can_write=identity.can("document:ingest") and identity.can("patient:read"))

    @app.post("/orchestration/links", response_class=HTMLResponse)
    async def links_action(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        # Naming a person on another system is records work: the same gate
        # as ingesting a document about them, plus reading the chart.
        require(identity, "document:ingest")
        require(identity, "patient:read")
        form = await request.form()
        action = str(form.get("action", ""))
        sel = selection()
        s = state()
        note = str(form.get("note", "") or "")[:200]
        try:
            if action == "add":
                patient = str(form.get("patient", "") or "")
                system = str(form.get("system", "") or "")
                if patient not in sel.patients:
                    raise LinkRefusal("the chart is not in the chosen set")
                if system not in systems():
                    raise LinkRefusal("no such system")
                link = s.orch_link_add(patient, system, str(form.get("system_id", "") or ""),
                                       by=identity.username, at=_now(), note=note)
                record(identity, "identity.link_added", f"links/{patient}/{system}",
                       sel.purpose)
            elif action == "verify":
                link = s.orch_link_verify(int(form.get("link_id", 0)), by=identity.username,
                                          at=_now(), note=note)
                record(identity, "identity.link_verified", f"links/{link.patient}/{link.system}",
                       sel.purpose)
            elif action == "reject":
                link = s.orch_link_reject(int(form.get("link_id", 0)), note=note)
                record(identity, "identity.link_rejected", f"links/{link.patient}/{link.system}",
                       sel.purpose)
            elif action == "revoke":
                link = s.orch_link_revoke(str(form.get("patient", "")), str(form.get("system", "")),
                                          by=identity.username, at=_now(), note=note)
                record(identity, "identity.link_revoked", f"links/{link.patient}/{link.system}",
                       sel.purpose)
            else:
                raise LinkRefusal("no such action")
        except (LinkRefusal, ValueError) as exc:
            record(identity, "identity.link_refused", f"links/{action}", sel.purpose)
            notice_set(request, f"Refused: {exc}")
        return RedirectResponse("/orchestration/links", status_code=303)

    # ---- practitioner crosswalk -----------------------------------------

    @app.get("/orchestration/crosswalk", response_class=HTMLResponse)
    def crosswalk_screen(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        st = state().orch_crosswalk
        users = sorted({identity.username, *[r.user for r in st.all()]})
        return page(request, "orch_crosswalk.html", identity, active="orch_crosswalk",
                    rows=st.all(), users=users, systems=systems(), sel=selection(),
                    notice=notice_take(request), can_write=identity.can("admin:config"))

    @app.post("/orchestration/crosswalk", response_class=HTMLResponse)
    async def crosswalk_action(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        require(identity, "admin:config")
        form = await request.form()
        action = str(form.get("action", ""))
        user = str(form.get("user", "") or "").strip()[:40]
        system = str(form.get("system", "") or "")
        s = state()
        try:
            if system not in systems():
                raise LinkRefusal("no such system")
            if action == "set":
                s.orch_crosswalk_set(user, system, str(form.get("practitioner_id", "") or ""),
                                     by=identity.username, at=_now(),
                                     note=str(form.get("note", "") or "")[:200])
                record(identity, "identity.crosswalk_set", f"crosswalk/{user}/{system}", "operations")
            elif action == "clear":
                if not s.orch_crosswalk_clear(user, system):
                    raise LinkRefusal(f"no crosswalk row for {user} on {system}")
                record(identity, "identity.crosswalk_cleared", f"crosswalk/{user}/{system}", "operations")
            else:
                raise LinkRefusal("no such action")
        except LinkRefusal as exc:
            record(identity, "identity.link_refused", f"crosswalk/{user}/{system}", "operations")
            notice_set(request, f"Refused: {exc}")
        return RedirectResponse("/orchestration/crosswalk", status_code=303)

    # ---- permission lattice ---------------------------------------------

    @app.get("/orchestration/lattice", response_class=HTMLResponse)
    def lattice_screen(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        sel = selection()
        sc = scope()
        charts = charts_in_set(sel, identity)
        purposes = [c for c, _ in role_allowed_purposes(identity)]
        rows = lattice(sel=sel, scope=sc, charts=charts, consents=state().orch_consents,
                       links=state().orch_links, systems=systems(), purposes=purposes)
        return page(request, "orch_lattice.html", identity, active="orch_lattice",
                    sel=sel, scope=sc, charts=charts, purposes=purposes, rows=rows,
                    wired=bool(sel.sources and sel.targets))

    # ---- what each system holds -----------------------------------------

    @app.get("/orchestration/holdings", response_class=HTMLResponse)
    def holdings_screen(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        sel = selection()
        charts = charts_in_set(sel, identity)
        store = store_holdings(current_reader(), charts)
        delivered = delivered_holdings(state().orch_runs)
        return page(request, "orch_holdings.html", identity, active="orch_holdings",
                    sel=sel, store=store, delivered=delivered, systems=systems())

    # ---- connected systems ----------------------------------------------

    @app.get("/orchestration/systems", response_class=HTMLResponse)
    def systems_screen(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        s = state()
        cfg = {k: s.config_get(k) for k in ("source_vendor", "source_base_url",
                                            "target_vendor", "target_base_url")}
        sel = selection()
        rows = []
        for key, sys_ in systems().items():
            roles = []
            if cfg["source_vendor"] == key:
                roles.append("configured source" + (f" — {cfg['source_base_url']}" if cfg["source_base_url"] else " (no base URL yet)"))
            if cfg["target_vendor"] == key:
                roles.append("configured target" + (f" — {cfg['target_base_url']}" if cfg["target_base_url"] else " (no base URL yet)"))
            if key in sel.sources:
                roles.append("wired as a source")
            if key in sel.targets:
                roles.append("wired as a target")
            rows.append({"system": sys_, "roles": roles,
                         "writes": EMR_VENDORS.get(key, {}).get("writes", "")})
        return page(request, "orch_systems.html", identity, active="orch_systems",
                    rows=rows, cfg=cfg, can_config=identity.can("admin:config"))
