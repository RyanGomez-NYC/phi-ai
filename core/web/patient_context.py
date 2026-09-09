# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The patient in context, carried across screens.

WHY THIS EXISTS. The demonstration has had sticky patient context since
the beginning: open a chart and every screen that has a patient dimension
follows it, while the screens that do not say so in as many words. The
platform had no such thing. `/patients/{id}/open` rendered one chart and
that was the end of it; every screen after that started from nobody, and
a clinician working one patient re-picked them on each screen. That is
the single largest behavioural gap between the two, and every capability
screen depends on closing it - a prior-auth packet, a set of patient
instructions and a chart summary are all *about someone*.

WHAT IS AND IS NOT IN HERE. The patient REFERENCE and a display label,
and nothing else. Not the chart, not a resource, not a name pulled from a
record - the reference is the EMR's own opaque server-assigned id, which
core/web/app.py's module docstring already establishes is not a
real-world identifier and already appears in every storage key. The label
is whatever the search row showed the person who chose it, so the top bar
can say who they are looking at without a second read.

THE PURPOSE OF USE IS DELIBERATELY NOT STICKY. Context says WHO; the
purpose says WHY, and it attaches to a specific act of reading, asserted
at the moment of that act. A purpose that persisted across screens would
let a treatment assertion made on a chart silently justify an operations
read three screens later - which is precisely the accounting failure the
audit trail exists to prevent. Each screen asks again.

IT LIVES IN THE SIGNED SESSION COOKIE, like the launch context beside it,
so it expires on the same clock as the identity that set it and cannot be
set by anything but this application.
"""

from __future__ import annotations

from typing import Optional

#: Session key. Named for the demo's own `ctx_patient` so the two systems
#: are recognisably the same feature when read side by side.
CONTEXT_KEY = "ctx_patient"


def set_patient(session, reference: str, label: str = "") -> None:
    """Put a patient in context. `reference` is 'Patient/<id>'."""
    reference = (reference or "").strip()
    if not reference:
        clear_patient(session)
        return
    session[CONTEXT_KEY] = {
        "reference": reference,
        "label": (label or reference.split("/", 1)[-1]).strip()[:120],
    }


def clear_patient(session) -> None:
    session.pop(CONTEXT_KEY, None)


def patient_in_context(session) -> Optional[dict]:
    """The patient in context as {'reference', 'label'}, or None.

    Tolerates a malformed or half-written entry by treating it as no
    context at all. A screen that believes it has a patient when it does
    not is worse than one that knows it has none: the first renders
    somebody else's data under a familiar name, the second asks.
    """
    raw = (session or {}).get(CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    reference = str(raw.get("reference") or "").strip()
    if not reference.startswith("Patient/") or len(reference) <= len("Patient/"):
        return None
    return {"reference": reference, "label": str(raw.get("label") or "").strip()
            or reference.split("/", 1)[-1]}


def reference_in_context(session) -> Optional[str]:
    ctx = patient_in_context(session)
    return ctx["reference"] if ctx else None


#: What a screen with no patient dimension says, so it says something
#: rather than silently ignoring the context. The demo's `_ctx_none_note`
#: partial is the same sentence.
NO_PATIENT_DIMENSION = (
    "This screen has no patient dimension - it reports across the whole "
    "store, and the patient in context does not narrow it."
)
# Made by Ryan Gomez & Co. Inc.
