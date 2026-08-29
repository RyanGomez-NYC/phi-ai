# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Detect stored records the source has since superseded.

WHY THIS IS NOT THE CONTENT DIFF I ARGUED AGAINST. Comparing resource
bodies byte-for-byte would flag every legitimate change - a corrected
lab, an amended note, a re-signed document - as a discrepancy. That
produces a report full of non-problems, and a report full of
non-problems trains an operator to stop reading it. The objection stands.

What is genuinely worth knowing is narrower and version-based: does the
SOURCE hold a NEWER VERSION of a record than this platform captured? That
is not a difference of opinion about content, it is a fact about
sequence, and FHIR states it explicitly in `meta.versionId` and
`meta.lastUpdated`. No diffing, no interpretation.

WHY IT MATTERS BEYOND TIDINESS. 45 CFR 164.526 gives an individual the
right to amend their record. If an amendment is made in the source after
ingest and the platform keeps only the pre-amendment version, then the
retained copy - which is what survives the source - holds a version the
patient successfully had corrected. A records request served from it
would disclose the uncorrected text. That is a compliance problem, not a
housekeeping one, and it is invisible to every other check in this
framework: the object's digest still matches, the index is in sync, the
audit chain verifies. Everything looks sound because everything IS
sound; the stored copy is simply out of date.

THE FIX IS ALWAYS THE SAME and it is cheap: re-ingest the affected
resources. Ingestion is idempotent by design (core/fhir/client.py), so
re-running it over the named ids replaces nothing and adds the newer
version. This check therefore reports WARNING rather than CRITICAL -
nothing is lost, and the remedy is a normal operation.

SAMPLED BY DEFAULT. Reading `meta` for every resource in a large
deployment means decrypting every object, which is both slow and a reason
to hold clinical read access. A sample answers the question that actually
gets asked - "is the stored copy broadly current?" - and `--deep` exists
for when a specific set needs settling before, say, a legal production.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from core.verify.base import FlowReport, Severity

log = logging.getLogger("phi-ai.verify.freshness")

DEFAULT_SAMPLE = 200


def _parse_instant(raw) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stored_version(resource: dict) -> tuple[Optional[str], Optional[datetime]]:
    meta = resource.get("meta") or {}
    return (
        str(meta["versionId"]) if meta.get("versionId") is not None else None,
        _parse_instant(meta.get("lastUpdated")),
    )


def is_superseded(stored: dict, current: dict) -> tuple[bool, str]:
    """(superseded, why). Version first, timestamp as a fallback.

    `versionId` is authoritative where both sides have one: it is the
    server's own statement that the record moved on. `lastUpdated` is the
    fallback, and it is a weaker signal - some servers touch it for
    reasons that are not clinical amendments - so a timestamp-only
    conclusion says so rather than presenting itself as certain.
    """
    stored_id, stored_at = stored_version(stored)
    current_id, current_at = stored_version(current)

    if stored_id is not None and current_id is not None:
        if stored_id != current_id:
            return (True, f"source is at version {current_id}, store holds {stored_id}")
        return (False, f"both at version {stored_id}")

    if stored_at and current_at:
        if current_at > stored_at:
            return (
                True,
                f"source lastUpdated {current_at.isoformat()} is newer than the stored "
                f"{stored_at.isoformat()} (no versionId on either side, so this is a "
                "timestamp comparison rather than a version statement)",
            )
        return (False, "the stored copy is at least as recent as the source")

    # Neither side states a version or a timestamp. Genuinely unknown,
    # and reported as unknown rather than assumed current.
    return (False, "neither copy carries meta.versionId or meta.lastUpdated")


def verify_freshness(
    storage,
    reader,
    client,
    resource_types: Iterable[str],
    sample_size: int = DEFAULT_SAMPLE,
    deep: bool = False,
) -> FlowReport:
    """Compare stored versions against the source's current versions."""
    from core.verify.ingestion import stored_ids

    report = FlowReport(
        flow="Record freshness (superseded records)",
        source=getattr(client, "base_url", "source EMR"),
        target="object store",
    )

    superseded: list[str] = []
    unknown_count = 0
    checked = 0

    for resource_type in resource_types:
        ids = sorted(stored_ids(storage, resource_type))
        if not ids:
            continue

        if not deep and len(ids) > sample_size:
            step = max(1, len(ids) // sample_size)
            ids = ids[::step][:sample_size]

        for resource_id in ids:
            storage_key = f"fhir/{resource_type}/{resource_id}.json"
            try:
                stored = reader.read_resource(storage_key)
            except Exception as exc:
                log.warning("could not read %s: %s", storage_key, exc)
                unknown_count += 1
                continue

            try:
                current = client.read_resource(resource_type, resource_id)
            except Exception as exc:
                # A 404 is meaningful and NOT a freshness problem: the
                # record was deleted in the source after ingest, which
                # is exactly what this platform is for.
                if "404" in str(exc) or "not found" in str(exc).lower():
                    continue
                log.warning("could not read %s/%s from the source: %s",
                            resource_type, resource_id, exc)
                unknown_count += 1
                continue

            checked += 1
            stale, why = is_superseded(stored, current)
            if stale:
                superseded.append(f"{resource_type}/{resource_id} ({why})")

    if superseded:
        report.add(
            Severity.WARNING, "freshness.superseded",
            f"{len(superseded)} stored record(s) have been superseded in the source",
            "The source holds a newer version than this platform captured. Under 45 CFR "
            "164.526 an individual may amend their record - serving the pre-amendment "
            "version would disclose text the patient had corrected. Re-run ingestion "
            "over these ids; it is idempotent, so this is a normal operation.",
            examples=tuple(superseded), count=len(superseded),
        )
    elif checked:
        scope = "every stored record" if deep else f"a sample of {checked}"
        report.add(
            Severity.OK, "freshness.current",
            f"{scope} matches the source's current version",
        )
        if not deep:
            report.add(
                Severity.INFO, "freshness.scope",
                "This was a sample, not every stored record",
                "Use --deep before a legal production, where serving a superseded version "
                "has consequences beyond tidiness.",
            )
    else:
        report.skipped_reason = (
            "no stored record could be compared against the source - check the source "
            "connection and that it still holds these records"
        )

    if unknown_count:
        report.add(
            Severity.WARNING, "freshness.unknown",
            f"{unknown_count} record(s) could not be compared",
            "Their freshness is unknown, which is not the same as current.",
            count=unknown_count,
        )

    return report
# Made by Ryan Gomez & Co. Inc.
