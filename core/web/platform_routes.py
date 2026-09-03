# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Integration, System and Documentation surfaces.

The web screens over machinery the platform already has:

- Integration: Source & target EMR configuration, the bulk import
  manager (core/fhir/bulk_client.py, bulk_scheduler.py), streaming
  intake, and the bulk/streaming export managers (core/fhir/bulk_export,
  delivery/). Run state comes from core/web/platform_state.py.
- System: the Control panel - the model registry (every model, any
  kind, bring-your-own), the live switches, the full configuration
  editor, the role/permission matrix, and a live audit-chain check.
- Documentation: six sections under /docs, readable by every signed-in
  role. The diagram sections render static server-side SVG with SMIL
  animation - this interface runs under `script-src 'none'`, and SMIL
  needs no script.

Same discipline as every other surface: nav visibility and route
enforcement come from the same table, refusals are audited, and every
switch on the control panel lands on the audit trail under the
administrator's own name.
"""

from __future__ import annotations

import logging

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web import docs_content
from core.web.auth import PERMISSIONS, Identity, Role
from core.web.platform_state import (EMR_VENDORS, MODEL_KINDS, MODEL_SLOTS,
                                     PlatformState, _now)

log = logging.getLogger("phi-ai.web.platform")

# The role always dictates what a user can see: sections a role cannot
# use are not offered, and a direct URL refuses. None = every signed-in
# role.
_DOCS_SECTIONS = {
    "system": ("Detailed system documentation", None),
    "setup": ("Implementation & setup guide", None),
    "features": ("Feature documentation", None),
    "emr": ("EMR connections", "integration:view"),
    "emulators": ("Emulators & non-PHI setup", "admin:config"),
    "compliance": ("Compliance & responsibility", None),
    "attribution": ("Attribution", None),
}


def _docs_allowed(identity: Identity) -> dict:
    return {k: label for k, (label, perm) in _DOCS_SECTIONS.items()
            if perm is None or identity.can(perm)}


def register(app, page, require, current_identity, record, reader) -> None:

    def state() -> PlatformState:
        return app.state.platform_state

    # ================= Integration ===================================

    @app.get("/integration/emrconfig", response_class=HTMLResponse)
    def emrconfig(request: Request,
                  identity: Identity = Depends(current_identity)):
        require(identity, "admin:config")
        s = state()
        cfg = {k: s.config_get(k) for k in
               ("source_vendor", "source_base_url", "source_client_id",
                "source_group_id", "target_vendor", "target_base_url",
                "target_client_id")}
        return page(request, "emrconfig.html", identity, active="emrconfig",
                    cfg=cfg, vendors=EMR_VENDORS,
                    saved=request.query_params.get("saved"))

    @app.post("/integration/emrconfig", response_class=HTMLResponse)
    async def emrconfig_save(request: Request,
                             identity: Identity = Depends(current_identity)):
        require(identity, "admin:config")
        form = await request.form()
        s = state()
        changed = []
        for k in ("source_vendor", "source_base_url", "source_client_id",
                  "source_group_id", "target_vendor", "target_base_url",
                  "target_client_id"):
            if k not in form:
                continue
            v = str(form[k]).strip()[:300]
            if k.endswith("vendor") and v not in EMR_VENDORS:
                v = "epic"
            if s.config_set(k, v):
                changed.append(k)
        if changed:
            record(identity, "config.changed",
                   "integration/" + ",".join(changed), "operations")
        return RedirectResponse("/integration/emrconfig?saved=1", status_code=303)

    @app.get("/integration/bulk", response_class=HTMLResponse)
    def bulk_manager(request: Request,
                     identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        s = state()
        runs = sorted(s.bulk_runs, key=lambda r: r["started_at"], reverse=True)
        last_complete = next((r for r in runs if r["status"] == "complete"), None)
        return page(request, "bulkimport.html", identity, active="bulkimport",
                    runs=runs, last_complete=last_complete,
                    group_id=s.config_get("source_group_id", "eGrp-7ac2"),
                    can_run=_can_run(s))

    @app.post("/integration/bulk/run", response_class=HTMLResponse)
    def bulk_run(request: Request,
                 identity: Identity = Depends(current_identity)):
        require(identity, "admin:config")
        s = state()
        vendor = EMR_VENDORS.get(s.config_get("source_vendor", "epic"),
                                 EMR_VENDORS["epic"])["name"]
        group = s.config_get("source_group_id", "eGrp-7ac2")
        if not _can_run(s):
            s.add_bulk_run(started_at=_now(), finished_at=_now(),
                           source=vendor, group_id=group, status="refused",
                           resources=0,
                           note="Kickoff inside the vendor's 24-hour window — "
                                "refused rather than queued silently.")
            record(identity, "bulk.refused", f"bulk/{group}", "operations")
        else:
            total = _platform_resource_total()
            s.add_bulk_run(started_at=_now(), finished_at=_now(),
                           source=vendor, group_id=group, status="complete",
                           resources=total,
                           note="Simulated full re-extract: kickoff → status "
                                "poll → NDJSON download → encrypt-store-index. "
                                "Watermark advanced.")
            record(identity, "bulk.completed", f"bulk/{group}", "operations")
        return RedirectResponse("/integration/bulk", status_code=303)

    @app.get("/integration/bulk/{run_id}", response_class=HTMLResponse)
    def bulk_run_detail(request: Request, run_id: int,
                        identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        run = state().get_bulk_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return page(request, "import_run.html", identity, active="bulkimport",
                    run=run, stages=_import_stages(run))

    @app.get("/integration/streaming", response_class=HTMLResponse)
    def streaming(request: Request,
                  identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        parts = state().stream_partitions
        return page(request, "streaming.html", identity, active="streaming",
                    parts=parts,
                    totals={"events": sum(p["events_today"] for p in parts),
                            "partitions": len(parts),
                            "gaps": sum(1 for p in parts if p["gap_detected"])})

    @app.get("/integration/export", response_class=HTMLResponse)
    def export_manager(request: Request,
                       identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        s = state()
        runs = sorted(s.bulk_exports, key=lambda r: r["started_at"], reverse=True)
        last_complete = next((r for r in runs if r["status"] == "complete"), None)
        target = EMR_VENDORS.get(s.config_get("target_vendor", "epic"),
                                 EMR_VENDORS["epic"])["name"]
        return page(request, "bulkexport.html", identity, active="bulkexport",
                    runs=runs, last_complete=last_complete, target=target)

    @app.post("/integration/export/run", response_class=HTMLResponse)
    def export_run(request: Request,
                   identity: Identity = Depends(current_identity)):
        require(identity, "admin:config")
        s = state()
        tv = s.config_get("target_vendor", "epic")
        entry = EMR_VENDORS.get(tv, EMR_VENDORS["epic"])
        vendor = entry["name"]
        if not entry["writable_resources"]:
            # Derived from the target's PROFILE, never from a key literal:
            # a vendor whose profile records no writable resource type has
            # no write surface to deliver into, and the export is refused
            # with that vendor's own seam named.
            s.add_bulk_export(started_at=_now(), finished_at=_now(),
                              destination=f"Target EMR ({vendor})",
                              format="FHIR NDJSON", scope="full population",
                              status="refused", resources=0, withheld=0,
                              note=f"{vendor}'s profile records no writable "
                                   "resource type (its own documentation: "
                                   f"{entry['writes']}). The export is refused "
                                   "with the seam named, not degraded into "
                                   "thousands of individual writes nobody "
                                   "asked for.")
            record(identity, "export.refused", "export/target-emr", "operations")
        else:
            total = _platform_resource_total()
            withheld = max(1, total // 15)
            s.add_bulk_export(started_at=_now(), finished_at=_now(),
                              destination=f"Target EMR ({vendor})",
                              format="FHIR NDJSON", scope="full population",
                              status="complete", resources=total - withheld,
                              withheld=withheld,
                              note="Simulated delivery: identity-mapped, "
                                   "signature-gated writes only. Sensitive-"
                                   "category records excluded at export with "
                                   "counts recorded, never silently.")
            record(identity, "export.completed", "export/target-emr", "operations")
        return RedirectResponse("/integration/export", status_code=303)

    @app.get("/integration/export/{run_id}", response_class=HTMLResponse)
    def export_run_detail(request: Request, run_id: int,
                          identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        run = state().get_bulk_export(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no such export run")
        return page(request, "export_run.html", identity, active="bulkexport",
                    run=run)

    @app.get("/integration/streamexport", response_class=HTMLResponse)
    def streamexport(request: Request,
                     identity: Identity = Depends(current_identity)):
        require(identity, "integration:view")
        feeds = state().stream_exports
        return page(request, "streamexport.html", identity,
                    active="streamexport", feeds=feeds,
                    totals={"delivered": sum(f["delivered_today"] for f in feeds),
                            "held": sum(f["latest_seq"] - f["delivered_seq"]
                                        for f in feeds),
                            "feeds": len(feeds)})

    def _can_run(s: PlatformState) -> bool:
        last = s.last_real_run_at()
        # The window is a calendar-day approximation over the seeded
        # timestamps; a real deployment asks the bulk scheduler.
        return last is None or last < _now()[:10]

    def _import_stages(run: dict) -> list[tuple[str, str, str]]:
        s = run["status"]
        return [
            ("Kickoff — Group $export against the source EMR",
             "refused inside the 24-hour window" if s == "refused"
             else "accepted (202, status URL issued)",
             "warn" if s == "refused" else "good"),
            ("Status polling",
             "not reached" if s == "refused" else "manifest received",
             "mute" if s == "refused" else "good"),
            ("NDJSON download",
             "not reached" if s == "refused" else
             ("interrupted — run marked failed" if s == "failed"
              else "all files downloaded"),
             "mute" if s == "refused" else ("warn" if s == "failed" else "good")),
            ("Encrypt · store · index",
             f"{run['resources']:,} records stored" if s == "complete"
             else "not completed",
             "good" if s == "complete" else "mute"),
            ("Watermark",
             "advanced" if s == "complete" else "held",
             "good" if s == "complete" else "warn"),
        ]

    def _platform_resource_total() -> int:
        try:
            return int(getattr(reader.stats(), "resource_count", 0)) or 27961
        except Exception:
            return 27961

    # ================= System: the control panel =====================

    @app.get("/system/control", response_class=HTMLResponse)
    def control_panel(request: Request,
                      identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        s = state()
        try:
            ok, checked, first_bad = reader.verify_audit_chain()
        except Exception as exc:
            ok, checked, first_bad = False, 0, f"verification unavailable: {exc}"
        role_matrix = [
            {"role": role.value,
             "permissions": sorted(PERMISSIONS.get(role, frozenset()))}
            for role in Role
        ]
        active_model = s.config_get("assistant_model") or "claude-sonnet-5"
        slot_overrides = {slot: s.config_get("active_model_" + slot, "")
                          for slot in MODEL_SLOTS if slot != "assistant"}
        return page(request, "controlpanel.html", identity,
                    active="controlpanel",
                    chain={"ok": ok, "checked": checked, "bad": first_bad},
                    models=s.list_models(), active_model=active_model,
                    slot_overrides=slot_overrides,
                    model_kinds=MODEL_KINDS, model_slots=MODEL_SLOTS,
                    rag_enabled=s.config_get("rag_enabled", "on") == "on",
                    assistant_live=s.config_get("assistant_live", "on") == "on",
                    assistant_available=getattr(app.state, "assistant", None)
                    is not None,
                    feeds=s.stream_exports,
                    hold_active=not _can_run(s),
                    role_matrix=role_matrix,
                    personas=app.state.dev_personas or [],
                    cfg={k: s.config_get(k) for k in
                         ("source_vendor", "source_base_url",
                          "source_client_id", "source_group_id",
                          "target_vendor", "target_base_url",
                          "target_client_id", "assistant_max_tokens")},
                    vendors=EMR_VENDORS)

    @app.post("/system/control/switch", response_class=HTMLResponse)
    def control_switch(request: Request,
                       name: str = Form(...),
                       identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        s = state()
        if name in ("rag_enabled", "assistant_live"):
            now = "off" if s.config_get(name, "on") == "on" else "on"
            s.config_set(name, now)
            record(identity, f"system.{name}_{now}", "controlpanel/" + name,
                   "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/model/add", response_class=HTMLResponse)
    async def model_add(request: Request,
                        identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        form = await request.form()
        row = state().register_model(
            name=str(form.get("name", "")), kind=str(form.get("kind", "")),
            slot=str(form.get("slot", "")),
            provider=str(form.get("provider", "")),
            model_id=str(form.get("model_id", "")),
            version=str(form.get("version", "")),
            endpoint_url=str(form.get("endpoint_url", "")),
            purpose=str(form.get("purpose", "")),
            registered_by=identity.username)
        if row:
            record(identity, "system.model_registered",
                   f"model/{row['id']} {row['name'][:60]}", "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/model/state", response_class=HTMLResponse)
    def model_state(request: Request, model_id: int = Form(...),
                    op: str = Form(...),
                    identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        status = "disabled" if op == "disable" else "enabled"
        if state().set_model_status(model_id, status):
            record(identity, f"system.model_{status}", f"model/{model_id}",
                   "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/model/activate", response_class=HTMLResponse)
    def model_activate(request: Request, model_id: int = Form(...),
                       identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        s = state()
        m = s.get_model(model_id)
        if m and (m["status"] == "enabled" or m["builtin"]):
            if m["slot"] == "assistant":
                if m["kind"] == "foundation" and m["model_id"]:
                    s.config_set("assistant_model",
                                 "" if m["builtin"] else m["model_id"])
                    record(identity, "system.model_activated",
                           f"model/{model_id} {m['model_id']}", "operations")
            else:
                s.config_set("active_model_" + m["slot"],
                             "" if m["builtin"] else str(m["id"]))
                record(identity, "system.model_activated",
                       f"model/{model_id} slot={m['slot']}", "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/model/delete", response_class=HTMLResponse)
    def model_delete(request: Request, model_id: int = Form(...),
                     identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        s = state()
        m = s.get_model(model_id)
        if m and not m["builtin"]:
            if m["model_id"] and s.config_get("assistant_model") == m["model_id"]:
                s.config_set("assistant_model", "")
            if s.config_get("active_model_" + m["slot"]) == str(m["id"]):
                s.config_set("active_model_" + m["slot"], "")
            if s.delete_model(model_id):
                record(identity, "system.model_removed", f"model/{model_id}",
                       "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/feed", response_class=HTMLResponse)
    def feed_toggle(request: Request, feed_id: int = Form(...),
                    identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        s = state()
        feed = next((f for f in s.stream_exports if f["id"] == feed_id), None)
        if feed and feed["state"] in ("healthy", "paused"):
            new = "healthy" if feed["state"] == "paused" else "paused"
            note = ("Paused by the System Administrator from the Control "
                    "panel. A paused feed accumulates sequence, it does not "
                    "leak.") if new == "paused" else ""
            s.set_feed_state(feed_id, new, note)
            record(identity,
                   "system.feed_" + ("paused" if new == "paused" else "resumed"),
                   f"streamexport/{feed_id}", "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/hold", response_class=HTMLResponse)
    def hold_release(request: Request,
                     identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        state().release_bulk_hold()
        record(identity, "system.hold_released", "controlpanel/bulk-hold",
               "operations")
        return RedirectResponse("/system/control", status_code=303)

    @app.post("/system/control/config", response_class=HTMLResponse)
    async def config_save(request: Request,
                          identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        form = await request.form()
        s = state()
        changed = []
        for k in ("source_vendor", "source_base_url", "source_client_id",
                  "source_group_id", "target_vendor", "target_base_url",
                  "target_client_id"):
            if k not in form:
                continue
            v = str(form[k]).strip()[:300]
            if k.endswith("vendor") and v not in EMR_VENDORS:
                v = "epic"
            if s.config_set(k, v):
                changed.append(k)
        if "assistant_max_tokens" in form:
            try:
                v = str(max(256, min(4096, int(str(form["assistant_max_tokens"])))))
            except ValueError:
                v = "1500"
            if s.config_set("assistant_max_tokens", v):
                changed.append("assistant_max_tokens")
        if changed:
            record(identity, "config.changed",
                   "controlpanel/" + ",".join(changed), "operations")
        return RedirectResponse("/system/control", status_code=303)

    # ================= Documentation =================================

    @app.get("/docs", response_class=HTMLResponse)
    def docs_index(request: Request,
                   identity: Identity = Depends(current_identity)):
        return RedirectResponse("/docs/system", status_code=303)

    @app.get("/docs/{section}", response_class=HTMLResponse)
    def docs_section(request: Request, section: str,
                     identity: Identity = Depends(current_identity)):
        if section in ("api", "architecture", "dataflows"):
            # Compiled into the system documentation; old links land on
            # the corresponding part of the merged page.
            return RedirectResponse(f"/docs/system#part-{section}",
                                    status_code=301)
        if section not in _DOCS_SECTIONS:
            raise HTTPException(status_code=404, detail="no such section")
        label, perm = _DOCS_SECTIONS[section]
        if perm is not None:
            require(identity, perm)
        diagrams = {}
        if section == "system":
            diagrams = docs_content.dataflow_svgs()
            diagrams["arch"] = docs_content.architecture_svg()
        # Every vendor table, count and port range the documentation pages
        # print is rendered from the registries - PROFILES (via
        # EMR_VENDORS), the emulator VENDORS and DEFAULT_PORTS - never
        # typed into a template, so a profile added to the platform shows
        # up on its own docs pages without anyone remembering to add a row.
        from emulators.vendors import DEFAULT_PORTS, VENDORS

        ports = sorted(DEFAULT_PORTS.values())
        contiguous = ports and ports[-1] - ports[0] + 1 == len(ports)
        emulator_rows = [
            {"key": key, "name": VENDORS[key].name, "port": DEFAULT_PORTS[key],
             "fhir_path": VENDORS[key].fhir_path,
             "grants": " or ".join(g for g, ok in (
                 ("JWT assertion", VENDORS[key].accepts_jwt_assertion),
                 ("client secret", VENDORS[key].accepts_client_secret)) if ok),
             "algorithms": "/".join(VENDORS[key].assertion_algorithms)
                           if VENDORS[key].accepts_jwt_assertion else "",
             "scopes_required": VENDORS[key].requires_token_scope,
             "wildcards_refused": VENDORS[key].refuses_wildcard_scope,
             "export": VENDORS[key].supports_bulk_export,
             "creatable": ", ".join(VENDORS[key].creatable) or "nothing"}
            for key in sorted(VENDORS, key=DEFAULT_PORTS.__getitem__)
        ]
        return page(request, f"docs_{section}.html", identity, active=None,
                    docs_sections=_docs_allowed(identity), docs_active=section,
                    diagrams=diagrams, screen_ref="DOCS",
                    screen_title=label,
                    vendors=EMR_VENDORS, vendor_keys=", ".join(sorted(EMR_VENDORS)),
                    vendor_names=", ".join(v["name"] for v in EMR_VENDORS.values()),
                    emulator_rows=emulator_rows, emulator_count=len(VENDORS),
                    emulator_port_range=(f"{ports[0]}–{ports[-1]}" if contiguous
                                         else ", ".join(str(p) for p in ports)),
                    first_emulator_port=ports[0] if ports else None)
# Made by Ryan Gomez & Co. Inc.
