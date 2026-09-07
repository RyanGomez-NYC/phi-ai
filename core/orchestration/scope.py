# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""What a run is about: one chart, every patient, or a segment.

One normaliser, used by the route that reads the form, the store that keeps
the record and the reader that hands it back. The demo learned this the
hard way - a selector added to the writer and not the reader was written,
kept, and silently dropped on the way out - so here there is one shape and
one function that produces it.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Mapping, Optional

MODES = ("chart", "all", "segment")

#: Age bands as the demo names them. Ranges are inclusive years.
AGE_BANDS: dict[str, tuple[int, Optional[int]]] = {
    "0-17": (0, 17), "18-44": (18, 44), "45-64": (45, 64), "65+": (65, None),
}

#: Purposes that may work over a POPULATION at all. `legal` names its
#: subject (a subpoena is about someone) and `patient_request` is one
#: person's own right of access; neither is a cohort. Mirrors the demo's
#: purpose_allows_population(), which asks whether the purpose permits the
#: FHIR Group resource.
POPULATION_PURPOSES = ("treatment", "payment", "operations", "research")

_SEGMENT_FIELDS = ("condition", "sex", "band", "payer", "state",
                   "medication", "living", "seen_since")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Scope:
    mode: str = "chart"
    condition: str = ""
    sex: str = ""
    band: str = ""
    payer: str = ""
    state: str = ""
    medication: str = ""
    living: str = ""          # '' | 'living' | 'deceased'
    seen_since: str = ""      # YYYY-MM-DD
    # NOT a segment selector: a decision about the whole run, sayable about
    # "every patient" too, and stronger than the consent gate rather than a
    # restatement of it. With it set a heightened record stays put even where
    # a disclosure consent exists - "we are not moving this" over "we may".
    exclude_sensitive: bool = False
    by: str = ""
    at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def purpose_allows_population(purpose: str) -> bool:
    return purpose in POPULATION_PURPOSES


def mode_for_purpose(mode: str, purpose: str) -> str:
    """A population mode under a purpose that cannot work over a population
    collapses to the chart. The mode is never silently kept and then refused
    at run time; the screen shows the mode the run will actually use."""
    if mode == "chart":
        return mode
    return mode if purpose_allows_population(purpose) else "chart"


def normalise_scope(raw: Mapping, purpose: str, *, by: str = "", at: str = "") -> Scope:
    """The one shape. ONLY a segment carries selectors: the form posts every
    selector field whatever mode is chosen, so switching to "every patient"
    with a condition still typed used to leave the scope silently filtered.
    Clearing them here means a stored scope can never disagree with its own
    mode."""
    mode = str(raw.get("mode", "")).strip()
    mode = mode if mode in MODES else "chart"
    mode = mode_for_purpose(mode, purpose)
    seg = mode == "segment"

    def text(key: str, n: int) -> str:
        return str(raw.get(key, "") or "").strip()[:n] if seg else ""

    living = str(raw.get("living", "") or "").strip()
    seen = str(raw.get("seen_since", "") or "").strip()
    band = str(raw.get("band", "") or "").strip()
    sex = str(raw.get("sex", "") or "").strip()
    return Scope(
        mode=mode,
        condition=text("condition", 60),
        sex=sex if seg and sex in ("female", "male") else "",
        band=band if seg and band in AGE_BANDS else "",
        payer=text("payer", 80),
        state=text("state", 40),
        medication=text("medication", 60),
        living=living if seg and living in ("living", "deceased") else "",
        seen_since=seen if seg and _DATE.match(seen) else "",
        exclude_sensitive=bool(raw.get("exclude_sensitive")),
        by=by, at=at,
    )


def scope_has_selectors(scope: Scope) -> bool:
    """True when a segment actually narrows anything."""
    return any(getattr(scope, k) for k in _SEGMENT_FIELDS)


def scope_label(scope: Scope, n_sources: int = 1) -> str:
    """One line that says what the run is about - the audit trail's line,
    so it names the exclusion in every mode."""
    x = ", heightened records excluded" if scope.exclude_sensitive else ""
    if scope.mode == "chart":
        return "the chart in context, or the chosen set" + x
    if scope.mode == "all":
        base = ("every patient the source system holds" if n_sources == 1
                else f"every patient the {n_sources} source systems hold, combined")
        return base + x
    if not scope_has_selectors(scope):
        return "every patient — no selector set yet" + x
    bits: list[str] = []
    if scope.condition:  bits.append(f'condition matching "{scope.condition}"')
    if scope.sex:        bits.append(scope.sex)
    if scope.band:       bits.append(f"aged {scope.band}")
    if scope.payer:      bits.append(f"payer {scope.payer}")
    if scope.state:      bits.append(f"in {scope.state}")
    if scope.medication: bits.append(f'on a medication matching "{scope.medication}"')
    if scope.living:     bits.append(scope.living)
    if scope.seen_since: bits.append(f"seen since {scope.seen_since}")
    if scope.exclude_sensitive: bits.append("heightened records excluded")
    return "patients with " + ", ".join(bits)
