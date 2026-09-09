# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Model monitoring: the third System screen, and the one nav.py asked for.

core/web/nav.py carried this comment beside the System group:

    The demonstration lists Model monitoring between the two; it has no
    platform route yet, and an entry with no route is a bug in this
    table, so it joins when it exists.

It exists now. That comment was the codebase naming its own gap honestly
and then leaving it, which is the pattern this whole pass has been about.

AN INSTRUMENT PANEL, NOT A REPORT. It is read at a glance, often on a
second screen, and it reuses the `.cx` dark scope the Components screen
already defines - the palette whose contrast was measured rather than
eyeballed. A second copy of those colours is how the two System screens
drift apart.

THE CONDITION IS COMPUTED FROM WHAT IS MISSING, NOT ONLY FROM WHAT IS
FAILING. A deployment with no telemetry connection and no drift history
reads UNKNOWN, never NOMINAL. Green because nothing was checked is the
single worst state an instrument panel can show, and it is the default
state of every monitoring screen that computes its condition from an
empty result set.
"""

from __future__ import annotations

import logging

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from core.web.auth import Identity

log = logging.getLogger("phi-ai.web.monitoring")

#: Read at a glance, so the vocabulary is short and ordered.
NOMINAL, ATTENTION, WARNING, UNKNOWN = "nominal", "attention", "warning", "unknown"

CONDITION_LABEL = {
    NOMINAL: "ALL SYSTEMS NOMINAL",
    ATTENTION: "ATTENTION",
    WARNING: "WARNING",
    UNKNOWN: "UNKNOWN — NOT CHECKED",
}


def assess(*, telemetry_configured: bool, drift_runs: list[dict],
           models: list[dict]) -> tuple[str, list[str]]:
    """The fleet condition and the reasons for it.

    Pure, and separated from the route on purpose: the interesting
    behaviour here is what makes a deployment NOT green, and that is worth
    testing without standing up an application.
    """
    reasons: list[str] = []

    if not telemetry_configured:
        reasons.append(
            "assistant telemetry is not configured, so no usage or drift "
            "history is being recorded"
        )
    if not drift_runs:
        reasons.append("no drift probe has ever run against this deployment")
    if not models:
        reasons.append("no model is registered in the platform's own registry")

    # UNKNOWN OUTRANKS EVERYTHING. If the instruments are not connected,
    # what they would have shown is not a finding.
    if not telemetry_configured or not drift_runs:
        return UNKNOWN, reasons

    failed = [r for r in drift_runs if (r.get("probes") or 0) > (r.get("passed") or 0)]
    if failed:
        latest = failed[0]
        reasons.append(
            f"{len(failed)} of the last {len(drift_runs)} drift runs had failing "
            f"probes; most recent on {latest.get('model') or 'an unnamed model'}"
            + (f": {latest['failed_probes']}" if latest.get("failed_probes") else "")
        )
        return (WARNING if len(failed) > 1 else ATTENTION), reasons

    if not models:
        return ATTENTION, reasons
    return NOMINAL, reasons


def register(app, page, require, current_identity, record, reader) -> None:
    """Attach Model monitoring. Registered with the other System screens."""

    @app.get("/system/models", response_class=HTMLResponse)
    def model_monitoring(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        require(identity, "system:admin")

        state = getattr(app.state, "platform_state", None)
        models = state.list_models() if state is not None else []

        rt = getattr(app.state, "assistant", None)
        ops = getattr(rt, "ops_connection", None) if rt is not None else None

        usage = None
        drift_runs: list[dict] = []
        error = None
        if ops is not None:
            from core.assistant import telemetry as assistant_telemetry

            try:
                conn = ops()
                try:
                    usage = assistant_telemetry.usage_summary(conn, days=30)
                    drift_runs = assistant_telemetry.drift_summary(conn)
                finally:
                    conn.close()
            except Exception as exc:      # an unreachable instrument is not a green one
                log.error("model monitoring telemetry read failed: %s", exc)
                error = f"Could not read telemetry: {exc}"

        condition, reasons = assess(
            telemetry_configured=ops is not None and error is None,
            drift_runs=drift_runs,
            models=models,
        )
        record(identity, "monitoring.viewed", f"fleet:{condition}", "operations")

        settings = rt.effective_settings() if rt is not None else None
        return page(request, "modelmonitor.html", identity, active="modelmonitor",
                    condition=condition, condition_label=CONDITION_LABEL[condition],
                    reasons=reasons, models=models, usage=usage,
                    drift_runs=drift_runs, error=error,
                    active_model=(settings.resolved_model if settings else None),
                    provider=(settings.provider if settings else None))
# Made by Ryan Gomez & Co. Inc.
