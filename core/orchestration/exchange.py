# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""An exchange: which systems read, which are delivered to, under what
purpose, and how it delivers. Saved by name so it comes back whole rather
than being rebuilt from memory.

Saved, not scheduled. The platform's bulk scheduler is the thing that runs
on a clock; a saved exchange is a configuration you can reload and
execute, and the screen says so rather than implying a delivery happened
while nothing was watching the clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.orchestration.scope import Scope
from core.orchestration.systems import system_keys

NAME_MAX = 60
#: How many charts the chosen set holds. Small on purpose: the set exists to
#: show the per-chart mechanism on real charts, not to pretend a population
#: can be consented one click at a time.
SET_MAX = 3

DELIVERY_MODES: dict[str, dict[str, str]] = {
    "once":     {"label": "One-off",
                 "blurb": "Runs when you execute it, and finishes."},
    "schedule": {"label": "On a cadence",
                 "blurb": "Recorded on the exchange and reported here; every firing "
                          "re-runs preflight, so consent withdrawn between two runs is "
                          "honoured by the second."},
    "stream":   {"label": "As records appear",
                 "blurb": "Records cross as they appear at the source."},
}
CADENCES: dict[str, dict[str, str]] = {
    "15m": {"label": "every 15 minutes"},
    "1h":  {"label": "hourly"},
    "24h": {"label": "daily"},
    "7d":  {"label": "weekly"},
}


@dataclass
class Selection:
    sources: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    patients: tuple[str, ...] = ()      # platform references: 'Patient/eAB12cd3'
    purpose: str = "treatment"
    name: str = ""
    delivery: str = "once"
    cadence: str = "24h"


@dataclass
class Exchange(Selection):
    """A named, saved selection, with the scope it was saved under."""
    scope: Optional[Scope] = None
    saved_by: str = ""
    saved_at: str = ""


def normalise_selection(sources, targets, patients, purpose: str, *,
                        name: Optional[str] = None, delivery: Optional[str] = None,
                        cadence: Optional[str] = None,
                        current: Optional[Selection] = None) -> Selection:
    """Keys that name profiled systems, in posted order; a chosen set capped
    at SET_MAX; a delivery mode and cadence that exist. `current` supplies
    whatever the caller did not post, so a lane change never blanks the name."""
    cur = current or Selection()
    pids: list[str] = []
    for p in (patients or ()):
        v = str(p or "").strip()
        if v.startswith("Patient/") and v not in pids and len(pids) < SET_MAX:
            pids.append(v)
    d = delivery if delivery in DELIVERY_MODES else cur.delivery
    c = cadence if cadence in CADENCES else cur.cadence
    n = cur.name if name is None else str(name).strip()[:NAME_MAX]
    return Selection(sources=system_keys(sources), targets=system_keys(targets),
                     patients=tuple(pids), purpose=purpose, name=n,
                     delivery=d if d in DELIVERY_MODES else "once",
                     cadence=c if c in CADENCES else "24h")


def selection_chosen(sel: Selection) -> bool:
    """Has the operator wired anything at all? A default is not a choice."""
    return bool(sel.sources) and bool(sel.targets)
