# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Commercial connectors: the write paths vendors sell separately.

WHAT THIS PACKAGE IS FOR. Nine of the fifteen profiles in
`core/fhir/emr_profiles.py` advertise create for nothing on their
CERTIFIED FHIR API, so `core/fhir/delivery/writer.py` refuses every write
to them - correctly, because the destination's own CapabilityStatement
says it will not accept one. That is not the whole story about those
vendors. Most of them do sell a fuller path:

    eClinicalWorks   FHIR Create/Update, a contracted add-on
    Altera           the proprietary Unity API, Integrator tiers
    Veradigm         the proprietary Unity API, Integrator tiers
    Greenway         GAPI, a proprietary API on its own portal
    ModMed           the EMA Proprietary API, for a fee
    MEDHOST          the licensed Interoperability package
    NextGen          the proprietary Enterprise APIs
    Nextech          Practice+ / Select writes, per product
    Epic             its write APIs, licensed per health system and type

None of those is reachable with the credentials a certified connector
gets. Each needs a contract, a separate registration, usually a different
base URL, and in several cases a protocol that is not FHIR at all. So the
platform cannot ship them working - and it should not pretend to.

WHAT IT DOES INSTEAD. One stub per vendor, each one:

  * REFUSES by default, with a message naming the vendor's own product,
    what has to be bought or signed, and who to ask. A stub that returned
    a plausible empty success would be the worst outcome here: a delivery
    that silently went nowhere.
  * States its evidence. Every claim in a stub is quoted from that
    vendor's documentation, and `docs/EXTENDING_CONNECTORS.md` says which
    page.
  * Is a real seam, not a comment. `CommercialConnector` has the same
    shape for every vendor, so implementing one is filling in three
    methods, and the delivery path can ask for one by vendor key without
    knowing which vendors have one.

Nothing here changes what the certified path does. A commercial connector
is consulted only when the operator has configured one, and its writes are
audited exactly like a FHIR delivery.
"""

from core.fhir.commercial.base import (  # noqa: F401
    CommercialConnector,
    CommercialWriteRefused,
    ConnectorNotConfigured,
    CommercialCapability,
)
from core.fhir.commercial.registry import (  # noqa: F401
    CONNECTORS,
    connector_for,
    explain,
    vendors_with_commercial_path,
)
