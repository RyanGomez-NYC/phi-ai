# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Epic FHIR Bulk Data Export client (Group-level).

PRIMARY SOURCE: fhir.epic.com/Documentation?docId=fhir_bulk_data, read
directly and cited throughout this module - not summarized from general
FHIR Bulk Data IG knowledge, since Epic's implementation deviates from
the general spec in load-bearing ways (see below).

WHY THIS EXISTS: Epic's regular per-type search API (core/fhir/client.py)
cannot do population-level queries. A live 400 response against the real
sandbox confirmed this directly: "This resource requires demographics or
_id parameter for searching" - Epic rejects an unscoped "give me every
Patient" search outright. Bulk Data Export is the only way to retrieve
data at population scale.

THREE FACTS FROM EPIC'S OWN DOCS THAT SHAPE EVERYTHING BELOW:

1. "Epic supports only the Group Export operation. We do not support
   _since or other bulk data operations at this time." There is no
   System-level or Patient-level export, and NO incremental/delta
   capability - every kickoff is a full re-extract of the Group's
   entire scope, every time. This is why the functions below take no
   `since` parameter the way core/fhir/client.py's iter_resources()
   does - there is nothing Epic-side for it to do.

2. Epic's own "Poor Use Cases for Bulk Data" guidance explicitly lists
   "Periodic loads of large amounts of clinical data," "Incremental data
   loads," and "Data synchronization with data warehouses or other
   databases" - which describes this project's core use case. Real-world
   confirmation this is an enforced limit, not just guidance: Epic
   rate-limits kickoff to once per 24 hours per group+client ID by
   default (documented independently at
   good-neighbor.smarthealthit.org/tips/, with the exact rejection text
   "The Client requested this Group too recently"). Given both of these,
   this module is meant to be run on a coarse, daily-at-most cadence
   (see core/fhir/bulk_scheduler.py), not scheduler.py's hourly default.
   Re-processing the same resources on every run is expected and safe:
   core/db/index.py's write_index_entry() already treats a duplicate
   (resource_type, resource_id) as a no-op, and S3 keys are addressed by
   resource type + ID, so a repeat write of an unchanged resource is
   idempotent, not wastefully-but-silently broken.

3. A Group FHIR ID is not discoverable through any API - "Contact the
   organization you are integrating with to discuss what group of
   patients to use for your integration and to get the FHIR ID for that
   group." For sandbox testing that means emailing openepic@epic.com;
   in a real deployment it comes from the healthcare organization. See
   PHI_AI_FHIR_GROUP_ID in core/config/settings.py.

RECOMMENDED POLL INTERVAL (Epic's own tutorial, verbatim): "every ten
minutes for groups with a hundred or fewer patients, every thirty
minutes for groups over a hundred, or using exponential backoff."
DEFAULT_POLL_INTERVAL_SECONDS below defaults to the ten-minute figure;
override for larger groups.

Separate Incoming API registrations are required beyond the regular
per-type search APIs: Bulk Data Kick-off, Bulk Data Status Request,
Bulk Data File Request, and Bulk Data Delete Request.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Iterator, Optional

log = logging.getLogger("phi-ai.fhir.bulk_client")

# Epic's own recommendation for groups of 100 or fewer patients - see
# module docstring for the citation. Raise this (Epic suggests 30
# minutes) for larger groups.
DEFAULT_POLL_INTERVAL_SECONDS = 600

# Generous but not unbounded. Epic's technical-constraints section gives
# requests up to fourteen days before results are deleted, but a process
# that might wait that long unattended is an operational risk in its own
# right - four hours comfortably covers any group at or under Epic's own
# recommended size cap (see RECOMMENDED_MAX_GROUP_SIZE) while still
# failing loudly, rather than silently, if something is actually stuck.
DEFAULT_MAX_WAIT_SECONDS = 4 * 60 * 60

# From good-neighbor.smarthealthit.org/tips/ (independent of Epic's own
# tutorial): "Epic recommends no more than 1,000 patients should be
# exported at once." Not enforced here - this project doesn't control
# group membership - but kept as a named constant so the documented
# figure lives in one place rather than as a comment someone has to find.
RECOMMENDED_MAX_GROUP_SIZE = 1000


class BulkExportError(Exception):
    """Raised for problems with the bulk export request/job itself - not
    raised for per-resource-type processing errors, which are logged and
    skipped the same way core/fhir/scheduler.py already handles them."""


@dataclass
class BulkExportJob:
    status_url: str
    resource_types_requested: Optional[list[str]] = None


def kickoff_export(
    base_url: str,
    group_id: str,
    access_token: str,
    resource_types: Optional[list[str]] = None,
    timeout: int = 30,
) -> BulkExportJob:
    """
    Start a Group-level bulk export. Returns the status polling URL from
    the Content-Location response header - per Epic's documentation,
    "the value of the 'Content-Location' header is the status URL for
    this bulk data request."

    `resource_types`, if given, is passed as the `_type` parameter.
    Epic's docs recommend this whenever possible: "limiting the scope of
    the request to only the resources you need decreases both response
    times and the amount of data stored."
    """
    import requests

    url = f"{base_url.rstrip('/')}/Group/{group_id}/$export"
    params = {}
    if resource_types:
        params["_type"] = ",".join(resource_types)

    headers = {
        "Authorization": f"Bearer {access_token}",
        # Both required per Epic's documentation ("Your request must
        # include the following headers") - not optional extras.
        "Accept": "application/fhir+json",
        "Prefer": "respond-async",
    }

    log.info("Kicking off bulk export: group=%s resource_types=%s", group_id, resource_types or "(all)")
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)

    if resp.status_code != 202:
        log.error("Bulk export kickoff failed: status=%s body=%s", resp.status_code, resp.text)
        resp.raise_for_status()
        raise BulkExportError(f"Expected 202 Accepted from kickoff, got {resp.status_code}")

    status_url = resp.headers.get("Content-Location")
    if not status_url:
        raise BulkExportError("Kickoff response was 202 but had no Content-Location header")

    log.info("Bulk export kicked off, status URL: %s", status_url)
    return BulkExportJob(status_url=status_url, resource_types_requested=resource_types)


def poll_status(status_url: str, access_token: str, timeout: int = 30) -> Optional[dict]:
    """
    Single status check - does not loop or sleep; see wait_for_export()
    for that. Returns None while still processing (202 - per Epic's
    docs, "If the bulk data request has not finished processing, the
    response body is empty"), or the completed manifest dict once ready
    (200).
    """
    import requests

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(status_url, headers=headers, timeout=timeout)

    if resp.status_code == 202:
        progress = resp.headers.get("X-Progress", "")
        log.info("Bulk export still in progress%s", f": {progress}" if progress else "")
        return None

    if resp.status_code == 200:
        return resp.json()

    log.error("Unexpected bulk export status response: status=%s body=%s", resp.status_code, resp.text)
    resp.raise_for_status()
    raise BulkExportError(f"Unexpected status code {resp.status_code} from status check")


def wait_for_export(
    job: BulkExportJob,
    access_token: str,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> dict:
    """
    Poll until the export completes or max_wait_seconds elapses.

    FIXED: previously tracked elapsed time with an accumulator that only
    counted time.sleep() calls, not the real wall-clock time each
    poll_status() HTTP round-trip itself took, and the loop's exit
    condition let one final full sleep happen even when that sleep would
    push past max_wait_seconds - concretely, with the defaults (4h
    ceiling, 10-minute polls), the function could run for up to 4h10m
    before actually raising, ten minutes past what its own ceiling
    documented. Now tracks real elapsed wall-clock time via time.time(),
    and caps the final sleep to whatever budget actually remains rather
    than always sleeping a full poll_interval_seconds - it will not wait
    past max_wait_seconds before giving up.
    """
    start = time.monotonic()
    while True:
        manifest = poll_status(job.status_url, access_token)
        if manifest is not None:
            errors = manifest.get("error") or []
            if errors:
                log.warning("Bulk export completed with %d request-level error(s): %s", len(errors), errors)
            return manifest

        elapsed = time.monotonic() - start
        remaining = max_wait_seconds - elapsed
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))

    raise BulkExportError(
        f"Bulk export did not complete within {max_wait_seconds}s "
        f"(status URL: {job.status_url}). Epic allows up to 14 days before "
        "results are deleted, so this is a configured ceiling, not Epic's own limit - "
        "raise max_wait_seconds if this export is genuinely expected to take longer."
    )


def iter_ndjson_resources(file_url: str, access_token: str, timeout: int = 120) -> Iterator[dict]:
    """
    Stream and parse one NDJSON output file. Per Epic's docs: "The
    format of the bulk data files is ndjson... similar to JSON, but is
    newline-sensitive." Streams rather than loading the whole file into
    memory first - a single file can hold up to 3,000 resource instances
    per Epic's documented cap.
    """
    import requests

    headers = {"Authorization": f"Bearer {access_token}"}
    with requests.get(file_url, headers=headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.strip():
                continue
            yield json.loads(line)


def delete_export(status_url: str, access_token: str, timeout: int = 30) -> None:
    """
    Best-effort cleanup after a completed export has been fully
    processed. Not required - Epic auto-deletes after fourteen days
    regardless - but freeing the request sooner is good practice and
    matches the documented Bulk Data Delete Request operation. Failures
    here are logged, not raised: this is cleanup, not part of the
    ingestion guarantee.
    """
    import requests

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.delete(status_url, headers=headers, timeout=timeout)
        if resp.status_code not in (202, 204, 404):
            log.warning("Bulk export delete returned unexpected status %s for %s", resp.status_code, status_url)
    except Exception as exc:
        log.warning("Bulk export delete request failed (non-fatal): %s", exc)
# Made by Ryan Gomez & Co. Inc.
