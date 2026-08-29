# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
DICOMweb: what the OHIF viewer actually talks to.

The viewer has no concept of buckets, encryption or an index - it speaks
DICOMweb (DICOM PS3.18) and nothing else. So embedding a viewer means
serving three things:

  QIDO-RS   search for studies, series and instances       (the worklist)
  WADO-RS   retrieve metadata and the objects themselves   (the images)
  WADO-URI  retrieve one object by query parameter         (legacy, simple)

This module is the pure half: index rows and decrypted bytes in, DICOM
JSON and object bytes out. It opens no connection, checks no permission
and writes no audit entry - core/web/dicomweb_routes.py does all three,
so that the rules about who may read what and what gets recorded live
next to every other route in this application rather than in a corner
that a reviewer has to find.

DICOM JSON, NOT A CONVENIENT SHAPE. Responses are the model defined in
PS3.18 Annex F: an object keyed by eight-digit hexadecimal tag, each
value carrying its VR and a `Value` array, with person names as
`{"Alphabetic": "..."}`. It is verbose and it is what viewers parse.

METADATA IS DECRYPTED ON DEMAND, and that is a real cost worth stating
rather than discovering. `/studies/{uid}/metadata` reads every instance
in the study - for a 2,000-slice CT that is 2,000 object reads and 2,000
AES-GCM decryptions before the viewer draws anything. The alternative,
writing a metadata sidecar object per instance at ingest, would double
the deployment's object count, and docs/SCALING.md is explicit that
object count is the number this system scales on. So the cost is paid at
read time, by the person who opened the study, rather than permanently by
every deployment. Series-level metadata is the cheaper path and is what
a viewer should be pointed at; see PHI_AI_IMAGING_MAX_STUDY_METADATA.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

log = logging.getLogger("phi-ai.dicom.dicomweb")

# DICOM tags this module emits, by keyword. Spelled out rather than
# resolved through pydicom's dictionary at request time: these are the
# QIDO-RS return keys (PS3.18 Table 10.6.1-5) and being able to read the
# exact wire shape off this file is worth more than the indirection.
STUDY_TAGS = {
    "StudyDate": ("00080020", "DA"),
    "StudyTime": ("00080030", "TM"),
    "AccessionNumber": ("00080050", "SH"),
    "ModalitiesInStudy": ("00080061", "CS"),
    "ReferringPhysicianName": ("00080090", "PN"),
    "StudyDescription": ("00081030", "LO"),
    "PatientName": ("00100010", "PN"),
    "PatientID": ("00100020", "LO"),
    "PatientBirthDate": ("00100030", "DA"),
    "PatientSex": ("00100040", "CS"),
    "StudyInstanceUID": ("0020000D", "UI"),
    "NumberOfStudyRelatedSeries": ("00201206", "IS"),
    "NumberOfStudyRelatedInstances": ("00201208", "IS"),
}

SERIES_TAGS = {
    "Modality": ("00080060", "CS"),
    "SeriesDescription": ("0008103E", "LO"),
    "BodyPartExamined": ("00180015", "CS"),
    "SeriesInstanceUID": ("0020000E", "UI"),
    "SeriesNumber": ("00200011", "IS"),
    "NumberOfSeriesRelatedInstances": ("00201209", "IS"),
}

INSTANCE_TAGS = {
    "SOPClassUID": ("00080016", "UI"),
    "SOPInstanceUID": ("00080018", "UI"),
    "InstanceNumber": ("00200013", "IS"),
    "NumberOfFrames": ("00280008", "IS"),
    "Rows": ("00280010", "US"),
    "Columns": ("00280011", "US"),
    "BitsAllocated": ("00280100", "US"),
}

# Person Name is the one VR whose JSON value is an object rather than a
# scalar - a component group with Alphabetic/Ideographic/Phonetic parts.
_PN = "PN"


def _value(vr: str, raw: Any) -> Optional[dict]:
    """One DICOM JSON attribute, or None when there is nothing to send.

    An absent attribute is OMITTED rather than sent with a null or an
    empty Value array. PS3.18 F.2.5 permits a zero-length Value for a
    present-but-empty attribute, but a viewer reading `Value[0]` on an
    attribute that was never stored is a viewer that crashes on old
    studies, and old studies are the entire population here.
    """
    if raw is None or raw == "":
        return None
    if vr == _PN:
        return {"vr": vr, "Value": [{"Alphabetic": str(raw)}]}
    if vr in ("US", "IS", "SL", "SS", "UL"):
        try:
            return {"vr": vr, "Value": [int(raw)]}
        except (TypeError, ValueError):
            return None
    return {"vr": vr, "Value": [str(raw)]}


def _build(tags: dict, row: dict, mapping: dict[str, str]) -> dict:
    out: dict[str, dict] = {}
    for keyword, column in mapping.items():
        tag, vr = tags[keyword]
        value = _value(vr, row.get(column))
        if value is not None:
            out[tag] = value
    return out


def study_json(row: dict) -> dict:
    """A QIDO-RS study result from an index row."""
    return _build(
        STUDY_TAGS,
        row,
        {
            "StudyInstanceUID": "study_instance_uid",
            "StudyDate": "study_date",
            "StudyTime": "study_time",
            "AccessionNumber": "accession_number",
            "ModalitiesInStudy": "modalities",
            "ReferringPhysicianName": "referring_physician_name",
            "StudyDescription": "study_description",
            "PatientName": "patient_name",
            "PatientID": "patient_id",
            "PatientBirthDate": "patient_birth_date",
            "PatientSex": "patient_sex",
            "NumberOfStudyRelatedSeries": "series_count",
            "NumberOfStudyRelatedInstances": "instance_count",
        },
    )


def series_json(row: dict) -> dict:
    return _build(
        SERIES_TAGS,
        row,
        {
            "SeriesInstanceUID": "series_instance_uid",
            "Modality": "modality",
            "SeriesNumber": "series_number",
            "SeriesDescription": "series_description",
            "BodyPartExamined": "body_part_examined",
            "NumberOfSeriesRelatedInstances": "instance_count",
        },
    )


def instance_json(row: dict) -> dict:
    return _build(
        INSTANCE_TAGS,
        row,
        {
            "SOPInstanceUID": "sop_instance_uid",
            "SOPClassUID": "sop_class_uid",
            "InstanceNumber": "instance_number",
            "NumberOfFrames": "number_of_frames",
            "Rows": "rows",
            "Columns": "columns",
            "BitsAllocated": "bits_allocated",
        },
    )


def instance_metadata(raw: bytes) -> dict:
    """Full DICOM JSON metadata for one instance, pixel data excluded.

    Pixel data is dropped deliberately: PS3.18 requires bulk data be
    referenced rather than inlined in a metadata response, and a
    base64-encoded CT slice inside a JSON document would make the
    response tens of megabytes for information the viewer fetches
    separately anyway.
    """
    import io

    import pydicom

    dataset = pydicom.dcmread(io.BytesIO(raw), stop_before_pixels=True)
    metadata = dataset.to_json_dict()
    # Belt and braces - stop_before_pixels should already have excluded
    # these, but a file with pixel data in an unexpected place should not
    # produce a 40 MB JSON response.
    for tag in ("7FE00010", "7FE00008", "7FE00009"):
        metadata.pop(tag, None)
    return metadata


def frame_bytes(raw: bytes, frame_numbers: list[int]) -> Iterator[bytes]:
    """Raw pixel bytes for the requested 1-based frame numbers.

    Handles both storage forms. Encapsulated (JPEG, JPEG 2000, RLE)
    transfer syntaxes keep each frame as its own fragment sequence and
    pydicom can extract one without decoding it - which matters, because
    decoding would require codec libraries this project does not ship and
    the viewer decodes client-side anyway. Native (uncompressed) pixel
    data is one contiguous block, so a frame is a byte-range slice
    computed from the image dimensions.
    """
    import io

    import pydicom
    from pydicom.uid import UID

    dataset = pydicom.dcmread(io.BytesIO(raw))
    pixel_data = dataset.get("PixelData")
    if pixel_data is None:
        raise ValueError("instance has no pixel data")

    transfer_syntax = UID(str(dataset.file_meta.TransferSyntaxUID))
    total_frames = int(getattr(dataset, "NumberOfFrames", 1) or 1)

    for number in frame_numbers:
        if number < 1 or number > total_frames:
            raise ValueError(f"frame {number} out of range (1-{total_frames})")

        if transfer_syntax.is_encapsulated:
            from pydicom.encaps import get_frame

            yield get_frame(dataset.PixelData, number - 1, number_of_frames=total_frames)
            continue

        rows = int(dataset.Rows)
        columns = int(dataset.Columns)
        samples = int(getattr(dataset, "SamplesPerPixel", 1) or 1)
        bits = int(getattr(dataset, "BitsAllocated", 8) or 8)
        frame_length = rows * columns * samples * (bits // 8)
        start = (number - 1) * frame_length
        yield bytes(dataset.PixelData[start : start + frame_length])


def wado_rs_url(base: str, study_uid: str, series_uid: str = "", sop_uid: str = "") -> str:
    """The RetrieveURL a viewer follows to fetch what a QIDO result names."""
    url = f"{base.rstrip('/')}/studies/{study_uid}"
    if series_uid:
        url += f"/series/{series_uid}"
    if sop_uid:
        url += f"/instances/{sop_uid}"
    return url
# Made by Ryan Gomez & Co. Inc.
