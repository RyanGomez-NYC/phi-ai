# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Import DICOM into the platform: `python -m core.dicom`

    python -m core.dicom import /mnt/pacs-export
    python -m core.dicom import /mnt/pacs-export --dry-run
    python -m core.dicom count /mnt/pacs-export

A BULK IMPORT IS A LONG-RUNNING, RESUMABLE OPERATION, not a transaction.
A PACS export is routinely hundreds of gigabytes across millions of
files, and any run of that length will be interrupted. So an instance
already present in storage is skipped by key rather than re-encrypted,
which makes re-running the same command the supported way to finish an
interrupted import - not a special repair mode.

THE SKIP REASON BELOW IS A CONTRACT WITH core/dicom/ingest.py. That
module appends the literal string "already stored" for an instance whose
object key is already in storage, and the summary here counts on that
exact value to tell a resumed-import skip apart from an unreadable file.
Nothing enforces the agreement at import time, so when the two drift the
failure is silent and misleading rather than loud: a resumed import
reports every skipped instance as unreadable, which reads like a corrupt
export. tests/test_dicom.py pins the same literal a third time. Change
all three together, or none.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

log = logging.getLogger("phi-ai.dicom.cli")

# The exact reason string core/dicom/ingest.py records for an instance
# skipped because its object key already exists. See this module's
# docstring.
_ALREADY_STORED = "already stored"


def _build(settings):
    from core.audit.log import AuditLog
    from core.crypto.envelope import EnvelopeEncryptor
    from core.storage.factory import build_audit_sink, build_kms, build_storage

    storage = build_storage(settings)
    sink = build_audit_sink(settings)
    audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())
    encryptor = EnvelopeEncryptor(kms=build_kms(settings))

    connection_factory = None
    if settings.imaging_target_configured():
        from core.db.connection import connect

        connection_factory = lambda: connect(settings, settings.imaging_db_username)  # noqa: E731
    else:
        log.warning(
            "PHI_AI_IMAGING_DB_USERNAME is not set, so imaging will be stored but not "
            "indexed. The objects are safe and retrievable by key, but nothing can "
            "search them and the viewer has nothing to talk to. See "
            "runbooks/RUNBOOK_DICOM_IMAGING.md."
        )
    return storage, encryptor, audit, connection_factory


def _retention_years(settings) -> int:
    """Imaging retention, which is commonly not this deployment's default.

    Read through the same per-resource-type override mechanism every
    other type uses, keyed on ImagingStudy - so a retention ruleset file
    can carry a citation and a reviewer for it like any other figure,
    rather than imaging silently inheriting a number chosen for
    documents.
    """
    from core.dicom.model import RETENTION_KEY

    return settings.retention_years_overrides.get(RETENTION_KEY, settings.retention_years)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.dicom")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="store a directory of DICOM files")
    imp.add_argument("directory", help="root of the export to walk")
    imp.add_argument("--dry-run", action="store_true",
                     help="report what would be stored, write nothing")
    imp.add_argument("--reimport", action="store_true",
                     help="re-encrypt and overwrite instances already stored")
    imp.add_argument("--actor", default=None, help="name recorded in the audit trail")

    cnt = sub.add_parser("count", help="count DICOM files under a directory")
    cnt.add_argument("directory")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2

    from core.dicom.ingest import DICOMIngestor, iter_dicom_files

    if args.command == "count":
        total = sum(1 for _ in iter_dicom_files(root))
        print(f"{total:,} DICOM file(s) under {root}")
        return 0

    if args.dry_run:
        import pydicom

        studies, instances = set(), 0
        for path in iter_dicom_files(root):
            try:
                dataset = pydicom.dcmread(path, stop_before_pixels=True)
                studies.add(str(dataset.StudyInstanceUID))
                instances += 1
            except Exception as exc:
                print(f"  unreadable: {path} ({exc})")
        print(f"\n{instances:,} instance(s) across {len(studies):,} stud(ies). Nothing written.")
        return 0

    from core.config.settings import ConfigError, Settings

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 1

    storage, encryptor, audit, connection_factory = _build(settings)
    actor = args.actor or _operator_name()

    ingestor = DICOMIngestor(
        storage=storage,
        encryptor=encryptor,
        audit=audit,
        connection_factory=connection_factory,
        retention_years=_retention_years(settings),
        actor=actor,
    )

    seen = {"n": 0}

    def progress(result):
        seen["n"] += 1
        if seen["n"] % 500 == 0:
            print(f"  ... {result.instance_count:,} stored, {len(result.skipped):,} skipped")

    result = ingestor.ingest_directory(
        root, skip_existing=not args.reimport, progress=progress
    )

    print(f"\nStored {result.instance_count:,} instance(s) "
          f"across {len(result.studies):,} stud(ies), "
          f"{result.bytes_stored / 1_000_000:,.0f} MB.")
    if result.skipped:
        already = sum(1 for _, reason in result.skipped if reason == _ALREADY_STORED)
        failed = len(result.skipped) - already
        print(f"Skipped {already:,} already stored, {failed:,} unreadable.")
        for path, reason in result.skipped[:10]:
            if reason != _ALREADY_STORED:
                print(f"  {path}: {reason}")
    return 0


def _operator_name() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
