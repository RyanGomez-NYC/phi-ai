# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Reading and writing the DICOM imaging index.

The queries QIDO-RS needs, and nothing else. Every function takes an open
connection rather than opening one, matching core/db/index.py - the
caller owns the connection because the caller owns the role it connected
as, and which role that is carries the access-control weight here.

MATCHING SEMANTICS ARE DICOM'S, NOT SQL's. QIDO-RS (PS3.18 §10.6) defines
single-value matching, wildcard matching with `*` and `?`, and range
matching on dates with `-`. Those are translated here rather than passed
through: a viewer sends `StudyDate=20240101-20241231` and expects a
range, and sending that string to a `=` comparison would silently return
nothing - a wrong answer that looks like an empty index.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("phi-ai.dicom.index")

# QIDO-RS default and ceiling. A viewer that asks for everything on a
# deployment holding a million studies would otherwise stream a million
# rows into a browser.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _wildcard(value: str) -> tuple[str, str]:
    """Translate a DICOM matching value into (operator, sql pattern).

    DICOM wildcards are `*` (any number of characters) and `?` (exactly
    one) - SQL LIKE spells the same two `%` and `_`. The literal SQL
    wildcards are escaped FIRST, so a patient whose name genuinely
    contains a percent sign does not become a wildcard search.
    """
    if "*" in value or "?" in value:
        pattern = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = pattern.replace("*", "%").replace("?", "_")
        return "ILIKE", pattern
    return "=", value


def _date_range(value: str) -> Optional[tuple[str, str]]:
    """DICOM date range matching: `a-b`, `a-` (from), `-b` (until).

    Returns (low, high) with empty strings for an open end, or None when
    the value is not a range at all.
    """
    if "-" not in value:
        return None
    low, _, high = value.partition("-")
    return low.strip(), high.strip()


# QIDO search keys this index supports, mapped to their column. Anything
# a viewer sends that is not here is IGNORED rather than rejected: PS3.18
# permits a server to support a subset, and failing a whole query because
# of one unsupported optional key would make the viewer look broken.
_STUDY_SEARCH_COLUMNS = {
    "PatientID": "patient_id",
    "PatientName": "patient_name",
    "AccessionNumber": "accession_number",
    "StudyInstanceUID": "study_instance_uid",
    "StudyDescription": "study_description",
    "ModalitiesInStudy": "modalities",
}


def search_studies(
    conn,
    filters: Optional[dict[str, str]] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    patient_reference: Optional[str] = None,
) -> list[dict]:
    """Studies matching the QIDO filters.

    `patient_reference` is NOT a QIDO filter - it is a server-side
    restriction the caller may impose regardless of what the viewer
    asked for, used to scope a search to one patient. A viewer cannot
    widen it by sending a different PatientID, because it is ANDed on top
    of everything else.
    """
    clauses: list[str] = []
    params: list[Any] = []

    for key, value in (filters or {}).items():
        column = _STUDY_SEARCH_COLUMNS.get(key)
        if not column or not value:
            continue
        if column == "modalities":
            # ModalitiesInStudy matches if ANY modality in the study
            # matches - the study column is a comma-separated list.
            clauses.append("modalities ILIKE %s")
            params.append(f"%{value}%")
            continue
        operator, pattern = _wildcard(value)
        clauses.append(f"{column} {operator} %s")
        params.append(pattern)

    study_date = (filters or {}).get("StudyDate")
    if study_date:
        span = _date_range(study_date)
        if span is None:
            clauses.append("study_date = %s")
            params.append(study_date)
        else:
            low, high = span
            if low:
                clauses.append("study_date >= %s")
                params.append(low)
            if high:
                clauses.append("study_date <= %s")
                params.append(high)

    if patient_reference:
        clauses.append("patient_reference = %s")
        params.append(patient_reference)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))])
    return _rows(
        conn,
        f"SELECT * FROM dicom_studies {where} "
        "ORDER BY study_date DESC NULLS LAST, study_instance_uid LIMIT %s OFFSET %s",
        tuple(params),
    )


def get_study(conn, study_uid: str) -> Optional[dict]:
    rows = _rows(conn, "SELECT * FROM dicom_studies WHERE study_instance_uid = %s", (study_uid,))
    return rows[0] if rows else None


def series_for_study(conn, study_uid: str) -> list[dict]:
    return _rows(
        conn,
        "SELECT * FROM dicom_series WHERE study_instance_uid = %s "
        "ORDER BY series_number NULLS LAST, series_instance_uid",
        (study_uid,),
    )


def instances_for_study(conn, study_uid: str) -> list[dict]:
    return _rows(
        conn,
        "SELECT * FROM dicom_instances WHERE study_instance_uid = %s "
        "ORDER BY series_instance_uid, instance_number NULLS LAST",
        (study_uid,),
    )


def instances_for_series(conn, study_uid: str, series_uid: str) -> list[dict]:
    return _rows(
        conn,
        "SELECT * FROM dicom_instances WHERE study_instance_uid = %s "
        "AND series_instance_uid = %s ORDER BY instance_number NULLS LAST",
        (study_uid, series_uid),
    )


def get_instance(conn, study_uid: str, series_uid: str, sop_uid: str) -> Optional[dict]:
    """One instance, addressed by its full three-UID path.

    All three are matched, not just the SOP UID. A SOP Instance UID is
    globally unique so matching on it alone would find the same row - but
    then a caller authorised for one study could retrieve an instance from
    another by pairing a permitted study UID with someone else's SOP UID.
    Matching the whole path makes the URL mean what it says.
    """
    rows = _rows(
        conn,
        "SELECT * FROM dicom_instances WHERE study_instance_uid = %s "
        "AND series_instance_uid = %s AND sop_instance_uid = %s",
        (study_uid, series_uid, sop_uid),
    )
    return rows[0] if rows else None


def upsert_study(conn, study, retention_until=None) -> None:
    """Insert or refresh a study row. Idempotent by design.

    Re-ingesting a study - after a failed run, or because the source
    exported it twice - must not duplicate or double-count it. Counts are
    recomputed by refresh_counts() from the instance rows rather than
    incremented here, so they are correct however many times this runs.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO dicom_studies (
                study_instance_uid, patient_reference, patient_id, patient_name,
                patient_birth_date, patient_sex, accession_number, study_date,
                study_time, study_description, referring_physician_name, retention_until
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (study_instance_uid) DO UPDATE SET
                patient_reference = EXCLUDED.patient_reference,
                patient_id = EXCLUDED.patient_id,
                patient_name = EXCLUDED.patient_name,
                patient_birth_date = EXCLUDED.patient_birth_date,
                patient_sex = EXCLUDED.patient_sex,
                accession_number = EXCLUDED.accession_number,
                study_date = EXCLUDED.study_date,
                study_time = EXCLUDED.study_time,
                study_description = EXCLUDED.study_description,
                referring_physician_name = EXCLUDED.referring_physician_name,
                retention_until = EXCLUDED.retention_until
            """,
            (
                study.study_instance_uid, study.patient_reference, study.patient_id,
                study.patient_name, study.patient_birth_date, study.patient_sex,
                study.accession_number, study.study_date, study.study_time,
                study.study_description, study.referring_physician_name, retention_until,
            ),
        )
    finally:
        cur.close()


def upsert_series(conn, series) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO dicom_series (
                series_instance_uid, study_instance_uid, modality, series_number,
                series_description, body_part_examined
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (series_instance_uid) DO UPDATE SET
                modality = EXCLUDED.modality,
                series_number = EXCLUDED.series_number,
                series_description = EXCLUDED.series_description,
                body_part_examined = EXCLUDED.body_part_examined
            """,
            (
                series.series_instance_uid, series.study_instance_uid, series.modality,
                series.series_number, series.series_description, series.body_part_examined,
            ),
        )
    finally:
        cur.close()


def upsert_instance(conn, instance) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO dicom_instances (
                sop_instance_uid, series_instance_uid, study_instance_uid, sop_class_uid,
                instance_number, rows, columns, bits_allocated, number_of_frames,
                transfer_syntax_uid, storage_key, storage_version_id, sha256_hex, size_bytes
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (sop_instance_uid) DO UPDATE SET
                storage_key = EXCLUDED.storage_key,
                storage_version_id = EXCLUDED.storage_version_id,
                sha256_hex = EXCLUDED.sha256_hex,
                size_bytes = EXCLUDED.size_bytes,
                transfer_syntax_uid = EXCLUDED.transfer_syntax_uid
            """,
            (
                instance.sop_instance_uid, instance.series_instance_uid,
                instance.study_instance_uid, instance.sop_class_uid,
                instance.instance_number, instance.rows, instance.columns,
                instance.bits_allocated, instance.number_of_frames,
                instance.transfer_syntax_uid, instance.storage_key,
                instance.version_id, instance.sha256_hex, instance.size_bytes,
            ),
        )
    finally:
        cur.close()


def refresh_counts(conn, study_uid: str) -> None:
    """Recompute the denormalised counts and modality list for a study.

    From the rows, not from a running total. A re-ingest, a partial run
    or a retry all converge on the right answer because this is a
    recomputation rather than an increment.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE dicom_series s SET instance_count = ("
            "  SELECT COUNT(*) FROM dicom_instances i "
            "  WHERE i.series_instance_uid = s.series_instance_uid"
            ") WHERE s.study_instance_uid = %s",
            (study_uid,),
        )
        cur.execute(
            "UPDATE dicom_studies st SET "
            "  series_count = (SELECT COUNT(*) FROM dicom_series s "
            "                  WHERE s.study_instance_uid = st.study_instance_uid), "
            "  instance_count = (SELECT COUNT(*) FROM dicom_instances i "
            "                    WHERE i.study_instance_uid = st.study_instance_uid), "
            "  modalities = (SELECT string_agg(DISTINCT s.modality, ',' ORDER BY s.modality) "
            "                FROM dicom_series s "
            "                WHERE s.study_instance_uid = st.study_instance_uid) "
            "WHERE st.study_instance_uid = %s",
            (study_uid,),
        )
    finally:
        cur.close()
# Made by Ryan Gomez & Co. Inc.
