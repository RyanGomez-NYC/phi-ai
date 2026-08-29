# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Verify EMR -> object store: did everything actually get stored?

THIS IS THE VERIFICATION THAT MATTERS MOST, AND THE ONE WITH A DEADLINE.

Every other check in this project compares two things that both still
exist, so it can be run at any time and re-run after a fix. This one
compares the object store against the SOURCE EMR, and the source EMR is
the system being decommissioned. After it is switched off there is
nothing left to compare against: a record that was never stored becomes
permanently missing AND permanently undetectable, because the only
evidence it ever existed went away with the server.

Run this before decommissioning. Not after. Not "at some point during".

TWO DEPTHS, and the difference is worth understanding before choosing:

  COUNTS  - ask the EMR how many of each resource type it holds, compare
            with the object store. One cheap request per type. Catches
            whole missing pages, an interrupted run, a resource type
            nobody configured. Does NOT catch a specific record being
            absent while the totals happen to agree.

  IDENTIFIERS - page every id out of the EMR and compare the sets.
            Definitive: names exactly which records are missing. Costs a
            full read of the source, which is the same work the ingestion
            run already did, so budget for roughly a second ingestion
            pass.

Counts first, identifiers before you sign anything off. A count match is
evidence; an identifier match is proof.

WHY NOT COMPARE CONTENT. This deliberately does not re-read and diff each
resource body. Clinical resources legitimately change in the source after
ingest - a corrected lab, an amended note - so a content difference is
not evidence of an ingestion failure, and treating it as one would
produce noise that trains an operator to ignore the report. Presence is
the question this answers.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from core.verify.base import FlowReport, Severity

log = logging.getLogger("phi-ai.verify.ingestion")


class BundledStoreNeedsReader(RuntimeError):
    """Identifier-level verification of a bundled store needs decryption."""


class BundleUnreadable(RuntimeError):
    """A bundle could not be opened, so its contents cannot be verified."""

# The platform stores one object per resource at fhir/{Type}/{id}.json, so
# the id set can be recovered from storage keys without decrypting
# anything. Verification therefore needs no PHI access at all - it can run
# under an identity that cannot read clinical content, which is a
# meaningful reduction in what a verification job puts at risk.
KEY_PREFIX = "fhir/"


def stored_ids(storage, resource_type: str, reader=None) -> set[str]:
    """Ids the object store holds for one type.

    LAYOUT MATTERS HERE, and getting it wrong is not a rounding error.
    Under the small profile an id is recoverable from the object KEY
    (fhir/Type/id.json), so this needs no decryption at all - which is
    what lets a verification job run under an identity with no clinical
    read access.

    Under the large profile a key is a BUNDLE (fhir/Type/patient.ndjson)
    and the ids are inside it. An earlier version of this function only
    recognised `.json` keys, so on a bundled store it returned an empty
    set and reported EVERY source record as critically missing - a
    verification that is confidently, catastrophically wrong is worse
    than one that refuses to run.

    So: bundles require a `reader` that can decrypt them. Without one
    this raises rather than silently under-reporting, and the caller
    falls back to count-based verification, which needs no decryption
    because the index records how many resources each object holds.
    """
    prefix = f"{KEY_PREFIX}{resource_type}/"
    ids: set[str] = set()
    bundles: list[str] = []

    for key in storage.iter_keys(prefix=prefix):
        name = key[len(prefix):]
        if name.endswith(".json"):
            ids.add(name[: -len(".json")])
        elif name.endswith(".ndjson"):
            bundles.append(key)

    if not bundles:
        return ids

    if reader is None:
        raise BundledStoreNeedsReader(
            f"{len(bundles)} bundled object(s) hold {resource_type} resources, and their "
            "ids are inside the encrypted objects rather than in the object keys. "
            "Identifier-level verification of a bundled store therefore needs "
            "decryption, and this check was given no reader. Use count-based "
            "verification, which reads counts from the index and decrypts nothing."
        )

    for key in bundles:
        try:
            for resource in reader.read_resources(key):
                if resource.get("id"):
                    ids.add(str(resource["id"]))
        except Exception as exc:
            # Surfaced, never swallowed: a bundle that cannot be read is
            # a finding in itself, and treating it as "no resources"
            # would report its contents as missing from the store.
            raise BundleUnreadable(f"could not read bundle {key}: {exc}") from exc

    return ids


def stored_count(storage, resource_type: str, conn=None) -> Optional[int]:
    """How many resources are held for one type, without decrypting.

    Reads resource_count from the index, which is 1 per row under the
    small profile and the bundle's size under the large one - so the same
    query answers the question under either layout.

    Returns None when there is no index, since counting a bundled store
    from storage alone would mean opening every bundle.
    """
    if conn is None:
        prefix = f"{KEY_PREFIX}{resource_type}/"
        keys = list(storage.iter_keys(prefix=prefix))
        if any(k.endswith(".ndjson") for k in keys):
            return None
        return len(keys)

    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COALESCE(SUM(resource_count), 0) FROM stored_resources "
            "WHERE resource_type = %s",
            (resource_type,),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        cursor.close()


def source_count(client, resource_type: str) -> Optional[int]:
    """Ask the EMR how many it holds.

    Uses the FHIR `_summary=count` search parameter, which every R4
    server supports and which returns a Bundle carrying only `total`.
    Returns None when the server declines to give a total - some do, for
    large or restricted result sets - so the caller can report "could not
    determine" rather than silently treating it as zero.
    """
    import requests

    url = f"{client.base_url}/{resource_type}"
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "Accept": "application/fhir+json",
    }
    try:
        response = requests.get(url, params={"_summary": "count"},
                                headers=headers, timeout=60)
        response.raise_for_status()
        total = response.json().get("total")
        return int(total) if total is not None else None
    except Exception as exc:
        log.warning("could not read count for %s: %s", resource_type, exc)
        return None


def source_ids(client, resource_type: str) -> set[str]:
    """Every id the EMR holds for one type, by paging its search API."""
    return {
        str(resource["id"])
        for resource in client.iter_resources(resource_type)
        if resource.get("id")
    }


def verify_ingestion(
    storage,
    client,
    resource_types: Iterable[str],
    deep: bool = False,
    reader=None,
    conn=None,
) -> FlowReport:
    """Compare the source EMR against the object store.

    `deep=False` compares counts; `deep=True` compares identifier sets and
    names the specific records that are missing.
    """
    report = FlowReport(
        flow="EMR ingestion completeness",
        source=getattr(client, "base_url", "source EMR"),
        target="object store",
    )

    report.add(
        Severity.INFO,
        "ingestion.deadline",
        "This check requires the source EMR to still exist",
        "Once the source system is decommissioned there is nothing left to compare "
        "against, and any gap becomes permanently undetectable. Complete this before "
        "switching the source off.",
    )

    for resource_type in resource_types:
        if deep:
            try:
                stored = stored_ids(storage, resource_type, reader=reader)
            except BundledStoreNeedsReader as exc:
                report.add(
                    Severity.WARNING, f"ingestion.{resource_type}",
                    f"{resource_type}: identifier-level check needs decryption on a "
                    "bundled store",
                    str(exc),
                )
                continue
            except BundleUnreadable as exc:
                report.add(
                    Severity.CRITICAL, f"ingestion.{resource_type}",
                    f"{resource_type}: a bundle could not be read",
                    str(exc),
                )
                continue
        else:
            counted = stored_count(storage, resource_type, conn=conn)
            if counted is None:
                report.add(
                    Severity.WARNING, f"ingestion.{resource_type}",
                    f"{resource_type}: cannot count a bundled store without the index",
                    "Configure the Postgres index, or run with deep verification and a "
                    "reader that can decrypt bundles.",
                )
                continue
            stored = None  # count-based below

        if deep:
            try:
                present = source_ids(client, resource_type)
            except Exception as exc:
                report.add(
                    Severity.WARNING, f"ingestion.{resource_type}",
                    f"could not enumerate {resource_type} in the source",
                    str(exc),
                )
                continue

            missing = sorted(present - stored)
            extra = sorted(stored - present)

            if missing:
                report.add(
                    Severity.CRITICAL, f"ingestion.{resource_type}",
                    f"{len(missing)} {resource_type} record(s) in the source are NOT stored",
                    "These exist in the EMR and not in the object store. If the source is "
                    "decommissioned in this state they are lost permanently.",
                    examples=tuple(missing), count=len(missing),
                )
            if extra:
                # Not a failure. Expected whenever a record was stored
                # and later deleted or corrected in the source - this
                # platform is meant to outlive the source's own retention.
                report.add(
                    Severity.INFO, f"ingestion.{resource_type}",
                    f"{len(extra)} stored {resource_type} record(s) are no longer in the source",
                    "Expected when a record was deleted or superseded in the EMR after "
                    "ingest. This platform is meant to outlive the source's own retention.",
                    examples=tuple(extra), count=len(extra),
                )
            if not missing and not extra:
                report.add(
                    Severity.OK, f"ingestion.{resource_type}",
                    f"{resource_type}: {len(stored)} record(s), identifiers match exactly",
                )
            continue

        total = source_count(client, resource_type)
        stored_total = counted
        if total is None:
            report.add(
                Severity.WARNING, f"ingestion.{resource_type}",
                f"the source would not report a count for {resource_type}",
                "Re-run with deep verification to compare identifiers directly.",
            )
            continue

        if stored_total < total:
            report.add(
                Severity.CRITICAL, f"ingestion.{resource_type}",
                f"{resource_type}: source has {total}, store has {stored_total}",
                f"{total - stored_total} record(s) appear to be missing. Re-run with deep "
                "verification to identify exactly which.",
                count=total - stored_total,
            )
        elif stored_total > total:
            report.add(
                Severity.INFO, f"ingestion.{resource_type}",
                f"{resource_type}: store has {stored_total}, source has {total}",
                "Expected when records were deleted in the source after ingest.",
            )
        else:
            report.add(
                Severity.OK, f"ingestion.{resource_type}",
                f"{resource_type}: {total} record(s), counts match",
            )

    return report
# Made by Ryan Gomez & Co. Inc.
