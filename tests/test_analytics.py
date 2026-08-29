# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Population analytics: cohort counts, facility breakdowns, name search.

Weighted toward the two things that actually go wrong here rather than
toward query mechanics. First, generated SQL: what the guard refuses and,
more importantly, what it does NOT claim to do. Second, counting: every
population number in this system is a COUNT(DISTINCT person_id), and a
regression to COUNT(*) would produce numbers roughly 3x too high that
look entirely plausible and that nobody re-derives.

No database. The guard is pure text, and the cohort and identity modules
take a connection object, so a recording fake proves which SQL is built
without needing Postgres - the same approach tests/test_web.py takes with
its fake reader.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.analytics import cohort, identity, sql_guard  # noqa: E402
from core.assistant import tools  # noqa: E402


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._conn.results.pop(0) if self._conn.results else [(0,)]

    @property
    def description(self):
        return [("col",)]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, results=None):
        self.executed = []
        self.results = list(results or [])

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
# The SQL guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO cdm.person VALUES (1)",
        "UPDATE cdm.person SET person_id = 2",
        "DELETE FROM cdm.person",
        "DROP TABLE cdm.person",
        "SELECT 1; DROP TABLE cdm.person",
        "SELECT 1; SELECT 2",
        "TRUNCATE cdm.person",
        "GRANT SELECT ON cdm.person TO public",
        "CREATE TABLE x (a int)",
        "SELECT * FROM cdm.person INTO OUTFILE '/tmp/x'",
    ],
)
def test_writes_and_multiple_statements_are_refused(sql):
    with pytest.raises(sql_guard.UnsafeQuery):
        sql_guard.guard(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM information_schema.tables",
        "SELECT dblink('...','...')",
        "SELECT pg_sleep(60)",
        "SELECT current_setting('is_superuser')",
    ],
)
def test_filesystem_and_catalogue_access_is_refused(sql):
    with pytest.raises(sql_guard.UnsafeQuery):
        sql_guard.guard(sql)


def test_a_comment_is_a_comment_and_not_a_second_statement():
    """A DELETE inside a block comment is inert to Postgres too, so this
    is allowed - and must still be allowed after the comment stripping
    that the keyword scan depends on."""
    guarded = sql_guard.guard("SELECT 1 /* ; DELETE FROM cdm.person */ FROM cdm.person")
    # The comment survives into what executes; only the SCAN sees it removed.
    assert "DELETE" in guarded.sql


def test_a_semicolon_followed_by_a_comment_is_refused():
    """Removing it correctly would mean knowing what is a comment, so it
    is refused with an instruction rather than guessed at."""
    with pytest.raises(sql_guard.UnsafeQuery, match="semicolon"):
        sql_guard.guard("SELECT 1 FROM cdm.person; -- trailing")


def test_string_literals_survive_into_the_executed_query():
    """The scan replaces literals with '' so a value cannot trip a
    keyword check. Executing that stripped copy would run
    `= 'female'` as `= ''` - a silently wrong answer, which is worse than
    a refused one."""
    guarded = sql_guard.guard(
        "SELECT count(*) FROM cdm.person WHERE gender_source_value = 'female'"
    )
    assert "'female'" in guarded.sql
    assert "= ''" not in guarded.sql


def test_a_semicolon_inside_a_string_is_not_a_second_statement():
    guarded = sql_guard.guard(
        "SELECT count(*) FROM cdm.person WHERE person_source_value = 'a;b'"
    )
    assert "'a;b'" in guarded.sql


def test_a_forbidden_word_inside_a_string_literal_is_not_a_write():
    """The opposite failure: refusing a legitimate query because a value
    contains a keyword. String literals are stripped before scanning."""
    guarded = sql_guard.guard(
        "SELECT count(*) FROM cdm.condition_occurrence WHERE stop_reason = 'deleted'"
    )
    assert "guarded_result" in guarded.sql


def test_a_column_named_like_a_keyword_is_not_a_write():
    guarded = sql_guard.guard("SELECT updated_at FROM cdm.person")
    assert guarded.original.startswith("SELECT")


def test_select_and_cte_are_allowed():
    assert sql_guard.guard("SELECT 1 FROM cdm.person")
    assert sql_guard.guard("WITH x AS (SELECT 1 FROM cdm.person) SELECT * FROM x")


def test_a_trailing_semicolon_is_fine():
    assert sql_guard.guard("SELECT 1 FROM cdm.person;")


def test_the_limit_is_applied_by_wrapping_and_cannot_be_evaded():
    """Appending LIMIT to a query that already has one does nothing.
    Wrapping cannot be defeated by anything the inner query contains."""
    guarded = sql_guard.guard("SELECT * FROM cdm.person LIMIT 1000000", row_limit=25)
    assert guarded.sql.strip().endswith("LIMIT 25")
    assert "guarded_result" in guarded.sql
    # The original is preserved verbatim for the audit entry.
    assert guarded.original == "SELECT * FROM cdm.person LIMIT 1000000"


def test_execute_sets_read_only_before_anything_else():
    """SET TRANSACTION must precede every other statement in the
    transaction; Postgres rejects it otherwise. Getting this backwards
    fails every analytics query with a message that reads like a driver
    problem."""
    conn = _FakeConn(results=[[(1,)]])
    sql_guard.execute(conn, sql_guard.guard("SELECT 1 FROM cdm.person"))

    statements = [s for s, _ in conn.executed]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert "statement_timeout" in statements[1]


# ---------------------------------------------------------------------------
# Counting - the trap the curated tools exist to remove
# ---------------------------------------------------------------------------


def test_condition_counts_are_distinct_patients_not_rows():
    """A patient diagnosed at four visits is one patient and four rows.
    COUNT(*) here would be ~3x too high on a real deployment and entirely
    plausible-looking."""
    conn = _FakeConn(results=[[(1,)], [(7,)], [(100,)]])
    cohort.count_patients_with_condition(conn, "diabetes")

    counting = [s for s, _ in conn.executed if "COUNT" in s and "condition_occurrence" in s]
    assert counting, "no count was issued"
    assert all("COUNT(DISTINCT co.person_id)" in s for s in counting)
    assert not any("COUNT(*)" in s for s in counting)


def test_facility_counts_are_distinct_patients_not_visits():
    conn = _FakeConn(results=[[("Main", "1", 5, 9)], [(5,)], [(0,)], [(100,)]])
    cohort.count_patients_by_facility(conn)

    headline = [
        s for s, _ in conn.executed
        if "COUNT(DISTINCT vo.person_id)" in s and "GROUP BY" not in s
    ]
    assert headline, "the headline figure must be its own DISTINCT query"


def test_a_condition_count_reports_its_denominator():
    """A bare count cannot be read. 12 out of 40 and 12 out of 400,000
    are different answers to the same question."""
    conn = _FakeConn(results=[[(1,)], [(12,)], [(400,)]])
    result = cohort.count_patients_with_condition(conn, "diabetes")
    assert result.total_patients_stored == 400
    assert result.patient_count == 12


def test_without_the_vocabulary_the_answer_says_so():
    """condition_concept_id is 0 on every row unless the licensed Athena
    vocabulary is loaded. Matching only on concepts would return a
    confident zero."""
    class _NoVocab(_FakeConn):
        def cursor(self):
            outer = self

            class C(_FakeCursor):
                def execute(self, sql, params=()):
                    if "vocab.concept" in sql:
                        raise RuntimeError('relation "vocab.concept" does not exist')
                    super().execute(sql, params)

            return C(outer)

    conn = _NoVocab(results=[[(3,)], [(50,)]])
    result = cohort.count_patients_with_condition(conn, "diabetes")
    assert any("vocabulary is not loaded" in c for c in result.caveats)
    # And it still matched on the raw source codes rather than giving up.
    assert any("ICD-10" in m for m in result.matched_on)


def test_plain_english_conditions_resolve_to_code_ranges():
    prefixes, name = cohort.resolve_condition("diabetes")
    assert "E11" in prefixes and name == "diabetes"
    # An unknown term is treated as a code prefix rather than refused.
    assert cohort.resolve_condition("E11.9") == ((), None)


# ---------------------------------------------------------------------------
# Name search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term,expected",
    [
        ("mary smith", ("mary", "smith")),
        ("smith, mary", ("mary", "smith")),
        ("smith", (None, "smith")),
        ("mary jane smith", ("mary jane", "smith")),
    ],
)
def test_names_are_split_in_either_order(term, expected):
    assert identity._split(term) == expected


def test_a_one_character_search_is_refused():
    """It would return an arbitrary slice of the patient list rather than
    an answer."""
    result = identity.search(_FakeConn(), "m")
    assert result.matches == []
    assert "at least two characters" in (result.note or "")


def test_results_are_capped_and_say_so():
    rows = [(f"Patient/{i}", i, f"Mary Smith {i}", None, None) for i in range(60)]
    conn = _FakeConn(results=[rows, [], [], [], []])
    result = identity.search(conn, "mary smith", limit=10)

    assert len(result.matches) == 10
    assert result.truncated
    assert "not a way to browse the patient list" in (result.note or "")


def test_birth_date_can_be_withheld_from_results():
    """Narrowing BY a birth date is disambiguation; returning it turns a
    lookup into a demographic extract."""
    match = identity.Match(
        patient_reference="Patient/1", person_id=1, full_name="Mary Smith",
        birth_date="1970-01-01", gender="female", matched_by="exact",
    )
    assert "birth_date" in match.to_dict(include_birth_date=True)
    assert "birth_date" not in match.to_dict(include_birth_date=False)


def test_search_degrades_when_pg_trgm_is_absent():
    """A managed Postgres that forbids extensions must still search."""
    class _NoTrgm(_FakeConn):
        def cursor(self):
            outer = self

            class C(_FakeCursor):
                def execute(self, sql, params=()):
                    if "pg_extension" in sql:
                        outer.executed.append(("EXT_CHECK", ()))
                        return
                    super().execute(sql, params)

                def fetchall(self):
                    return []

            return C(outer)

    result = identity.search(_NoTrgm(), "mary smith")
    assert result.matches == []  # nothing in the fake, but it did not raise


# ---------------------------------------------------------------------------
# Tool gating
# ---------------------------------------------------------------------------


@pytest.fixture
def kb():
    from core.assistant import knowledge

    return knowledge.load()


def _access(**kw):
    return tools.AnalyticsAccess(
        analytics_connection=kw.pop("analytics", lambda: _FakeConn()),
        identity_connection=kw.pop("ident", lambda: _FakeConn()),
        record_query=kw.pop("record", None),
        **kw,
    )


def test_analytics_tools_need_the_analytics_permission(kb):
    box = tools.build(kb, capabilities=frozenset({"assistant:use"}), analytics=_access())
    assert "count_patients_with_condition" not in box.names
    assert "run_analytics_query" not in box.names


def test_name_search_is_permissioned_separately_from_cohort_counts(kb):
    """Counting patients with a condition and finding out who they are are
    different privileges - an analyst gets one, not both."""
    analyst = tools.build(kb, capabilities=frozenset({"analytics:query"}), analytics=_access())
    assert "count_patients_with_condition" in analyst.names
    assert "find_patients_by_name" not in analyst.names

    clerk = tools.build(kb, capabilities=frozenset({"identity:search"}), analytics=_access())
    assert "find_patients_by_name" in clerk.names
    assert "count_patients_with_condition" not in clerk.names


def test_the_analyst_role_cannot_open_a_record():
    """The whole point of the role: population access without disclosure."""
    from core.web.auth import PERMISSIONS, Role

    analyst = PERMISSIONS[Role.ANALYST]
    assert "analytics:query" in analyst
    assert "patient:read" not in analyst
    assert "identity:search" not in analyst


def test_auditor_and_disposition_get_no_population_access():
    from core.web.auth import PERMISSIONS, Role

    for role in (Role.AUDITOR, Role.DISPOSITION):
        assert "analytics:query" not in PERMISSIONS[role]
        assert "identity:search" not in PERMISSIONS[role]


def test_a_generated_query_is_audited_verbatim_before_it_runs(kb):
    """The audit entry is the whole reason generated SQL is acceptable:
    a reviewer reads the exact statement, not a tool name."""
    audited = []
    box = tools.build(
        kb,
        capabilities=frozenset({"analytics:query"}),
        analytics=_access(record=lambda action, detail: audited.append((action, detail))),
    )
    box.run("run_analytics_query", {
        "sql": "SELECT COUNT(DISTINCT person_id) FROM cdm.person",
        "rationale": "how many patients",
    })

    assert audited, "the query was not audited"
    action, detail = audited[0]
    assert action == "analytics.query"
    assert "SELECT COUNT(DISTINCT person_id) FROM cdm.person" in detail
    assert "how many patients" in detail


def test_a_refused_query_is_not_audited_as_an_access(kb):
    """Nothing ran, so nothing was read, so there is no access to record."""
    audited = []
    box = tools.build(
        kb,
        capabilities=frozenset({"analytics:query"}),
        analytics=_access(record=lambda action, detail: audited.append((action, detail))),
    )
    text, _ = box.run("run_analytics_query", {"sql": "DELETE FROM cdm.person", "rationale": "x"})

    assert "refused" in text.lower()
    assert audited == []


# ---------------------------------------------------------------------------
# Regressions found only by running against real PostgreSQL 16
# ---------------------------------------------------------------------------


def test_search_terms_are_folded_the_same_way_the_columns_are():
    """identity.patient_identity strips apostrophes, hyphens, periods and
    commas in its GENERATED columns. A term folded differently can never
    match: searching "obrien" for a patient stored as O'Brien returned
    nothing at all until both sides were folded identically."""
    assert identity._normalise("O'Brien") == "obrien"
    assert identity._normalise("Smith-Jones") == "smithjones"
    assert identity._normalise("  Mary   Smith ") == "mary smith"
    assert identity._normalise("St. John") == "st john"


def test_fuzzy_matching_uses_word_similarity_not_whole_string_similarity():
    """similarity() compares whole trigram sets, so a short term against a
    long stored name scores far below any useful threshold - measured,
    similarity('mary jane smith o''brien', 'smyth') is 0.111. Every
    realistic misspelling was rejected until this changed."""
    import inspect

    source = inspect.getsource(identity.search)
    assert "word_similarity(%s, full_name_norm)" in source
    assert "similarity(full_name_norm" not in source
    # 0.333 is what smyth/smith actually scores; the threshold has to admit it.
    assert identity.TRIGRAM_THRESHOLD < 0.333


def test_the_identity_write_does_not_use_savepoints():
    """_execute_upsert() commits per row, and a commit destroys every
    savepoint in the transaction - so a SAVEPOINT wrapper here failed with
    "savepoint does not exist" and took down the ETL it was meant to
    protect. It was also unnecessary: the per-row commit already isolates
    a failed identity write. Asking whether the table exists is both
    simpler and correct."""
    import inspect

    from core.db import omop_etl

    source = inspect.getsource(omop_etl._write_identity_if_available)
    # Matched as an EXECUTED statement, not as the word: the fix's own
    # docstring explains why savepoints are wrong here, and a test that
    # cannot tell an explanation from a use punishes writing it down.
    assert 'execute("SAVEPOINT' not in source
    assert "ROLLBACK TO SAVEPOINT" not in source
    assert "_identity_index_available" in source
    assert "to_regclass" in inspect.getsource(omop_etl._identity_index_available)


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_identity_grants_are_conditional(cloud):
    """identity.patient_identity exists only if the operator applied
    core/db/identity_schema.sql, name search is off by default, and these
    files set ON_ERROR_STOP. An unconditional grant would make the OMOP
    bootstrap fail for every deployment that enabled analytics without
    enabling name search - the combination the runbook recommends
    starting from. The same bug class as the DICOM grants in
    tests/test_entrypoints.py."""
    omop = (Path(__file__).resolve().parents[1] / "core" / "db"
            / f"omop_bootstrap_{cloud}.sql").read_text()
    assert "to_regclass('identity.patient_identity')" in omop
    for line in omop.splitlines():
        stripped = line.strip()
        if stripped.startswith("GRANT") and "identity." in stripped:
            pytest.fail(f"unconditional identity grant in omop_bootstrap_{cloud}.sql: {stripped}")
# Made by Ryan Gomez & Co. Inc.
