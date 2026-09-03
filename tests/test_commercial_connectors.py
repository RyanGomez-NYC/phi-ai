# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The commercial connectors are stubs, and must behave like honest stubs.

The danger with an unimplemented integration is not that it fails - it is
that it quietly succeeds at nothing. These tests hold the package to the
one property that makes a refusal trustworthy: every path that would touch
a vendor raises, with a reason naming what is missing.
"""
from __future__ import annotations

import pytest

from core.fhir.commercial import (
    CONNECTORS,
    CommercialConnector,
    ConnectorNotConfigured,
    connector_for,
    explain,
    vendors_with_commercial_path,
)
from core.fhir.emr_profiles import PROFILES

ALL = sorted(CONNECTORS)


@pytest.mark.parametrize("key", ALL)
def test_every_connector_is_for_a_real_vendor(key: str) -> None:
    """A connector for a vendor the platform does not profile is a typo."""
    assert key in PROFILES, f"{key} has a connector but no profile"


@pytest.mark.parametrize("key", ALL)
def test_every_entry_point_refuses_until_configured(key: str) -> None:
    """Nothing here may return a plausible empty success."""
    c = connector_for(key)
    assert isinstance(c, CommercialConnector)
    assert c.available() is False, "a stub must not claim to be available"
    with pytest.raises(ConnectorNotConfigured):
        c.authenticate()
    with pytest.raises(ConnectorNotConfigured):
        c.capabilities()
    with pytest.raises(ConnectorNotConfigured):
        c.create("DocumentReference", {"resourceType": "DocumentReference"})
    # Even a dry run refuses: composing a write we cannot send is not a
    # success either, and saying so is the whole point of the stub.
    with pytest.raises(ConnectorNotConfigured):
        c.create("DocumentReference", {}, dry_run=True)


@pytest.mark.parametrize("key", ALL)
def test_a_refusal_names_the_product_and_how_to_get_it(key: str) -> None:
    """An operator reading the refusal must know what to do next."""
    c = connector_for(key)
    assert c.product, f"{key}: no product named"
    assert len(c.how_to_obtain) > 40, f"{key}: how_to_obtain is not an explanation"
    assert c.sources, f"{key}: no source cited for its claims"
    msg = c.refusal()
    assert c.product in msg
    assert c.how_to_obtain[:30] in msg


@pytest.mark.parametrize("key", ALL)
def test_a_connector_only_claims_what_the_profile_agrees_with(key: str) -> None:
    """A vendor whose certified surface already writes needs no commercial
    path to explain it - unless the commercial path is a DIFFERENT product
    from the certified one. Three are legitimately both, each for a reason
    that can be checked against the vendor:

      epic     certified DocumentReference create exists, but the write API
               must be licensed and enabled per health system and per type
      nextgen  certified DocumentReference create exists alongside the
               separate proprietary Enterprise APIs
      nextech  Select/NexCloud documents DocumentReference create; the wider
               Practice+ write surface is a different product

    Anything else claiming both is a contradiction between two files, and
    this list failing when a fourth appears is the point: someone should
    have to justify it."""
    profile = PROFILES[key]
    if key in {"epic", "nextgen", "nextech"}:
        assert profile.write_notes, f"{key} is exempted but its profile explains nothing"
        return
    assert not profile.writable_resources, (
        f"{key} has writable_resources {profile.writable_resources} on its certified API "
        f"and a commercial connector; one of the two is wrong"
    )


def test_vendors_without_a_commercial_path_say_so_plainly() -> None:
    """None is an answer, not a gap - and the explanation must not imply
    that buying something would help when nothing is published."""
    for key in ("trubridge", "practicefusion", "meditech", "netsmart"):
        assert key in PROFILES
        assert connector_for(key) is None, f"{key} unexpectedly has a connector"
        msg = explain(key)
        assert "no commercial write path" in msg
        assert key in msg


def test_the_registry_and_its_helper_agree() -> None:
    assert vendors_with_commercial_path() == tuple(ALL)
    assert connector_for("not-a-vendor") is None
    assert connector_for("") is None


def test_describe_is_safe_to_render() -> None:
    """The demo and the runbooks render this; it must be plain data."""
    for key in ALL:
        d = connector_for(key).describe()
        assert set(d) == {"vendor_key", "product", "is_fhir", "how_to_obtain",
                          "contact", "available", "sources"}
        assert d["available"] is False
        assert isinstance(d["sources"], list)
