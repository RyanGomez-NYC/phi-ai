# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Verify object store -> exported files: did the export contain everything?

core/fhir/bulk_export.py exists so this project is never a one-way door:
whatever it stores, a deployer can get back out in a format any
FHIR-aware system reads. That promise is only as good as the export being
COMPLETE, and an export that silently omitted a resource type - because a
prefix was wrong, a page was dropped, or a write failed late - looks
exactly like a successful one.

This compares the ids in the NDJSON against the ids in storage. Storage
is authoritative, as everywhere else in this project: the index can drift,
the export is derived, the objects are the record.

Reads the export by IDENTIFIER, not by decrypting and diffing content.
The exported NDJSON is plaintext PHI by nature - that is what makes it
useful to a receiving system - so a verification that had to decrypt the
object store to compare bodies would need clinical read access it does
not otherwise require. Comparing id sets answers "is anything missing"
without that.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.verify.base import FlowReport, Severity
from core.verify.ingestion import stored_ids

log = logging.getLogger("phi-ai.verify.export")


def exported_ids(export_dir: str, resource_type: str) -> set[str]:
    """Ids present in the NDJSON file for one resource type."""
    path = Path(export_dir) / f"{resource_type}.ndjson"
    if not path.is_file():
        return set()

    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                # A malformed line is itself a finding: the receiving
                # system would fail on it too, and reporting it here is
                # cheaper than discovering it during a migration.
                log.error("%s line %d is not valid JSON: %s", path, line_number, exc)
                continue
            rid = resource.get("id")
            if rid:
                ids.add(str(rid))
    return ids


def verify_export(storage, export_dir: str, resource_types) -> FlowReport:
    report = FlowReport(
        flow="Bulk export completeness",
        source="object store",
        target=export_dir,
    )

    if not Path(export_dir).is_dir():
        report.skipped_reason = f"no export directory at {export_dir}"
        return report

    for resource_type in resource_types:
        in_store = stored_ids(storage, resource_type)
        in_export = exported_ids(export_dir, resource_type)

        if not in_store and not in_export:
            continue

        missing = sorted(in_store - in_export)
        extra = sorted(in_export - in_store)

        if missing:
            report.add(
                Severity.CRITICAL, f"export.{resource_type}",
                f"{len(missing)} stored {resource_type} record(s) are absent from the export",
                "The export is incomplete. A receiving system given this file would be "
                "missing these records with no indication anything was omitted.",
                examples=tuple(missing), count=len(missing),
            )
        if extra:
            report.add(
                Severity.WARNING, f"export.{resource_type}",
                f"{len(extra)} exported {resource_type} record(s) are not in the store",
                "The export contains records storage does not. Most likely a stale export "
                "from before a disposal run.",
                examples=tuple(extra), count=len(extra),
            )
        if not missing and not extra:
            report.add(
                Severity.OK, f"export.{resource_type}",
                f"{resource_type}: {len(in_store)} record(s), export matches the store",
            )

    return report
# Made by Ryan Gomez & Co. Inc.
