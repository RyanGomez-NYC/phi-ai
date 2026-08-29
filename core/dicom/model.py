# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
What the platform keeps about a DICOM study, and where it puts it.

ONE OBJECT PER SOP INSTANCE, not per study or per series. A study is not
a file - it is a tree of Study -> Series -> Instance, and a viewer opens
one image at a time. Storing a study as a single object would mean
decrypting and transferring a whole CT (often 1-2 GB, thousands of
slices) to display one slice, which is not a viewer, it is a download.
The cost is object count, and docs/SCALING.md's own table already
anticipates it: 100 TB of DICOM at 100 MB per STUDY is 1.1 million
objects and trivial, but the same holdings counted per INSTANCE are
closer to a billion. Estimate instances, not studies, before choosing a
scale profile.

DICOM HEADERS ARE FULL OF PHI, AND THAT IS NOT AVOIDABLE HERE.
PatientName, PatientID, PatientBirthDate, PatientSex, AccessionNumber,
InstitutionName, ReferringPhysicianName and StudyDescription are all
standard attributes, and a viewer's worklist is made of exactly those
fields - there is no such thing as a de-identified worklist that still
lets a records clerk find the right study. So the imaging index this
module feeds holds identifying PHI by construction. It is therefore a
SEPARATE, OPT-IN store with its own database role, deliberately shaped
like the OMOP analytics layer (core/db/omop_schema.sql) rather than like
the lightweight stored_resources index, whose "no clinical content,
ever" rule stays intact and unchanged.

WHAT THIS MODULE CANNOT PROTECT YOU FROM: burned-in annotation. Pixel
data may itself contain a patient's name, an accession number or a date,
rendered into the image by the acquiring modality - ultrasound and
secondary-capture images especially. No header inspection finds it, this
project does not attempt to strip it, and DICOM's own
BurnedInAnnotation (0028,0301) attribute is optional and frequently
absent or wrong. Anything relying on de-identification needs a dedicated
tool and a human review pass; see runbooks/RUNBOOK_DICOM_IMAGING.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger("phi-ai.dicom.model")

# Retention for imaging is configured under this key in
# PHI_AI_RETENTION_YEARS_OVERRIDES, or as a resource_type in a
# retention ruleset file. "ImagingStudy" rather than "DICOM" so it reads
# as the FHIR resource type an operator already reasons about, and so a
# ruleset can carry a citation for it like any other type - imaging
# retention genuinely differs from general clinical records in several
# states.
RETENTION_KEY = "ImagingStudy"

# Object key prefix. Separate from `fhir/` so a lifecycle rule, an
# inventory report or a cost breakdown can address imaging on its own -
# it will dominate the byte count in any deployment that has it.
KEY_PREFIX = "dicom"


@dataclass(frozen=True)
class InstanceRecord:
    """One SOP instance: the unit that is encrypted, stored and served."""

    sop_instance_uid: str
    sop_class_uid: str
    series_instance_uid: str
    study_instance_uid: str
    instance_number: Optional[int]
    rows: Optional[int]
    columns: Optional[int]
    bits_allocated: Optional[int]
    number_of_frames: int
    transfer_syntax_uid: str
    size_bytes: int

    # Filled in once written.
    storage_key: str = ""
    sha256_hex: str = ""
    version_id: Optional[str] = None


@dataclass(frozen=True)
class SeriesRecord:
    series_instance_uid: str
    study_instance_uid: str
    modality: str
    series_number: Optional[int]
    series_description: Optional[str]
    body_part_examined: Optional[str]


@dataclass(frozen=True)
class StudyRecord:
    """Study-level attributes, including the identifying ones.

    `patient_reference` is what joins imaging to the rest of the stored
    record. DICOM's PatientID is, in an Epic-sourced deployment, the same
    opaque identifier that appears in stored_resources.patient_reference -
    so a study and a patient's FHIR records resolve to each other without
    a second identity map. Where the source PACS used a different
    identifier space, this will not join, and that is a data problem to
    resolve at ingest rather than something this layer can paper over.
    """

    study_instance_uid: str
    patient_id: Optional[str]
    patient_reference: Optional[str]
    patient_name: Optional[str]
    patient_birth_date: Optional[str]
    patient_sex: Optional[str]
    accession_number: Optional[str]
    study_date: Optional[str]
    study_time: Optional[str]
    study_description: Optional[str]
    referring_physician_name: Optional[str]
    modalities: tuple[str, ...] = ()


@dataclass
class IngestResult:
    studies: dict[str, StudyRecord] = field(default_factory=dict)
    series: dict[str, SeriesRecord] = field(default_factory=dict)
    instances: list[InstanceRecord] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    @property
    def instance_count(self) -> int:
        return len(self.instances)

    @property
    def bytes_stored(self) -> int:
        return sum(i.size_bytes for i in self.instances)


def instance_key(study_uid: str, series_uid: str, sop_uid: str) -> str:
    """Object key for one SOP instance.

    Mirrors the DICOM hierarchy exactly, so a key is readable as a
    location in the study tree and a prefix listing enumerates a study or
    a series without touching the index. UIDs are the DICOM standard's own
    identifiers - dotted digits, already opaque, and never a real-world
    identifier - so they are safe in a key for the same reason
    core/db/index.py accepts an EMR's patient reference in one.
    """
    return f"{KEY_PREFIX}/{study_uid}/{series_uid}/{sop_uid}.dcm"


def study_prefix(study_uid: str) -> str:
    return f"{KEY_PREFIX}/{study_uid}/"


def _text(dataset: Any, tag: str) -> Optional[str]:
    """A string attribute, or None. Never raises on a missing tag."""
    value = getattr(dataset, tag, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(dataset: Any, tag: str) -> Optional[int]:
    value = getattr(dataset, tag, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_records(dataset: Any, size_bytes: int) -> tuple[StudyRecord, SeriesRecord, InstanceRecord]:
    """Pull the study/series/instance records out of a parsed dataset.

    Deliberately tolerant. Real collections contain files written by
    decades of different modalities, and a missing SeriesDescription or
    BodyPartExamined is normal rather than corrupt. What is NOT tolerated
    is a missing UID at any of the three levels: without those there is no
    place to put the object and no way to retrieve it, so ingest rejects
    the file rather than inventing one.
    """
    study_uid = _text(dataset, "StudyInstanceUID")
    series_uid = _text(dataset, "SeriesInstanceUID")
    sop_uid = _text(dataset, "SOPInstanceUID")
    if not (study_uid and series_uid and sop_uid):
        missing = [
            name
            for name, value in (
                ("StudyInstanceUID", study_uid),
                ("SeriesInstanceUID", series_uid),
                ("SOPInstanceUID", sop_uid),
            )
            if not value
        ]
        raise ValueError(f"missing required UID(s): {', '.join(missing)}")

    patient_id = _text(dataset, "PatientID")
    study = StudyRecord(
        study_instance_uid=study_uid,
        patient_id=patient_id,
        patient_reference=f"Patient/{patient_id}" if patient_id else None,
        patient_name=_text(dataset, "PatientName"),
        patient_birth_date=_text(dataset, "PatientBirthDate"),
        patient_sex=_text(dataset, "PatientSex"),
        accession_number=_text(dataset, "AccessionNumber"),
        study_date=_text(dataset, "StudyDate"),
        study_time=_text(dataset, "StudyTime"),
        study_description=_text(dataset, "StudyDescription"),
        referring_physician_name=_text(dataset, "ReferringPhysicianName"),
    )

    series = SeriesRecord(
        series_instance_uid=series_uid,
        study_instance_uid=study_uid,
        modality=_text(dataset, "Modality") or "OT",  # OT = Other, the DICOM default
        series_number=_number(dataset, "SeriesNumber"),
        series_description=_text(dataset, "SeriesDescription"),
        body_part_examined=_text(dataset, "BodyPartExamined"),
    )

    transfer_syntax = ""
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None:
        transfer_syntax = str(getattr(file_meta, "TransferSyntaxUID", "") or "")

    instance = InstanceRecord(
        sop_instance_uid=sop_uid,
        sop_class_uid=_text(dataset, "SOPClassUID") or "",
        series_instance_uid=series_uid,
        study_instance_uid=study_uid,
        instance_number=_number(dataset, "InstanceNumber"),
        rows=_number(dataset, "Rows"),
        columns=_number(dataset, "Columns"),
        bits_allocated=_number(dataset, "BitsAllocated"),
        number_of_frames=_number(dataset, "NumberOfFrames") or 1,
        transfer_syntax_uid=transfer_syntax,
        size_bytes=size_bytes,
    )
    return study, series, instance
# Made by Ryan Gomez & Co. Inc.
