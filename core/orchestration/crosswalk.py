# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The practitioner crosswalk: who the people who work here are in each
other system.

A delivery is authored by a person, and the receiving system records the
author by ITS Practitioner id, not ours. Without a crosswalk row a
delivery either goes out unattributed or under an id the target does not
recognise; with one it is signed the way the target expects. Set by an
administrator, never inferred from a name.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from core.orchestration.links import FHIR_ID, LinkRefusal


@dataclass
class Crosswalk:
    user: str                    # the platform username
    system: str                  # profile key
    practitioner_id: str         # that system's Practitioner id
    note: str = ""
    by: str = ""
    at: str = ""

    @property
    def key(self) -> str:
        return f"{self.user}|{self.system}"

    def as_dict(self) -> dict:
        return asdict(self)


class CrosswalkStore:
    def __init__(self, rows: Iterable[Crosswalk] = ()):
        self._rows: dict[str, Crosswalk] = {r.key: r for r in rows}

    def all(self) -> list[Crosswalk]:
        return sorted(self._rows.values(), key=lambda r: (r.user, r.system))

    def get(self, user: str, system: str) -> Optional[Crosswalk]:
        return self._rows.get(f"{user}|{system}")

    def for_user(self, user: str) -> list[Crosswalk]:
        return [r for r in self.all() if r.user == user]

    def set(self, user: str, system: str, practitioner_id: str, *, by: str, at: str,
            note: str = "") -> Crosswalk:
        user = str(user or "").strip()
        practitioner_id = str(practitioner_id or "").strip()
        if not user:
            raise LinkRefusal("no user named")
        if not system:
            raise LinkRefusal("no system named")
        if not FHIR_ID.match(practitioner_id):
            raise LinkRefusal(f"'{practitioner_id[:40]}' is not a FHIR logical id")
        r = Crosswalk(user, system, practitioner_id, note, by, at)
        self._rows[r.key] = r
        return r

    def clear(self, user: str, system: str) -> bool:
        return self._rows.pop(f"{user}|{system}", None) is not None
