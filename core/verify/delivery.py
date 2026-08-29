# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Verify store -> destination EMR: does it actually hold what we sent?

A delivery reports what it wrote. That is a record of what this system
BELIEVES happened, which is not the same as what the destination now
holds. A create can return 201 and then be rejected downstream by an
interface engine, land in a staging area a clinician never sees, or be
merged into a different record by the destination's own duplicate
handling. None of those look like failures from this side.

HOW THIS FINDS DELIVERED RECORDS. core/fhir/delivery/writer.py tags every
delivered resource with `meta.source` naming the exact stored object it
came from. That tag is what makes verification possible at all: without
it there is no way to ask a destination "do you have the record that came
from this storage key", only "do you have something that looks similar",
which is guessing.

WHAT A MISS MEANS, and why it is a WARNING rather than CRITICAL: the
object store still holds the record. Nothing is lost - the delivery simply
did not land, and can be repeated. That is the opposite of an ingestion
gap, where the source is gone and the record with it. Severity here tracks
recoverability, not effort.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from core.fhir.delivery.writer import PRIOR_RECORD_TAG_SYSTEM
from core.verify.base import FlowReport, Severity

log = logging.getLogger("phi-ai.verify.delivery")


def verify_delivery(
    delivery_result,
    destination_base_url: str,
    access_token: str,
    http_get=None,
) -> FlowReport:
    """Confirm each record a delivery claims to have sent is present."""
    report = FlowReport(
        flow="EMR delivery confirmation",
        source="store",
        target=destination_base_url,
    )

    if delivery_result.dry_run:
        report.skipped_reason = (
            "the delivery was a dry run, so nothing was sent and there is nothing to "
            "confirm"
        )
        return report

    search = http_get or _default_search
    sent = [item for item in delivery_result.items if item.sent]

    if not sent:
        report.skipped_reason = "the delivery sent no records"
        return report

    confirmed, missing, unknown = [], [], []

    for item in sent:
        # Query on meta.source, which is exactly the stored object key.
        # Matching on anything looser would confirm the presence of a
        # similar record rather than THIS one.
        query = (
            f"{destination_base_url}/{item.resource_type}"
            f"?_source={quote(f'{item.storage_key}', safe='')}"
        )
        try:
            bundle = search(query, access_token)
        except Exception as exc:
            log.warning("could not confirm %s: %s", item.storage_key, exc)
            unknown.append(item.storage_key)
            continue

        total = bundle.get("total")
        entries = bundle.get("entry") or []
        found = total if isinstance(total, int) else len(entries)

        if found == 0:
            missing.append(item.storage_key)
        elif found > 1:
            # Worth flagging on its own: a duplicate in a live chart is a
            # different problem from an absence, and one this project's
            # conditional-create handling is specifically meant to prevent.
            report.add(
                Severity.WARNING, "delivery.duplicate",
                f"{item.resource_type} {item.source_id} appears {found} times in the "
                "destination",
                "More than one copy of a delivered record suggests the delivery ran twice "
                "against a destination without conditional create.",
                examples=(item.storage_key,),
            )
            confirmed.append(item.storage_key)
        else:
            confirmed.append(item.storage_key)

    if confirmed:
        report.add(
            Severity.OK, "delivery.confirmed",
            f"{len(confirmed)} delivered record(s) confirmed present in the destination",
            count=len(confirmed),
        )
    if missing:
        report.add(
            Severity.WARNING, "delivery.missing",
            f"{len(missing)} record(s) were reported as delivered but are NOT in the "
            "destination",
            "The object store still holds these, so nothing is lost - the delivery did not "
            "land and can be repeated. Common causes: an interface engine rejected them "
            "downstream, or they are in a staging area not exposed to search.",
            examples=tuple(missing), count=len(missing),
        )
    if unknown:
        report.add(
            Severity.WARNING, "delivery.unconfirmed",
            f"{len(unknown)} record(s) could not be checked",
            "The destination did not answer the confirmation query. Their status is "
            "unknown - which is not the same as delivered.",
            examples=tuple(unknown), count=len(unknown),
        )

    return report


def _default_search(url: str, access_token: str) -> dict:
    import requests

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/fhir+json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()
# Made by Ryan Gomez & Co. Inc.
