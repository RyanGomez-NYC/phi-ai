# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Live-Postgres verification of the retrieval, telemetry, users and
disposal layers - the semantics fakes cannot exercise.

SKIPPED unless PHI_AI_TEST_PG_DSN names a reachable PostgreSQL superuser
connection, e.g.:

    PHI_AI_TEST_PG_DSN="host=127.0.0.1 port=5432 user=postgres" \
        python -m pytest tests/test_live_postgres.py -v

WHY THIS FILE EXISTS. The unit suite fakes every connection, which is
what lets it run without infrastructure - and is exactly why generated
tsvector columns, websearch_to_tsquery parsing, ts_headline, GIN index
use, CHECK constraints, and role-grant separation were unverified until
run against a real server (first verified 2026-08-21 on PostgreSQL 17.11
during the AI-features build; this file makes that a standing check
rather than a session note). The psychotherapy-table separation in
particular is a POSTGRES guarantee, not an application one - a test
that fakes the connection is structurally unable to test it.

The fixture builds a THROWAWAY database per run, applies the real
schema files verbatim, and applies the real AWS bootstrap files with
only their cluster-level lines filtered (CREATE ROLE / GRANT rds_iam -
roles are cluster-wide and may already exist; the roles themselves are
ensured separately). Grants are per-database, so they are re-applied
from the real files each run - the test cannot drift from the SQL it
verifies. The database is dropped afterwards; the phi_ai_* roles remain
on the cluster, which matches how the bootstraps document themselves
(run once per cluster).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("PHI_AI_TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="PHI_AI_TEST_PG_DSN not set - live Postgres verification skipped"
)

ROLES = (
    "phi_ai_ingest", "phi_ai_reader", "phi_ai_disposition",
    "phi_ai_retrieval_etl", "phi_ai_retrieval_search", "phi_ai_retrieval_psych",
    "phi_ai_assistant_ops",
)

SCHEMA_FILES = (
    "core/db/schema.sql",
    "core/db/users_schema.sql",
    "core/db/retrieval_schema.sql",
    "core/db/telemetry_schema.sql",
)
BOOTSTRAP_FILES = (
    "core/db/bootstrap_aws.sql",
    "core/db/retrieval_bootstrap_aws.sql",
    "core/db/telemetry_bootstrap_aws.sql",
)

CONDITION = {
    "resourceType": "Condition", "id": "c1",
    "subject": {"reference": "Patient/eAB12"},
    "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                         "display": "Type 2 diabetes mellitus"}],
             "text": "Type 2 diabetes"},
    "note": [{"text": "Patient reports insulin pump failure overnight."}],
    "recordedDate": "2024-03-07T10:00:00Z",
}
OTHER = {
    "resourceType": "Observation", "id": "o1",
    "subject": {"reference": "Patient/eZZ99"},
    "code": {"text": "Blood pressure"},
    "note": [{"text": "Pediatric follow-up, pump functioning normally."}],
    "effectiveDateTime": "2024-05-01T10:00:00Z",
}


def _bootstrap_sql(path: Path) -> str:
    """The real bootstrap file minus its cluster-level role creation.

    Roles are cluster-wide and the files document themselves as run-once
    per cluster; everything else in them (GRANTs, DO blocks, sequence
    grants) is per-database and idempotent enough to re-apply. Filtering
    rather than duplicating keeps this test pointed at the shipped SQL.
    """
    kept = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("CREATE ROLE ")
        and not line.startswith("GRANT rds_iam")
        and not line.startswith("\\")  # psql meta-commands (\set ...)
    ]
    return "\n".join(kept)


@pytest.fixture(scope="module")
def db():
    import psycopg

    dbname = f"phi_live_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        for role in ROLES:
            admin.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %s) "
                "THEN EXECUTE format('CREATE ROLE %%I', %s); END IF; END $$;"
                % ("'" + role + "'", "'" + role + "'")
            )

        conn = psycopg.connect(DSN, dbname=dbname)
        with conn:
            for f in SCHEMA_FILES:
                conn.execute((ROOT / f).read_text(encoding="utf-8"))
            for f in BOOTSTRAP_FILES:
                conn.execute(_bootstrap_sql(ROOT / f))
        conn.close()

        def connect():
            return psycopg.connect(DSN, dbname=dbname)

        yield connect
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (dbname,),
        )
        admin.execute(f'DROP DATABASE "{dbname}"')
        admin.close()


# ---------------------------------------------------------------------------
# users role constraint
# ---------------------------------------------------------------------------

def test_new_roles_grantable_and_bogus_roles_refused(db):
    import psycopg

    with db() as conn:
        conn.execute("INSERT INTO authn.local_users (username, password_hash, created_by) "
                     "VALUES ('t.researcher', 'x', 'test')")
        conn.execute("INSERT INTO authn.local_user_roles (username, role, granted_by) "
                     "VALUES ('t.researcher', 'researcher', 'test'), "
                     "       ('t.researcher', 'psychotherapy', 'test')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO authn.local_user_roles (username, role, granted_by) "
                         "VALUES ('t.researcher', 'superuser', 'test')")


# ---------------------------------------------------------------------------
# retrieval: ETL, generated columns, websearch semantics
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded(db):
    from core.db import retrieval_etl

    with db() as conn:
        retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE,
                                   "fhir/Condition/c1.json", [CONDITION],
                                   source_digest="d1")
        retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE,
                                   "fhir/Observation/o1.json", [OTHER],
                                   source_digest="d2")
        retrieval_etl.index_object(conn, retrieval_etl.PSYCHOTHERAPY_TABLE,
                                   "notes/DocumentReference/n1.json",
                                   [{"resourceType": "DocumentReference", "id": "n1",
                                     "subject": {"reference": "Patient/eAB12"},
                                     "description": "Session note: recurring nightmares discussed."}],
                                   source_digest="p1")
    return db


def test_etl_rows_generate_tsvector_and_linkage(seeded):
    with seeded() as conn:
        row = conn.execute(
            "SELECT content, content_tsv IS NOT NULL, clinical_date::text, patient_reference "
            "FROM retrieval.clinical_text WHERE storage_key = 'fhir/Condition/c1.json'"
        ).fetchone()
    assert "insulin pump failure" in row[0]
    assert row[1] is True
    assert row[2] == "2024-03-07"
    assert row[3] == "Patient/eAB12"


def test_reindex_replaces_and_digest_reads_back(seeded):
    from core.db import retrieval_etl

    with seeded() as conn:
        retrieval_etl.index_object(conn, retrieval_etl.CLINICAL_TABLE,
                                   "fhir/Condition/c1.json", [CONDITION],
                                   source_digest="d1b")
        count = conn.execute("SELECT count(*) FROM retrieval.clinical_text "
                             "WHERE storage_key = 'fhir/Condition/c1.json'").fetchone()[0]
        assert count == 1
        assert retrieval_etl.stored_digest(
            conn, retrieval_etl.CLINICAL_TABLE, "fhir/Condition/c1.json") == "d1b"


def test_websearch_phrase_exclusion_and_scoping(seeded):
    from core.db import retrieval

    with seeded() as conn:
        phrase = retrieval.search_clinical(conn, '"pump failure"')
        assert [r["storage_key"] for r in phrase] == ["fhir/Condition/c1.json"]
        assert "<b>" in phrase[0]["snippet"], "ts_headline should mark the match"

        excluded = retrieval.search_clinical(conn, "pump -pediatric")
        assert [r["storage_key"] for r in excluded] == ["fhir/Condition/c1.json"]

        scoped = retrieval.search_clinical(conn, "pump",
                                           patient_reference="Patient/eZZ99")
        assert [r["storage_key"] for r in scoped] == ["fhir/Observation/o1.json"]


def test_psychotherapy_text_never_appears_in_the_general_index(seeded):
    from core.db import retrieval

    with seeded() as conn:
        assert retrieval.search_psychotherapy(conn, "nightmares")
        assert not retrieval.search_clinical(conn, "nightmares")


def test_the_gin_index_serves_the_search(seeded):
    with seeded() as conn:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0] for r in conn.execute(
                "EXPLAIN (COSTS OFF) SELECT storage_key FROM retrieval.clinical_text "
                "WHERE content_tsv @@ websearch_to_tsquery('english', 'insulin')"
            ).fetchall()
        )
    assert "idx_retrieval_clinical_tsv" in plan


# ---------------------------------------------------------------------------
# grant separation - the guarantees that are Postgres's, not the app's
# ---------------------------------------------------------------------------

def _as_role(db, role, sql):
    import psycopg

    with db() as conn:
        conn.execute(f"SET ROLE {role}")
        try:
            conn.execute(sql)
            return True
        except psycopg.errors.InsufficientPrivilege:
            return False


@pytest.mark.parametrize("role,sql,allowed", [
    ("phi_ai_retrieval_search", "SELECT count(*) FROM retrieval.clinical_text", True),
    ("phi_ai_retrieval_search", "SELECT count(*) FROM retrieval.psychotherapy_text", False),
    ("phi_ai_retrieval_search",
     "INSERT INTO retrieval.clinical_text (storage_key, resource_index, resource_type, content) "
     "VALUES ('x', 0, 'T', 'c')", False),
    ("phi_ai_retrieval_psych", "SELECT count(*) FROM retrieval.psychotherapy_text", True),
    ("phi_ai_retrieval_psych", "SELECT count(*) FROM retrieval.clinical_text", False),
    ("phi_ai_disposition",
     "DELETE FROM retrieval.clinical_text WHERE storage_key = 'no-such-key'", True),
    ("phi_ai_disposition", "SELECT content FROM retrieval.clinical_text", False),
    ("phi_ai_assistant_ops",
     "INSERT INTO aiops.assistant_interactions (username, provider, model) "
     "VALUES ('t', 'p', 'm')", True),
    ("phi_ai_assistant_ops",
     "DELETE FROM aiops.assistant_interactions WHERE username = 't'", False),
])
def test_role_grants(seeded, role, sql, allowed):
    assert _as_role(seeded, role, sql) is allowed


# ---------------------------------------------------------------------------
# disposal through the real purge path
# ---------------------------------------------------------------------------

def test_dispose_one_removes_retrieval_and_index_rows(seeded):
    import core.fhir.purge as purge

    class Storage:
        def delete_all_versions(self, key):
            return 2

    with seeded() as conn:
        conn.execute(
            "INSERT INTO stored_resources (resource_type, resource_id, patient_reference, "
            " storage_key, storage_version_id, sha256_hex, stored_at, retention_until) "
            "VALUES ('Condition', 'c1', 'Patient/eAB12', 'fhir/Condition/c1.json', "
            " 'v1', repeat('a', 64), now(), now() + interval '10 years')")
        conn.commit()
        purge._dispose_one("fhir/Condition/c1.json", Storage(), conn,
                           omop_enabled=False, retrieval_enabled=True)
        left = conn.execute(
            "SELECT (SELECT count(*) FROM retrieval.clinical_text "
            "        WHERE storage_key = 'fhir/Condition/c1.json'), "
            "       (SELECT count(*) FROM stored_resources "
            "        WHERE storage_key = 'fhir/Condition/c1.json')").fetchone()
    assert left == (0, 0)


# ---------------------------------------------------------------------------
# telemetry and drift on real SQL
# ---------------------------------------------------------------------------

def test_telemetry_roundtrip_and_summaries(seeded):
    import psycopg

    from core.assistant import drift, telemetry

    assert telemetry.record_interaction(
        seeded, username="dr.chen", roles="researcher", provider="bedrock",
        model="m1", latency_ms=1200, input_tokens=900, output_tokens=250,
        tool_calls=2, tools_used="search_documentation,search_clinical_records",
        phi_reads=1)
    assert telemetry.record_interaction(
        seeded, username="aud.rey", roles="auditor", provider="bedrock",
        model="m1", latency_ms=300, refused=True)
    drift.record_results(
        seeded,
        [drift.ProbeResult(probe=drift.Probe(name="retention-probe", question="q"),
                           passed=False, failures=["cited none"], latency_ms=800)],
        provider="bedrock", model="m1")

    with seeded() as conn:
        summary = telemetry.usage_summary(conn, days=30)
        totals = summary["totals"]
        assert totals["questions"] >= 2 and totals["refusals"] >= 1
        assert totals["p95_latency_ms"] >= totals["p50_latency_ms"]
        assert summary["by_day"] and summary["by_role"] and summary["by_model"]

        runs = telemetry.drift_summary(conn)
        assert runs and runs[0]["passed"] == 0
        assert "retention-probe" in (runs[0]["failed_probes"] or "")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO aiops.assistant_interactions "
                         "(kind, username, provider, model) VALUES ('sneaky', 'x', 'p', 'm')")
# Made by Ryan Gomez & Co. Inc.
