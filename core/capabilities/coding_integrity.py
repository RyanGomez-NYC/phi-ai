# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Documentation gaps, read in BOTH directions (docs/SPEC.md §5.5).

THE DIRECTION IS THE WHOLE DESIGN. A tool that finds only the first kind
of gap - a diagnosis the chart's own results support but the problem list
omits - is a revenue tool wearing a clinical name. Every finding it
produces adds a code, every code adds money, and nobody is ever told to
take one away. That is upcoding with better branding, and it is what most
of this category ships.

So this module refuses to look one way. It reports:

  UNCODED SUPPORT  - results in the chart that support a condition absent
                     from the problem list. A care gap first (nobody is
                     managing it) and a revenue gap second.
  UNSUPPORTED BILL - a billed service line with no clinical documentation
                     on file to support it. A compliance gap, and the
                     finding that costs the organization money to act on.

If a deployment only ever acts on the first list, that is visible: both
counts are returned together and the screen shows them side by side.

IT DRAFTS QUERIES. IT NEVER CHANGES A CODE OR A NOTE. Every finding is a
question for a human coder - "the chart shows X, the problem list does not
say X, is that right?" - carrying the citations that provoked it. Nothing
here writes to a record, and the module exposes no function that could.

WHAT IT IS NOT. This is not a coding engine and it does not know
ICD-10 or CPT. It matches the codes already in the record against each
other and reports where one side is present and the other is not. A real
deployment supplies the code relationships through `support_map`; without
one, only the exact-code direction is checked, and the report says so
rather than implying a clean chart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

#: Resource types that constitute clinical documentation supporting a
#: billed line. A note is documentation; an appointment is not.
DOCUMENTING_TYPES = frozenset(
    {"DocumentReference", "DiagnosticReport", "Observation", "Procedure", "Encounter"}
)

#: Resource types that carry a billed service line.
BILLING_TYPES = frozenset({"ExplanationOfBenefit", "Claim"})

#: Where a condition is asserted.
PROBLEM_TYPES = frozenset({"Condition"})

#: Where evidence for a condition is found.
EVIDENCE_TYPES = frozenset({"Observation", "DiagnosticReport", "Procedure"})


@dataclass(frozen=True)
class UncodedSupport:
    """Chart evidence for a condition the problem list does not carry."""

    system: str
    code: str
    display: str
    evidence_keys: tuple[str, ...]

    @property
    def query(self) -> str:
        return (
            f"The chart carries {len(self.evidence_keys)} result(s) coded "
            f"{self.display or self.code} ({self.system} {self.code}), and the "
            "problem list does not include it. Should it?"
        )


@dataclass(frozen=True)
class UnsupportedBill:
    """A billed line with no clinical documentation on file."""

    storage_key: str
    system: str
    code: str
    display: str

    @property
    def query(self) -> str:
        return (
            f"{self.display or self.code} ({self.system} {self.code}) is billed "
            "and no clinical documentation on file supports it. What supports "
            "this line?"
        )


@dataclass
class CodingReport:
    uncoded_support: list[UncodedSupport] = field(default_factory=list)
    unsupported_bills: list[UnsupportedBill] = field(default_factory=list)
    #: True when a support map was supplied; without one only exact-code
    #: matching ran and the report is narrower than it looks.
    support_map_used: bool = False

    @property
    def both_directions(self) -> bool:
        """Whether this report has anything to say in each direction.

        Surfaced so a screen can show it. A report with findings in only
        one direction is not wrong - a clean chart in one direction is a
        real result - but a DEPLOYMENT that never sees the second kind is
        one whose coding tool has quietly become a revenue tool.
        """
        return bool(self.uncoded_support) and bool(self.unsupported_bills)

    def render(self) -> str:
        lines = [
            f"Documentation gaps: {len(self.uncoded_support)} uncoded support, "
            f"{len(self.unsupported_bills)} unsupported bills"
        ]
        for u in self.uncoded_support:
            lines.append(f"  care/revenue gap: {u.query}")
        for b in self.unsupported_bills:
            lines.append(f"  compliance gap: {b.query}")
        return "\n".join(lines)


def _codings(node) -> Iterable[tuple[str, str, str]]:
    """Every (system, code, display) in a resource, at any depth."""
    if isinstance(node, dict):
        if "code" in node and isinstance(node.get("code"), str):
            yield (node.get("system") or "", node["code"], node.get("display") or "")
        for value in node.values():
            yield from _codings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _codings(item)


def analyze(
    resources_by_key: Mapping[str, Mapping],
    *,
    support_map: Optional[Mapping[str, Sequence[str]]] = None,
) -> CodingReport:
    """Both directions over one patient's resources.

    `support_map` maps an evidence code to the condition codes it
    supports, and is the deployment's clinical content - this module does
    not ship one, because a code relationship asserted by a platform
    vendor rather than by the organization's own coding policy is exactly
    the kind of clinical claim that should not arrive as a default.
    """
    report = CodingReport(support_map_used=bool(support_map))

    problem_codes: set[str] = set()
    evidence: dict[tuple[str, str], list[str]] = {}
    evidence_display: dict[tuple[str, str], str] = {}
    billed: list[tuple[str, str, str, str]] = []
    documented = False

    for key in sorted(resources_by_key):
        resource = resources_by_key[key]
        rtype = resource.get("resourceType")
        if rtype in PROBLEM_TYPES:
            for _system, code, _display in _codings(resource):
                problem_codes.add(code)
        elif rtype in EVIDENCE_TYPES:
            for system, code, display in _codings(resource):
                evidence.setdefault((system, code), []).append(key)
                if display:
                    evidence_display[(system, code)] = display
        if rtype in BILLING_TYPES:
            for system, code, display in _codings(resource):
                billed.append((key, system, code, display))
        if rtype in DOCUMENTING_TYPES:
            documented = True

    # Direction one: evidence for a condition the problem list omits.
    for (system, code), keys in sorted(evidence.items()):
        supported = [code]
        if support_map:
            supported = list(support_map.get(code, [code]))
        if any(c in problem_codes for c in supported):
            continue
        report.uncoded_support.append(
            UncodedSupport(
                system=system, code=code,
                display=evidence_display.get((system, code), ""),
                evidence_keys=tuple(sorted(keys)),
            )
        )

    # Direction two: a billed line with nothing on file supporting it.
    #
    # DELIBERATELY COARSE, and stated rather than hidden: "no clinical
    # documentation of any kind in this chart" is a weaker test than "no
    # documentation of THIS line", and it is the one that can be made
    # without a code map the organization has not supplied. It produces
    # fewer findings, not more - which is the right direction to be wrong
    # in for a list whose findings cost money to act on.
    if not documented:
        for key, system, code, display in billed:
            report.unsupported_bills.append(
                UnsupportedBill(storage_key=key, system=system, code=code,
                                display=display)
            )

    return report
# Made by Ryan Gomez & Co. Inc.
