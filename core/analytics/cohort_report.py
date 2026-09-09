# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The cohort report: every population metric, at the cohort level, over
the OMOP CDM.

THE COHORT IS THE SCREEN'S COHORT. The persons figure here is the same
predicate the Cohort builder counts with - cohort.cohort_where(): the
condition (a search's exact name, or the contains match), sex and age
band, each alone a cohort and nothing set every person. The two can
never disagree because they are one function.

EVERY CATEGORY COMES FROM THE DATA. Each breakdown is a GROUP BY over
the CDM - sex from gender_source_value, conditions from
condition_source_value, facilities from care_site - never a list typed
here; a hand-written list rots in both directions. The two things that
look like lists are not: the age bands and histogram bins are the
DEFINITION of a dimension computed from a column, and the LOINC codes
below are the identity of a test in a CDM that stores nothing but the
code.

BOTH SERIES IN ONE PASS. "Cohort share beside everyone's share" is one
query per breakdown: the base table LEFT JOINed to the cohort, with a
FILTER on the cohort side - so the two numbers are counted the same
way at the same moment.

SMALL CELLS. A count under 11 renders as "< 11" (a hatched stub on the
chart) and, in a two-cell breakdown or a partition of the persons, its
complement is withheld too - there is no recovery by subtraction.

MINIMUM NECESSARY. Rows whose code falls in a heightened category are
excluded from every code-level breakdown. The category membership is
the deployment's own sensitive-category value sets - the same predicate
core/governance/segmentation.py applies at serialisation - never a list
typed here. Without value sets the code-level breakdowns are WITHHELD
and say so, rather than drawn fail-open with heightened rows in them.
The purpose the report runs under is operations (an analytics read),
which never admits heightened rows: a treatment-purpose read is a
chart, not a report.

NOT IN THIS CDM, SAID IN ONE SENTENCE. This deployment's OMOP layer has
no cost, payer, death or note tables (core/db/omop_schema.sql), no
allergy table (deliberately unmapped), and writes immunizations into
drug_exposure under the same type concept as medication orders, so they
cannot be told apart. Each such section says so and draws nothing.

NO-SHOW RISK. The platform has no no-show scorer over OMOP (its No-show
screen is a registered model slot with no engine behind it yet), so the
distribution here PORTS THE DEMONSTRATION'S HEURISTIC -
www-demo/app/population_ai.php noshow_score() - over
cdm.visit_occurrence: the gap since the last visit, the encounter count,
the age band and the count of distinct conditions, banded at the
No-show page's own thresholds (high ≥ 60, medium ≥ 35). The demo's payer
factor (Medicaid) has no CDM column and is omitted; the table view says
so. It is a heuristic, not a model: no directive rides on it.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Optional

from core.analytics.cohort import (
    BANDS,
    SMALL_CELL,
    SUPPRESSED,
    _query,
    _total_patients,
    age_sql as _age_sql,
    band_sql as _band_sql,
    cohort_definition,
    cohort_where,
    resolve_condition,
    sex_sql as _sex_sql,
)

log = logging.getLogger("phi-ai.analytics.cohort_report")

ENC_BINS = ("0", "1", "2", "3", "4", "5-9", "10+")
MED_BINS = ("0", "1", "2", "3", "4", "5+")
TOP_CONDITIONS = 12
TOP_MEDICATIONS = 12
TOP_PROCEDURES = 10
TOP_FACILITIES = 10
TOP_TESTS = 8
ONSET_YEARS = 20
MONTHS = 24
HIST_BINS = 10
NOSHOW_HIGH = 60      # the No-show page's own thresholds (www-demo/app/views/noshow.php)
NOSHOW_MEDIUM = 35

#: The tests the contract names first, and the two the quality measures
#: read. Terminology facts, not a category list: the CDM stores the LOINC
#: code Epic sent (measurement_source_value) and a test has no other
#: identity here. Source: LOINC (loinc.org) - 4548-4 Hemoglobin A1c/
#: Hemoglobin.total in Blood; 85354-9 Blood pressure panel; 8480-6
#: Systolic blood pressure; 8462-4 Diastolic blood pressure; 39156-5
#: Body mass index.
NAMED_TESTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("A1c", ("4548-4",)),
    ("blood pressure", ("85354-9", "8480-6", "8462-4")),
    ("BMI", ("39156-5",)),
)

NOT_IN_CDM = {
    "cost": ("This deployment's CDM carries no cost tables, so claims per person, "
             "billed / allowed / paid per person, patient responsibility share, payer mix "
             "by paid, claim status and top services are not drawn."),
    "payer": "This deployment's CDM carries no payer table.",
    "deceased": "This deployment's CDM carries no death table, so deceased is not drawn.",
    "documents": "This deployment's CDM carries no note table, so documents by type is not drawn.",
    "allergies": ("This deployment's CDM carries no allergy table - AllergyIntolerance is "
                  "deliberately unmapped (core/db/omop_schema.sql)."),
    "immunizations": ("This deployment's CDM writes immunizations into drug_exposure under the "
                      "same type concept as medication orders, so they cannot be told apart "
                      "from medications here and are not drawn."),
    "flu": ("This deployment's CDM cannot tell an immunization from a medication order "
            "(see immunizations above), so flu immunization in the last 12 months is not "
            "in this CDM."),
}
WITHHELD_NO_VALUE_SETS = (
    "The sensitive-category value sets are not configured on this deployment "
    "(PHI_AI_SENSITIVE_VALUE_SETS), so code-level breakdowns are withheld rather than "
    "drawn with heightened rows in them."
)
# The builder's one sentence where the demonstration has a payer selector:
# the report's own fact about the CDM, restated for the control that is
# not there (the contract's addendum, item 1).
PAYER_SELECTOR_NOTE = NOT_IN_CDM["payer"].rstrip(".") + ", so payer is not a selector here."
# "condition status" with no condition named (the addendum, item 5): the
# status OF a condition, so the sentence and no chart.
NO_CONDITION_STATUS = "No condition was named, so there is no status to show."


class CellError(ValueError):
    """A drill-down cell that cannot be listed: unknown, or a heightened code."""


# ---------------------------------------------------------------------------
# SQL fragments shared by the charts and the drill-down, so the two agree
# by construction
# ---------------------------------------------------------------------------

# The sex, age and band expressions are cohort.sex_sql / age_sql /
# band_sql - the selector's own, imported above - so a chart groups by
# exactly what the definition filtered by.

def _status_sql(alias: str) -> str:
    # The status the source stated when it stated one; otherwise OMOP's own
    # reading of an end date: a condition with none is current.
    return (f"COALESCE(NULLIF(lower({alias}.condition_status_source_value), ''), "
            f"CASE WHEN {alias}.condition_end_date IS NOT NULL THEN 'resolved' ELSE 'active' END)")


_ENC_BIN_SQL = "CASE WHEN n >= 10 THEN '10+' WHEN n >= 5 THEN '5-9' ELSE n::text END"
_MED_BIN_SQL = "CASE WHEN n >= 5 THEN '5+' ELSE n::text END"

# The demonstration's noshow_score() factors, as SQL over one person's
# facts: gap_days (NULL = never seen), enc, age, conds. See the module
# docstring on the port and the omitted payer factor.
_NOSHOW_SCORE_SQL = (
    "LEAST(95, GREATEST(5, 10"
    " + CASE WHEN {gap} IS NULL OR {gap} > 730 THEN 30 WHEN {gap} > 365 THEN 20"
    "        WHEN {gap} > 180 THEN 10 ELSE 0 END"
    " + CASE WHEN {enc} <= 2 THEN 15 WHEN {enc} >= 15 THEN -10 ELSE 0 END"
    " + CASE WHEN {age} BETWEEN 18 AND 34 THEN 12 WHEN {age} >= 75 THEN 8 ELSE 0 END"
    " + CASE WHEN {conds} >= 4 THEN 8 ELSE 0 END))"
)
_NOSHOW_BAND_SQL = (f"CASE WHEN {{score}} >= {NOSHOW_HIGH} THEN 'high' "
                    f"WHEN {{score}} >= {NOSHOW_MEDIUM} THEN 'medium' ELSE 'low' END")
NOSHOW_BANDS = ("low", "medium", "high")

_CODE_TABLES = {
    "condition": ("condition_occurrence", "condition_source_value", "condition_concept_id"),
    "drug": ("drug_exposure", "drug_source_value", "drug_concept_id"),
    "procedure": ("procedure_occurrence", "procedure_source_value", "procedure_concept_id"),
    "measurement": ("measurement", "measurement_source_value", "measurement_concept_id"),
}


def _pct(fraction: Optional[float]) -> str:
    if fraction is None:
        return "—"
    value = float(fraction) * 100
    if value == 0:
        return "0%"
    if value < 1:
        return "<1%"
    if value < 10:
        return f"{value:.1f}%"
    return f"{value:.0f}%"


def _fmt(n) -> str:
    if n is None:
        return "—"
    if isinstance(n, int) or float(n).is_integer():
        return f"{int(n):,}"
    return f"{float(n):,.1f}"


def _visit_label(concept_id) -> str:
    """The FHIR encounter class the ETL mapped this visit concept from -
    read back from the ETL's own table, never restated here."""
    from core.db.omop_etl import _VISIT_CONCEPT_IDS

    try:
        cid = int(concept_id or 0)
    except (TypeError, ValueError):
        return str(concept_id)
    for cls, mapped in _VISIT_CONCEPT_IDS.items():
        if mapped == cid:
            return f"{cls} ({cid})"
    return f"unmapped ({cid})"


# ---------------------------------------------------------------------------
# The cohort definition and the query context
# ---------------------------------------------------------------------------

def excluded_codes(value_sets) -> Optional[tuple[str, ...]]:
    """Every curated heightened code, system-agnostic (the CDM keeps only
    the code). None when no value sets are configured - the caller must
    withhold, not proceed."""
    if value_sets is None:
        return None
    codes = {
        str(code) for members in (getattr(value_sets, "codes", {}) or {}).values()
        for _system, code in members if code
    }
    return tuple(sorted(codes))


def _definition(conn, term: str, sex: str, band: str, exact: bool = False):
    """The cohort CTE: cohort.cohort_where() over cdm.person, whose key is
    person_id - one row per person whatever the condition matched, so
    every breakdown counts persons and never diagnosis rows."""
    where, params, matched_on, caveats, vocabulary = cohort_where(conn, term, sex, band, exact)
    cte = (
        "WITH cohort AS (\n"
        "  SELECT p.person_id\n"
        "    FROM cdm.person p\n"
        f"   WHERE {where}\n"
        ")"
    )
    return cte, list(params), matched_on, caveats, vocabulary


@dataclass
class _Ctx:
    conn: Any
    cte: str
    params: list
    persons: int = 0
    total: int = 0
    encounters: int = 0
    encounters_all: int = 0
    excluded: Optional[tuple[str, ...]] = None
    vocabulary: bool = False
    named: bool = False        # a condition is part of the definition
    today: date = field(default_factory=date.today)

    def q(self, sql: str, params: Iterable = ()) -> list[tuple]:
        """Run one metric query under the cohort CTE. A metric may open with
        ', name AS (...)' to add its own CTEs."""
        return _query(self.conn, self.cte + "\n" + sql, tuple(self.params) + tuple(params))

    @property
    def withheld(self) -> bool:
        return self.excluded is None

    def excl(self, column: str) -> tuple[str, tuple]:
        """' AND column NOT IN (...)' over the heightened codes, and its params."""
        if not self.excluded:
            return "", ()
        return (f" AND {column} NOT IN ({', '.join(['%s'] * len(self.excluded))})",
                tuple(self.excluded))

    def labelled(self, kind: str) -> tuple[str, str]:
        """(FROM clause aliased x, label expression): the standard concept's
        name when the vocabulary is loaded, else the source code."""
        table, code_col, concept_col = _CODE_TABLES[kind]
        if self.vocabulary:
            return (f"cdm.{table} x LEFT JOIN vocab.concept vc ON vc.concept_id = x.{concept_col} "
                    f"AND x.{concept_col} <> 0",
                    f"COALESCE(NULLIF(vc.concept_name, ''), x.{code_col})")
        return f"cdm.{table} x", f"x.{code_col}"


# ---------------------------------------------------------------------------
# Suppression and row shaping
# ---------------------------------------------------------------------------

def _cell(n, denominator) -> dict:
    if n is None:
        return {"n": None, "value": None, "text": "—", "n_text": "—", "suppressed": False}
    n = int(n)
    if n < SMALL_CELL:
        return {"n": None, "value": None, "text": SUPPRESSED, "n_text": SUPPRESSED,
                "suppressed": True}
    share = (n / denominator) if denominator else None
    return {"n": n, "value": share, "text": _pct(share) if denominator else _fmt(n),
            "n_text": _fmt(n), "suppressed": False}


def _suppress(row: dict, s: str) -> None:
    row[s] = None
    row[f"{s}_n"] = None
    row[f"{s}_text"] = SUPPRESSED
    row[f"{s}_n_text"] = SUPPRESSED
    row[f"{s}_suppressed"] = True


def _complement(rows: list[dict], s: str, partition: bool) -> None:
    """No recovery by subtraction. In a two-cell breakdown the complement
    of a withheld cell is withheld too; in a partition of the persons, the
    smallest remaining cell goes as well while exactly one is withheld."""
    if len(rows) < 2 or not (len(rows) == 2 or partition):
        return
    raw = "_nc" if s == "cohort" else "_na"
    while True:
        withheld = [r for r in rows if r[f"{s}_suppressed"]]
        open_ = [r for r in rows if not r[f"{s}_suppressed"]]
        if len(withheld) != 1 or not open_:
            return
        _suppress(min(open_, key=lambda r: r[raw]), s)


def _rows(raw: Iterable[tuple], den_cohort: int, den_all: int, partition: bool = False,
          label_of: Optional[Callable] = None) -> list[dict]:
    rows = []
    for key, label, n_c, n_a in raw:
        c, a = _cell(n_c, den_cohort), _cell(n_a, den_all)
        rows.append({
            "key": "" if key is None else str(key),
            "label": label_of(key, label) if label_of else ("(none)" if label is None else str(label)),
            "cohort": c["value"], "cohort_n": c["n"], "cohort_text": c["text"],
            "cohort_n_text": c["n_text"], "cohort_suppressed": c["suppressed"],
            "everyone": a["value"], "everyone_n": a["n"], "everyone_text": a["text"],
            "everyone_n_text": a["n_text"], "everyone_suppressed": a["suppressed"],
            "_nc": int(n_c or 0), "_na": int(n_a or 0),
        })
    _complement(rows, "cohort", partition)
    _complement(rows, "everyone", partition)
    for r in rows:
        r.pop("_nc")
        r.pop("_na")
    return rows


def _order(rows: list[dict], sequence: Iterable[str]) -> list[dict]:
    """Rows in a dimension's natural order (bands, bins, weekdays), the
    unexpected ones last."""
    rank = {k: i for i, k in enumerate(sequence)}
    return sorted(rows, key=lambda r: (rank.get(r["key"], len(rank)), r["label"]))


def _bins(counts: dict, sequence: Iterable[str], cell: str) -> list[dict]:
    """Histogram bins in order, every count under 11 withheld."""
    out = []
    for label in sequence:
        n = int(counts.get(label, 0) or 0)
        suppressed = n < SMALL_CELL
        out.append({"key": label, "label": label, "n": None if suppressed else n,
                    "n_text": SUPPRESSED if suppressed else _fmt(n), "suppressed": suppressed,
                    "cell": f"{cell}:{label}"})
    return out


def _point(x: str, n_c, n_a, den_c: int, den_a: int, cell: str) -> dict:
    """One point of a per-1,000 line, both series."""
    p = {"x": x, "cell": f"{cell}:{x}"}
    for s, n, den in (("cohort", n_c, den_c), ("everyone", n_a, den_a)):
        n = int(n or 0)
        if n < SMALL_CELL:
            p[s], p[f"{s}_n_text"], p[f"{s}_text"], p[f"{s}_suppressed"] = None, SUPPRESSED, SUPPRESSED, True
        else:
            rate = (n / den * 1000) if den else None
            p[s] = rate
            p[f"{s}_n_text"] = _fmt(n)
            p[f"{s}_text"] = "—" if rate is None else f"{rate:,.1f}"
            p[f"{s}_suppressed"] = False
    return p


def _two_series(ctx: _Ctx, key_sql: str, label_sql: str, from_sql: str, where_sql: str = "TRUE",
                params: Iterable = (), limit: Optional[int] = None,
                count_col: str = "x.person_id") -> list[tuple]:
    """One GROUP BY, both series: the base table (aliased x) LEFT JOINed to
    the cohort, the cohort's count as a FILTER. Rows of (key, label,
    n_cohort, n_all), the cohort's largest first."""
    sql = (
        f"SELECT {key_sql} AS key, {label_sql} AS label,\n"
        f"       COUNT(DISTINCT {count_col}) FILTER (WHERE c.person_id IS NOT NULL) AS n_cohort,\n"
        f"       COUNT(DISTINCT {count_col}) AS n_all\n"
        f"  FROM {from_sql}\n"
        f"  LEFT JOIN cohort c ON c.person_id = x.person_id\n"
        f" WHERE {where_sql}\n"
        f" GROUP BY 1, 2\n"
        f" ORDER BY 3 DESC, 4 DESC, 2"
        + (f"\n LIMIT {int(limit)}" if limit else "")
    )
    return [(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in ctx.q(sql, params) if len(r) >= 4]


def _scalar(rows: list[tuple], index: int = 0, default=0):
    if rows and len(rows[0]) > index and rows[0][index] is not None:
        return rows[0][index]
    return default


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_cohort(ctx: _Ctx) -> dict:
    persons_cell = _cell(ctx.persons, None)
    per_person = None
    if ctx.persons >= SMALL_CELL:
        per_person = ctx.encounters / ctx.persons
    return {
        "persons": ctx.persons if not persons_cell["suppressed"] else None,
        "persons_text": persons_cell["n_text"],
        "total": ctx.total,
        "encounters": ctx.encounters,
        "encounters_text": _fmt(ctx.encounters),
        "encounters_all": ctx.encounters_all,
        "per_person_text": SUPPRESSED if per_person is None else f"{per_person:,.1f}",
        "cost": NOT_IN_CDM["cost"],
    }


def _section_demographics(ctx: _Ctx) -> dict:
    sex = _rows(_two_series(ctx, _sex_sql("x"), _sex_sql("x"), "cdm.person x"),
                ctx.persons, ctx.total, partition=True)
    band = _order(_rows(_two_series(ctx, _band_sql("x"), _band_sql("x"), "cdm.person x"),
                        ctx.persons, ctx.total, partition=True), BANDS + ("unknown",))
    return {"sex": sex, "band": band, "payer": NOT_IN_CDM["payer"],
            "deceased": NOT_IN_CDM["deceased"]}


def _section_conditions(ctx: _Ctx) -> dict:
    out: dict = {"top": [], "status": [], "withheld": None, "status_note": None}
    # Status is the status OF a condition: with none named there is
    # nothing to show, and the section says so instead of drawing.
    if not ctx.named:
        out["status_note"] = NO_CONDITION_STATUS
    if ctx.withheld:
        out["withheld"] = WITHHELD_NO_VALUE_SETS
    else:
        frm, label = ctx.labelled("condition")
        ex, exp = ctx.excl("x.condition_source_value")
        out["top"] = _rows(_two_series(ctx, "x.condition_source_value", label, frm,
                                       "x.condition_source_value IS NOT NULL" + ex, exp,
                                       limit=TOP_CONDITIONS), ctx.persons, ctx.total)
        if ctx.named:
            out["status"] = _rows(_two_series(ctx, _status_sql("x"), _status_sql("x"),
                                              "cdm.condition_occurrence x", "TRUE" + ex, exp),
                                  ctx.persons, ctx.total)
    # Onset: the year of each person's FIRST recorded condition, per 1,000
    # persons of the group - one definition for both series. A date is not
    # a category, so no code exclusion applies here.
    rows = ctx.q(
        "SELECT EXTRACT(YEAR FROM f.d)::int AS y,\n"
        "       COUNT(*) FILTER (WHERE c.person_id IS NOT NULL) AS n_cohort, COUNT(*) AS n_all\n"
        "  FROM (SELECT co.person_id, MIN(co.condition_start_date) AS d\n"
        "          FROM cdm.condition_occurrence co GROUP BY 1) f\n"
        "  LEFT JOIN cohort c ON c.person_id = f.person_id\n"
        f" WHERE f.d >= make_date(EXTRACT(YEAR FROM CURRENT_DATE)::int - {ONSET_YEARS - 1}, 1, 1)\n"
        " GROUP BY 1 ORDER BY 1"
    )
    by_year = {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows if len(r) >= 3 and r[0] is not None}
    first = ctx.today.year - ONSET_YEARS + 1
    out["onset"] = [_point(str(y), *by_year.get(y, (0, 0)), ctx.persons, ctx.total, "onset")
                    for y in range(first, ctx.today.year + 1)]
    return out


def _section_medications(ctx: _Ctx) -> dict:
    out: dict = {"top": [], "on_medication": [], "per_person": [], "withheld": None}
    if ctx.withheld:
        out["withheld"] = WITHHELD_NO_VALUE_SETS
        return out
    frm, label = ctx.labelled("drug")
    ex, exp = ctx.excl("x.drug_source_value")
    out["top"] = _rows(_two_series(ctx, "x.drug_source_value", label, frm,
                                   "x.drug_source_value IS NOT NULL" + ex, exp,
                                   limit=TOP_MEDICATIONS), ctx.persons, ctx.total)
    exd, expd = ctx.excl("de.drug_source_value")
    rows = ctx.q(
        "SELECT CASE WHEN per.n > 0 THEN 'at least one' ELSE 'none' END AS key,\n"
        "       CASE WHEN per.n > 0 THEN 'at least one' ELSE 'none' END AS label,\n"
        "       COUNT(*) FILTER (WHERE per.in_cohort) AS n_cohort, COUNT(*) AS n_all\n"
        "  FROM (SELECT p.person_id, (c.person_id IS NOT NULL) AS in_cohort,\n"
        "               COUNT(de.drug_exposure_id) AS n\n"
        "          FROM cdm.person p\n"
        "          LEFT JOIN cohort c ON c.person_id = p.person_id\n"
        f"          LEFT JOIN cdm.drug_exposure de ON de.person_id = p.person_id{exd}\n"
        "         GROUP BY 1, 2) per\n"
        " GROUP BY 1, 2 ORDER BY 1",
        expd,
    )
    out["on_medication"] = _order(
        _rows([(r[0], r[1], r[2], r[3]) for r in rows if len(r) >= 4], ctx.persons, ctx.total,
              partition=True),
        ("at least one", "none"))
    rows = ctx.q(
        f"SELECT {_MED_BIN_SQL} AS bin, COUNT(*)\n"
        "  FROM (SELECT c.person_id, COUNT(DISTINCT de.drug_source_value) AS n\n"
        "          FROM cohort c\n"
        f"          LEFT JOIN cdm.drug_exposure de ON de.person_id = c.person_id{exd}\n"
        "         GROUP BY 1) per\n"
        " GROUP BY 1",
        expd,
    )
    out["per_person"] = _bins({str(r[0]): r[1] for r in rows if len(r) >= 2}, MED_BINS, "meds")
    return out


def _latest_cte(code_param: str = "%s") -> str:
    return (
        ", latest AS (\n"
        "  SELECT DISTINCT ON (m.person_id) m.person_id, m.value_as_number AS v,\n"
        "         m.range_low AS lo, m.range_high AS hi\n"
        "    FROM cdm.measurement m JOIN cohort c ON c.person_id = m.person_id\n"
        f"   WHERE m.measurement_source_value = {code_param} AND m.value_as_number IS NOT NULL\n"
        "   ORDER BY m.person_id, m.measurement_date DESC, m.measurement_id DESC\n"
        ")"
    )


def _section_measurements(ctx: _Ctx) -> dict:
    out: dict = {"tests": [], "withheld": None}
    if ctx.withheld:
        out["withheld"] = WITHHELD_NO_VALUE_SETS
        return out
    frm, label = ctx.labelled("measurement")
    ex, exp = ctx.excl("x.measurement_source_value")
    top = _two_series(ctx, "x.measurement_source_value", label, frm,
                      "x.measurement_source_value IS NOT NULL" + ex, exp, limit=TOP_TESTS)
    named = {code: name for name, codes in NAMED_TESTS for code in codes}
    name_rank = {name: i for i, (name, _codes) in enumerate(NAMED_TESTS)}
    # A1c, blood pressure and BMI first when present; then by persons measured.
    top.sort(key=lambda r: (name_rank.get(named.get(str(r[0])), len(name_rank)), -r[2]))
    for key, lbl, n_c, n_a in top:
        code = str(key)
        row = _rows([(key, lbl, n_c, n_a)], ctx.persons, ctx.total)[0]
        if code in named and named[code] != row["label"]:
            row["label"] = f"{named[code]} · {row['label']}"
        test = {"code": code, "label": row["label"], "measured": row, "abnormal": None,
                "hist": [], "range_note": None}
        stats = ctx.q(
            _latest_cte() + "\n"
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE lo IS NOT NULL OR hi IS NOT NULL),\n"
            "       COUNT(*) FILTER (WHERE (lo IS NOT NULL AND v < lo) OR (hi IS NOT NULL AND v > hi)),\n"
            "       MIN(v), MAX(v)\n"
            "  FROM latest",
            (code,),
        )
        n_latest = int(_scalar(stats, 0))
        ranged = int(_scalar(stats, 1))
        abnormal = int(_scalar(stats, 2))
        lo, hi = _scalar(stats, 3, None), _scalar(stats, 4, None)
        if ranged == 0:
            test["range_note"] = ("No reference range is recorded in this CDM for this test, so "
                                  "abnormal share cannot be drawn.")
        else:
            a = _cell(abnormal, ranged)
            d = _cell(ranged, None)
            suppressed = a["suppressed"] or d["suppressed"]
            test["abnormal"] = {"numerator": None if suppressed else abnormal,
                                "denominator": None if suppressed else ranged,
                                "value": None if suppressed else a["value"],
                                "text": SUPPRESSED if suppressed else a["text"],
                                "suppressed": suppressed}
        if n_latest and lo is not None and hi is not None:
            rows = ctx.q(
                _latest_cte() + ",\n"
                "bounds AS (SELECT MIN(v) AS lo,\n"
                "                  MAX(v) + GREATEST((MAX(v) - MIN(v)) / 1000.0, 0.000001) AS hi\n"
                "             FROM latest)\n"
                f"SELECT width_bucket(l.v, b.lo, b.hi, {HIST_BINS}) AS bucket, COUNT(*)\n"
                "  FROM latest l, bounds b\n"
                " GROUP BY 1 ORDER BY 1",
                (code,),
            )
            counts = {int(r[0]): int(r[1]) for r in rows if len(r) >= 2 and r[0] is not None}
            lo_f, hi_f = float(lo), float(hi)
            width = (hi_f - lo_f) / HIST_BINS
            bins = []
            for i in range(1, HIST_BINS + 1):
                start, end = lo_f + (i - 1) * width, lo_f + i * width
                lbl_bin = f"{start:g}–{end:g}" if width else f"{lo_f:g}"
                n = counts.get(i, 0)
                suppressed = n < SMALL_CELL
                bins.append({"key": str(i), "label": lbl_bin, "n": None if suppressed else n,
                             "n_text": SUPPRESSED if suppressed else _fmt(n),
                             "suppressed": suppressed, "cell": f"value:{code}:{i}"})
            test["hist"] = bins
        out["tests"].append(test)
    return out


def _section_procedures(ctx: _Ctx) -> dict:
    out: dict = {"top": [], "withheld": None, "imaging": None,
                 "immunizations": NOT_IN_CDM["immunizations"], "allergies": NOT_IN_CDM["allergies"]}
    if ctx.withheld:
        out["withheld"] = WITHHELD_NO_VALUE_SETS
        return out
    frm, label = ctx.labelled("procedure")
    ex, exp = ctx.excl("x.procedure_source_value")
    out["top"] = _rows(_two_series(ctx, "x.procedure_source_value", label, frm,
                                   "x.procedure_source_value IS NOT NULL" + ex, exp,
                                   limit=TOP_PROCEDURES), ctx.persons, ctx.total)
    return out


def _section_utilization(ctx: _Ctx) -> dict:
    out: dict = {}
    rows = ctx.q(
        f"SELECT {_ENC_BIN_SQL} AS bin, COUNT(*)\n"
        "  FROM (SELECT c.person_id, COUNT(vo.visit_occurrence_id) AS n\n"
        "          FROM cohort c LEFT JOIN cdm.visit_occurrence vo ON vo.person_id = c.person_id\n"
        "         GROUP BY 1) per\n"
        " GROUP BY 1"
    )
    out["per_person"] = _bins({str(r[0]): r[1] for r in rows if len(r) >= 2}, ENC_BINS, "enc")

    if ctx.vocabulary:
        frm = ("cdm.visit_occurrence x LEFT JOIN vocab.concept vc ON vc.concept_id = x.visit_concept_id "
               "AND x.visit_concept_id <> 0")
        label = "COALESCE(NULLIF(vc.concept_name, ''), x.visit_concept_id::text)"
    else:
        frm, label = "cdm.visit_occurrence x", "x.visit_concept_id::text"

    def visit_label(key, lbl):
        text = "" if lbl is None else str(lbl)
        return text if text and not text.isdigit() else _visit_label(key)

    out["type"] = _rows(_two_series(ctx, "x.visit_concept_id", label, frm,
                                    count_col="x.visit_occurrence_id"),
                        ctx.encounters, ctx.encounters_all, label_of=visit_label)

    rows = ctx.q(
        "SELECT to_char(date_trunc('month', x.visit_start_date), 'YYYY-MM') AS m,\n"
        "       COUNT(*) FILTER (WHERE c.person_id IS NOT NULL), COUNT(*)\n"
        "  FROM cdm.visit_occurrence x LEFT JOIN cohort c ON c.person_id = x.person_id\n"
        f" WHERE x.visit_start_date >= (date_trunc('month', CURRENT_DATE) - INTERVAL '{MONTHS - 1} months')::date\n"
        " GROUP BY 1 ORDER BY 1"
    )
    by_month = {str(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows if len(r) >= 3 and r[0]}
    months = []
    y, m = ctx.today.year, ctx.today.month
    for _ in range(MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    out["per_month"] = [_point(k, *by_month.get(k, (0, 0)), ctx.persons, ctx.total, "month")
                        for k in months]

    def weekday_label(key, _lbl):
        try:
            return calendar.day_abbr[(int(key) + 6) % 7]   # Postgres DOW: 0 = Sunday
        except (TypeError, ValueError):
            return str(key)

    weekday = _rows(_two_series(ctx, "EXTRACT(DOW FROM x.visit_start_date)::int",
                                "EXTRACT(DOW FROM x.visit_start_date)::int", "cdm.visit_occurrence x",
                                count_col="x.visit_occurrence_id"),
                    ctx.encounters, ctx.encounters_all, label_of=weekday_label)
    out["weekday"] = _order(weekday, tuple(str((i + 1) % 7) for i in range(7)))  # Mon..Sun

    out["facilities"] = _rows(_two_series(
        ctx, "cs.care_site_id", "COALESCE(cs.care_site_name, '(unnamed)')",
        "cdm.visit_occurrence x JOIN cdm.care_site cs ON cs.care_site_id = x.care_site_id",
        limit=TOP_FACILITIES), ctx.persons, ctx.total)
    out["documents"] = NOT_IN_CDM["documents"]
    return out


def _measure(title: str, numerator, denominator, cell_num: str, cell_den: str) -> dict:
    num, den = int(numerator or 0), int(denominator or 0)
    withheld = den < SMALL_CELL or num < SMALL_CELL
    return {
        "title": title,
        "numerator": None if withheld else num,
        "denominator": None if withheld else den,
        "numerator_text": SUPPRESSED if withheld else _fmt(num),
        "denominator_text": SUPPRESSED if withheld else _fmt(den),
        "rate": None if withheld or not den else num / den,
        "text": SUPPRESSED if withheld else _pct(num / den if den else None),
        "suppressed": withheld,
        "cell_num": cell_num, "cell_den": cell_den,
    }


def _condition_prefix_sql(alias: str, prefixes: tuple[str, ...]) -> tuple[str, tuple]:
    if not prefixes:
        return "FALSE", ()
    return ("(" + " OR ".join([f"{alias}.condition_source_value LIKE %s"] * len(prefixes)) + ")",
            tuple(f"{p}%" for p in prefixes))


def _codes_sql(alias: str, codes: tuple[str, ...]) -> tuple[str, tuple]:
    return (f"{alias}.measurement_source_value IN ({', '.join(['%s'] * len(codes))})", tuple(codes))


def _section_risk(ctx: _Ctx) -> dict:
    score = _NOSHOW_SCORE_SQL.format(gap="per.gap_days", enc="per.enc", age="per.age",
                                     conds="COALESCE(conds.n, 0)")
    rows = ctx.q(
        ", per AS (\n"
        "  SELECT c.person_id, COUNT(vo.visit_occurrence_id) AS enc,\n"
        "         (CURRENT_DATE - MAX(vo.visit_start_date)) AS gap_days,\n"
        f"         {_age_sql('p')} AS age\n"
        "    FROM cohort c JOIN cdm.person p ON p.person_id = c.person_id\n"
        "    LEFT JOIN cdm.visit_occurrence vo ON vo.person_id = c.person_id\n"
        "   GROUP BY c.person_id, p.year_of_birth),\n"
        "conds AS (\n"
        "  SELECT co.person_id, COUNT(DISTINCT co.condition_source_value) AS n\n"
        "    FROM cdm.condition_occurrence co JOIN cohort c ON c.person_id = co.person_id\n"
        "   GROUP BY 1),\n"
        f"scored AS (SELECT {score} AS score FROM per LEFT JOIN conds ON conds.person_id = per.person_id)\n"
        f"SELECT {_NOSHOW_BAND_SQL.format(score='score')} AS band, COUNT(*)\n"
        "  FROM scored GROUP BY 1"
    )
    counts = {str(r[0]): int(r[1] or 0) for r in rows if len(r) >= 2}
    noshow = _order(_rows([(b, b, counts.get(b, 0), None) for b in NOSHOW_BANDS],
                          ctx.persons, ctx.total, partition=True), NOSHOW_BANDS)

    visit = ctx.q(
        "SELECT COUNT(*),\n"
        "       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM cdm.visit_occurrence vo\n"
        "              WHERE vo.person_id = c.person_id\n"
        "                AND vo.visit_start_date >= CURRENT_DATE - INTERVAL '12 months'))\n"
        "  FROM cohort c"
    )
    # Keyed, so the template can carry the titles verbatim (the parity
    # test reads them there) and still find each measure's figures.
    measures = {"visit12": _measure("persons with a visit in the last 12 months",
                                    _scalar(visit, 1), _scalar(visit, 0), "measure:visit12", "")}

    named = dict(NAMED_TESTS)
    for title, shortcut, test, key in (
        ("diabetics with an A1c in the last 12 months", "diabetes", "A1c", "a1c"),
        ("hypertensives with a blood pressure in the last 12 months", "hypertension",
         "blood pressure", "bp"),
    ):
        prefixes, _name = resolve_condition(shortcut)
        cond_sql, cond_params = _condition_prefix_sql("co", prefixes)
        meas_sql, meas_params = _codes_sql("m", named[test])
        rows = ctx.q(
            "SELECT COUNT(*) FILTER (WHERE d.has_condition),\n"
            "       COUNT(*) FILTER (WHERE d.has_condition AND d.measured)\n"
            "  FROM (SELECT c.person_id,\n"
            f"               EXISTS (SELECT 1 FROM cdm.condition_occurrence co WHERE co.person_id = c.person_id AND {cond_sql}) AS has_condition,\n"
            f"               EXISTS (SELECT 1 FROM cdm.measurement m WHERE m.person_id = c.person_id AND {meas_sql}\n"
            "                       AND m.measurement_date >= CURRENT_DATE - INTERVAL '12 months') AS measured\n"
            "          FROM cohort c) d",
            tuple(cond_params) + tuple(meas_params),
        )
        measures[key] = _measure(title, _scalar(rows, 1), _scalar(rows, 0),
                                 f"measure:{key}-num", f"measure:{key}-den")
    return {"noshow": noshow, "measures": measures, "flu": NOT_IN_CDM["flu"],
            "noshow_note": ("The demonstration's scorer ported over cdm.visit_occurrence: gap since "
                            "the last visit, encounter count, age band, distinct conditions. Its "
                            "payer factor has no CDM column and is omitted. A heuristic, not a "
                            "model; no directive rides on it.")}


def _references(ctx: _Ctx) -> list[str]:
    rows = ctx.q(
        "SELECT p.person_source_value FROM cohort c JOIN cdm.person p ON p.person_id = c.person_id\n"
        " WHERE p.person_source_value IS NOT NULL ORDER BY 1"
    )
    refs = []
    for r in rows:
        if not r or r[0] is None:
            continue
        value = str(r[0])
        refs.append(value if value.startswith("Patient/") else f"Patient/{value}")
    return refs


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

def _guard(name: str, fn: Callable[[], dict], report: dict) -> None:
    """One failing section names its error; it does not blank the page."""
    try:
        report[name] = fn()
    except Exception as exc:  # the OMOP layer answered with an error
        log.warning("cohort report section %s failed: %s", name, exc)
        report[name] = {"error": f"the OMOP layer answered with an error: {exc}"}


def build_report(conn, term: str, sex: str = "", band: str = "", *, exact: bool = False,
                 value_sets=None, with_references: bool = False,
                 today: Optional[date] = None) -> dict:
    """Every section of the cohort report, as plain dicts of counts and
    shares - no markup, no PHI. The term may be empty: the selectors alone
    are a cohort, and nothing set is every person. `exact` names the
    condition by concept-name equality (a pick from the builder's search)
    rather than the contains match. `value_sets` is the deployment's
    CategoryValueSets (None withholds the code-level breakdowns);
    `with_references` adds the cohort's opaque patient references for the
    caller that joins the store and the run ledger, and nothing else."""
    term = (term or "").strip()
    sex = (sex or "").strip().lower()
    band = band if band in BANDS else ""
    cte, params, matched_on, caveats, vocabulary = _definition(conn, term, sex, band, exact)
    ctx = _Ctx(conn=conn, cte=cte, params=params, excluded=excluded_codes(value_sets),
               vocabulary=vocabulary, named=bool(term), today=today or date.today())

    ctx.persons = int(_scalar(ctx.q("SELECT COUNT(*) FROM cohort")))
    ctx.total = _total_patients(conn)
    enc = ctx.q("SELECT COUNT(*) FILTER (WHERE c.person_id IS NOT NULL), COUNT(*)\n"
                "  FROM cdm.visit_occurrence x LEFT JOIN cohort c ON c.person_id = x.person_id")
    ctx.encounters, ctx.encounters_all = int(_scalar(enc, 0)), int(_scalar(enc, 1))

    report: dict = {
        "term": term, "sex": sex, "band": band, "exact": exact, "named": bool(term),
        # The cohort's name and, apart, its selectors: the h1 and the line under it.
        "definition": cohort_definition(term, sex, band),
        "selectors": " · ".join(p for p in (sex, band) if p),
        "matched_on": matched_on, "caveats": list(caveats),
        "vocabulary": vocabulary,
        "persons": ctx.persons, "total": ctx.total,
        "small_cell": SMALL_CELL,
        "withheld": WITHHELD_NO_VALUE_SETS if ctx.withheld else None,
    }
    _guard("cohort", lambda: _section_cohort(ctx), report)
    _guard("demographics", lambda: _section_demographics(ctx), report)
    _guard("conditions", lambda: _section_conditions(ctx), report)
    _guard("medications", lambda: _section_medications(ctx), report)
    _guard("measurements", lambda: _section_measurements(ctx), report)
    _guard("procedures", lambda: _section_procedures(ctx), report)
    _guard("utilization", lambda: _section_utilization(ctx), report)
    report["cost"] = {"sentence": NOT_IN_CDM["cost"]}
    _guard("risk", lambda: _section_risk(ctx), report)
    if with_references:
        try:
            report["references"] = _references(ctx)
        except Exception as exc:
            log.warning("cohort references failed: %s", exc)
            report["references"] = []
    return report


# ---------------------------------------------------------------------------
# The stores outside the CDM: the PHI AI store, the run ledger, imaging
# ---------------------------------------------------------------------------

def store_holdings_for(reader, references: list[str]) -> dict:
    """What the PHI AI store holds for these charts: charts with at least
    one stored resource, records, by type. A reader without the bulk
    method (an older deployment, a test fake) says so rather than
    iterating every chart."""
    empty = {"charts": 0, "records": 0, "by_type": {}, "note": None,
             "charts_text": SUPPRESSED, "records_text": SUPPRESSED}
    if not references:
        return empty
    fn = getattr(reader, "holdings_for_patients", None)
    if fn is None:
        return dict(empty, charts_text="—", records_text="—",
                    note="This reader cannot count holdings for a set of charts.")
    try:
        held = fn(list(references)) or {}
    except Exception as exc:
        log.warning("store holdings for the cohort failed: %s", exc)
        return dict(empty, charts_text="—", records_text="—",
                    note=f"the index answered with an error: {exc}")
    charts, records = int(held.get("charts", 0) or 0), int(held.get("records", 0) or 0)
    return {"charts": charts, "records": records,
            "by_type": dict(held.get("by_type", {}) or {}), "note": None,
            "charts_text": SUPPRESSED if charts < SMALL_CELL else _fmt(charts),
            "records_text": SUPPRESSED if charts < SMALL_CELL else _fmt(records)}


def ledger_holdings_for(runs: Iterable[dict], references: list[str], systems: dict,
                        store_charts: int, cohort_charts: int) -> dict:
    """Where the cohort lives, from the run ledger: for each target, the
    distinct charts of this cohort a written deliver step reached, and the
    gap to it. The ledger tallies resource types per RUN, not per chart, so
    records moved by type cannot be given for a cohort - the section says
    so rather than showing the whole exchange as if it were the cohort's."""
    refs = set(references)
    charts_by_target: dict[str, set] = {}
    moved_by_target: dict[str, int] = {}
    population_steps = 0
    for run in runs or []:
        for step in (run.get("steps") or []):
            if step.get("direction") != "deliver" or not step.get("written"):
                continue
            target = str(step.get("system", ""))
            patient = step.get("patient")
            if patient is None:
                population_steps += 1
                continue
            if patient in refs:
                charts_by_target.setdefault(target, set()).add(patient)
                moved_by_target[target] = moved_by_target.get(target, 0) + int(step.get("moved") or 0)
    def row(key: str, label: str, held: int, gap: Optional[int], moved: Optional[int]) -> dict:
        # Shaped for charts.bars(series=("cohort",)): the share of the
        # cohort's charts held there, the count as the direct label.
        small = held < SMALL_CELL
        return {"key": key, "label": label, "held": held,
                "held_text": SUPPRESSED if small else _fmt(held),
                "cohort": None if small or not cohort_charts else held / cohort_charts,
                "cohort_text": SUPPRESSED if small else _fmt(held), "cohort_suppressed": small,
                "gap": gap,
                "gap_text": "—" if gap is None else (SUPPRESSED if gap < SMALL_CELL else _fmt(gap)),
                "gap_share": None if gap is None or gap < SMALL_CELL or not cohort_charts
                else gap / cohort_charts,
                "moved_text": "—" if moved is None else (SUPPRESSED if moved < SMALL_CELL else _fmt(moved))}

    rows = [row("store", "PHI AI store", store_charts, None, None)]
    for key, charts in sorted(charts_by_target.items()):
        held = len(charts)
        name = systems[key].name if key in systems else key
        rows.append(row(key, name, held, max(cohort_charts - held, 0), moved_by_target.get(key, 0)))
    across = sum(r["held"] for r in rows)
    # The gap chart's rows: the same targets, the gap as the mark.
    gaps = [{"key": r["key"], "label": r["label"], "cohort": r["gap_share"],
             "cohort_text": r["gap_text"], "cohort_suppressed": r["gap"] < SMALL_CELL,
             "gap_text": r["gap_text"]}
            for r in rows if r["gap"] is not None]
    note = None
    if population_steps:
        note = (f"{population_steps} population-mode deliver step(s) carry no chart reference and "
                "cannot be attributed to a cohort.")
    return {"rows": rows, "gaps": gaps, "across": across,
            "across_text": SUPPRESSED if across < SMALL_CELL else _fmt(across),
            "by_type": ("The run ledger tallies resource types per run, not per chart, so records "
                        "moved by type cannot be given for a cohort."),
            "note": note}


def imaging_modalities_for(conn, references: list[str]) -> list[dict]:
    """Studies of these charts in the imaging index, by modality (the
    series' own modality column) - the cohort only."""
    if not references:
        return []
    rows = _query(
        conn,
        "SELECT se.modality, COUNT(DISTINCT st.study_instance_uid)\n"
        "  FROM dicom_studies st JOIN dicom_series se ON se.study_instance_uid = st.study_instance_uid\n"
        " WHERE st.patient_reference = ANY(%s)\n"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
        (list(references),),
    )
    out = []
    for r in rows:
        if len(r) < 2:
            continue
        n = int(r[1] or 0)
        suppressed = n < SMALL_CELL
        out.append({"key": str(r[0]), "label": str(r[0] or "(none)"), "n": None if suppressed else n,
                    "n_text": SUPPRESSED if suppressed else _fmt(n), "suppressed": suppressed,
                    "cohort": None, "cohort_text": SUPPRESSED if suppressed else _fmt(n),
                    "cohort_suppressed": suppressed})
    return out


# ---------------------------------------------------------------------------
# The drill-down: the persons behind one number
# ---------------------------------------------------------------------------

def _cell_predicate(cell: str, excluded: Optional[tuple[str, ...]]) -> tuple[str, tuple, str]:
    """(SQL over cdm.person p, params, a label) for one cell of the report.
    The same expressions the charts group by, so a cell's rows are the
    persons its bar counted."""
    cell = (cell or "").strip()
    if not cell:
        return "TRUE", (), "the whole cohort"
    dim, _sep, value = cell.partition(":")
    value = value.strip()
    if not value:
        raise CellError("a cell names a dimension and a value")

    def exists(table: str, condition: str, params: tuple) -> tuple[str, tuple, str]:
        return (f"EXISTS (SELECT 1 FROM cdm.{table} z WHERE z.person_id = p.person_id AND {condition})",
                params, f"{dim} {value}")

    if dim == "sex":
        return f"{_sex_sql('p')} = %s", (value,), f"sex {value}"
    if dim == "band":
        return f"{_band_sql('p')} = %s", (value,), f"age band {value}"
    if dim in _CODE_TABLES:
        if excluded is None:
            raise CellError(WITHHELD_NO_VALUE_SETS)
        if value in excluded:
            raise CellError("that code is in a heightened category; its persons are not listed")
        table, col, _concept = _CODE_TABLES[dim]
        return exists(table, f"z.{col} = %s", (value,))
    if dim == "value":
        # One bin of a test's latest-value histogram: the cohort-wide
        # bounds are recomputed here exactly as the chart computed them.
        if excluded is None:
            raise CellError(WITHHELD_NO_VALUE_SETS)
        code, _sep2, bucket = value.partition(":")
        if code in excluded or not bucket.isdigit():
            raise CellError("no such value bin")
        return (
            f"(SELECT width_bucket(l.v, b.lo, b.hi, {HIST_BINS})\n"
            "   FROM (SELECT m.value_as_number AS v FROM cdm.measurement m\n"
            "          WHERE m.person_id = p.person_id AND m.measurement_source_value = %s\n"
            "            AND m.value_as_number IS NOT NULL\n"
            "          ORDER BY m.measurement_date DESC, m.measurement_id DESC LIMIT 1) l,\n"
            "        (SELECT MIN(v) AS lo, MAX(v) + GREATEST((MAX(v) - MIN(v)) / 1000.0, 0.000001) AS hi\n"
            "           FROM (SELECT DISTINCT ON (m.person_id) m.value_as_number AS v\n"
            "                   FROM cdm.measurement m JOIN cohort c ON c.person_id = m.person_id\n"
            "                  WHERE m.measurement_source_value = %s AND m.value_as_number IS NOT NULL\n"
            "                  ORDER BY m.person_id, m.measurement_date DESC, m.measurement_id DESC) z) b) = %s",
            (code, code, int(bucket)), f"{code} latest value, bin {bucket}")
    if dim == "status":
        if excluded is None:
            raise CellError(WITHHELD_NO_VALUE_SETS)
        ex = (f" AND z.condition_source_value NOT IN ({', '.join(['%s'] * len(excluded))})"
              if excluded else "")
        return exists("condition_occurrence", f"{_status_sql('z')} = %s{ex}", (value,) + tuple(excluded))
    if dim == "facility":
        return exists("visit_occurrence", "z.care_site_id = %s", (int(value),))
    if dim == "visit":
        return exists("visit_occurrence", "z.visit_concept_id = %s", (int(value),))
    if dim == "weekday":
        return exists("visit_occurrence", "EXTRACT(DOW FROM z.visit_start_date)::int = %s", (int(value),))
    if dim == "month":
        return exists("visit_occurrence",
                      "to_char(date_trunc('month', z.visit_start_date), 'YYYY-MM') = %s", (value,))
    if dim == "onset":
        return ("(SELECT EXTRACT(YEAR FROM MIN(z.condition_start_date))::int FROM cdm.condition_occurrence z"
                " WHERE z.person_id = p.person_id) = %s", (int(value),), f"first condition in {value}")
    if dim == "enc":
        if value not in ENC_BINS:
            raise CellError("no such encounter bin")
        return (f"(SELECT {_ENC_BIN_SQL} FROM (SELECT COUNT(*) AS n FROM cdm.visit_occurrence z"
                f" WHERE z.person_id = p.person_id) t) = %s", (value,), f"{value} encounters")
    if dim in ("meds", "medicated"):
        if excluded is None:
            raise CellError(WITHHELD_NO_VALUE_SETS)
        ex = (f" AND z.drug_source_value NOT IN ({', '.join(['%s'] * len(excluded))})"
              if excluded else "")
        if dim == "meds":
            if value not in MED_BINS:
                raise CellError("no such medication bin")
            return (f"(SELECT {_MED_BIN_SQL} FROM (SELECT COUNT(DISTINCT z.drug_source_value) AS n"
                    f" FROM cdm.drug_exposure z WHERE z.person_id = p.person_id{ex}) t) = %s",
                    tuple(excluded) + (value,), f"{value} medications")
        op = "> 0" if value == "at least one" else "= 0"
        return (f"(SELECT COUNT(*) FROM cdm.drug_exposure z WHERE z.person_id = p.person_id{ex}) {op}",
                tuple(excluded), f"{value} medication")
    if dim == "noshow":
        if value not in NOSHOW_BANDS:
            raise CellError("no such risk band")
        score = _NOSHOW_SCORE_SQL.format(gap="s.gap_days", enc="s.enc", age="s.age", conds="s.conds")
        return (
            "(SELECT " + _NOSHOW_BAND_SQL.format(score=score) + " FROM (\n"
            "   SELECT COUNT(vo.visit_occurrence_id) AS enc,\n"
            "          (CURRENT_DATE - MAX(vo.visit_start_date)) AS gap_days,\n"
            f"          {_age_sql('q')} AS age,\n"
            "          (SELECT COUNT(DISTINCT co.condition_source_value) FROM cdm.condition_occurrence co\n"
            "            WHERE co.person_id = q.person_id) AS conds\n"
            "     FROM cdm.person q LEFT JOIN cdm.visit_occurrence vo ON vo.person_id = q.person_id\n"
            "    WHERE q.person_id = p.person_id GROUP BY q.person_id, q.year_of_birth) s) = %s",
            (value,), f"no-show risk {value}")
    if dim == "measure":
        named = dict(NAMED_TESTS)
        recent = "z.visit_start_date >= CURRENT_DATE - INTERVAL '12 months'"
        if value == "visit12":
            return exists("visit_occurrence", recent, ())
        for key, shortcut, test in (("a1c", "diabetes", "A1c"), ("bp", "hypertension", "blood pressure")):
            if value not in (f"{key}-den", f"{key}-num"):
                continue
            prefixes, _name = resolve_condition(shortcut)
            cond_sql, cond_params = _condition_prefix_sql("z", prefixes)
            den = f"EXISTS (SELECT 1 FROM cdm.condition_occurrence z WHERE z.person_id = p.person_id AND {cond_sql})"
            if value.endswith("-den"):
                return den, cond_params, f"{shortcut} in the cohort"
            meas_sql, meas_params = _codes_sql("m", named[test])
            num = (f"EXISTS (SELECT 1 FROM cdm.measurement m WHERE m.person_id = p.person_id AND {meas_sql}"
                   " AND m.measurement_date >= CURRENT_DATE - INTERVAL '12 months')")
            return f"({den} AND {num})", tuple(cond_params) + tuple(meas_params), f"{shortcut} with a {test}"
        raise CellError("no such measure")
    raise CellError("no such cell")


def build_rows(conn, term: str, sex: str = "", band: str = "", cell: str = "", *,
               exact: bool = False, value_sets=None, limit: int = 200) -> dict:
    """The persons behind one number: opaque patient references only - the
    same thing the patient search returns - capped, with the cap stated."""
    term = (term or "").strip()
    sex = (sex or "").strip().lower()
    band = band if band in BANDS else ""
    cte, params, _matched, _caveats, _vocab = _definition(conn, term, sex, band, exact)
    where, cell_params, label = _cell_predicate(cell, excluded_codes(value_sets))
    sql = (
        cte + "\n"
        "SELECT p.person_source_value\n"
        "  FROM cohort c JOIN cdm.person p ON p.person_id = c.person_id\n"
        f" WHERE p.person_source_value IS NOT NULL AND ({where})\n"
        f" ORDER BY 1 LIMIT {int(limit) + 1}"
    )
    rows = _query(conn, sql, tuple(params) + tuple(cell_params))
    refs = []
    for r in rows:
        if not r or r[0] is None:
            continue
        value = str(r[0])
        refs.append(value if value.startswith("Patient/") else f"Patient/{value}")
    truncated = len(refs) > limit
    return {"references": refs[:limit], "truncated": truncated, "label": label,
            "count_text": (f"first {limit:,}" if truncated else _fmt(len(refs)))}
# Made by Ryan Gomez & Co. Inc.
