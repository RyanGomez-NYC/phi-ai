# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The systems an exchange can be wired between, with the posture that
decides each one's fate.

DERIVED from core/fhir/emr_profiles.py PROFILES, never typed here: a card
on the canvas says what that vendor's own documentation says, and the
citation for every flag is the vendor's chapter in docs/EMR_CONNECTORS.md.
The prose is the same prose the Source & target EMRs screen shows
(core/web/platform_state.py EMR_VENDORS) so the two screens cannot describe
one vendor's grant two ways.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class System:
    key: str                       # the profile key: `cerner`, never a second spelling
    name: str
    vendor: str
    auth: str                      # prose, from the profile
    bulk: str                      # prose, from the profile
    writes: str                    # prose, from the profile
    supports_bulk_export: bool     # can be a SOURCE of a population read
    creatable: tuple[str, ...]     # resource types the vendor advertises create for
    conditional_create: bool

    @property
    def writable(self) -> bool:
        """Can be a TARGET at all. A target that advertises no create for any
        type is read-only, and the decision engine refuses it rather than
        pretending a delivery happened."""
        return bool(self.creatable)


def systems() -> dict[str, System]:
    """Every profiled vendor, keyed by profile key, in profile order."""
    from core.fhir.emr_profiles import PROFILES
    from core.web.platform_state import EMR_VENDORS

    out: dict[str, System] = {}
    for key, profile in PROFILES.items():
        prose = EMR_VENDORS.get(key, {})
        out[key] = System(
            key=key,
            name=str(prose.get("name") or getattr(profile, "display_name", key)),
            vendor=key,
            auth=str(prose.get("auth", "")),
            bulk=str(prose.get("bulk", "")),
            writes=str(prose.get("writes", "")),
            supports_bulk_export=bool(getattr(profile, "supports_bulk_export", False)),
            creatable=tuple(getattr(profile, "writable_resources", ()) or ()),
            conditional_create=bool(getattr(profile, "supports_conditional_create", False)),
        )
    return out


def system_keys(posted: object) -> tuple[str, ...]:
    """Keep only keys that name a profiled system, in posted order, once each.
    A form can post anything; the profiles decide what exists."""
    known = systems()
    seen: list[str] = []
    for raw in (posted if isinstance(posted, (list, tuple)) else [posted]):
        k = str(raw or "").strip().lower()
        if k in known and k not in seen:
            seen.append(k)
    return tuple(seen)
