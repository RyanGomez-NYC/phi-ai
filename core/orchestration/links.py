# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The patient link set: which chart in which system is this person.

A link is an identifier on another system, typed by an operator and
verified by a person. It is never a match across systems made by a
program: this store records who asserted it and who verified it, and the
per-chart delivery is decided against the VERIFIED link at the target -
with no verified identifier the identity bound refuses, and a wall of red
is what an exchange with no integration work done looks like.

The verified links ARE the platform's identity map: to_identity_map()
hands core.fhir.delivery the same PatientMapping rows a records team
would have produced in a spreadsheet, with the verifier named.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

#: A FHIR logical id: RFC-style token, 1-64 characters. The same rule the
#: delivery writer applies before it puts an id on a wire.
FHIR_ID = re.compile(r"^[A-Za-z0-9\-\.]{1,64}$")
LINK_STATES = ("candidate", "verified", "rejected")


class LinkRefusal(ValueError):
    """A link that cannot be recorded, with the reason a person can act on."""


@dataclass
class Link:
    id: int
    patient: str                 # the platform's reference: 'Patient/eAB12cd3'
    system: str                  # profile key of the other system
    system_id: str               # that system's own Patient id
    status: str = "candidate"    # LINK_STATES
    entered_by: str = ""
    entered_at: str = ""
    verified_by: str = ""
    verified_at: str = ""
    note: str = ""
    revoked: bool = False
    revoked_by: str = ""
    revoked_at: str = ""

    @property
    def live(self) -> bool:
        return not self.revoked and self.status in ("candidate", "verified")

    def as_dict(self) -> dict:
        return asdict(self)


class LinkStore:
    """In-memory truth; PlatformState mirrors mutations to SQL."""

    def __init__(self, rows: Iterable[Link] = ()):
        self._rows: dict[int, Link] = {r.id: r for r in rows}
        self._next = (max(self._rows) + 1) if self._rows else 1

    def all(self) -> list[Link]:
        return sorted(self._rows.values(), key=lambda r: r.id)

    def get(self, link_id: int) -> Optional[Link]:
        return self._rows.get(int(link_id))

    def for_patient(self, patient: str) -> list[Link]:
        return [r for r in self.all() if r.patient == patient]

    def live_for(self, patient: str, system: str) -> Optional[Link]:
        for r in self.all():
            if r.patient == patient and r.system == system and r.live:
                return r
        return None

    def verified_for(self, patient: str, system: str) -> Optional[Link]:
        r = self.live_for(patient, system)
        return r if r is not None and r.status == "verified" else None

    def add(self, patient: str, system: str, system_id: str, *, by: str, at: str,
            note: str = "") -> Link:
        system_id = str(system_id or "").strip()
        if not patient.startswith("Patient/"):
            raise LinkRefusal("no patient named")
        if not system:
            raise LinkRefusal("no system named")
        if not FHIR_ID.match(system_id):
            raise LinkRefusal(f"'{system_id[:40]}' is not a FHIR logical id (1-64 of A-Z a-z 0-9 - .)")
        live = self.live_for(patient, system)
        if live is not None:
            raise LinkRefusal(f"{patient} already has a {live.status} link on {system} "
                              f"({live.system_id}); revoke it before adding another")
        r = Link(self._next, patient, system, system_id, "candidate", by, at, note=note)
        self._rows[r.id] = r
        self._next += 1
        return r

    def verify(self, link_id: int, *, by: str, at: str, note: str = "") -> Link:
        r = self.get(link_id)
        if r is None or r.revoked:
            raise LinkRefusal(f"no live link #{link_id}")
        if r.status == "rejected":
            raise LinkRefusal("link is already rejected")
        if r.status == "verified":
            raise LinkRefusal(f"link is already verified by {r.verified_by}")
        if by and by == r.entered_by:
            # Two people, not one: the person who typed it cannot be the
            # person who vouches for it.
            raise LinkRefusal("a link is verified by someone other than the person who entered it")
        r.status, r.verified_by, r.verified_at = "verified", by, at
        if note:
            r.note = note
        return r

    def reject(self, link_id: int, *, note: str = "") -> Link:
        r = self.get(link_id)
        if r is None or r.revoked:
            raise LinkRefusal(f"no live link #{link_id}")
        if r.status == "rejected":
            raise LinkRefusal("link is already rejected")
        r.status = "rejected"
        if note:
            r.note = note
        return r

    def revoke(self, patient: str, system: str, *, by: str, at: str, note: str = "") -> Link:
        r = self.live_for(patient, system)
        if r is None:
            raise LinkRefusal(f"no live link for {patient} on {system}")
        r.revoked, r.revoked_by, r.revoked_at = True, by, at
        if note:
            r.note = note
        return r

    def to_identity_map(self, system: str):
        """The verified links at one system, as core.fhir.delivery's identity
        map - the thing the writer resolves a source reference against."""
        from core.fhir.delivery.identity import IdentityMap, PatientMapping
        rows = [PatientMapping(source_patient_id=r.patient.split("/", 1)[1],
                               target_patient_id=r.system_id, verified_by=r.verified_by,
                               note=r.note)
                for r in self.all() if r.system == system and r.live and r.status == "verified"]
        return IdentityMap(rows)
