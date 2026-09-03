# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Which vendors have a commercial write path, and how to get one.

The registry is keyed by the same vendor keys as
`core.fhir.emr_profiles.PROFILES`, and a test holds the two in step: a
connector for a vendor that does not exist, or a profile whose write_notes
describe a commercial path with no connector, is a defect rather than a
matter of taste.
"""

from __future__ import annotations

from typing import Optional

from core.fhir.commercial.base import CommercialConnector
from core.fhir.commercial import vendors as _v

CONNECTORS: dict[str, type[CommercialConnector]] = {
    "eclinicalworks": _v.EClinicalWorksWriteAddOn,
    "altera":         _v.AlteraUnity,
    "veradigm":       _v.VeradigmUnity,
    "greenway":       _v.GreenwayGAPI,
    "modmed":         _v.ModMedProprietary,
    "medhost":        _v.MedhostInteroperabilityPackage,
    "nextgen":        _v.NextGenEnterprise,
    "nextech":        _v.NextechPracticePlus,
    "epic":           _v.EpicLicensedWrite,
}


def connector_for(vendor_key: str, settings=None, audit=None, http=None) -> Optional[CommercialConnector]:
    """The connector for a vendor, or None where the vendor sells no such path.

    None is a real answer, not a gap: TruBridge, Practice Fusion, MEDITECH
    and Netsmart publish no commercial write path this project has found,
    so there is nothing to configure and the delivery simply refuses.
    """
    cls = CONNECTORS.get((vendor_key or "").strip().lower())
    return cls(settings=settings, audit=audit, http=http) if cls else None


def vendors_with_commercial_path() -> tuple[str, ...]:
    """Vendor keys that have a documented commercial write path."""
    return tuple(sorted(CONNECTORS))


def explain(vendor_key: str) -> str:
    """One paragraph an operator can act on, or why there is nothing to do."""
    c = connector_for(vendor_key)
    if c is None:
        return (
            f"{vendor_key}: no commercial write path is documented by this vendor. The certified "
            f"FHIR API is the whole surface, and a delivery it will not accept has nowhere else to go."
        )
    return c.refusal()
