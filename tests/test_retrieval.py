# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The clinical retrieval index: extraction rules, ETL idempotency, search
SQL, and disposal.

The extraction tests are pointed at the RULES retrieval_text.py's
docstring states - what the index must and must not hold - rather than
at the code, because those rules are the security boundary someone will
one day need to verify: the index must never become a second identity
store, never hold identifiers or storage-key material in its content,
and never index psychotherapy text by accident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db import retrieval, retrieval_etl, retrieval_purge, retrieval_text  # noqa: E402


# ---------------------------------------------------------------------------
# Extraction rules
# ---------------------------------------------------------------------------

CONDITION = {
    "resourceType": "Condition",
    "id": "cond-1",
    "subject": {"reference": "Patient/eAB12"},
    "code": {
        "coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                    "display": "Type 2 diabetes mellitus"}],
        "text": "Type 2 diabetes",
    },
    "note": [{"text": "Patient reports insulin pump failure overnight."}],
    "recordedDate": "2024-03-07T10:00:00Z",
    "text": {"status": "generated",
             "div": "<div xmlns='...'>Type 2 <b>diabetes</b> mellitus, well controlled</div>"},
}


def test_prose_is_extracted_and_html_is_stripped():
    text = retrieval_text.extract_text(CONDITION)
    assert "Type 2 diabetes mellitus" in text
    assert "insulin pump failure" in text
    assert "<b>" not in text and "<div" not in text


def test_identifiers_references_and_systems_are_never_indexed():
    text = retrieval_text.extract_text(CONDITION)
    assert "Patient/eAB12" not in text, "references are keys, not clinical language"
    assert "snomed.info" not in text, "coding systems are URLs, not prose"
    assert "44054006" not in text or "44054006" in CONDITION["code"]["text"], (
        "bare codes are not prose"
    )


def test_a_patient_resource_contributes_no_names_or_contact_details():
    """The not-a-second-identity-index rule, held structurally."""
    patient = {
        "resourceType": "Patient",
        "id": "eAB12",
        "name": [{"family": "Smith", "given": ["Mary"], "text": "Mary Smith"}],
        "telecom": [{"system": "phone", "value": "555-0100"}],
        "address": [{"city": "Example City", "line": ["1 Main St"]}],
        "identifier": [{"system": "urn:oid:1.2.3", "value": "MRN-778899"}],
    }
    text = retrieval_text.extract_text(patient)
    assert "Smith" not in text
    assert "555-0100" not in text
    assert "Main St" not in text
    assert "MRN-778899" not in text


def test_attachment_base64_is_not_indexed():
    doc = {
        "resourceType": "DocumentReference",
        "id": "doc-1",
        "description": "Discharge summary, scanned",
        "content": [{"attachment": {"contentType": "application/pdf",
                                    "data": "JVBERi0xLjQKJcOkw7zDtsOf",
                                    "title": "discharge-summary.pdf"}}],
    }
    text = retrieval_text.extract_text(doc)
    assert "JVBERi0" not in text
    assert "Discharge summary, scanned" in text
    assert "discharge-summary.pdf" in text


def test_an_empty_resource_yields_no_row():
    assert retrieval_text.resource_row({"resourceType": "Patient", "id": "x",
                                        "name": [{"family": "Q"}]},
                                       "fhir/Patient/x.json") is None


def test_row_carries_linkage_and_clinical_date():
    row = retrieval_text.resource_row(CONDITION, "fhir/Condition/cond-1.json")
    assert row["patient_reference"] == "Patient/eAB12"
    assert row["resource_type"] == "Condition"
    assert row["resource_id"] == "cond-1"
    assert str(row["clinical_date"]) == "2024-03-07"
    assert row["storage_key"] == "fhir/Condition/cond-1.json"


def test_content_is_capped():
    huge = dict(CONDITION, note=[{"text": "x" * 100_000}])
    row = retrieval_text.resource_row(huge, "k")
    assert len(row["content"]) <= retrieval_text.MAX_CONTENT_CHARS


# ---------------------------------------------------------------------------
# ETL: delete-then-insert idempotency, against a fake connection
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self.description = None
        self._rows: list = []

    def execute(self, sql, params=()):
        self.conn.statements.append((" ".join(sql.split()), tuple(params)))
        sql_flat = " ".join(sql.split())
        if sql_flat.startswith("DELETE"):
            key = params[0]
            before = len(self.conn.rows)
            self.conn.rows = [r for r in self.conn.rows if r[0] != key]
            self.rowcount = before - len(self.conn.rows)
        elif sql_flat.startswith("INSERT"):
            self.conn.rows.append(tuple(params))
        elif "source_digest" in sql_flat and sql_flat.startswith("SELECT"):
            key = params[0]
            match = [r for r in self.conn.rows if r[0] == key]
            self._rows = [(match[0][7],)] if match else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.rows: list = []
        self.statements: list = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_reindexing_an_object_replaces_its_rows_rather_than_appending():
    conn = FakeConn()
    key = "fhir/Condition/cond-1.json"
    n = retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE, key,
                                   [CONDITION], source_digest="d1")
    assert n == 1 and len(conn.rows) == 1
    # Re-ingest: the corrected object now holds a shorter bundle.
    n = retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE, key,
                                   [CONDITION], source_digest="d2")
    assert n == 1 and len(conn.rows) == 1, "delete-then-insert, never append"


def test_unknown_table_names_are_refused():
    """The interpolation guard - a caller-supplied table name is how an
    injection is born, so only the two known spellings pass."""
    with pytest.raises(ValueError):
        retrieval_etl.index_object(FakeConn(), "retrieval.other; DROP", "k", [])
    with pytest.raises(ValueError):
        retrieval_etl.stored_digest(FakeConn(), "public.stored_resources", "k")


def test_unchanged_objects_are_skipped_by_digest():
    conn = FakeConn()
    key = "fhir/Condition/cond-1.json"
    retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE, key,
                               [CONDITION], source_digest="digest-abc")
    assert retrieval_etl.stored_digest(conn, retrieval_etl.CLINICAL_TABLE, key) == "digest-abc"


# ---------------------------------------------------------------------------
# Search SQL shape
# ---------------------------------------------------------------------------

class RecordingCursor:
    description = [("storage_key",), ("snippet",)]

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params):
        self.conn.executed.append((" ".join(sql.split()), list(params)))

    def fetchall(self):
        return []

    def close(self):
        pass


class RecordingConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return RecordingCursor(self)


def test_search_is_parameterised_websearch_with_a_capped_limit():
    conn = RecordingConn()
    retrieval.search_clinical(conn, 'insulin "pump failure" -pediatric', limit=999)
    sql, params = conn.executed[0]
    assert "websearch_to_tsquery" in sql
    assert "retrieval.clinical_text" in sql
    assert 'insulin "pump failure" -pediatric' in params, "query travels as a parameter"
    assert params[-1] == retrieval.MAX_LIMIT, "limit is clamped server-side"


def test_search_can_scope_to_patient_and_type():
    conn = RecordingConn()
    retrieval.search_clinical(conn, "sepsis", patient_reference="Patient/e1",
                              resource_type="Condition", limit=5)
    sql, params = conn.executed[0]
    assert "patient_reference = %s" in sql and "resource_type = %s" in sql
    assert "Patient/e1" in params and "Condition" in params


def test_psychotherapy_search_targets_only_its_own_table():
    conn = RecordingConn()
    retrieval.search_psychotherapy(conn, "nightmares")
    sql, _ = conn.executed[0]
    assert "retrieval.psychotherapy_text" in sql
    assert "clinical_text" not in sql


# ---------------------------------------------------------------------------
# Disposal
# ---------------------------------------------------------------------------

def test_disposal_deletes_by_storage_key_and_commits():
    conn = FakeConn()
    key = "fhir/Condition/cond-1.json"
    retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE, key, [CONDITION])
    commits_before = conn.commits
    retrieval_purge.delete_clinical(conn, key)
    assert conn.rows == []
    assert conn.commits == commits_before + 1


def test_dispose_one_deletes_retrieval_rows_before_the_index_row():
    """core/fhir/purge.py's ordering: OMOP, then retrieval, then index,
    then storage - verified by the calls' order, since under-deleting on
    failure (rows survive, object survives) is the documented safe
    direction and this order is what produces it."""
    import core.fhir.purge as purge

    calls = []

    class Storage:
        def delete_all_versions(self, key):
            calls.append(("storage", key))
            return 1

    class Conn:
        def cursor(self):
            raise AssertionError("dispose must go through the purge helpers")

    import core.db.retrieval_purge as rp
    import core.db.index as index_mod

    orig_rp, orig_idx = rp.delete_clinical, index_mod.delete_index_entry
    rp.delete_clinical = lambda conn, key: calls.append(("retrieval", key))
    index_mod.delete_index_entry = lambda conn, key: calls.append(("index", key))
    try:
        purge._dispose_one("fhir/Condition/c.json", Storage(), Conn(),
                           omop_enabled=False, retrieval_enabled=True)
    finally:
        rp.delete_clinical, index_mod.delete_index_entry = orig_rp, orig_idx

    assert calls == [
        ("retrieval", "fhir/Condition/c.json"),
        ("index", "fhir/Condition/c.json"),
        ("storage", "fhir/Condition/c.json"),
    ]
# Made by Ryan Gomez & Co. Inc.
