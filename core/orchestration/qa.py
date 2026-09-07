# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The QA agents: independent checks over a finished run, each answering
one question the run's own tiles cannot be trusted to answer about
themselves.

Three agents, as the demonstration has them. Completeness asks whether
everything eligible was delivered. Integrity asks whether anything crossed
that should not have - a heightened category without a consent, a record
the scope excluded, a write to a target that advertises no create. Counts
asks whether the ledger and the tiles agree, and whether every step can
say why. A finding is evidence, not a verdict: it names the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from core.orchestration.consents import ConsentStore
from core.orchestration.heightened import category_label
from core.orchestration.run import Run
from core.orchestration.systems import System


@dataclass
class Finding:
    agent: str
    check: str
    passed: bool
    evidence: str


def audit_run(run: Run, consents: ConsentStore, systems: Mapping[str, System]) -> list[Finding]:
    out: list[Finding] = []
    deliver = [s for s in run.steps if s.direction == "deliver"]

    # ---- Completeness ----------------------------------------------------
    out.append(Finding("Completeness", "A run to audit", bool(run.steps),
                       f"{len(run.steps)} step{'' if len(run.steps) == 1 else 's'} on the ledger"
                       if run.steps else "the run recorded no steps"))
    for tk in run.targets:
        name = systems[tk].name if tk in systems else tk
        mine = [s for s in deliver if s.system == tk and s.decision["decision"] == "allow"]
        eligible = sum(s.offered for s in mine)
        got = run.delivered.get(tk, 0)
        if run.written:
            ok = got == eligible
            ev = f"eligible {eligible}, delivered {got}"
        else:
            ok = got == 0
            ev = (f"eligible {eligible}, delivered 0 - decided, not written: "
                  f"no delivery destination is configured for {name}")
        out.append(Finding("Completeness", f"{name}: everything eligible was delivered", ok, ev))

    # ---- Integrity -------------------------------------------------------
    bad_release = []
    for s in deliver:
        for cat in s.decision.get("released", {}):
            if s.patient is None or not consents.has(s.system, s.patient, cat):
                bad_release.append(f"{s.who}: {category_label(cat)} at {s.system_name}")
    out.append(Finding("Integrity", "No heightened category crossed without a disclosure consent",
                       not bad_release,
                       "every released category has a consent on file for that chart at that target"
                       if not bad_release else "released without a consent: " + "; ".join(bad_release)))

    if run.scope_excluded:
        leaked = sum(sum(s.decision.get("released", {}).values()) for s in deliver)
        out.append(Finding("Integrity", "Nothing crossed that the scope excluded", leaked == 0,
                           "the scope excluded heightened records and none were released"
                           if leaked == 0 else f"{leaked} heightened records released although the "
                                               "scope excluded them"))

    wrote_readonly = [s.system_name for s in deliver
                      if s.moved > 0 and s.system in systems and not systems[s.system].writable]
    out.append(Finding("Integrity", "Nothing written to a target that advertises no create",
                       not wrote_readonly,
                       "no write reached a read-only target" if not wrote_readonly
                       else "written to: " + ", ".join(sorted(set(wrote_readonly)))))

    # ---- Counts ----------------------------------------------------------
    ledger_moved = sum(s.moved for s in deliver)
    ledger_withheld = sum(sum(s.decision.get("withheld", {}).values()) for s in deliver)
    ledger_released = sum(sum(s.decision.get("released", {}).values()) for s in deliver)
    agree = (ledger_moved == run.moved and ledger_withheld == run.withheld
             and ledger_released == run.released and sum(run.delivered.values()) == run.moved)
    out.append(Finding("Counts", "The tiles agree with the ledger", agree,
                       f"moved {run.moved}/{ledger_moved}, withheld {run.withheld}/{ledger_withheld}, "
                       f"released {run.released}/{ledger_released} (tile/ledger)"))
    silent = [s.seq for s in run.steps if not str(s.decision.get("reason", "")).strip()]
    out.append(Finding("Counts", "Every step carries its reason", not silent,
                       "every step names the bound that decided it" if not silent
                       else "steps without a reason: " + ", ".join(map(str, silent))))
    return out
