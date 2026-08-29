# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Egress guard: what must never reach the model.

WHAT THIS IS, AND WHAT IT IS NOT. This is a backstop, not the boundary.
The boundary is architectural: no code path in core/assistant/ reads
clinical content. core/assistant/posture.py returns counts and verdicts,
never rows; core/assistant/knowledge.py serves committed documentation
files and nothing else; core/assistant/tools.py exposes only those two.
There is no tool that decrypts an object, no tool that returns a patient
reference, and no tool that queries stored_resources for anything but
aggregates. That absence is the control.

This module exists for the one input the architecture cannot constrain:
what a human types into the box. An operator debugging a failed restore
will paste a stack trace, and a stack trace can carry a storage key; an
HIM administrator asking why a record looks wrong may paste the record.
So every outbound message is scanned, and a match REFUSES the request
rather than silently stripping it.

WHY REFUSE RATHER THAN REDACT. Silent redaction teaches the operator
that pasting PHI is fine because something downstream handles it, and
partial redaction of a clinical note leaves enough to re-identify while
looking safe. A refusal that names the category and asks for a rephrase
is the only outcome that both protects the data and tells the human what
the rule is.

WHAT IT CANNOT CATCH, STATED PLAINLY RATHER THAN LEFT TO BE DISCOVERED.
A patient's name is a word. "Margaret Chen was seen on Tuesday" has no
structure to match, and no regex will ever flag it. Free text is not
made safe by this module, and anyone reading it should not come away
believing otherwise. What it does catch is the structured, machine-shaped
PHI that actually turns up in pasted material - FHIR resources, storage
keys, identifiers, contact details - which is the realistic accident.
The defence against a typed name is that the assistant has no reason to
want one: the system prompt tells the model to refuse patient-specific
questions, and there is no tool that could answer one.

MATCHED TEXT IS NEVER RETURNED, LOGGED, OR PUT IN AN ERROR MESSAGE. A
finding carries a category and a position. Echoing the matched value into
an exception would move the PHI from the request into the application
log, which is a worse place for it than where it started.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


class EgressBlocked(Exception):
    """Raised when text assembled by this application would carry PHI.

    A caller seeing this from context THIS CODE built has a bug: it means
    a tool returned something it should not have. A caller seeing it from
    user input has a user who needs to be told to rephrase.
    """

    def __init__(self, findings: "list[Finding]", source: str):
        self.findings = findings
        self.source = source
        categories = sorted({f.category for f in findings})
        super().__init__(
            f"refusing to send {source} to the model: it appears to contain "
            + ", ".join(categories)
        )


@dataclass(frozen=True)
class Finding:
    category: str
    explanation: str
    # Offset only. The matched text is deliberately absent - see the
    # module docstring.
    position: int


# Ordered most-specific first so the explanation an operator sees names
# the most useful thing found, not merely the first thing.
#
# Each entry is (category, compiled pattern, explanation shown to a human).
_PATTERNS: tuple[tuple[str, re.Pattern, str], ...] = (
    (
        "a FHIR resource",
        re.compile(r'"resourceType"\s*:', re.IGNORECASE),
        "JSON containing a \"resourceType\" field is a stored clinical record",
    ),
    (
        "a storage key",
        # Matches the layouts core/storage/layout.py writes under either
        # profile: fhir/{Type}/{id}.json and fhir/{Type}/{patient}.ndjson,
        # plus the source-document prefix core/web/app.py serves from.
        re.compile(r"\b(?:fhir/[A-Za-z]+/|documents/source/)\S+"),
        "an object key, which identifies a specific patient's record",
    ),
    (
        "a patient reference",
        re.compile(r"\bPatient/[A-Za-z0-9\-.]{2,}"),
        "a Patient/<id> reference to an individual in the source EMR",
    ),
    (
        "a social security number",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "a nine-digit number formatted as an SSN",
    ),
    (
        "a medical record number",
        re.compile(r"\b(?:MRN|medical record (?:number|no\.?))\b\s*[:#=]?\s*\S+", re.IGNORECASE),
        "a labelled medical record number",
    ),
    (
        "a date of birth",
        # Anchored on the label rather than on date shape. Bare dates are
        # everywhere in runbooks, timestamps and version strings; flagging
        # them would make the guard fire constantly and get switched off,
        # which is the failure mode that matters most for a control like
        # this one.
        re.compile(r"\b(?:DOB|date of birth|birth ?date)\b\s*[:#=]?\s*\S+", re.IGNORECASE),
        "a labelled date of birth",
    ),
    (
        "a telephone number",
        re.compile(r"(?<!\d)\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"),
        "a ten-digit number formatted as a US telephone number",
    ),
    (
        "an email address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "an email address, which is a direct identifier under 45 CFR 164.514(b)(2)",
    ),
)

# Documentation legitimately contains every shape above - RUNBOOK_DATA_RESTORE.md
# is full of example storage keys, and .env.example carries example URLs.
# Corpus text is therefore trusted (it is committed, reviewed, non-PHI
# content) and scanned only for the resource-type marker, which no
# document should ever contain. See scan_corpus_text().
_CORPUS_PATTERNS = _PATTERNS[:1]


def scan(text: str) -> list[Finding]:
    """Every PHI-shaped thing in `text`, most specific category first."""
    if not text:
        return []
    findings: list[Finding] = []
    for category, pattern, explanation in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                Finding(category=category, explanation=explanation, position=match.start())
            )
    return findings


def scan_corpus_text(text: str) -> list[Finding]:
    """The narrower scan applied to documentation loaded from disk.

    Runbooks are written ABOUT patient references and storage keys, with
    synthetic examples throughout, so the full scan would reject the
    knowledge base wholesale. What a runbook must never contain is a real
    stored resource, and that is what this checks - the case where
    someone has pasted live output into a document and committed it.
    """
    if not text:
        return []
    return [
        Finding(category=category, explanation=explanation, position=match.start())
        for category, pattern, explanation in _CORPUS_PATTERNS
        for match in pattern.finditer(text)
    ]


def assert_clean(text: str, source: str) -> None:
    """Raise if `text` carries PHI. Used on everything this app assembles.

    Applied to tool results before they are handed back to the model. A
    tool in core/assistant/tools.py returning something that trips this is
    a bug in that tool, and it should surface as a loud failure here
    rather than as an entry in someone's outbound traffic.
    """
    findings = scan(text)
    if findings:
        raise EgressBlocked(findings, source)


def refusal_message(findings: Iterable[Finding]) -> str:
    """What to tell a human whose question was refused.

    Names categories and reasons; quotes nothing. The advice at the end
    matters as much as the refusal: the assistant genuinely cannot answer
    patient-specific questions, so "remove it and ask again" would be
    misleading if the question itself was about one patient.
    """
    reasons = []
    seen = set()
    for finding in findings:
        if finding.category in seen:
            continue
        seen.add(finding.category)
        reasons.append(f"  - {finding.category}: {finding.explanation}")

    return (
        "That question was not sent to the model, because it looks like it contains "
        "protected health information:\n\n"
        + "\n".join(reasons)
        + "\n\nThe assistant is deliberately built without any access to clinical "
        "content - it can explain how this platform works and report counts and "
        "verification results, but it cannot see a patient's records and cannot answer "
        "questions about a specific individual. Ask the question in general terms "
        "(\"why would a restore fail with a digest mismatch?\" rather than pasting the "
        "record), and use the platform's own patient view for anything patient-specific, "
        "where the access is audited."
    )
# Made by Ryan Gomez & Co. Inc.
