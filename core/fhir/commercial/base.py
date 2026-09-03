# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The shape every commercial connector has.

Three methods, and a refusal that explains itself. The contract is
deliberately small: the point is that adding a vendor is filling in
`authenticate`, `capabilities` and `create`, not learning a framework.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("phi-ai.fhir.commercial")


class CommercialWriteRefused(RuntimeError):
    """A write this connector cannot make, with the reason a person needs.

    Raised - never swallowed, never turned into an empty success. A
    delivery that silently went nowhere is worse than one that failed
    loudly, because nobody goes looking for it.
    """


class ConnectorNotConfigured(CommercialWriteRefused):
    """The vendor sells this path; this deployment has not bought or wired it.

    Separate from CommercialWriteRefused so an operator can tell "you have
    not set this up" apart from "this vendor will not do that at all".
    """


@dataclass(frozen=True)
class CommercialCapability:
    """What a configured connector says it can actually write.

    Mirrors the role the destination's CapabilityStatement plays for FHIR
    delivery: the vendor's sales material is a planning aid, this is what
    the running integration reports. `verified_at` is when a human last
    confirmed it against the live endpoint - an unverified capability is
    still a claim, and the writer says so.
    """

    resource_types: tuple[str, ...] = ()
    supports_update: bool = False
    verified_at: Optional[str] = None
    notes: str = ""


@dataclass
class CommercialWrite:
    """One write attempted through a commercial path."""

    resource_type: str
    source_id: str
    target_patient: str
    sent: bool = False
    destination_id: Optional[str] = None
    refused_reason: Optional[str] = None
    error: Optional[str] = None


class CommercialConnector:
    """
    A vendor's separately-sold write path.

    Subclasses fill in three things. Everything else - the refusal
    behaviour, the audit contract, the dry-run default - is here so it is
    identical for every vendor and cannot drift per implementation.

    THE DEFAULT IS REFUSAL. The base class raises ConnectorNotConfigured
    from every method that would touch a vendor. A subclass that has not
    been implemented yet inherits exactly that, so an unfinished connector
    cannot quietly do nothing: it stops the delivery and says what is
    missing.
    """

    #: Vendor key, matching core.fhir.emr_profiles.PROFILES.
    vendor_key: str = ""
    #: The vendor's own name for the product being integrated.
    product: str = ""
    #: How a customer obtains it, in the vendor's own terms.
    how_to_obtain: str = ""
    #: Who to contact - an address or portal the vendor publishes.
    contact: str = ""
    #: Whether this path speaks FHIR at all. Several do not.
    is_fhir: bool = False
    #: Documentation this stub's claims came from.
    sources: tuple[str, ...] = ()

    def __init__(self, settings=None, audit=None, http=None):
        self.settings = settings
        self.audit = audit
        self.http = http

    # -- what a subclass implements ---------------------------------

    def authenticate(self) -> None:
        """Obtain whatever this product uses for credentials.

        Not necessarily OAuth: Unity and GAPI are their own protocols.
        """
        raise ConnectorNotConfigured(self._unconfigured("authenticate"))

    def capabilities(self) -> CommercialCapability:
        """What this connection can write, confirmed against the endpoint."""
        raise ConnectorNotConfigured(self._unconfigured("read capabilities from"))

    def create(self, resource_type: str, resource: dict, *, dry_run: bool = True) -> CommercialWrite:
        """Write one resource. Dry run by default, like the FHIR writer."""
        raise ConnectorNotConfigured(self._unconfigured(f"write {resource_type} to"))

    # -- shared behaviour -------------------------------------------

    def available(self) -> bool:
        """True when this deployment has configured the connector.

        A stub answers False, which is what lets the delivery path offer
        the commercial route only where it actually exists.
        """
        return False

    def refusal(self) -> str:
        """Why a delivery to this vendor refuses today, in one paragraph."""
        return self._unconfigured("write to")

    def _unconfigured(self, verb: str) -> str:
        return (
            f"cannot {verb} {self.vendor_key or 'this vendor'}: the certified FHIR API does not "
            f"accept this write, and {self.product or 'the vendor’s commercial API'} is not "
            f"configured for this deployment. {self.how_to_obtain} "
            f"{('Contact: ' + self.contact) if self.contact else ''}".strip()
        )

    def describe(self) -> dict:
        """Everything a screen or a runbook needs to explain this vendor."""
        return {
            "vendor_key": self.vendor_key,
            "product": self.product,
            "is_fhir": self.is_fhir,
            "how_to_obtain": self.how_to_obtain,
            "contact": self.contact,
            "available": self.available(),
            "sources": list(self.sources),
        }
