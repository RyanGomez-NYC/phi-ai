# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""A run: every delivery in the exchange, decided by decide_delivery() and
then performed - or, where this deployment has nothing to write to yet,
recorded as decided and not written, with the seam named.

Deciding and writing are separate on purpose. The decision is the
platform's; the write is a configured destination's - a target URL, a
token, a verified identity map (core.fhir.delivery). A run whose steps were
decided but not written says so in its status and on every step, rather
than reporting a delivery that did not happen: the platform's export
manager takes the same line, and it is the only honest one.

Accounting is the demonstration's, tile for tile: what the store holds for
the scope, what was delivered, what was withheld as heightened, what was
released under a consent - and each zero explains itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Mapping, Optional

from core.orchestration.consents import ConsentStore
from core.orchestration.decide import Decision, decide_delivery
from core.orchestration.exchange import Selection
from core.orchestration.heightened import classify_by_labels
from core.orchestration.scope import Scope
from core.orchestration.systems import System


@dataclass
class MoveResult:
    """What a mover did with the resources it was offered."""
    moved: int = 0
    skipped: int = 0
    written: bool = False           # did anything reach a destination?
    note: str = ""
    sent_types: dict[str, int] = field(default_factory=dict)


#: (target, chart-or-None for a population write, resources to offer,
#: released categories) -> MoveResult. The routes hand in the deployment's
#: mover; tests hand in one that counts.
Mover = Callable[[System, Optional[dict], list[dict], Mapping[str, int]], MoveResult]


def decided_not_written(target: System, chart: Optional[dict], resources: list[dict],
                        released: Mapping[str, int]) -> MoveResult:
    """The mover for a deployment with nothing to write to. Every record it is
    offered is skipped with the seam named; nothing is pretended."""
    return MoveResult(moved=0, skipped=len(resources), written=False,
                      note=f"decided, not written: no delivery destination is configured for "
                           f"{target.name} on this deployment. Deliveries are performed by the "
                           "delivery service (core.fhir.delivery) against a configured target "
                           "URL with a verified identity map; this run records what it would "
                           "carry.")


@dataclass
class Step:
    seq: int
    system: str
    system_name: str
    direction: str                  # 'read' | 'deliver'
    patient: Optional[str]          # reference, or None for a population step
    who: str
    decision: dict                  # Decision as a dict (persisted as JSON)
    offered: int = 0
    moved: int = 0
    skipped: int = 0
    written: bool = False
    note: str = ""


@dataclass
class Run:
    id: int
    started_at: str
    finished_at: str
    status: str                     # 'complete' | 'decided' | 'partial' | 'refused'
    purpose: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    scope: dict
    population: bool
    scope_excluded: bool
    scope_heightened: int           # heightened records the scope's charts carry (chart mode)
    charts: int
    stored: int                     # records the store holds for this scope
    delivered: dict[str, int] = field(default_factory=dict)     # target -> moved
    by_type: dict[str, dict] = field(default_factory=dict)      # type -> {'stored': n, 'delivered': {target: n}}
    allowed: int = 0
    refused: int = 0
    withheld: int = 0
    released: int = 0
    skipped_heightened: int = 0
    moved: int = 0
    written: bool = False
    steps: list[Step] = field(default_factory=list)
    by: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = list(self.sources)
        d["targets"] = list(self.targets)
        return d

    @classmethod
    def from_dict(cls, d: Mapping) -> "Run":
        steps = [Step(**s) for s in d.get("steps", [])]
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "steps"}
        fields["sources"] = tuple(fields.get("sources", ()))
        fields["targets"] = tuple(fields.get("targets", ()))
        return cls(steps=steps, **fields)


def _heightened_category(resource: Mapping) -> Optional[str]:
    return classify_by_labels(resource)


def execute(*, run_id: int, sel: Selection, scope: Scope, charts: list[dict],
            systems: Mapping[str, System], consents: ConsentStore,
            resources_for: Callable[[str], Iterable[Mapping]], mover: Mover,
            population_stored: int = 0, started_at: str, finished_at: str,
            by: str = "") -> Run:
    """Perform the exchange: one read step per source, then one deliver step
    per target per chart (or per target for a population), each decided by
    decide_delivery() and then handed to the mover with only the records the
    decision allows - a heightened record travels only in a released
    category. Everything else it holds is counted as withheld, never dropped.
    """
    population = scope.mode != "chart"
    steps: list[Step] = []
    seq = 0

    # ---- what the store holds for this scope --------------------------
    chart_resources: dict[str, list[dict]] = {}
    scope_heightened = 0
    if not population:
        for ch in charts:
            res = [dict(r) for r in (resources_for(ch["ref"]) or [])
                   if isinstance(r, Mapping) and r.get("resourceType") != "Patient"]
            chart_resources[ch["ref"]] = res
            scope_heightened += sum(1 for r in res if _heightened_category(r))
        stored = sum(len(v) for v in chart_resources.values())
    else:
        stored = int(population_stored)

    by_type: dict[str, dict] = {}
    for res_list in chart_resources.values():
        for r in res_list:
            t = str(r.get("resourceType", "?"))
            by_type.setdefault(t, {"stored": 0, "delivered": {}})["stored"] += 1

    # ---- read steps: the sources this exchange reads from ---------------
    for sk in sel.sources:
        s = systems.get(sk)
        if s is None:
            continue
        seq += 1
        d = Decision("allow", "profile", "read into the PHI AI store - the store is this run's "
                     "destination for reads, never its scope")
        steps.append(Step(seq, sk, s.name, "read", None, "population" if population else
                          f"{len(charts)} chart{'' if len(charts) == 1 else 's'}",
                          asdict(d), offered=stored, moved=stored, written=True,
                          note="the store holds these records for this scope"))

    # ---- deliver steps ---------------------------------------------------
    counts = {"allow": 0, "refuse": 0}
    withheld_total = released_total = skipped_heightened = moved_total = 0
    delivered: dict[str, int] = {}
    any_written = False

    for tk in sel.targets:
        target = systems.get(tk)
        if target is None:
            continue
        delivered.setdefault(tk, 0)
        units: list[tuple[Optional[dict], list[dict]]] = (
            [(None, [])] if population else [(ch, chart_resources.get(ch["ref"], [])) for ch in charts])
        for chart, res in units:
            held: dict[str, int] = {}
            for r in res:
                cat = _heightened_category(r)
                if cat:
                    held[cat] = held.get(cat, 0) + 1
            d = decide_delivery(target, patient=chart["ref"] if chart else None, held=held,
                                purpose=sel.purpose, consents=consents, scope=scope,
                                who=chart["name"] if chart else "population")
            seq += 1
            counts["allow" if d.decision == "allow" else "refuse"] += 1
            withheld_total += sum(d.withheld.values())
            released_total += sum(d.released.values())
            skipped_heightened += sum(d.withheld.values())
            step = Step(seq, tk, target.name, "deliver", chart["ref"] if chart else None,
                        chart["name"] if chart else "population", asdict(d))
            if d.decision == "allow":
                offer = [r for r in res if not _heightened_category(r)
                         or _heightened_category(r) in d.released]
                step.offered = len(offer)
                if offer or population:
                    mr = mover(target, chart, offer, d.released)
                    step.moved, step.skipped, step.written, step.note = (
                        mr.moved, mr.skipped, mr.written, mr.note)
                    any_written = any_written or mr.written
                    moved_total += mr.moved
                    delivered[tk] += mr.moved
                    for t, n in mr.sent_types.items():
                        by_type.setdefault(t, {"stored": 0, "delivered": {}})["delivered"][tk] = (
                            by_type[t]["delivered"].get(tk, 0) + int(n))
                else:
                    step.note = "nothing to offer: every record this chart holds is heightened " \
                                "and withheld"
            else:
                step.note = d.fix or ""
            steps.append(step)

    if counts["allow"] == 0 and counts["refuse"] > 0:
        status = "refused"
    elif not any_written:
        status = "decided"
    elif all(s.written or s.direction == "read" or s.decision["decision"] == "refuse"
             or s.offered == 0 for s in steps):
        status = "complete"
    else:
        status = "partial"

    return Run(id=run_id, started_at=started_at, finished_at=finished_at, status=status,
               purpose=sel.purpose, sources=tuple(sel.sources), targets=tuple(sel.targets),
               scope=scope.as_dict(), population=population,
               scope_excluded=bool(scope.exclude_sensitive),
               scope_heightened=scope_heightened, charts=len(charts), stored=stored,
               delivered=delivered, by_type=by_type,
               allowed=counts["allow"], refused=counts["refuse"], withheld=withheld_total,
               released=released_total, skipped_heightened=skipped_heightened,
               moved=moved_total, written=any_written, steps=steps, by=by)
