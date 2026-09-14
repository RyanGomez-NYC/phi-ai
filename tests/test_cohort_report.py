# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The cohort report, platform half (core/analytics/cohort_report.py,
core/web/charts.py, the /cohort/report routes).

Three things are worth guarding. The chart helpers are pure functions,
so their markup is asserted directly: the marks, the labels on the cohort
series only, the hatched "< 11" stub, the one axis. The metrics module is
asserted through a recording fake connection, the way tests/test_analytics.py
proves which SQL the cohort count builds: every breakdown is a GROUP BY,
every code-level one carries the heightened-code exclusion, small cells
and their complements are withheld, and an empty CDM yields every section
without raising. The route is asserted the way tests/test_web.py asserts
every PHI surface: the permission, and the audit row before the work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.analytics import cohort  # noqa: E402
from core.analytics import cohort_report as cr  # noqa: E402
from core.governance.segmentation import CategoryValueSets, SensitiveCategory  # noqa: E402
from core.web import charts  # noqa: E402
from test_web import _RecordingAudit, _client  # noqa: E402


# ---------------------------------------------------------------------------
# THE CONTRACT: the report's ten sections and every tile and chart title.
#
# These live HERE, with the platform's own test, because the platform's
# template is what has to satisfy them. A mirror elsewhere pins itself to
# this tuple in its own test - the dependency runs that way round, so a tree
# that carries only the platform still has the contract and can still check
# it. (It used to run the other way, and the platform's test would not even
# import without the mirror's.)
# ---------------------------------------------------------------------------
SECTIONS = (
    "Cohort", "Demographics", "Conditions", "Medications", "Measurements",
    "Procedures, imaging, immunizations, allergies", "Utilization", "Cost",
    "Risk and quality", "Where the cohort lives",
)
TITLES = (
    # 1 Cohort tiles
    "persons", "charts held across systems", "records in the store", "encounters",
    "encounters per person", "claims paid per person",
    # 2 Demographics
    "sex", "age band", "payer", "deceased",
    # 3 Conditions
    "most common conditions in this cohort", "condition status", "onset by year",
    # 4 Medications
    "most common medications", "persons on at least one medication", "medications per person",
    # 5 Measurements
    "persons measured", "abnormal share",
    # 7 Utilization
    "encounter type", "encounters per month", "by weekday", "facilities", "documents by type",
    # 8 Cost
    "claims per person", "billed / allowed / paid per person", "patient responsibility share",
    "payer mix by paid", "claim status", "top services",
    # 9 Risk and quality
    "no-show risk", "persons with a visit in the last 12 months",
    "diabetics with an A1c in the last 12 months",
    "hypertensives with a blood pressure in the last 12 months",
    "flu immunization in the last 12 months",
    # 10 Where the cohort lives
    "charts held by system", "records moved by type", "gap to each target",
)
LEAD = "Counts under 11 are withheld."

ICD = "http://hl7.org/fhir/sid/icd-10-cm"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        self._conn.last = " ".join(sql.split())
        self._conn.executed.append((self._conn.last, tuple(params)))

    def fetchall(self):
        # Answers keyed on a fragment of the statement, so the order in
        # which a route happens to ask does not matter; anything else is
        # an empty result set.
        for fragment, rows in self._conn.answers:
            if fragment in self._conn.last:
                return list(rows)
        return []

    @property
    def description(self):
        return [("col",)]

    def close(self):
        pass


class _Conn:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.executed = []
        self.last = ""
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True

    def rollback(self):
        pass


def _value_sets(*codes, category=SensitiveCategory.MENTAL_HEALTH):
    return CategoryValueSets(codes={category: frozenset((ICD, c) for c in codes)})


def _wire_connection(monkeypatch, conn):
    """A live-looking OMOP layer behind the route's own _omop_connection():
    settings that name an analyst role, and a connect() that hands back
    the fake."""
    import core.config.settings as settings_module
    import core.db.connection as connection_module

    class _Settings:
        omop_analyst_username = "omop_analyst"

    monkeypatch.setattr(settings_module.Settings, "from_env", classmethod(lambda cls: _Settings()))
    monkeypatch.setattr(connection_module, "connect", lambda settings, username: conn)


def _no_connection(monkeypatch):
    import core.config.settings as settings_module

    def refuse(cls):
        raise RuntimeError("no platform settings in this test")

    monkeypatch.setattr(settings_module.Settings, "from_env", classmethod(refuse))


def _statements(conn):
    return [s for s, _ in conn.executed]


# ---------------------------------------------------------------------------
# charts.py - pure functions
# ---------------------------------------------------------------------------

def test_bars_draws_thin_rounded_marks_with_titles_and_labels_the_cohort_only():
    rows = [
        {"label": "female", "cohort": 0.62, "everyone": 0.51, "cohort_text": "62%", "everyone_text": "51%"},
        {"label": "male", "cohort": 0.38, "everyone": 0.49, "cohort_text": "38%", "everyone_text": "49%"},
    ]
    svg = str(charts.bars("sex", rows))
    assert svg.startswith('<svg class="cr-svg cr-bars" role="img" aria-labelledby="cr-sex-title"')
    assert '<title id="cr-sex-title">sex</title>' in svg
    # Two rows × two series = four marks, every one with a <title>.
    assert svg.count('class="cr-mark"') == 4
    assert svg.count("<title>") == 4
    assert "female — this cohort: 62%" in svg and "male — everyone: 49%" in svg
    # The 4px rounded data end, and no stroke drawn around a mark.
    assert "A4,4 0 0 1" in svg
    assert "stroke=" not in svg.split("<path")[1].split(">")[0]
    # Direct labels on the cohort series only: two rows, two value labels.
    assert svg.count('class="cr-v"') == 2
    assert ">62%<" in svg and ">51%<" not in svg
    # The legend names both series; the marks carry the contract's colours.
    assert "this cohort" in svg and "everyone" in svg
    assert f'fill="{charts.COHORT}"' in svg and f'fill="{charts.EVERYONE}"' in svg
    # Thin: the cohort's bar, the surface gap and everyone's bar fit under
    # the 24px cap, and the chart is drawn at its natural pixel size - one
    # scale for every chart on the page, never width="100%".
    assert charts._BAR_COHORT + charts._GAP + charts._BAR_EVERYONE <= 24
    assert 'width="600" height="104"' in svg and 'width="100%"' not in svg


def test_bars_renders_a_hatched_stub_for_a_withheld_cell():
    rows = [{"label": "unknown", "cohort": None, "everyone": 0.02, "cohort_suppressed": True,
             "everyone_text": "2%"}]
    svg = str(charts.bars("sex", rows))
    assert '<pattern id="cr-sex-hatch-cohort"' in svg and 'patternTransform="rotate(45)"' in svg
    assert 'fill="url(#cr-sex-hatch-cohort)"' in svg
    assert "cr-stub" in svg
    assert "&lt; 11" in svg          # the label, escaped by markupsafe
    assert "unknown — this cohort: &lt; 11" in svg


def test_hist_is_one_hue_darker_with_magnitude_and_caps_every_column():
    bins = [{"label": "0", "n": 30}, {"label": "1", "n": 60},
            {"label": "2", "n": None, "suppressed": True}]
    svg = str(charts.hist("encounters per person", bins))
    assert 'aria-labelledby="cr-encounters-per-person-title"' in svg
    assert f'fill="{charts.RAMP[1]}"' in svg           # the peak takes the dark end
    assert 'fill="url(#cr-encounters-per-person-hatch-cohort)"' in svg
    assert ">30<" in svg and ">60<" in svg and "&lt; 11" in svg
    assert svg.count('class="cr-grid"') == 3           # hairline gridlines, one axis
    assert svg.count("<title>") == 3


def test_line_has_two_px_round_joins_ringed_end_markers_and_one_axis():
    points = [{"x": "2025-01", "cohort": 4.0, "everyone": 12.0},
              {"x": "2025-02", "cohort": None, "everyone": 11.0, "cohort_suppressed": True},
              {"x": "2025-03", "cohort": 6.5, "everyone": 12.0}]
    svg = str(charts.line("encounters per month", points))
    assert svg.count('stroke-width="2" stroke-linejoin="round" stroke-linecap="round"') == 2
    assert 'r="6" fill="#ffffff"' in svg               # the 2px surface ring under the marker
    assert svg.count('r="4"') == 2                     # one ≥ 8px end marker per series
    assert "this cohort 6.5" in svg and "everyone 12" in svg
    # Hairline gridlines at clean values (the ceiling over 12 is 20: 0, 5,
    # 10, 15, 20 - never 33.33), and one axis.
    assert svg.count('class="cr-grid"') == len(charts._ticks(20, dense=True)) == 5
    assert ">15<" in svg and "33.33" not in svg and "cr-axis-right" not in svg
    assert charts._ticks(100, dense=True) == [0, 20, 40, 60, 80, 100]
    assert charts._ticks(2.5, dense=True) == [0, 0.5, 1, 1.5, 2, 2.5]
    assert charts._ticks(500) == [0, 250, 500]
    # The withheld point breaks the cohort line rather than drawing a zero.
    cohort_path = [p for p in svg.split("<path")[1:] if f'stroke="{charts.COHORT}"' in p][0]
    d = cohort_path.split('d="')[1].split('"')[0]
    assert d.count("M") == 2 and "L" not in d
    assert "2025-02 — this cohort: &lt; 11" in svg


def test_tile_sets_label_value_hint_and_link():
    html = str(charts.tile("persons", "1,234", "distinct person_id", href="/rows"))
    assert '<a href="/rows">1,234</a>' in html
    assert '<div class="cr-tile-k">persons</div>' in html
    assert "distinct person_id" in html
    assert "<a" not in str(charts.tile("encounters", "0"))


def test_share_labels_agree_between_the_report_and_the_charts():
    for f in (None, 0, 0.004, 0.045, 0.62, 1.0):
        assert cr._pct(f) == charts.pct(f)


# ---------------------------------------------------------------------------
# cohort_report.py - the metrics
# ---------------------------------------------------------------------------

def test_build_report_on_an_empty_cdm_yields_every_section_and_never_raises():
    conn = _Conn()
    report = cr.build_report(conn, "diabetes")
    for section in ("cohort", "demographics", "conditions", "medications", "measurements",
                    "procedures", "utilization", "cost", "risk"):
        assert section in report, section
        assert "error" not in report[section], report[section]
    assert report["persons"] == 0 and report["cohort"]["persons_text"] == "< 11"
    assert report["cost"]["sentence"] == cr.NOT_IN_CDM["cost"]
    assert report["demographics"]["payer"] == cr.NOT_IN_CDM["payer"]
    assert report["demographics"]["deceased"] == cr.NOT_IN_CDM["deceased"]
    assert report["utilization"]["documents"] == cr.NOT_IN_CDM["documents"]
    assert report["procedures"]["allergies"] == cr.NOT_IN_CDM["allergies"]
    assert report["risk"]["flu"] == cr.NOT_IN_CDM["flu"]
    # No value sets: the code-level breakdowns withhold themselves and say why.
    assert report["withheld"] == cr.WITHHELD_NO_VALUE_SETS
    for section in ("conditions", "medications", "measurements", "procedures"):
        assert report[section]["withheld"] == cr.WITHHELD_NO_VALUE_SETS
    assert not any("drug_source_value" in s and "AS key" in s for s in _statements(conn))
    # The dimensions that are not code-level still render their empty state.
    assert report["demographics"]["sex"] == [] and report["utilization"]["facilities"] == []
    assert [b["label"] for b in report["utilization"]["per_person"]] == list(cr.ENC_BINS)
    assert all(b["suppressed"] for b in report["utilization"]["per_person"])
    assert len(report["utilization"]["per_month"]) == cr.MONTHS
    assert len(report["conditions"]["onset"]) == cr.ONSET_YEARS
    assert [r["label"] for r in report["risk"]["noshow"]] == list(cr.NOSHOW_BANDS)
    assert [m["title"] for m in report["risk"]["measures"].values()] == [
        "persons with a visit in the last 12 months",
        "diabetics with an A1c in the last 12 months",
        "hypertensives with a blood pressure in the last 12 months",
    ]
    assert list(report["risk"]["measures"]) == ["visit12", "a1c", "bp"]
    assert all(m["suppressed"] for m in report["risk"]["measures"].values())


def test_every_breakdown_is_a_group_by_and_code_level_ones_exclude_heightened_codes():
    conn = _Conn()
    report = cr.build_report(conn, "diabetes", value_sets=_value_sets("F32.9"))
    assert report["withheld"] is None
    statements = _statements(conn)
    grouped = [s for s in statements if "GROUP BY" in s]
    assert len(grouped) >= 12
    # The cohort predicate is the cohort screen's own, and every two-series
    # breakdown counts DISTINCT persons against it, never rows.
    for s in grouped:
        assert ("WITH cohort AS ( SELECT p.person_id FROM cdm.person p WHERE EXISTS (SELECT 1 FROM "
                "cdm.condition_occurrence co WHERE co.person_id = p.person_id AND") in s
        if "AS key" in s and "LEFT JOIN cohort c ON c.person_id = x.person_id" in s:
            # A two-series breakdown counts DISTINCT persons (or encounters);
            # COUNT(*) here would count diagnosis rows and look plausible.
            assert "COUNT(DISTINCT" in s and "COUNT(*)" not in s.split("LEFT JOIN cohort")[0]
    # Every code-level breakdown carries the exclusion, with the curated code bound.
    for col in ("condition_source_value", "drug_source_value", "procedure_source_value",
                "measurement_source_value"):
        keyed = [(s, p) for s, p in conn.executed if f"SELECT x.{col} AS key" in s]
        assert keyed, col
        for s, p in keyed:
            assert f"x.{col} NOT IN (%s)" in s, s
            assert p[-1] == "F32.9"
    assert any("condition_status_source_value" in s and "NOT IN (%s)" in s for s in statements)
    assert any("COUNT(DISTINCT de.drug_source_value)" in s and "de.drug_source_value NOT IN (%s)" in s
               for s in statements)
    # The definition itself: the diabetes shortcut's five prefixes, bound as LIKE patterns.
    first = conn.executed[1]
    assert "co.condition_source_value LIKE %s" in first[0]
    assert first[1][:5] == ("E08%", "E09%", "E10%", "E11%", "E13%")


def test_the_selectors_narrow_the_cohort_with_the_same_expressions_the_charts_group_by():
    conn = _Conn()
    cr.build_report(conn, "E11", sex="female", band="45-64", value_sets=_value_sets())
    cte = _statements(conn)[1].split("SELECT COUNT(*) FROM cohort")[0]
    assert "COALESCE(NULLIF(lower(p.gender_source_value), ''), 'unknown') = %s" in cte
    assert "WHEN (EXTRACT(YEAR FROM CURRENT_DATE)::int - p.year_of_birth) < 65 THEN '45-64'" in cte
    assert conn.executed[1][1] == ("E11%", "female", "45-64")
    # The age-band chart groups cdm.person by the same CASE the selector
    # used, over the person alias x this time.
    band_chart = [s for s in _statements(conn) if "AS key" in s and "cdm.person x" in s
                  and "WHEN x.year_of_birth IS NULL THEN 'unknown'" in s]
    assert len(band_chart) == 1
    assert "(EXTRACT(YEAR FROM CURRENT_DATE)::int - x.year_of_birth) < 65 THEN '45-64'" in band_chart[0]


def test_small_cells_and_their_complements_are_withheld():
    two = cr._rows([("female", "female", 500, 900), ("male", "male", 7, 800)], 507, 1700, partition=True)
    assert all(r["cohort_suppressed"] for r in two)          # no recovery by subtraction
    assert not any(r["everyone_suppressed"] for r in two)
    assert two[0]["cohort_text"] == "< 11" and two[0]["cohort_n"] is None
    three = cr._rows([("a", "a", 300, 0), ("b", "b", 40, 0), ("c", "c", 5, 0)], 345, 0, partition=True)
    assert [r["cohort_suppressed"] for r in three] == [False, True, True]
    top = cr._rows([("a", "a", 300, 0), ("b", "b", 40, 0), ("c", "c", 5, 0)], 345, 0)
    assert [r["cohort_suppressed"] for r in top] == [False, False, True]   # a top-N list is not a partition
    assert top[0]["cohort_text"] == "87%" and top[0]["cohort_n_text"] == "300"
    bins = cr._bins({"0": 3, "1": 40}, cr.MED_BINS, "meds")
    assert [b["suppressed"] for b in bins] == [True, False, True, True, True, True]
    assert bins[1]["cell"] == "meds:1"
    assert cr._measure("t", 5, 100, "a", "b")["suppressed"] is True
    assert cr._measure("t", 50, 100, "a", "b")["text"] == "50%"
    point = cr._point("2026-01", 25, 300, 500, 6000, "month")
    assert point["cohort"] == 50.0 and point["cohort_text"] == "50.0" and point["everyone_text"] == "50.0"
    assert cr._point("2026-02", 3, 300, 500, 6000, "month")["cohort_suppressed"] is True


def test_the_no_show_distribution_is_the_demonstrations_scorer_at_its_own_bands():
    """The demonstration's own no-show scorer, at the demonstration's own
    bands; the payer factor has no CDM column and is stated as omitted."""
    assert (cr.NOSHOW_HIGH, cr.NOSHOW_MEDIUM) == (60, 35)
    for factor in ("> 730 THEN 30", "> 365 THEN 20", "> 180 THEN 10", "<= 2 THEN 15",
                   ">= 15 THEN -10", "BETWEEN 18 AND 34 THEN 12", ">= 75 THEN 8", ">= 4 THEN 8"):
        assert factor in cr._NOSHOW_SCORE_SQL, factor
    assert "Medicaid" not in cr._NOSHOW_SCORE_SQL
    conn = _Conn()
    report = cr.build_report(conn, "asthma", value_sets=_value_sets())
    assert "payer factor has no CDM column" in report["risk"]["noshow_note"]
    scored = [s for s in _statements(conn) if "scored AS" in s]
    assert scored and "GROUP BY 1" in scored[0] and "LEAST(95, GREATEST(5, 10" in scored[0]


def test_a_protected_term_is_read_from_the_value_sets_not_a_list():
    mental = _value_sets("F32.9", "F41.1")
    assert cohort.protected_term("depression", mental) == "mental_health"   # F32/F33 cover F32.9
    assert cohort.protected_term("F32", mental) == "mental_health"          # a prefix over a curated code
    assert cohort.protected_term("F32.91", mental) == "mental_health"       # narrower than a curated code
    assert cohort.protected_term("mental health", mental) == "mental_health"
    assert cohort.protected_term("diabetes", mental) is None
    assert cohort.protected_term("hiv", _value_sets(category=SensitiveCategory.HIV)) == "hiv"
    assert cohort.protected_term("hiv", mental) is None
    assert cohort.protected_term("depression", None) is None                 # nothing configured, nothing protected


def test_the_cohort_count_still_builds_the_same_sql_through_the_shared_clause():
    conn = _Conn(answers=[("COUNT(DISTINCT co.person_id)", [(12,)]), ("SELECT COUNT(*) FROM cdm.person", [(400,)])])
    result = cohort.count_patients_with_condition(conn, "diabetes")
    assert result.patient_count == 12 and result.total_patients_stored == 400
    counting = [s for s in _statements(conn) if "COUNT(DISTINCT co.person_id)" in s][0]
    assert counting == ("SELECT COUNT(DISTINCT co.person_id) FROM cdm.condition_occurrence co WHERE "
                        "((co.condition_source_value LIKE %s OR co.condition_source_value LIKE %s OR "
                        "co.condition_source_value LIKE %s OR co.condition_source_value LIKE %s OR "
                        "co.condition_source_value LIKE %s))")


def test_rows_use_the_charts_expressions_and_refuse_a_heightened_code():
    conn = _Conn(answers=[("SELECT p.person_source_value", [("eAB12cd3",), ("Patient/eXYZ",)])])
    rows = cr.build_rows(conn, "diabetes", sex="female", cell="condition:E11.9", value_sets=_value_sets("F32.9"))
    assert rows["references"] == ["Patient/eAB12cd3", "Patient/eXYZ"] and not rows["truncated"]
    sql, params = conn.executed[-1]
    assert "EXISTS (SELECT 1 FROM cdm.condition_occurrence z WHERE z.person_id = p.person_id AND z.condition_source_value = %s)" in sql
    assert params[-1] == "E11.9" and "female" in params
    with pytest.raises(cr.CellError):
        cr.build_rows(_Conn(), "diabetes", cell="condition:F32.9", value_sets=_value_sets("F32.9"))
    with pytest.raises(cr.CellError):
        cr.build_rows(_Conn(), "diabetes", cell="condition:E11.9")        # no value sets: withheld
    with pytest.raises(cr.CellError):
        cr.build_rows(_Conn(), "diabetes", cell="nonsense:1", value_sets=_value_sets())
    for cell in ("sex:female", "band:65+", "status:active", "facility:7", "visit:9202", "weekday:1",
                 "month:2026-01", "onset:2019", "enc:5-9", "meds:2", "medicated:none", "noshow:high",
                 "measure:visit12", "measure:a1c-num", "measure:bp-den", "value:4548-4:3"):
        cr.build_rows(_Conn(), "diabetes", cell=cell, value_sets=_value_sets())


def test_where_the_cohort_lives_reads_the_ledger_per_chart_and_never_per_run():
    refs = [f"Patient/{i}" for i in range(30)]
    runs = [{"steps": [
        {"direction": "deliver", "written": True, "system": "cerner", "patient": f"Patient/{i}", "moved": 3}
        for i in range(12)
    ] + [{"direction": "deliver", "written": True, "system": "cerner", "patient": "Patient/999", "moved": 3},
         {"direction": "deliver", "written": False, "system": "epic", "patient": "Patient/1", "moved": 0},
         {"direction": "deliver", "written": True, "system": "epic", "patient": None, "moved": 40},
         {"direction": "read", "written": True, "system": "epic", "patient": "Patient/2", "moved": 5}]}]
    lives = cr.ledger_holdings_for(runs, refs, {}, store_charts=30, cohort_charts=30)
    by_key = {r["key"]: r for r in lives["rows"]}
    assert by_key["store"]["held"] == 30 and by_key["cerner"]["held"] == 12
    assert by_key["cerner"]["gap_text"] == "18" and by_key["cerner"]["moved_text"] == "36"
    assert "epic" not in by_key                       # decided-not-written and reads hold nothing
    assert lives["across"] == 42 and "population-mode" in lives["note"]
    assert lives["gaps"][0]["cohort_text"] == "18"
    assert "per run, not per chart" in lives["by_type"]
    store = cr.store_holdings_for(object(), refs)     # a reader without the bulk method says so
    assert store["records_text"] == "—" and "cannot count" in store["note"]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def test_the_report_requires_analytics_query(monkeypatch):
    _no_connection(monkeypatch)
    viewer, _, _ = _client(roles="viewer")
    assert viewer.get("/cohort/report", params={"term": "diabetes"}).status_code == 403
    analyst, _, _ = _client(roles="analyst")
    assert analyst.get("/cohort/report", params={"term": "diabetes"}).status_code == 200


def test_the_report_audits_before_it_runs_and_names_every_section(monkeypatch):
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    _wire_connection(monkeypatch, _Conn())
    seen = {}
    real = cr.build_report

    def stub(conn, term, sex="", band="", **kw):
        seen["audited_first"] = any(e["action"] == "analytics.cohort_report" for e in audit.events)
        seen["term"] = term
        return real(_Conn(), term, sex, band, **kw)      # the real shape, over an empty CDM

    monkeypatch.setattr(cr, "build_report", stub)
    r = client.get("/cohort/report", params={"term": "diabetes", "sex": "female", "band": "45-64"})
    assert r.status_code == 200
    assert seen == {"audited_first": True, "term": "diabetes"}
    # The audit row records the whole definition, and so does the h1; the
    # line under it lists the selectors.
    event = [e for e in audit.events if e["action"] == "analytics.cohort_report"]
    assert event == [{"actor": "tester", "action": "analytics.cohort_report",
                      "resource_key": "cohort/diabetes · female · 45-64", "purpose_of_use": "operations"}]
    body = r.text
    for title in SECTIONS:
        assert title in body, title
    assert "<h1>Cohort report — diabetes · female · 45-64</h1>" in body
    assert '<p class="cr-def">female · 45-64</p>' in body
    assert LEAD in body
    assert "carries no cost tables, so claims per person" in body
    assert 'id="cohort"' in body and 'id="lives"' in body
    assert body.count('class="cr-table"') >= 10 and "as a table" in body
    assert 'role="img"' in body
    # An analyst holds no patient:read, so no number links to a rows view.
    assert "/cohort/report/rows" not in body
    assert "&lt; 11" in body


def test_the_template_carries_every_title_the_parity_test_pins():
    template = (ROOT / "core" / "web" / "templates" / "cohort_report.html").read_text(encoding="utf-8")
    for title in SECTIONS + TITLES:
        assert title in template, title
    assert LEAD in template and 'class="cr-table"' in template and "as a table" in template


def test_without_an_omop_connection_the_report_says_so_plainly(monkeypatch):
    _no_connection(monkeypatch)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort/report", params={"term": "diabetes"})
    assert r.status_code == 200
    assert "Not answered" in r.text and 'id="cohort"' not in r.text
    # The audit row is written before anything runs, connection or not.
    assert any(e["action"] == "analytics.cohort_report" for e in audit.events)


def test_a_protected_term_takes_the_cohort_screens_refusal_path(monkeypatch):
    import core.terminology.loader as loader

    _no_connection(monkeypatch)
    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort/report", params={"term": "depression"})
    assert r.status_code == 200
    assert "Not answered" in r.text and "heightened category" in r.text
    assert not any(e["action"] == "analytics.cohort_report" for e in audit.events)
    denied = [e for e in audit.events if e["action"] == "access.denied"]
    assert denied and denied[0]["resource_key"] == "cohort/depression heightened:mental_health"
    # The cohort screen refuses the same term the same way, and counts nothing.
    r2 = client.get("/cohort", params={"q": "depression", "sex": "female"})
    assert r2.status_code == 200 and "heightened category" in r2.text
    assert not any(e["action"] == "analytics.cohort" for e in audit.events)
    assert "distinct persons —" not in r2.text and "conditions matching" not in r2.text
    # The form still round-trips the definition the reader had set.
    assert 'value="depression"' in r2.text and '<option value="female" selected>' in r2.text
    # And an ordinary term is unaffected by the value sets being present.
    r3 = client.get("/cohort/report", params={"term": "diabetes"})
    assert r3.status_code == 200 and "heightened category" not in r3.text


def test_rows_need_patient_read_on_top_of_analytics_query(monkeypatch):
    _no_connection(monkeypatch)
    analyst, _, _ = _client(roles="analyst")
    assert analyst.get("/cohort/report/rows", params={"term": "diabetes"}).status_code == 403
    audit = _RecordingAudit()
    researcher, _, _ = _client(roles="researcher", audit=audit)
    r = researcher.get("/cohort/report/rows", params={"term": "diabetes", "cell": "sex:female"})
    assert r.status_code == 200 and "Not answered" in r.text
    assert any(e["action"] == "analytics.cohort_rows" and e["resource_key"] == "cohort/diabetes/sex:female"
               for e in audit.events)


def test_rows_list_opaque_references_with_the_purpose_asserting_open_form(monkeypatch):
    conn = _Conn(answers=[("SELECT p.person_source_value", [("eAB12cd3",)])])
    _wire_connection(monkeypatch, conn)
    client, _, _ = _client(roles="researcher")
    r = client.get("/cohort/report/rows", params={"term": "diabetes", "cell": "sex:female"})
    assert r.status_code == 200
    assert "Patient/eAB12cd3" in r.text
    assert 'action="/patients/eAB12cd3/open"' in r.text and 'name="purpose_of_use"' in r.text
    assert client.get("/cohort/report/rows", params={"term": "diabetes", "cell": "nonsense:1"}).status_code == 400


def test_the_cohort_result_offers_the_report_with_the_whole_definition(monkeypatch):
    conn = _Conn(answers=[("COUNT(DISTINCT p.person_id)", [(12,)]),
                          ("SELECT COUNT(*) FROM cdm.person", [(100,)])])
    _wire_connection(monkeypatch, conn)
    client, _, _ = _client(roles="analyst")
    r = client.get("/cohort", params={"q": "diabetes", "exact": "", "sex": "", "band": "45-64"})
    assert r.status_code == 200
    assert 'href="/cohort/report?term=diabetes&amp;exact=&amp;sex=&amp;band=45-64"' in r.text
    assert ">Report</a>" in r.text and "distinct persons — diabetes · 45-64" in r.text


# ---------------------------------------------------------------------------
# The builder, v2: each variable alone a cohort, the condition a search
# ---------------------------------------------------------------------------

def test_the_report_with_no_condition_is_the_selectors_cohort_and_says_so():
    conn = _Conn()
    report = cr.build_report(conn, "", sex="female", band="65+", value_sets=_value_sets())
    assert report["definition"] == "female · 65+" and report["selectors"] == "female · 65+"
    assert report["named"] is False
    # The cohort is every person the selectors name - no condition row required.
    cte = _statements(conn)[1].split("SELECT COUNT(*) FROM cohort")[0]
    assert cte.startswith("WITH cohort AS ( SELECT p.person_id FROM cdm.person p WHERE "
                          "COALESCE(NULLIF(lower(p.gender_source_value), ''), 'unknown') = %s AND "
                          "CASE WHEN p.year_of_birth IS NULL THEN 'unknown'")
    assert "condition_occurrence" not in cte and conn.executed[1][1] == ("female", "65+")
    # "condition status" is the one sentence and no query; "onset by year"
    # counts every condition; the cohort's most common conditions still draw.
    assert report["conditions"]["status"] == []
    assert report["conditions"]["status_note"] == cr.NO_CONDITION_STATUS
    assert not any("condition_status_source_value" in s for s in _statements(conn))
    assert len([s for s in _statements(conn) if "MIN(co.condition_start_date)" in s]) == 1
    assert len(report["conditions"]["onset"]) == cr.ONSET_YEARS
    assert any("SELECT x.condition_source_value AS key" in s for s in _statements(conn))
    # A named condition keeps its status chart.
    named = _Conn()
    report = cr.build_report(named, "diabetes", value_sets=_value_sets())
    assert report["named"] is True and report["conditions"]["status_note"] is None
    assert any("condition_status_source_value" in s for s in _statements(named))
    # Nothing set at all is every person, named so.
    everyone = _Conn()
    report = cr.build_report(everyone, "", value_sets=_value_sets())
    assert report["definition"] == "all persons" and report["selectors"] == ""
    assert "WITH cohort AS ( SELECT p.person_id FROM cdm.person p WHERE TRUE )" in _statements(everyone)[1]
    assert everyone.executed[1][1] == ()


def test_an_exact_pick_defines_the_cohort_by_concept_name_equality():
    conn = _Conn(answers=[("SELECT 1 FROM vocab.concept", [(1,)])])
    cr.build_report(conn, "Type 2 diabetes mellitus", sex="female", exact=True, value_sets=_value_sets())
    sql, params = conn.executed[1]
    assert ("WHERE EXISTS (SELECT 1 FROM cdm.condition_occurrence co WHERE co.person_id = p.person_id AND "
            "EXISTS (SELECT 1 FROM vocab.concept vc WHERE vc.concept_name = %s AND "
            + cohort.CONDITION_NAME_JOIN + "))") in sql
    assert params == ("Type 2 diabetes mellitus", "female") and "LIKE" not in sql
    # The drill-down defines the cohort the same way.
    rows = _Conn(answers=[("SELECT 1 FROM vocab.concept", [(1,)])])
    cr.build_rows(rows, "Type 2 diabetes mellitus", sex="female", cell="sex:female", exact=True,
                  value_sets=_value_sets())
    sql, params = rows.executed[-1]
    assert "vc.concept_name = %s" in sql and params[0] == "Type 2 diabetes mellitus"


def test_the_builder_counts_the_selectors_alone_and_nothing_set_as_all_persons(monkeypatch):
    conn = _Conn(answers=[("COUNT(DISTINCT p.person_id)", [(1200,)]),
                          ("SELECT COUNT(*) FROM cdm.person", [(5000,)]),
                          ("AS sex, COUNT(*) FROM cdm.person p GROUP BY 1", [("female", 2600), ("male", 2400)])])
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    # A bare /cohort asks nothing yet: the form, the population headline,
    # the CDM's own sex values as options - no count, no audit row, no chips.
    r = client.get("/cohort")
    assert r.status_code == 200
    body = r.text
    assert '<input type="search" name="q" placeholder="Search conditions…"' in body
    assert '<option value="female"' in body and '<option value="male"' in body
    assert "distinct persons —" not in body and audit.events == []
    assert not any("COUNT(DISTINCT p.person_id)" in s for s in _statements(conn))
    assert 'class="starter"' not in body and "persons in the OMOP layer" in body
    assert "carries no payer table, so payer is not a selector here." in body
    # Sex alone is a cohort: one COUNT(DISTINCT person_id) over cdm.person.
    r = client.get("/cohort", params={"q": "", "exact": "", "sex": "female", "band": ""})
    assert r.status_code == 200
    assert ">1,200<" in r.text and "distinct persons — female" in r.text
    sql, params = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert sql == ("SELECT COUNT(DISTINCT p.person_id) FROM cdm.person p WHERE "
                   "COALESCE(NULLIF(lower(p.gender_source_value), ''), 'unknown') = %s")
    assert params == ("female",)
    assert audit.events[-1] == {"actor": "tester", "action": "analytics.cohort",
                                "resource_key": "cohort/female", "purpose_of_use": "operations"}
    assert 'href="/cohort/report?term=&amp;exact=&amp;sex=female&amp;band="' in r.text
    assert '<option value="female" selected>' in r.text
    # Nothing set - the search box emptied - is every person.
    r = client.get("/cohort", params={"q": "", "exact": "", "sex": "", "band": ""})
    assert "distinct persons — all persons" in r.text
    sql, params = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert sql == "SELECT COUNT(DISTINCT p.person_id) FROM cdm.person p WHERE TRUE" and params == ()
    assert audit.events[-1]["resource_key"] == "cohort/all persons"
    # Under the small-cell floor the tile reads "< 11".
    small = _Conn(answers=[("COUNT(DISTINCT p.person_id)", [(7,)]),
                           ("SELECT COUNT(*) FROM cdm.person", [(5000,)])])
    _wire_connection(monkeypatch, small)
    r = client.get("/cohort", params={"band": "0-17"})
    assert '<div class="n">&lt; 11</div>' in r.text and "distinct persons — 0-17" in r.text


def test_the_search_lists_the_cdm_s_condition_names_derived_with_exact_links(monkeypatch):
    import core.terminology.loader as loader

    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    conn = _Conn(answers=[
        ("SELECT 1 FROM vocab.concept", [(1,)]),
        ("SELECT vc.concept_name, COUNT(DISTINCT co.person_id) AS persons",
         [("Type 2 diabetes mellitus", 840), ("Type 1 diabetes mellitus", 90), ("Diabetes insipidus", 4)]),
        ("COUNT(DISTINCT p.person_id)", [(930,)]),
        ("SELECT COUNT(*) FROM cdm.person", [(5000,)]),
    ])
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort", params={"q": "diab", "exact": "", "sex": "", "band": "65+"})
    assert r.status_code == 200
    body = r.text
    assert "conditions matching “diab”" in body
    # Derived: one GROUP BY over the vocabulary's names joined to the CDM's
    # rows, the most persons first, twelve at most, the heightened code out.
    search = [(s, p) for s, p in conn.executed if "SELECT vc.concept_name" in s]
    assert len(search) == 1
    sql, params = search[0]
    assert cohort.CONDITION_NAME_JOIN in sql and "lower(vc.concept_name) LIKE %s" in sql
    assert sql.endswith("GROUP BY 1 ORDER BY persons DESC, 1 LIMIT 12")
    assert "co.condition_source_value NOT IN (%s)" in sql and params == ("%diab%", "F32.9")
    # Each name is a link that sets the condition EXACTLY and keeps the
    # selectors; the order is the query's; a small count reads "< 11".
    assert 'href="/cohort?q=Type%202%20diabetes%20mellitus&amp;exact=1&amp;sex=&amp;band=65%2B"' in body
    assert body.index("Type 2 diabetes mellitus") < body.index("Type 1 diabetes mellitus") < body.index("Diabetes insipidus")
    assert ">840 persons<" in body and ">&lt; 11 persons<" in body
    # The typed term counted by the contains match meanwhile.
    assert "distinct persons — diab · 65+" in body and ">930<" in body
    assert audit.events[-1]["resource_key"] == "cohort/diab · 65+"
    # The pick: equality on the name, the picked row marked, exact carried to the report.
    r = client.get("/cohort", params={"q": "Type 2 diabetes mellitus", "exact": "1", "sex": "", "band": "65+"})
    body = r.text
    sql, params = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert "vc.concept_name = %s" in sql and params == ("Type 2 diabetes mellitus", "65+")
    assert 'class="on"' in body and 'aria-current="true"' in body
    assert "the condition named exactly &#39;Type 2 diabetes mellitus&#39;" in body
    assert 'href="/cohort/report?term=Type%202%20diabetes%20mellitus&amp;exact=1&amp;sex=&amp;band=65%2B"' in body
    assert audit.events[-1]["resource_key"] == "cohort/Type 2 diabetes mellitus · 65+"
    # One character asks for no list.
    r = client.get("/cohort", params={"q": "d"})
    assert "conditions matching" not in r.text and len(search) == 1


def test_the_search_list_withholds_itself_without_the_value_sets(monkeypatch):
    conn = _Conn(answers=[("SELECT 1 FROM vocab.concept", [(1,)]),
                          ("COUNT(DISTINCT p.person_id)", [(930,)]),
                          ("SELECT COUNT(*) FROM cdm.person", [(5000,)])])
    _wire_connection(monkeypatch, conn)
    client, _, _ = _client(roles="analyst")
    r = client.get("/cohort", params={"q": "diab"})
    assert r.status_code == 200
    assert "conditions matching “diab”" in r.text and cr.WITHHELD_NO_VALUE_SETS in r.text
    assert not any("SELECT vc.concept_name" in s for s in _statements(conn))
    assert ">930<" in r.text                                   # the count itself is not a breakdown


def test_the_report_with_no_condition_is_titled_by_its_definition(monkeypatch):
    import core.terminology.loader as loader

    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    _wire_connection(monkeypatch, _Conn())
    r = client.get("/cohort/report", params={"sex": "female", "band": "65+"})
    assert r.status_code == 200
    body = r.text
    assert "<h1>Cohort report — female · 65+</h1>" in body
    assert '<p class="cr-def">female · 65+</p>' in body
    assert audit.events[-1]["resource_key"] == "cohort/female · 65+"
    for title in SECTIONS:
        assert title in body, title
    # "condition status" is the sentence and no chart - and the sentence
    # wins over "withheld": with no condition there is no status whether or
    # not the value sets are configured.
    assert "condition status" in body and cr.NO_CONDITION_STATUS in body
    assert "cr-condition-status-title" not in body
    assert "onset by year" in body
    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    full = client.get("/cohort/report", params={"sex": "female", "band": "65+"}).text
    assert cr.NO_CONDITION_STATUS in full and "cr-condition-status-title" not in full
    assert "cr-most-common-conditions-in-this-cohort-title" in full   # the cohort's conditions still draw
    assert 'class="cr-selectors" data-autoapply' in body and 'name="exact" value=""' in body
    # Nothing set: every person, and the line that says there is no selector.
    r = client.get("/cohort/report")
    assert "<h1>Cohort report — all persons</h1>" in r.text
    assert '<p class="cr-def">everyone — no selector</p>' in r.text
    assert audit.events[-1]["resource_key"] == "cohort/all persons"
    # The rows view takes the same definition.
    researcher, _, _ = _client(roles="researcher", audit=audit)
    r = researcher.get("/cohort/report/rows", params={"band": "65+", "cell": "sex:female"})
    assert r.status_code == 200 and "<h1>Cohort rows — 65+</h1>" in r.text
    assert audit.events[-1]["resource_key"] == "cohort/65+/sex:female"


def test_app_js_carries_the_autoapply_handler_and_both_forms_carry_the_attribute(monkeypatch):
    js = (ROOT / "core" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    handler = js.split("form[data-autoapply]", 1)[1]
    assert "requestSubmit" in handler
    # A discrete control submits 120 ms after it changes ...
    assert "el.tagName !== 'SELECT' && el.type !== 'radio' && el.type !== 'checkbox'" in handler
    assert "later(120)" in handler
    # ... the search box 500 ms after typing pauses, two characters or emptied;
    # any other text input never submits itself; Enter submits at once.
    assert "el.type !== 'search'" in handler
    assert "value.length >= 2 || value.length === 0" in handler and "later(500)" in handler
    assert "ev.key !== 'Enter'" in handler
    assert "data-autoapply-reset" in handler
    assert "data-oc-autoscope" in js                            # the scope handler is untouched
    _no_connection(monkeypatch)
    client, _, _ = _client(roles="analyst")
    assert '<form method="get" action="/cohort" class="cb-form" data-autoapply>' in client.get("/cohort").text
    report = client.get("/cohort/report", params={"term": "diabetes"}).text
    assert 'class="cr-selectors" data-autoapply>' in report


# ---------------------------------------------------------------------------
# The condition box is an AUTOCOMPLETE (the contract's addendum, section 6)
# ---------------------------------------------------------------------------

def _suggest_conn():
    return _Conn(answers=[
        ("SELECT 1 FROM vocab.concept", [(1,)]),
        ("SELECT vc.concept_name, COUNT(DISTINCT co.person_id) AS persons",
         [("Essential hypertension", 1240), ("Hypertensive heart disease", 90),
          ("Hypertensive crisis", 4)]),
        ("COUNT(DISTINCT p.person_id)", [(1240,)]),
        ("SELECT COUNT(*) FROM cdm.person", [(5000,)]),
    ])


def test_the_suggestion_endpoint_answers_known_names_with_their_counts(monkeypatch):
    """The panel's names are the corpus's own, derived by the same GROUP BY
    the list below the box is, with the same heightened exclusion - and the
    label carries the small-cell rule, so the client never formats a
    suppressed count."""
    import core.terminology.loader as loader

    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    conn = _suggest_conn()
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort/suggest", params={"q": " hyp "})
    assert r.status_code == 200
    body = r.json()
    assert body["q"] == "hyp" and body["withheld"] is None and body["note"] is None
    assert body["conditions"] == [
        {"name": "Essential hypertension", "persons": 1240, "label": "1,240 persons"},
        {"name": "Hypertensive heart disease", "persons": 90, "label": "90 persons"},
        # Under the floor the count is not returned at all, and the label
        # is the suppression - never a number the client could format.
        {"name": "Hypertensive crisis", "persons": None, "label": "< 11 persons"},
    ]
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("application/json")
    # Derived, twelve at most, the heightened code excluded - one query.
    search = [(s, p) for s, p in conn.executed if "SELECT vc.concept_name" in s]
    assert len(search) == 1
    sql, params = search[0]
    assert cohort.CONDITION_NAME_JOIN in sql
    assert sql.endswith("GROUP BY 1 ORDER BY persons DESC, 1 LIMIT 12")
    assert params == ("%hyp%", "F32.9")
    # A vocabulary lookup is not a count: no audit row, and no person.
    assert audit.events == []
    assert not any("person_source_value" in s for s in _statements(conn))
    # Fewer than two characters, or a term longer than the column, asks nothing.
    for term in ("h", "", "x" * 201):
        empty = client.get("/cohort/suggest", params={"q": term}).json()
        assert empty["conditions"] == [] and empty["withheld"] is None
    assert len([s for s in _statements(conn) if "SELECT vc.concept_name" in s]) == 1


def test_the_suggestion_endpoint_takes_the_cohort_screens_permission(monkeypatch):
    _no_connection(monkeypatch)
    viewer, _, _ = _client(roles="viewer")
    assert viewer.get("/cohort/suggest", params={"q": "hyp"}).status_code == 403
    analyst, _, _ = _client(roles="analyst")
    assert analyst.get("/cohort/suggest", params={"q": "hyp"}).status_code == 200


def test_the_suggestion_endpoint_withholds_itself_without_the_value_sets(monkeypatch):
    """No value sets, no exclusion vocabulary: the panel withholds itself
    exactly as the list below the box does, and says so in one line."""
    conn = _suggest_conn()
    _wire_connection(monkeypatch, conn)
    client, _, _ = _client(roles="analyst")
    body = client.get("/cohort/suggest", params={"q": "hyp"}).json()
    assert body["conditions"] == [] and body["withheld"] == cr.WITHHELD_NO_VALUE_SETS
    assert not any("SELECT vc.concept_name" in s for s in _statements(conn))


def test_only_known_conditions_count_and_the_page_says_so(monkeypatch):
    """Ryan typed "High Blood Pressure" and the page contains-matched it in
    silence. Text the corpus knows no condition by now builds nothing: the
    cohort is the selectors alone, the sentence is stated verbatim, and the
    count, the report and the audit row all name the same cohort."""
    import core.terminology.loader as loader

    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    # The vocabulary is loaded and answers the search with nothing.
    conn = _Conn(answers=[("SELECT 1 FROM vocab.concept", [(1,)]),
                          ("COUNT(DISTINCT p.person_id)", [(2600,)]),
                          ("SELECT COUNT(*) FROM cdm.person", [(5000,)])])
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort", params={"q": "High Blood Pressure", "sex": "female"})
    assert r.status_code == 200
    body = r.text
    assert ("No condition in the corpus is named “High Blood Pressure”. Pick one of the "
            "names above, or clear the box to count everyone.") in body
    # The cohort is the selectors alone - no condition predicate at all.
    sql, params = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert sql == ("SELECT COUNT(DISTINCT p.person_id) FROM cdm.person p WHERE "
                   "COALESCE(NULLIF(lower(p.gender_source_value), ''), 'unknown') = %s")
    assert params == ("female",) and "condition_occurrence" not in sql
    # The tile, the audit row and the Report button agree on that cohort,
    # and the box still holds what the reader typed, to be fixed.
    assert "distinct persons — female" in body and ">2,600<" in body
    assert audit.events[-1]["resource_key"] == "cohort/female"
    assert 'value="High Blood Pressure"' in body
    assert "conditions matching" not in body
    # The report never claims the condition either, however it is reached.
    report = client.get("/cohort/report",
                        params={"term": "High Blood Pressure", "sex": "female"})
    assert report.status_code == 200
    assert "<h1>Cohort report — female</h1>" in report.text
    # The report's sentence is the builder's, pointing at the condition box
    # its own selector bar carries - and the box holds the words that named
    # nothing, so they can be fixed where they were typed.
    assert ("No condition in the corpus is named “High Blood Pressure”. Pick one of the "
            "names the condition box below suggests, or clear the box to count everyone."
            ) in report.text
    assert 'value="High Blood Pressure"' in report.text
    assert audit.events[-1]["resource_key"] == "cohort/female"


def test_a_term_that_is_a_known_name_resolves_to_it_exactly(monkeypatch):
    """One name, and the corpus's own spelling of it: a term equal to a
    known name is that condition, picked from the panel or typed."""
    import core.terminology.loader as loader

    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    conn = _suggest_conn()
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    r = client.get("/cohort", params={"q": "essential HYPERTENSION", "exact": "1"})
    assert r.status_code == 200
    sql, params = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert "vc.concept_name = %s" in sql and params == ("Essential hypertension",)
    assert "distinct persons — Essential hypertension" in r.text
    assert audit.events[-1]["resource_key"] == "cohort/Essential hypertension"
    # A term that merely CONTAINS known names keeps the contains match.
    r = client.get("/cohort", params={"q": "hyp"})
    sql, _ = [(s, p) for s, p in conn.executed if "COUNT(DISTINCT p.person_id)" in s][-1]
    assert "LIKE" in sql and "distinct persons — hyp" in r.text


def test_the_condition_box_is_a_combobox_over_the_suggestion_endpoint(monkeypatch):
    _no_connection(monkeypatch)
    client, _, _ = _client(roles="analyst")
    body = client.get("/cohort").text
    assert 'role="combobox"' in body and 'aria-autocomplete="list"' in body
    assert 'aria-expanded="false"' in body and 'aria-controls="cb-suggest"' in body
    assert 'aria-activedescendant=""' in body and 'data-suggest="/cohort/suggest"' in body
    assert '<ul class="cr-suggest" id="cb-suggest" role="listbox"' in body
    assert 'role="option"' in body and "data-suggest-row" in body
    assert 'data-autoapply-reset data-suggest-exact' in body
    # The server-rendered list stays: it IS the no-script fallback.
    assert 'placeholder="Search conditions…"' in body and 'autocomplete="off"' in body


def _selector_bar(body: str) -> str:
    """The report's own selector bar, on its own: everything the reader can
    re-cut the cohort with has to be INSIDE the form, or Redraw sends it
    nowhere."""
    assert '<form method="get" action="/cohort/report" class="cr-selectors" data-autoapply>' in body
    bar = body.split('class="cr-selectors" data-autoapply>', 1)[1].split("</form>", 1)[0]
    assert bar, "the report has no selector bar"
    return bar


def test_the_reports_selector_bar_carries_the_same_condition_box(monkeypatch):
    """The report is re-cut from the report (the demonstration's bar, which
    the standing rule pins this to): a condition COMBOBOX over the corpus's
    own names, sex, age band and Redraw, so a reader never has to go back to
    the builder to change one variable. Payer is not a control - this CDM
    has no payer table - and the bar says so in the one sentence."""
    import core.terminology.loader as loader

    monkeypatch.setattr(loader, "configured_value_sets", lambda: _value_sets("F32.9"))
    conn = _suggest_conn()
    _wire_connection(monkeypatch, conn)
    audit = _RecordingAudit()
    client, _, _ = _client(roles="analyst", audit=audit)
    bar = _selector_bar(client.get("/cohort/report").text)
    # The condition control, the builder's own, wired to the same endpoint.
    assert 'type="search" name="term"' in bar and 'placeholder="Search conditions…"' in bar
    assert 'role="combobox"' in bar and 'aria-autocomplete="list"' in bar
    assert 'aria-expanded="false"' in bar and 'aria-controls="cb-suggest"' in bar
    assert 'aria-activedescendant=""' in bar and 'data-suggest="/cohort/suggest"' in bar
    assert '<ul class="cr-suggest" id="cb-suggest" role="listbox"' in bar
    assert 'role="option"' in bar and "data-suggest-row" in bar
    assert 'autocomplete="off"' in bar
    # A pick rides in the same hidden field the panel sets, and a keystroke
    # clears it (data-autoapply-reset), exactly as on the builder.
    assert 'name="exact" value="" data-autoapply-reset data-suggest-exact' in bar
    # The rest of the definition, and the button the demonstration labels.
    assert 'name="sex"' in bar and 'name="band"' in bar
    assert ">Redraw</button>" in bar
    # (escaped, the way the template renders it - the note has an apostrophe)
    from markupsafe import escape

    assert str(escape(cr.PAYER_SELECTOR_NOTE)) in bar and 'name="payer"' not in bar
    # And it redraws: the picked name counts exactly, and the report the
    # reader lands on is that cohort - the audit row included.
    picked = client.get("/cohort/report", params={"term": "Essential hypertension", "exact": "1"})
    assert picked.status_code == 200
    assert "<h1>Cohort report — Essential hypertension</h1>" in picked.text
    assert audit.events[-1]["resource_key"] == "cohort/Essential hypertension"
    assert 'value="Essential hypertension"' in _selector_bar(picked.text)


def test_the_condition_box_is_one_definition_for_both_screens():
    """ONE control, not two: the builder and the report include the same
    template. Two copies would drift into offering different vocabularies,
    different keys and different keyboard behaviour - the demonstration
    factored views/_condition_box.php for the same reason."""
    templates = ROOT / "core" / "web" / "templates"
    box = templates / "_condition_box.html"
    assert box.is_file(), "the condition box has no single definition"
    markup = box.read_text(encoding="utf-8")
    assert 'role="combobox"' in markup and 'role="listbox"' in markup
    assert "data-suggest-row" in markup and "data-suggest-exact" in markup
    for name in ("cohort.html", "cohort_report.html"):
        src = (templates / name).read_text(encoding="utf-8")
        assert '{% from "_condition_box.html" import condition_box %}' in src, name
        assert "condition_box(" in src, name
        # The markup is in the macro, and nowhere else.
        assert 'role="combobox"' not in src, f"{name} spells the combobox a second time"
        assert 'class="cr-suggest"' not in src, f"{name} spells the panel a second time"


def test_a_slow_lookup_cannot_reload_the_report_mid_word(monkeypatch):
    """The contract's §8, pinned for the REPORT's bar as well as the
    builder's. The bar the reader now types into is a second form with the
    same two handlers on it, and the race is the same one: a lookup that
    answers slower than 4's 500 ms submit used to reload the page out from
    under a half-typed word. It cannot, because 5 stands 4 down when the
    request GOES OUT - and both pages inherit that from the one handler,
    which is keyed on the attributes both forms carry, not on a page."""
    _no_connection(monkeypatch)
    client, _, _ = _client(roles="analyst")
    bar = _selector_bar(client.get("/cohort/report", params={"term": "diabetes"}).text)
    builder = client.get("/cohort").text
    # Both forms carry the attribute, and both boxes are INSIDE their form -
    # a box outside it would never reach the handler that stands down.
    assert 'class="cb-form" data-autoapply>' in builder
    assert "data-suggest=" in bar and "data-suggest=" in builder
    js = (ROOT / "core" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    autoapply = js.split("form[data-autoapply]", 1)[1].split("5. The condition box", 1)[0]
    suggest = js.split("input[data-suggest]", 1)[1]
    # One handler for every form[data-autoapply] and every input
    # [data-suggest] on the page - never a selector naming a screen.
    assert "querySelectorAll('form[data-autoapply]')" in js
    assert "querySelectorAll('input[data-suggest]')" in js
    for page_only in ("cb-form", "cr-selectors", "/cohort/report"):
        assert page_only not in js, f"app.js singles out {page_only}"
    # The stand-down is at REQUEST time: pending() runs before the fetch,
    # and it both raises the flag 4 reads on the next keystroke and cancels
    # the submit 4 has already scheduled.
    assert "function pending()" in suggest
    assert suggest.index("pending();") < suggest.index("fetch(endpoint")
    assert "form.setAttribute('data-suggest-open', '1')" in suggest
    assert "tell('phi-suggest-open', false)" in suggest
    assert "phi-suggest-open', function () { clearTimeout(timer); }" in autoapply
    assert "form.getAttribute('data-suggest-open') === '1') { clearTimeout(timer); return; }" in autoapply
    # Nothing to suggest hands the typing back (so an unknown term is still
    # answered, with the page's own sentence); Escape and a pick do not.
    assert "detail.resume" in autoapply and "later(500)" in autoapply
    assert "close(true)" in suggest and "close(false)" in suggest
    # ... and a lookup that never answers is not allowed to hold the typing.
    assert "watchdog" in suggest and "}, 4000);" in suggest


def test_app_js_suggests_known_conditions_and_stands_down_while_the_list_is_open():
    js = (ROOT / "core" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    autoapply = js.split("form[data-autoapply]", 1)[1].split("5. The condition box", 1)[0]
    suggest = js.split("input[data-suggest]", 1)[1]
    # 150 ms, two characters, the previous request aborted.
    assert "}, 150);" in suggest and "value.length < 2" in suggest
    assert "new AbortController()" in suggest and "controller.abort()" in suggest
    # The keys, and the active row named to a screen reader.
    for key in ("'ArrowDown'", "'ArrowUp'", "'Enter'", "'Escape'"):
        assert key in suggest, key
    assert "aria-activedescendant" in suggest and "aria-expanded" in suggest
    assert "[role=option]" in suggest and "row.addEventListener('click'" in suggest
    # A pick sets the condition EXACTLY and counts at once; typing un-picks.
    assert "data-suggest-exact" in suggest and "requestSubmit" in suggest
    assert "function unpick()" in suggest
    # The two handlers are reconciled: while the panel is open the 500 ms
    # submit-on-typing stands down, and a close hands the typing back.
    assert "data-suggest-open" in autoapply and "phi-suggest-open" in autoapply
    assert "phi-suggest-close" in autoapply and "detail.resume" in autoapply
    assert "form.setAttribute('data-suggest-open', '1')" in suggest
    assert "tell('phi-suggest-open'" in suggest and "tell('phi-suggest-close'" in suggest
    # 4 stands down when the LOOKUP GOES OUT, not when the panel finally
    # opens: on a slow answer its 500 ms submit would otherwise land first
    # and take the list away as it arrived (seen in the browser).
    assert "function pending()" in suggest
    assert suggest.index("pending();") < suggest.index("fetch(endpoint")
    # ... and a lookup that never answers hands the typing back rather than
    # suspending it for good.
    assert "watchdog" in suggest and "}, 4000);" in suggest
    # ... and 4's own behaviour is untouched otherwise.
    assert "later(500)" in autoapply and "later(120)" in autoapply
# Made by Ryan Gomez & Co. Inc.
