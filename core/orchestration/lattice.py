# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The permission lattice: the decision, opened up.

For every system in the exchange and every purpose this operator may
assert, what would the engine decide - and why. Sources are decided on
whether a population read can be scheduled from them at all; targets go
through decide_delivery(), the same function the preflight and the run
call, with the chart in scope, its heightened categories, the consents on
file and the verified identifier at that target. Nothing here is a second
opinion: it is the one decision, shown across every purpose at once, so a
reader can see which purpose would open which gate.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from core.orchestration.consents import ConsentStore
from core.orchestration.decide import Decision, decide_delivery
from core.orchestration.exchange import Selection
from core.orchestration.links import LinkStore
from core.orchestration.scope import Scope, purpose_allows_population
from core.orchestration.systems import System


def source_decision(system: System, scope: Scope, purpose: str) -> Decision:
    """A read from a source: population reads need Bulk Data $export at the
    vendor and a purpose that may work over a population; a chart read
    needs neither."""
    if scope.mode != "chart":
        if not purpose_allows_population(purpose):
            return Decision("refuse", "purpose", f"purpose {purpose} does not work over a "
                            "population; the read collapses to one chart",
                            fix="assert treatment, payment, operations or research")
        if not system.supports_bulk_export:
            return Decision("refuse", "profile", f"{system.name} advertises no Bulk Data "
                            "$export; a population read from it cannot be scheduled and the "
                            "bulk scheduler refuses rather than degrading",
                            fix="read from it one chart at a time, or choose a source that exports")
    return Decision("allow", "profile", "read into the PHI AI store - the store is this run's "
                    "destination, never its scope")


def lattice(*, sel: Selection, scope: Scope, charts: list[dict], consents: ConsentStore,
            links: LinkStore, systems: Mapping[str, System],
            purposes: Iterable[str]) -> list[dict]:
    """Rows of {system, role, purpose, chart, decision}: every system in the
    selection x every purpose, and for targets on a chart scope, every chart
    in the set."""
    rows: list[dict] = []
    purposes = list(purposes)
    for key in sel.sources:
        s = systems.get(key)
        if s is None:
            continue
        for p in purposes:
            rows.append({"system": s, "role": "source", "purpose": p, "chart": None,
                         "decision": source_decision(s, scope, p)})
    for key in sel.targets:
        t = systems.get(key)
        if t is None:
            continue
        for p in purposes:
            if scope.mode != "chart" or not charts:
                rows.append({"system": t, "role": "target", "purpose": p, "chart": None,
                             "decision": decide_delivery(t, patient=None, held={}, purpose=p,
                                                         consents=consents, scope=scope)})
                continue
            for ch in charts:
                linked = links.verified_for(ch["ref"], key) is not None
                rows.append({"system": t, "role": "target", "purpose": p, "chart": ch,
                             "decision": decide_delivery(t, patient=ch["ref"],
                                                         held=ch["categories"], purpose=p,
                                                         consents=consents, scope=scope,
                                                         who=ch["name"], link_verified=linked)})
    return rows
