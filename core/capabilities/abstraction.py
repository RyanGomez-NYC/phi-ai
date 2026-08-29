# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Registry and quality-measure abstraction (SPEC §5.10): eCQM and
clinical-registry element abstraction with A HUMAN CONFIRMING EACH
ELEMENT and a citation per element. Human sign-off is mandatory, not
configurable — implemented the way Invariant 17's gate is: there is no
export path for an unconfirmed worklist, so no flag can create one.

Flow: define the measure's elements with retrieval queries → the
platform proposes cited evidence per element → a named human confirms
each element's value against its citations (or marks it
not-found-in-chart, which is also a confirmation) → only a fully
confirmed worklist exports. Any write of abstracted values back to an
EMR goes through core/governance/writeback.py (Invariant 13); this
module produces the confirmed artifact, never the write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from core.rag.retriever import GrantScope, retrieve
from core.rag.serialization import Chunk


class AbstractionError(Exception):
    pass


@dataclass(frozen=True)
class MeasureElement:
    element_id: str
    description: str
    query: str


@dataclass
class ElementState:
    element: MeasureElement
    proposed_citations: tuple[str, ...] = ()
    proposed_excerpts: tuple[str, ...] = ()
    confirmed: bool = False
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    confirmed_value: Optional[str] = None
    #: citations the human actually relied on - may be a subset of the
    #: proposal, and may be empty only for a not-found confirmation.
    confirmed_citations: tuple[str, ...] = ()
    not_found_in_chart: bool = False


class AbstractionWorklist:
    def __init__(
        self,
        measure_id: str,
        patient_reference: str,
        elements: Sequence[MeasureElement],
        *,
        audit=None,
    ):
        if not elements:
            raise AbstractionError("a measure with no elements abstracts nothing")
        self.measure_id = measure_id
        self.patient_reference = patient_reference
        self._audit = audit
        self._elements: dict[str, ElementState] = {
            e.element_id: ElementState(element=e) for e in elements
        }

    def propose(self, chunks: Sequence[Chunk], *, k: int = 5) -> None:
        """Fill each element's proposal from the chart. Negated content
        is never proposed as evidence — the same rule as prior auth."""
        scope = GrantScope(patient_reference=self.patient_reference)
        for state in self._elements.values():
            hits = [
                (c, s)
                for c, s in retrieve(state.element.query, chunks, scope, k=k)
                if c.verification_status not in ("refuted", "entered-in-error")
            ]
            state.proposed_citations = tuple(c.storage_key for c, _ in hits)
            state.proposed_excerpts = tuple(c.text for c, _ in hits)

    def confirm(
        self,
        element_id: str,
        *,
        confirmed_by: str,
        value: str,
        citations: Sequence[str],
    ) -> None:
        state = self._elements.get(element_id)
        if state is None:
            raise AbstractionError(f"no element {element_id!r} in this worklist")
        if not confirmed_by.strip():
            raise AbstractionError(
                "confirmation requires a named human abstractor (SPEC §5.10)"
            )
        if not citations:
            raise AbstractionError(
                "a confirmed value requires at least one citation; for a "
                "value absent from the chart use mark_not_found()"
            )
        unknown = [c for c in citations if c not in state.proposed_citations]
        if unknown:
            raise AbstractionError(
                f"citations not among the proposed evidence: {unknown}; "
                "confirm against what the chart shows, or re-propose"
            )
        state.confirmed = True
        state.confirmed_by = confirmed_by
        state.confirmed_at = datetime.now(timezone.utc).isoformat()
        state.confirmed_value = value
        state.confirmed_citations = tuple(citations)
        if self._audit is not None:
            self._audit.record(
                actor=confirmed_by,
                action="abstraction.element_confirmed",
                resource_key=f"measure/{self.measure_id}/{element_id}",
                purpose_of_use="operations",
            )

    def mark_not_found(self, element_id: str, *, confirmed_by: str) -> None:
        """Also a human decision, also recorded — 'not documented' is an
        abstraction result, not a blank."""
        state = self._elements.get(element_id)
        if state is None:
            raise AbstractionError(f"no element {element_id!r} in this worklist")
        if not confirmed_by.strip():
            raise AbstractionError("mark_not_found requires a named human")
        state.confirmed = True
        state.confirmed_by = confirmed_by
        state.confirmed_at = datetime.now(timezone.utc).isoformat()
        state.not_found_in_chart = True
        if self._audit is not None:
            self._audit.record(
                actor=confirmed_by,
                action="abstraction.element_not_found",
                resource_key=f"measure/{self.measure_id}/{element_id}",
                purpose_of_use="operations",
            )

    def unconfirmed(self) -> tuple[str, ...]:
        return tuple(
            eid for eid, s in sorted(self._elements.items()) if not s.confirmed
        )

    def export(self) -> dict:
        """The confirmed artifact. Refuses while ANY element lacks its
        human confirmation — there is no partial export and no
        auto-confirm, which is the whole of §5.10's mandate."""
        pending = self.unconfirmed()
        if pending:
            raise AbstractionError(
                f"worklist {self.measure_id!r} has unconfirmed elements: "
                f"{', '.join(pending)}; every element requires a human "
                "confirmation before export (SPEC §5.10)"
            )
        return {
            "measure_id": self.measure_id,
            "patient_reference": self.patient_reference,
            "elements": {
                eid: {
                    "value": None if s.not_found_in_chart else s.confirmed_value,
                    "not_found_in_chart": s.not_found_in_chart,
                    "confirmed_by": s.confirmed_by,
                    "confirmed_at": s.confirmed_at,
                    "citations": list(s.confirmed_citations),
                }
                for eid, s in sorted(self._elements.items())
            },
        }
# Made by Ryan Gomez & Co. Inc.
