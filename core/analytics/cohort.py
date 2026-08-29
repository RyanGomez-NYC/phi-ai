# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Curated population queries over the OMOP analytics layer.

WHY THESE EXIST ALONGSIDE THE SQL TOOL. The generated-SQL path in
sql_guard.py can express anything; these cover the questions that get
asked constantly, and they cover them the same way every time. That
matters more here than it would in a reporting tool, for one specific
reason: **counting patients is a join away from being wrong, and wrong in
a direction nobody notices.**

`SELECT count(*) FROM cdm.condition_occurrence WHERE ...` answers "how
many diagnoses", not "how many patients" - a patient diagnosed with
diabetes at four visits counts four times. The number looks plausible,
nobody queries it twice, and it is roughly 3x too high on a real
deployment. Every count in this file is `COUNT(DISTINCT person_id)` for
that reason. An LLM writing fresh SQL gets this right most of the time,
which is precisely the failure mode worth removing from the common path.

CODES, NOT JUST CONCEPTS. `condition_concept_id` is 0 on every row unless
the deploying organisation has loaded the OHDSI Athena vocabulary, which
is a separate licensed download this project cannot bundle (see
README.md). So every lookup here matches on BOTH the standard concept and
the raw `condition_source_value` - the ICD-10 or SNOMED code Epic sent.
That makes "how many patients have diabetes" answerable on a deployment
with no vocabulary at all, which is most of them, and better on one that
has it. Vocabulary-only matching would have produced a confident zero.

WHAT A ZERO MEANS IS AMBIGUOUS AND IS REPORTED AS SUCH. "No patients
match" and "this resource type was never ingested" are different answers,
and a bare 0 conflates them. Every result here carries the deployment's
denominator alongside the numerator so the caller can tell.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("phi-ai.analytics.cohort")

# Common condition families, as ICD-10 prefixes. Deliberately small and
# deliberately not a clinical terminology: this is a convenience for the
# questions operators actually ask, not an attempt to reimplement a
# vocabulary. Anything not here goes through the code or the SQL tool.
#
# Sourced from the ICD-10-CM chapter ranges. A deployment that has loaded
# the Athena vocabulary should prefer concept matching, which these
# supplement rather than replace.
CONDITION_SHORTCUTS: dict[str, tuple[str, ...]] = {
    "diabetes": ("E08", "E09", "E10", "E11", "E13"),
    "hypertension": ("I10", "I11", "I12", "I13", "I15"),
    "asthma": ("J45",),
    "copd": ("J44",),
    "heart failure": ("I50",),
    "atrial fibrillation": ("I48",),
    "chronic kidney disease": ("N18",),
    "depression": ("F32", "F33"),
    "anxiety": ("F41",),
    "obesity": ("E66",),
    "breast cancer": ("C50",),
    "lung cancer": ("C34",),
    "colorectal cancer": ("C18", "C19", "C20"),
    "stroke": ("I63", "I64"),
    "myocardial infarction": ("I21", "I22"),
    "dementia": ("F01", "F02", "F03", "G30"),
    "pregnancy": ("Z34", "O80", "O09"),
}


@dataclass
class CohortResult:
    question: str
    patient_count: int
    # The deployment's total, so a small number can be read in proportion
    # and a zero can be told apart from an empty deployment.
    total_patients_stored: int
    matched_on: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    detail: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "patients_matching": self.patient_count,
            "total_patients_stored": self.total_patients_stored,
            "matched_on": self.matched_on,
            "caveats": self.caveats,
            "detail": self.detail,
        }


def _query(conn, sql: str, params: tuple = ()) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def _total_patients(conn) -> int:
    rows = _query(conn, "SELECT COUNT(*) FROM cdm.person")
    return int(rows[0][0]) if rows else 0


def _vocabulary_loaded(conn) -> bool:
    """Whether the Athena vocabulary is present.

    Determines whether concept matching can contribute anything. Cheap,
    and the answer changes what the caveats say.
    """
    try:
        rows = _query(conn, "SELECT 1 FROM vocab.concept LIMIT 1")
        return bool(rows)
    except Exception:
        return False


def resolve_condition(term: str) -> tuple[tuple[str, ...], Optional[str]]:
    """(ICD-10 prefixes, the shortcut name used) for a plain-English term."""
    normalised = (term or "").strip().lower()
    if normalised in CONDITION_SHORTCUTS:
        return CONDITION_SHORTCUTS[normalised], normalised
    for name, prefixes in CONDITION_SHORTCUTS.items():
        if name in normalised or normalised in name:
            return prefixes, name
    return (), None


def count_patients_with_condition(
    conn, term: str, since: Optional[str] = None, until: Optional[str] = None
) -> CohortResult:
    """How many DISTINCT patients have a matching condition.

    `term` is either a plain-English name from CONDITION_SHORTCUTS, or a
    code prefix ("E11", "I50.9"). Both are matched as prefixes against
    condition_source_value, and against concept_name when a vocabulary is
    loaded.
    """
    prefixes, shortcut = resolve_condition(term)
    matched_on: list[str] = []
    caveats: list[str] = []

    clauses: list[str] = []
    params: list[Any] = []

    if prefixes:
        matched_on.append(
            f"ICD-10 codes starting {', '.join(prefixes)} (the '{shortcut}' shortcut)"
        )
        clauses.append(
            "(" + " OR ".join(["co.condition_source_value LIKE %s"] * len(prefixes)) + ")"
        )
        params.extend(f"{p}%" for p in prefixes)
    else:
        # Treat the term itself as a code prefix.
        matched_on.append(f"condition codes starting '{term}'")
        clauses.append("co.condition_source_value LIKE %s")
        params.append(f"{term}%")

    if _vocabulary_loaded(conn):
        matched_on.append("standard concept names containing the term")
        clauses.append(
            "co.condition_concept_id IN ("
            "  SELECT concept_id FROM vocab.concept WHERE lower(concept_name) LIKE %s"
            ")"
        )
        params.append(f"%{(shortcut or term).lower()}%")
    else:
        caveats.append(
            "The OHDSI Athena vocabulary is not loaded in this deployment, so matching "
            "used the raw source codes Epic sent rather than standard concepts. A "
            "condition recorded under a code outside the ranges above will not be "
            "counted - see runbooks/RUNBOOK_OMOP_SETUP.md."
        )

    where = "(" + " OR ".join(clauses) + ")"
    if since:
        where += " AND co.condition_start_date >= %s"
        params.append(since)
    if until:
        where += " AND co.condition_start_date <= %s"
        params.append(until)

    # COUNT(DISTINCT person_id), never COUNT(*) - see the module docstring.
    rows = _query(
        conn,
        f"SELECT COUNT(DISTINCT co.person_id) FROM cdm.condition_occurrence co WHERE {where}",
        tuple(params),
    )
    count = int(rows[0][0]) if rows else 0

    total = _total_patients(conn)
    caveats.append(
        "Counts patients with the condition RECORDED in this deployment. A patient "
        "diagnosed before the stored period, or at an organisation whose records "
        "are not here, will not appear."
    )
    return CohortResult(
        question=f"patients with {term}",
        patient_count=count,
        total_patients_stored=total,
        matched_on=matched_on,
        caveats=caveats,
    )


def count_patients_by_facility(
    conn, facility: Optional[str] = None, since: Optional[str] = None,
    until: Optional[str] = None, limit: int = 50,
) -> CohortResult:
    """Distinct patients seen per care site.

    With no `facility`, returns the breakdown across all of them, which is
    what "how many patients went to each facility" actually asks. With
    one, returns that site only, matched on name substring or source id.
    """
    params: list[Any] = []
    where = ["vo.care_site_id IS NOT NULL"]

    if facility:
        where.append("(lower(cs.care_site_name) LIKE %s OR cs.care_site_source_value = %s)")
        params.extend([f"%{facility.strip().lower()}%", facility.strip()])
    if since:
        where.append("vo.visit_start_date >= %s")
        params.append(since)
    if until:
        where.append("vo.visit_start_date <= %s")
        params.append(until)

    clause = " AND ".join(where)
    rows = _query(
        conn,
        f"""
        SELECT COALESCE(cs.care_site_name, '(unnamed)') AS facility,
               cs.care_site_source_value,
               COUNT(DISTINCT vo.person_id) AS patients,
               COUNT(*)                     AS visits
          FROM cdm.visit_occurrence vo
          JOIN cdm.care_site cs ON cs.care_site_id = vo.care_site_id
         WHERE {clause}
         GROUP BY 1, 2
         ORDER BY patients DESC
         LIMIT {int(limit)}
        """,
        tuple(params),
    )

    detail = [
        {"facility": r[0], "facility_id": r[1], "patients": int(r[2]), "visits": int(r[3])}
        for r in rows
    ]
    # Summing per-facility counts would double-count anyone seen at two
    # sites, so the headline number is its own DISTINCT query.
    distinct = _query(
        conn,
        f"""
        SELECT COUNT(DISTINCT vo.person_id)
          FROM cdm.visit_occurrence vo
          JOIN cdm.care_site cs ON cs.care_site_id = vo.care_site_id
         WHERE {clause}
        """,
        tuple(params),
    )
    count = int(distinct[0][0]) if distinct else 0

    caveats = [
        "A patient seen at more than one facility is counted once in the headline "
        "figure and once per facility in the breakdown, so the rows will not sum to "
        "the total.",
    ]
    unmapped = _query(
        conn, "SELECT COUNT(*) FROM cdm.visit_occurrence WHERE care_site_id IS NULL"
    )
    if unmapped and int(unmapped[0][0]):
        caveats.append(
            f"{int(unmapped[0][0]):,} visit(s) have no facility recorded and are excluded. "
            "Encounters ingested before facility mapping was added carry no care site "
            "until the OMOP layer is re-run - see runbooks/RUNBOOK_OMOP_SETUP.md."
        )

    return CohortResult(
        question=f"patients seen at {facility}" if facility else "patients by facility",
        patient_count=count,
        total_patients_stored=_total_patients(conn),
        matched_on=["cdm.visit_occurrence joined to cdm.care_site"],
        caveats=caveats,
        detail=detail,
    )


def list_facilities(conn, limit: int = 100) -> list[dict]:
    rows = _query(
        conn,
        f"""
        SELECT cs.care_site_name, cs.care_site_source_value,
               COUNT(DISTINCT vo.person_id), COUNT(vo.visit_occurrence_id)
          FROM cdm.care_site cs
          LEFT JOIN cdm.visit_occurrence vo ON vo.care_site_id = cs.care_site_id
         GROUP BY 1, 2
         ORDER BY 3 DESC NULLS LAST
         LIMIT {int(limit)}
        """,
    )
    return [
        {"facility": r[0], "facility_id": r[1], "patients": int(r[2] or 0),
         "visits": int(r[3] or 0)}
        for r in rows
    ]


def population_demographics(conn) -> dict:
    """Population shape: how many people, their age spread, sex breakdown.

    The question behind most first questions - "what is actually in here?"
    - answered without anyone having to write a query.
    """
    total = _total_patients(conn)
    by_gender = _query(
        conn,
        "SELECT COALESCE(gender_source_value, 'unknown'), COUNT(*) "
        "FROM cdm.person GROUP BY 1 ORDER BY 2 DESC",
    )
    birth_years = _query(
        conn,
        "SELECT MIN(year_of_birth), MAX(year_of_birth), "
        "       COUNT(*) FILTER (WHERE year_of_birth IS NULL) "
        "FROM cdm.person",
    )
    visits = _query(conn, "SELECT COUNT(*), MIN(visit_start_date), MAX(visit_start_date) "
                          "FROM cdm.visit_occurrence")
    return {
        "patients": total,
        "by_gender": {r[0]: int(r[1]) for r in by_gender},
        "earliest_birth_year": birth_years[0][0] if birth_years else None,
        "latest_birth_year": birth_years[0][1] if birth_years else None,
        "patients_with_no_birth_date": int(birth_years[0][2]) if birth_years else 0,
        "visits": int(visits[0][0]) if visits else 0,
        "earliest_visit": str(visits[0][1]) if visits and visits[0][1] else None,
        "latest_visit": str(visits[0][2]) if visits and visits[0][2] else None,
    }
# Made by Ryan Gomez & Co. Inc.
