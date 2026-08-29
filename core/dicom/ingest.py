# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Bring a directory of DICOM files into the platform.

    python -m core.dicom import /mnt/pacs-export

WHY FILESYSTEM IMPORT RATHER THAN DICOM NETWORKING. This platform holds
records for systems being retired, and imaging leaves a decommissioned
PACS as a bulk export onto a disk or a NAS share far more often than it
leaves over a live DIMSE association. Reading a directory needs no AE
title registration on either side, no listening DICOM port, no firewall
change and no transfer-syntax negotiation - and everything downstream of
ingest (storage layout, index, DICOMweb serving) is identical whichever
source eventually feeds it, so adding C-MOVE or a DICOMweb pull later
changes this file and nothing else.

THE FILE IS STORED BYTE-FOR-BYTE. What is encrypted and written is the
original DICOM Part 10 file exactly as it was read - not a re-serialised
dataset. Re-encoding would silently normalise private tags, padding and
transfer syntax, and a retired imaging system is precisely where those
details are least reproducible. Metadata is parsed separately, for the
index only; the bytes are never round-tripped through pydicom.

AUDITING IS PER STUDY, NOT PER INSTANCE, and that is a deliberate
deviation from core/fhir/client.py, which records one entry per resource
written. A single CT can be several thousand instances, and
core/audit/sink.py writes ONE OBJECT PER EVENT - so per-instance
auditing would multiply the audit log by three orders of magnitude and
make the chain verification this project depends on materially slower,
in exchange for forensic detail the index and a storage listing already
provide. Storing a study is one operation a human performs and one entry
is what makes the trail readable. Reads are audited per study for the
same reason - see core/web/dicomweb_routes.py, where the reasoning
matters more because a read is a disclosure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from core.dicom import model

log = logging.getLogger("phi-ai.dicom.ingest")

# Read for metadata only. Pixel data is the overwhelming majority of a
# DICOM file's bytes and none of it reaches the index, so parsing it
# would be the difference between an ingest that runs overnight and one
# that runs for a week.
_METADATA_ONLY = True


class DICOMIngestError(RuntimeError):
    pass


def _stored_sha256_hex(nonce: bytes, ciphertext: bytes) -> str:
    """Digest over the exact bytes written, matching core/fhir/client.py.

    The same contract, for the same reason: the nonce is prefixed onto
    the ciphertext in storage, so a digest over the ciphertext alone
    would describe bytes that were never written and every integrity
    check would fail against a healthy object.
    """
    return hashlib.sha256(nonce + ciphertext).hexdigest()


def _retention_until(now: datetime, years: int) -> Optional[datetime]:
    """Full calendar years, matching core/fhir/client.py's _retention_until."""
    if years < 1:
        return None
    try:
        return now.replace(year=now.year + years)
    except ValueError:
        # 29 February in a non-leap target year.
        return now.replace(year=now.year + years, day=28)


def iter_dicom_files(root: Path) -> Iterator[Path]:
    """Every file under `root` that is plausibly DICOM.

    Detection is by the DICM magic at offset 128, not by extension. A
    PACS export routinely contains files named with no extension at all,
    or with the modality's own convention, and DICOMDIR indexes are
    themselves DICOM files that must NOT be ingested as images.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.upper() == "DICOMDIR":
            # An index of the export, not an image. Walking the tree
            # directly finds everything it points at anyway.
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(128)
                if handle.read(4) != b"DICM":
                    continue
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            continue
        yield path


class DICOMIngestor:
    """Encrypt, store and index a directory of DICOM files.

    Takes its storage, encryptor and audit log as arguments for the same
    reason core/fhir/client.py does: so the whole path can be tested
    without a bucket, a KMS or a database.
    """

    def __init__(
        self,
        storage,
        encryptor,
        audit,
        connection_factory: Optional[Callable[[], object]] = None,
        retention_years: int = 10,
        actor: str = "dicom-import",
    ):
        self.storage = storage
        self.encryptor = encryptor
        self.audit = audit
        self.connection_factory = connection_factory
        self.retention_years = retention_years
        self.actor = actor

    def ingest_directory(
        self, root: Path, skip_existing: bool = True, progress: Optional[Callable] = None
    ) -> model.IngestResult:
        result = model.IngestResult()
        for path in iter_dicom_files(root):
            try:
                self._ingest_file(path, result, skip_existing=skip_existing)
            except Exception as exc:
                log.warning("skipping %s: %s", path, exc)
                result.skipped.append((str(path), str(exc)))
            if progress is not None:
                progress(result)

        self._write_index(result)
        self._audit_studies(result)
        return result

    def _ingest_file(self, path: Path, result: model.IngestResult, skip_existing: bool) -> None:
        import pydicom

        raw = path.read_bytes()
        dataset = pydicom.dcmread(path, stop_before_pixels=_METADATA_ONLY)
        study, series, instance = model.read_records(dataset, size_bytes=len(raw))

        storage_key = model.instance_key(
            study.study_instance_uid, series.series_instance_uid, instance.sop_instance_uid
        )

        if skip_existing and self.storage.object_exists(storage_key):
            # Re-running an interrupted import is the normal case, not the
            # exception - a PACS export is large and an import will be
            # resumed. Re-encrypting produces a different ciphertext for
            # identical input (a fresh nonce), so "already there" is
            # answered by the key, not by comparing digests.
            result.skipped.append((str(path), "already stored"))
            return

        payload = self.encryptor.encrypt(raw)
        storage_bytes = payload.nonce + payload.ciphertext
        digest = _stored_sha256_hex(payload.nonce, payload.ciphertext)

        stored = self.storage.put_object(
            key=storage_key,
            ciphertext=storage_bytes,
            wrapped_dek_b64=payload.wrapped_dek_b64,
            sha256_hex=digest,
            retention_until=_retention_until(datetime.now(timezone.utc), self.retention_years),
            content_type="application/dicom",
        )

        from dataclasses import replace

        result.studies[study.study_instance_uid] = study
        result.series[series.series_instance_uid] = series
        result.instances.append(
            replace(
                instance,
                storage_key=storage_key,
                sha256_hex=digest,
                version_id=stored.version_id,
            )
        )

    def _write_index(self, result: model.IngestResult) -> None:
        """Best-effort index write, exactly like the FHIR path.

        Storage and the audit log are already durable by the time this
        runs. A failure here must never look like a failed write - the
        imaging IS stored either way, and the index can be rebuilt from
        the objects under the `dicom/` prefix.
        """
        if self.connection_factory is None or not result.instances:
            return

        from core.dicom import index as imaging_index

        try:
            conn = self.connection_factory()
        except Exception as exc:
            log.error("imaging index unavailable (imaging stored successfully): %s", exc)
            return

        try:
            retention = _retention_until(datetime.now(timezone.utc), self.retention_years)
            for study in result.studies.values():
                imaging_index.upsert_study(conn, study, retention_until=retention)
            for series in result.series.values():
                imaging_index.upsert_series(conn, series)
            for instance in result.instances:
                imaging_index.upsert_instance(conn, instance)
            for study_uid in result.studies:
                imaging_index.refresh_counts(conn, study_uid)
            if hasattr(conn, "commit"):
                conn.commit()
        except Exception as exc:
            log.error("imaging index write failed (imaging stored successfully): %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _audit_studies(self, result: model.IngestResult) -> None:
        """One entry per study - see this module's docstring."""
        if self.audit is None:
            return
        per_study: dict[str, int] = {}
        for instance in result.instances:
            per_study[instance.study_instance_uid] = per_study.get(instance.study_instance_uid, 0) + 1
        for study_uid, count in per_study.items():
            self.audit.record(
                actor=self.actor,
                action="record.write.imaging.study",
                resource_key=f"{model.study_prefix(study_uid)} ({count} instances)",
                purpose_of_use="operations",
            )
# Made by Ryan Gomez & Co. Inc.
