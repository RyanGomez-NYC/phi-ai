# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The one place a delivery is decided.

The preflight shows this decision, the run acts on it, the ledger records
it: all three call decide_delivery() and nothing else evaluates a bound,
so they cannot disagree about a single record. Every reason is written for
the reader who has to act on it - it names the gate, the party, and the
categories, and where a gate can be cleared it says how.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from core.orchestration.consents import ConsentStore
from core.orchestration.heightened import category_label
from core.orchestration.scope import Scope
from core.orchestration.systems import System

#: Heightened categories may cross only under a purpose that is about the
#: person: continuity of their care, or their own request. Payment,
#: operations, research and legal never carry them, whatever consent exists.
PURPOSES_ALLOWING_SENSITIVE = ("treatment", "patient_request")


def purpose_allows_sensitive(purpose: str) -> bool:
    return purpose in PURPOSES_ALLOWING_SENSITIVE


@dataclass
class Decision:
    decision: str                       # 'allow' | 'refuse'
    bound: str                          # which gate decided it
    reason: str                         # prose for the reader
    withheld: dict[str, int] = field(default_factory=dict)   # category -> records
    released: dict[str, int] = field(default_factory=dict)   # category -> records
    fix: Optional[str] = None           # how to clear a refusal, when it can be


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def decide_delivery(target: System, *, patient: Optional[str], held: Mapping[str, int],
                    purpose: str, consents: ConsentStore, scope: Scope,
                    who: str = "this chart", link_verified: Optional[bool] = None) -> Decision:
    """Decide one delivery: this target, this chart (or a population step
    when patient is None), these heightened categories on the chart.

    Order of the gates is the order a reader would want them named:
      1. the target can take a write at all;
      2. the purpose is one the platform recognises;
      3. per heightened category - released only when the SCOPE does not
         exclude them AND the purpose permits them AND this target holds a
         disclosure consent for this chart in this category. The scope's
         exclusion outranks a consent rather than restating it: a consent
         says "this MAY move", the switch says "this run is not moving it".
    A population step withholds every heightened category unconditionally -
    a bulk write is decided once for the whole scope, and consent is decided
    per chart, so nothing carrying a sensitivity travels that way.
    """
    if not target.writable:
        return Decision("refuse", "target", f"{target.name} advertises no create for any "
                        "resource type; a delivery to it cannot be a write",
                        fix="choose a target whose profile advertises create")
    if not purpose:
        return Decision("refuse", "purpose", "no purpose of use is asserted for this run",
                        fix="assert a purpose on the exchange")
    # The identity bound. A per-chart delivery is decided against that chart's
    # VERIFIED identifier on the target - an id typed by one person and vouched
    # for by another. None means the caller did not consult the link set (a
    # population step, or a screen that has no chart); False is a refusal.
    # Identity refuses the DELIVERY, but the chart's heightened categories are
    # still evaluated below: the checklist and the consent matrix stay on
    # screen, so linking the chart and recording its consents proceed in
    # parallel rather than one hiding the other.
    identity_ok = not (patient is not None and link_verified is False)

    withheld: dict[str, int] = {}
    released: dict[str, int] = {}
    reason = ""
    if held:
        purpose_ok = purpose_allows_sensitive(purpose)
        scope_excludes = bool(scope.exclude_sensitive)
        for cat, n in held.items():
            cat = str(cat)
            if (patient is not None and not scope_excludes and purpose_ok
                    and consents.has(target.key, patient, cat)):
                released[cat] = int(n)
            else:
                withheld[cat] = int(n)

        if released:
            tot = sum(released.values())
            reason = (f"releases {tot} heightened {_plural(tot, 'record', 'records')} in "
                      f"{len(released)} {_plural(len(released), 'category', 'categories')} - "
                      f"{target.name} holds a disclosure consent for {who} covering "
                      + ", ".join(category_label(c) for c in released)
                      + f", and purpose {purpose} permits them")
        if withheld:
            gates: list[str] = []
            if patient is None:
                gates.append("a population write is decided once for the whole scope and "
                             "never carries them; each is decided per chart")
            elif scope_excludes:
                gates.append("this scope excludes heightened records, so they stay behind "
                             "even where a consent exists")
            elif not purpose_ok:
                gates.append(f"purpose {purpose} does not permit heightened categories")
            else:
                gates.append(f"{target.name} holds no disclosure consent for {who} covering "
                             + ", ".join(category_label(c) for c in withheld))
            tot = sum(withheld.values())
            line = (f"withheld {tot} heightened {_plural(tot, 'record', 'records')} in "
                    f"{len(withheld)} {_plural(len(withheld), 'category', 'categories')} - "
                    + "; ".join(gates))
            reason = line if not reason else f"{reason}. {line[0].upper()}{line[1:]}"

    if not identity_ok:
        line = (f"no verified identifier for {who} on {target.name}; a per-chart delivery is "
                "decided against that chart's verified identifier on the target")
        reason = line if not reason else f"{line}. {reason[0].upper()}{reason[1:]}"
        return Decision("refuse", "identity", reason, withheld=withheld, released=released,
                        fix="add the identifier on the Patient link set and have a second person verify it")
    if not reason:
        reason = "allowed on every consulted bound"
    return Decision("allow", "consent" if held else "profile", reason,
                    withheld=withheld, released=released)
