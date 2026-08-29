# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Shared vocabulary for verifying every data flow in this platform.

WHY THIS IS ONE FRAMEWORK RATHER THAN FIVE SCRIPTS. The platform already
had three verifications - index against storage (core/db/reconcile.py),
the audit chain (core/audit/verify.py), and per-object digests
(ObjectStore.verify_integrity) - and they shared nothing. Each printed
its own shape of output and had to be run and interpreted separately, so
"is this deployment sound?" had no single answer. Worse, the flows with
the highest consequences had no verification at all.

THE FLOWS AND WHAT EACH CAN GO WRONG:

  EMR -> store          records the source had that the store does not.
                        THE ONE WITH A DEADLINE - see below.
  index <-> storage     drift between the queryable index and the objects
                        that are the actual system of record.
  audit chain           an entry removed or altered.
  object integrity      ciphertext that no longer matches its digest.
  store -> export       an export that silently omitted records.
  store -> EMR          records believed delivered that the destination
                        does not actually hold.

INGESTION VERIFICATION HAS AN EXPIRY DATE, and it is the single most
important operational fact in this file. Comparing the object store
against the source EMR requires the source EMR to still exist. Once it is
decommissioned - which is the entire point of building this platform -
there is nothing left to compare against, and any gap becomes permanent
and undetectable. Verify ingestion BEFORE the source system is turned
off, not after. Nothing in software can recover from getting that order
wrong.

SEVERITY MEANS "WHAT SHOULD HAPPEN NEXT", not "how bad does this feel".
A finding is CRITICAL when data may be permanently unrecoverable,
WARNING when something needs attention but is recoverable, and INFO when
it is worth recording and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    CRITICAL = "CRITICAL"   # data may be permanently lost or wrong
    WARNING = "WARNING"     # needs attention, recoverable
    INFO = "INFO"
    OK = "OK"

    @property
    def rank(self) -> int:
        return {"OK": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}[self.value]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    check: str
    summary: str
    detail: str = ""
    # A bounded sample rather than every identifier. A verification report
    # is read by a human; ten thousand ids in a terminal is not a finding,
    # it is a wall. The counts carry the magnitude.
    examples: tuple[str, ...] = ()
    count: Optional[int] = None

    def rendered_examples(self, limit: int = 8) -> str:
        if not self.examples:
            return ""
        shown = list(self.examples[:limit])
        more = len(self.examples) - len(shown)
        text = ", ".join(shown)
        return f"{text}{f' … and {more} more' if more > 0 else ''}"


@dataclass
class FlowReport:
    """Verification of one data flow."""

    flow: str
    source: str
    target: str
    findings: list[Finding] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Set when a flow could not be checked at all. Distinct from "checked
    # and found nothing wrong" - conflating the two is how an unverified
    # deployment comes to look verified.
    skipped_reason: Optional[str] = None

    def add(self, severity: Severity, check: str, summary: str, detail: str = "",
            examples: tuple[str, ...] = (), count: Optional[int] = None) -> None:
        self.findings.append(
            Finding(severity, check, summary, detail, tuple(examples), count)
        )

    @property
    def worst(self) -> Severity:
        if self.skipped_reason:
            return Severity.WARNING
        if not self.findings:
            return Severity.OK
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    @property
    def ok(self) -> bool:
        return self.worst.rank <= Severity.INFO.rank and not self.skipped_reason


@dataclass
class VerificationReport:
    """Every flow, in one answer."""

    flows: list[FlowReport] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add(self, flow: FlowReport) -> None:
        self.flows.append(flow)

    @property
    def worst(self) -> Severity:
        if not self.flows:
            return Severity.OK
        return max((f.worst for f in self.flows), key=lambda s: s.rank)

    @property
    def critical(self) -> list[Finding]:
        return [f for flow in self.flows for f in flow.findings
                if f.severity is Severity.CRITICAL]

    @property
    def skipped(self) -> list[FlowReport]:
        return [f for f in self.flows if f.skipped_reason]

    def exit_code(self) -> int:
        """0 clean, 1 warnings, 2 critical.

        A SKIPPED flow yields 1, never 0. A deployment nobody could verify
        must not report success - that is the difference between "sound"
        and "unexamined", and a CI job or a runbook step keying on exit
        code should be able to tell them apart.
        """
        if self.worst is Severity.CRITICAL:
            return 2
        if self.worst.rank >= Severity.WARNING.rank or self.skipped:
            return 1
        return 0

    def render(self) -> str:
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("PLATFORM VERIFICATION")
        lines.append(f"{self.generated_at.isoformat()}")
        lines.append("=" * 72)

        for flow in self.flows:
            lines.append("")
            marker = {"OK": "OK  ", "INFO": "INFO", "WARNING": "WARN", "CRITICAL": "FAIL"}[
                flow.worst.value
            ]
            lines.append(f"[{marker}] {flow.flow}")
            lines.append(f"        {flow.source} -> {flow.target}")

            if flow.skipped_reason:
                lines.append(f"        NOT CHECKED: {flow.skipped_reason}")
                continue

            if not flow.findings:
                lines.append("        no discrepancies found")
                continue

            for finding in flow.findings:
                lines.append(f"        {finding.severity.value:<8} {finding.summary}")
                if finding.count is not None:
                    lines.append(f"                 count: {finding.count}")
                if finding.detail:
                    for wrapped in _wrap(finding.detail, 60):
                        lines.append(f"                 {wrapped}")
                sample = finding.rendered_examples()
                if sample:
                    lines.append(f"                 e.g. {sample}")

        lines.append("")
        lines.append("-" * 72)
        lines.append(f"OVERALL: {self.worst.value}")
        if self.critical:
            lines.append(f"  {len(self.critical)} critical finding(s) - data may be at risk")
        if self.skipped:
            lines.append(
                f"  {len(self.skipped)} flow(s) NOT CHECKED - an unverified deployment is "
                "not a verified one"
            )
        lines.append("-" * 72)
        return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
# Made by Ryan Gomez & Co. Inc.
