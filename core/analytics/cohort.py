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

# The small-cell floor every figure on the analytics plane observes: a
# count under it reads "< 11". One constant, read by the cohort screen,
# the report (core/analytics/cohort_report.py) and its charts.
SMALL_CELL = 11
SUPPRESSED = "< 11"

# The contract's age bands. Not a hand-maintained list: the DEFINITION of
# a dimension computed from year_of_birth (band_sql below) - the CASE the
# selector filters by and the report's age-band chart groups by.
BANDS = ("0-17", "18-44", "45-64", "65+")

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


# ---------------------------------------------------------------------------
# The selector expressions, over a cdm.person alias. Written once so the
# cohort definition, the report's charts and the drill-down cannot compute
# a person's sex or band three ways.
# ---------------------------------------------------------------------------

def age_sql(alias: str) -> str:
    """Age this calendar year. The CDM's month and day of birth are
    optional, and a band a year wide does not need them."""
    return f"(EXTRACT(YEAR FROM CURRENT_DATE)::int - {alias}.year_of_birth)"


def band_sql(alias: str) -> str:
    age = age_sql(alias)
    return (f"CASE WHEN {alias}.year_of_birth IS NULL THEN 'unknown' "
            f"WHEN {age} < 18 THEN '0-17' WHEN {age} < 45 THEN '18-44' "
            f"WHEN {age} < 65 THEN '45-64' ELSE '65+' END")


def sex_sql(alias: str) -> str:
    return f"COALESCE(NULLIF(lower({alias}.gender_source_value), ''), 'unknown')"


# The two ways a condition row reaches a concept NAME in vocab.concept,
# over `cdm.condition_occurrence co` and `vocab.concept vc`. The CDM's own
# convention first: a mapped row points at its concept. Until the ETL maps
# them (core/db/omop_etl.py writes concept_id 0 and says so), a row is
# reached by the code the source sent, read against the vocabulary's own
# code column inside its Condition domain - the vocabulary's classification,
# never a list typed here. Read by the search and by an exact pick alike.
CONDITION_NAME_JOIN = (
    "((co.condition_concept_id <> 0 AND vc.concept_id = co.condition_concept_id) OR "
    "(co.condition_concept_id = 0 AND vc.domain_id = 'Condition' "
    "AND vc.concept_code = co.condition_source_value))"
)


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
    # The cohort's name (cohort_definition) - "diabetes · female · 65+",
    # or "all persons" - and whether the vocabulary answered.
    definition: str = ""
    vocabulary_loaded: bool = False

    @property
    def patient_count_text(self) -> str:
        """The count as the screen shows it: under the small-cell floor
        it reads "< 11", the same floor the report keeps."""
        return SUPPRESSED if self.patient_count < SMALL_CELL else f"{self.patient_count:,}"

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "patients_matching": self.patient_count,
            "total_patients_stored": self.total_patients_stored,
            "matched_on": self.matched_on,
            "caveats": self.caveats,
            "detail": self.detail,
            "definition": self.definition,
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


def condition_clause(conn, term: str) -> tuple[str, list[Any], list[str], list[str], bool]:
    """The predicate that DEFINES a condition cohort, over
    `cdm.condition_occurrence co`.

    Returns (where, params, matched_on, caveats, vocabulary_loaded). One
    function so the cohort count (count_patients_with_condition) and the
    cohort report (core/analytics/cohort_report.py) can never define the
    same term two ways: the report's persons figure is this predicate,
    the screen's count is this predicate.
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

    vocabulary = _vocabulary_loaded(conn)
    if vocabulary:
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

    return "(" + " OR ".join(clauses) + ")", params, matched_on, caveats, vocabulary


def exact_condition_clause(conn, term: str) -> tuple[str, list[Any], list[str], list[str], bool]:
    """The predicate for a condition named EXACTLY - a pick from the
    search list (search_conditions), not typed text: concept-name
    equality, over `cdm.condition_occurrence co`. The same shape
    condition_clause returns, so a cohort is defined one way whichever
    matching its term asked for."""
    matched_on = [f"the condition named exactly '{term}'"]
    caveats: list[str] = []
    vocabulary = _vocabulary_loaded(conn)
    if not vocabulary:
        caveats.append(
            "The OHDSI Athena vocabulary is not loaded in this deployment, so a condition "
            "cannot be matched by name and nothing was counted - see "
            "runbooks/RUNBOOK_OMOP_SETUP.md."
        )
        return "FALSE", [], matched_on, caveats, False
    return (
        "EXISTS (SELECT 1 FROM vocab.concept vc WHERE vc.concept_name = %s "
        f"AND {CONDITION_NAME_JOIN})",
        [term], matched_on, caveats, True,
    )


def cohort_definition(term: str, sex: str, band: str) -> str:
    """The cohort's name: its set parts joined by ' · ' - "diabetes ·
    female · 65+" - or "all persons" when nothing is set. The same words
    on the builder's tile, the report's title and the audit row."""
    parts = [p for p in ((term or "").strip(), (sex or "").strip(), (band or "").strip()) if p]
    return " · ".join(parts) if parts else "all persons"


def cohort_where(conn, term: str, sex: str, band: str, exact: bool = False
                 ) -> tuple[str, list[Any], list[str], list[str], bool]:
    """The predicate that DEFINES a cohort, over `cdm.person p`.

    A cohort is any combination of its variables and each alone is one:
    the condition (any person with a matching row - by the search's
    exact name, or by condition_clause's contains/shortcut/prefix match),
    the sex value, the age band. Nothing set is every person. Returns
    (where, params, matched_on, caveats, vocabulary_loaded). One function
    so the cohort count (count_cohort) and the cohort report
    (core/analytics/cohort_report.py) can never define the same cohort
    two ways.
    """
    term = (term or "").strip()
    clauses: list[str] = []
    params: list[Any] = []
    matched_on: list[str] = []
    caveats: list[str] = []
    if term:
        clause = exact_condition_clause if exact else condition_clause
        where, condition_params, matched, condition_caveats, vocabulary = clause(conn, term)
        clauses.append("EXISTS (SELECT 1 FROM cdm.condition_occurrence co "
                       f"WHERE co.person_id = p.person_id AND {where})")
        params.extend(condition_params)
        matched_on.extend(matched)
        caveats.extend(condition_caveats)
    else:
        vocabulary = _vocabulary_loaded(conn)
    if sex:
        clauses.append(f"{sex_sql('p')} = %s")
        params.append(sex)
        matched_on.append(f"sex '{sex}' (gender_source_value)")
    if band:
        clauses.append(f"{band_sql('p')} = %s")
        params.append(band)
        matched_on.append(f"age band {band} (year_of_birth against this calendar year)")
    if not clauses:
        matched_on.append("every person in cdm.person - nothing set")
    return (" AND ".join(clauses) if clauses else "TRUE"), params, matched_on, caveats, vocabulary


def protected_term(term: str, value_sets) -> Optional[str]:
    """The heightened category a cohort term falls inside, or None.

    A cohort over "depression" IS a mental-health cohort: counting it,
    let alone breaking it down, is a disclosure of the category. The
    decision is made from the deployment's own sensitive-category value
    sets (core/governance/segmentation.CategoryValueSets - the same
    predicate serialisation applies), never from a list typed here: the
    term's code prefixes are compared with every curated code, and the
    term itself with every category's name. With no value sets configured
    nothing can be told protected, which matches how the count has always
    behaved; the report's code-level breakdowns then withhold themselves
    instead (see cohort_report.py).
    """
    if value_sets is None:
        return None
    normalised = " ".join((term or "").strip().lower().split())
    if not normalised:
        return None
    prefixes, _shortcut = resolve_condition(term)
    probes = tuple(p.upper() for p in prefixes) or (normalised.upper(),)
    for category, members in getattr(value_sets, "codes", {}).items():
        name = getattr(category, "value", str(category))
        if normalised in (name, name.replace("_", " ")):
            return name
        for _system, code in members:
            code = str(code or "").upper()
            if not code:
                continue
            # Either direction: a prefix that covers a curated code (F32
            # covers F32.9), or a term narrower than a curated code.
            if any(code.startswith(p) or p.startswith(code) for p in probes):
                return name
    return None


def count_patients_with_condition(
    conn, term: str, since: Optional[str] = None, until: Optional[str] = None
) -> CohortResult:
    """How many DISTINCT patients have a matching condition.

    `term` is either a plain-English name from CONDITION_SHORTCUTS, or a
    code prefix ("E11", "I50.9"). Both are matched as prefixes against
    condition_source_value, and against concept_name when a vocabulary is
    loaded.
    """
    where, params, matched_on, caveats, _vocabulary = condition_clause(conn, term)
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


def count_cohort(conn, term: str = "", sex: str = "", band: str = "",
                 exact: bool = False) -> CohortResult:
    """How many DISTINCT persons a definition names: one
    COUNT(DISTINCT person_id) over cdm.person, whatever combination of
    condition, sex and age band is set - and every person when none is.
    The predicate is cohort_where, the report's own."""
    term = (term or "").strip()
    where, params, matched_on, caveats, vocabulary = cohort_where(conn, term, sex, band, exact)
    # COUNT(DISTINCT person_id), never COUNT(*) - see the module docstring.
    rows = _query(
        conn,
        f"SELECT COUNT(DISTINCT p.person_id) FROM cdm.person p WHERE {where}",
        tuple(params),
    )
    count = int(rows[0][0]) if rows else 0
    total = _total_patients(conn)
    if term:
        caveats.append(
            "Counts persons with the condition RECORDED in this deployment. A person "
            "diagnosed before the stored period, or at an organisation whose records "
            "are not here, will not appear."
        )
    definition = cohort_definition(term, sex, band)
    return CohortResult(
        question=f"persons — {definition}",
        patient_count=count,
        total_patients_stored=total,
        matched_on=matched_on,
        caveats=caveats,
        definition=definition,
        vocabulary_loaded=vocabulary,
    )


def search_conditions(conn, term: str, limit: int = 12, *, excluded=None) -> list[dict]:
    """The distinct condition names containing the term, each with the
    number of persons who have it, the most first - derived by GROUP BY
    over the CDM's concept names (CONDITION_NAME_JOIN), never typed.

    Two characters or more, and nothing without the vocabulary, which is
    where the names live. `excluded` is the deployment's heightened codes
    (cohort_report.excluded_codes): a row carrying one is left out, the
    exclusion every breakdown of the report applies. A count under
    SMALL_CELL reads SUPPRESSED.
    """
    normalised = " ".join((term or "").strip().lower().split())
    if len(normalised) < 2 or not _vocabulary_loaded(conn):
        return []
    params: list[Any] = [f"%{normalised}%"]
    exclusion = ""
    if excluded:
        exclusion = ("\n           AND co.condition_source_value NOT IN ("
                     + ", ".join(["%s"] * len(excluded)) + ")")
        params.extend(excluded)
    rows = _query(
        conn,
        f"""
        SELECT vc.concept_name, COUNT(DISTINCT co.person_id) AS persons
          FROM cdm.condition_occurrence co
          JOIN vocab.concept vc ON {CONDITION_NAME_JOIN}
         WHERE lower(vc.concept_name) LIKE %s{exclusion}
         GROUP BY 1
         ORDER BY persons DESC, 1
         LIMIT {int(limit)}
        """,
        tuple(params),
    )
    out = []
    for r in rows:
        if len(r) < 2 or r[0] is None:
            continue
        n = int(r[1] or 0)
        small = n < SMALL_CELL
        out.append({"name": str(r[0]), "persons": None if small else n,
                    "persons_text": SUPPRESSED if small else f"{n:,}"})
    return out


def known_condition_name(conn, term: str, excluded=None) -> Optional[str]:
    """The corpus's own spelling of a condition named EXACTLY `term`, or
    None - case-insensitively, over the same join and the same exclusion
    the search list uses, so "known" means one thing on this screen.

    A name with no condition_occurrence row behind it is not in the
    corpus and is not returned: the search list would not offer it
    either. Read only when the search list itself could not settle the
    question (it stops at twelve names), so the common path is one query.
    """
    normalised = " ".join((term or "").strip().lower().split())
    if not normalised or not _vocabulary_loaded(conn):
        return None
    params: list[Any] = [normalised]
    exclusion = ""
    if excluded:
        exclusion = (" AND co.condition_source_value NOT IN ("
                     + ", ".join(["%s"] * len(excluded)) + ")")
        params.extend(excluded)
    rows = _query(
        conn,
        "SELECT DISTINCT vc.concept_name"
        "\n  FROM cdm.condition_occurrence co"
        f"\n  JOIN vocab.concept vc ON {CONDITION_NAME_JOIN}"
        f"\n WHERE lower(vc.concept_name) = %s{exclusion}"
        "\n LIMIT 1",
        tuple(params),
    )
    return str(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None


@dataclass
class ResolvedCondition:
    """What a typed condition term resolved to - the one answer the count,
    the report and the audit row all read.

    `term`/`exact` are what the cohort is counted by: the corpus's own
    name when the term named one, the typed text when it matched known
    names by containment, and NOTHING (an empty term, so the cohort is
    the selectors alone) when the corpus knows no condition by that name.
    `note` is the sentence the screen states in that last case. `checked`
    says whether the corpus's names could be consulted at all.
    """
    typed: str = ""
    term: str = ""
    exact: bool = False
    known: bool = True
    checked: bool = False
    note: Optional[str] = None


# The sentence the builder states for text that names no condition the
# corpus holds (the contract's addendum, item 2: "only known conditions
# count"). One constant, stated verbatim by the screen; the report's own
# restatement sits beside it because the two screens point at different
# places for the names.
#
# The builder points at "the names above" - the server-rendered "conditions
# matching" list, which sits above its box. The REPORT has no such list and
# never will: it is a report, not a search. What it does have, now that its
# selector bar carries the same condition box, is that box directly below
# the sentence, whose listbox drops the corpus's own names into the gap - so
# the second clause names the box instead of a list that is not there. The
# first clause and the last are the builder's, word for word, and the
# demonstration states both sentences identically (www-demo/app/views/
# cohort.php and cohort_report.php).
UNRESOLVED_CONDITION = (
    "No condition in the corpus is named “{term}”. Pick one of the names above, "
    "or clear the box to count everyone."
)
UNRESOLVED_CONDITION_REPORT = (
    "No condition in the corpus is named “{term}”. Pick one of the names the condition "
    "box below suggests, or clear the box to count everyone."
)


def unresolved_condition_note(term: str, report: bool = False) -> str:
    template = UNRESOLVED_CONDITION_REPORT if report else UNRESOLVED_CONDITION
    return template.format(term=(term or "").strip())


def resolve_cohort_term(conn, term: str = "", *, exact: bool = False,
                        excluded=None, matches=None) -> ResolvedCondition:
    """What the typed condition term counts as - ONLY a condition the
    corpus knows (the contract's addendum, item 2).

    A term equal to a known name resolves to that name, exactly, whether
    or not the reader picked it from the list. Text that matches known
    names by containment keeps the contains match the builder has always
    given it. Text the corpus knows nothing by resolves to NOTHING: the
    cohort is the selectors alone and `note` is the sentence that says
    so, because a condition that cannot be resolved is not silently a
    contains match.

    Two deployments cannot be told anything: one with no vocabulary (there
    are no names to be known by) and one with no sensitive-category value
    sets (`excluded` is None - the names cannot be listed at all, which is
    why the search list withholds itself). Neither is evidence that the
    corpus has no such condition, so the term is left as it was and
    `checked` is False.

    `matches` is the search list the caller already ran for this term
    (search_conditions), so the common path adds no query.
    """
    typed = (term or "").strip()
    if not typed:
        return ResolvedCondition(checked=excluded is not None)
    if excluded is None or not _vocabulary_loaded(conn):
        return ResolvedCondition(typed=typed, term=typed, exact=exact, checked=False)
    names = list(matches) if matches is not None else search_conditions(
        conn, typed, excluded=excluded)
    lowered = " ".join(typed.lower().split())
    for match in names:
        name = str(match.get("name") or "")
        if " ".join(name.lower().split()) == lowered:
            return ResolvedCondition(typed=typed, term=name, exact=True, checked=True)
    if exact:
        # A pick the list cannot settle: it stops at twelve names, and a
        # name can be picked from a narrower search than this one. One
        # precise lookup rather than a wrong sentence.
        canonical = known_condition_name(conn, typed, excluded)
        if canonical:
            return ResolvedCondition(typed=typed, term=canonical, exact=True, checked=True)
    elif names:
        return ResolvedCondition(typed=typed, term=typed, exact=False, checked=True)
    return ResolvedCondition(typed=typed, term="", exact=False, known=False, checked=True,
                             note=unresolved_condition_note(typed))


def sex_values(conn) -> list[str]:
    """The sex selector's options: the values the CDM holds, by the very
    expression the selector filters on, the most common first - derived,
    never typed."""
    rows = _query(
        conn,
        f"SELECT {sex_sql('p')} AS sex, COUNT(*) FROM cdm.person p GROUP BY 1 ORDER BY 2 DESC, 1",
    )
    return [str(r[0]) for r in rows if r and r[0] is not None]


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
