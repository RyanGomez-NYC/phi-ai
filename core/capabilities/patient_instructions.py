# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Patient-friendly instructions (SPEC §5.3): a reading-level
transformation of ALREADY-AUTHORED discharge instructions — never
generation of clinical content — with the no-new-assertions check that
diffs asserted clinical facts against the source and FAILS ON ANY
ADDITION. Release is gated by Invariant 17
(core/governance/release_gate.py); this module is the content check
that runs before staging.

What counts as an asserted clinical fact, for the diff:

- **numbers with their units** — doses, frequencies, durations,
  temperatures, thresholds ("500 mg", "3 times", "101 F"). A number in
  the output that appears nowhere in the source is a new clinical
  assertion by definition, and the classic transformation failure is
  precisely a dose or frequency drifting during simplification;
- **medication names** — the output may only name medications present
  in the clinician's structured medication list or the source text;
- **follow-up actors** — the output may only tell the patient to see /
  call providers named in the structured follow-up list or the source.

The check is deliberately ASYMMETRIC: it fails on additions only.
Omissions are the CLINICIAN'S call to catch at the release gate — a
reading-level rewrite legitimately drops detail, but it may never add
any. And the check is conservative in the fail-closed direction: a
false positive costs a clinician a shrug at review; a false negative
is an uninstructed dose on a discharge sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

#: number + optional unit token ("500 mg", "3 times", "101.5 F", "2 weeks")
_NUMBER = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|units?|tablets?|capsules?|puffs?|drops?|"
    r"times?|hours?|hrs?|days?|weeks?|months?|f|c|degrees)?\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z][a-z'-]+", re.IGNORECASE)


def _number_facts(text: str) -> set[tuple[str, str]]:
    facts = set()
    for value, unit in _NUMBER.findall(text):
        facts.add((value.replace(",", "."), (unit or "").lower().rstrip("s")))
    return facts


def _vocabulary(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


@dataclass(frozen=True)
class AssertionCheck:
    ok: bool
    new_numbers: tuple[str, ...] = ()
    new_medications: tuple[str, ...] = ()
    new_followups: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.ok:
            return "no new assertions"
        parts = []
        if self.new_numbers:
            parts.append(f"numbers not in source: {', '.join(self.new_numbers)}")
        if self.new_medications:
            parts.append(
                f"medications not in source: {', '.join(self.new_medications)}"
            )
        if self.new_followups:
            parts.append(
                f"follow-up actors not in source: {', '.join(self.new_followups)}"
            )
        return "; ".join(parts)


def check_no_new_assertions(
    source_text: str,
    output_text: str,
    *,
    medication_names: Sequence[str] = (),
    followup_names: Sequence[str] = (),
    known_medication_vocabulary: Sequence[str] = (),
) -> AssertionCheck:
    """
    `source_text` is the clinician's authored instructions;
    `medication_names` / `followup_names` are the STRUCTURED lists the
    spec names as inputs (5.3) — they extend what the source permits,
    since restating a listed medication is not a new assertion.
    `known_medication_vocabulary` is the deployment's drug-name lexicon
    (from the terminology loader when licensed); with it, ANY drug-name
    token in the output that the source and structured list lack is
    flagged; without it, medication screening still covers the
    structured list's names against the output.
    """
    allowed_numbers = _number_facts(source_text)
    for name_list in (medication_names, followup_names):
        for item in name_list:
            allowed_numbers |= _number_facts(item)

    new_numbers = []
    for value, unit in sorted(_number_facts(output_text)):
        if (value, unit) not in allowed_numbers and (value, "") not in allowed_numbers:
            # a unit-less source occurrence permits the bare number, but
            # a number the source never states in any form is new
            if not any(v == value for v, _ in allowed_numbers):
                new_numbers.append(f"{value} {unit}".strip())

    source_vocab = _vocabulary(source_text)
    allowed_meds = source_vocab | {
        w for name in medication_names for w in _vocabulary(name)
    }
    lexicon = {w.lower() for w in known_medication_vocabulary}
    new_meds = sorted(
        w
        for w in _vocabulary(output_text)
        if w in lexicon and w not in allowed_meds
    )

    allowed_followups = source_vocab | {
        w for name in followup_names for w in _vocabulary(name)
    }
    followup_mentions = re.findall(
        r"\b(?:see|call|contact|visit|follow up with)\s+(?:dr\.?\s+)?([a-z][a-z'-]+)",
        output_text,
        re.IGNORECASE,
    )
    generic = {"your", "the", "a", "doctor", "provider", "us", "clinic", "office"}
    new_followups = sorted(
        {
            m.lower()
            for m in followup_mentions
            if m.lower() not in allowed_followups and m.lower() not in generic
        }
    )

    ok = not (new_numbers or new_meds or new_followups)
    return AssertionCheck(
        ok=ok,
        new_numbers=tuple(new_numbers),
        new_medications=tuple(new_meds),
        new_followups=tuple(new_followups),
    )
# Made by Ryan Gomez & Co. Inc.
