# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""A disclosure consent: this chart's records in this heightened category
may be released to this receiving system, under this purpose.

Per chart, per category, AND per receiving system. A release to one
hospital earns nothing at another, and releasing a mental-health record
does not release the SUD record beside it. That triple is what a real
authorisation names, and it is what this store keys on.

Not core.governance.consent_gate: that answers "may I record this visit"
(ambient capture). This answers "may this leave for that system".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


@dataclass(frozen=True)
class Consent:
    system: str
    patient: str                 # the platform's reference: 'Patient/eAB12cd3'
    category: str
    purpose: str
    by: str = ""
    at: str = ""

    @property
    def key(self) -> str:
        return consent_key(self.system, self.patient, self.category)


def consent_key(system: str, patient: str, category: str) -> str:
    """One string the screen can post back: `system|patient|category`. The
    patient is the platform's own reference (`Patient/eAB12cd3`), which
    carries no `|`, so the split is unambiguous."""
    return f"{system}|{patient}|{category}"


def parse_consent_key(raw: str) -> Optional[tuple[str, str, str]]:
    parts = str(raw or "").split("|")
    if len(parts) != 3:
        return None
    system, patient, cat = (p.strip() for p in parts)
    if not system or not cat or not patient.startswith("Patient/"):
        return None
    return system, patient, cat


class ConsentStore:
    """In-memory truth; PlatformState mirrors mutations to SQL. Kept as its
    own class so the decision engine can be handed one in a test with no
    web app around it."""

    def __init__(self, rows: Iterable[Consent] = ()):
        self._rows: dict[str, Consent] = {c.key: c for c in rows}

    def grant(self, system: str, patient: str, category: str, purpose: str,
              *, by: str = "", at: str = "") -> Consent:
        c = Consent(system, str(patient), category, purpose, by, at)
        self._rows[c.key] = c
        return c

    def revoke(self, system: str, patient: str, category: str) -> bool:
        return self._rows.pop(consent_key(system, patient, category), None) is not None

    def has(self, system: str, patient: str, category: str) -> bool:
        return consent_key(system, patient, category) in self._rows

    def get(self, system: str, patient: str, category: str) -> Optional[Consent]:
        return self._rows.get(consent_key(system, patient, category))

    def for_patient(self, patient: str) -> list[Consent]:
        return [c for c in self._rows.values() if c.patient == patient]

    def all(self) -> list[Consent]:
        return list(self._rows.values())

    def released(self, system: str, patient: str, held: Mapping[str, int]) -> dict[str, int]:
        """Of the categories a chart holds, the ones this system holds a
        consent for. The purpose test is the decision engine's, not this
        store's - a consent exists or it does not."""
        return {cat: int(n) for cat, n in held.items()
                if self.has(system, patient, str(cat))}
