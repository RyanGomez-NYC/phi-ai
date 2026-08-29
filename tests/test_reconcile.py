# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/db/reconcile.py.

build_report() and print_cleanup_sql() are pure functions of their
inputs (a storage object and a connection, or a tuple of keys), so
they're tested directly against lightweight fakes - no real S3 or
Postgres needed. main() itself (argument parsing, the actual AWS/DB
connections) is intentionally not exercised here; that's an integration
concern better suited to a real dev stack, not a unit test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.reconcile import build_report, print_cleanup_sql, _sql_literal  # noqa: E402


class _FakeStorage:
    def __init__(self, keys):
        self._keys = keys

    def list_keys(self, prefix=""):
        return [k for k in self._keys if k.startswith(prefix)]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        pass

    def fetchall(self):
        return [(r,) for r in self._rows]

    def close(self):
        """Required because core/db/index.py's all_storage_keys() opens
        the cursor directly rather than via `with`, and closes it in a
        finally. Omitting this here made every build_report test fail
        with AttributeError on correct production code - the fake was
        missing a method, not the code under test.

        Records the call rather than being a no-op so the release is
        assertable: reconciliation runs against a long-lived connection,
        so a leaked cursor per call is a real (if slow) problem."""
        self.closed = True


class _FakeConnection:
    def __init__(self, keys):
        self._cursor = _FakeCursor(keys)

    def cursor(self):
        return self._cursor


# ---------------------------------------------------------------------------
# build_report()
# ---------------------------------------------------------------------------

def test_perfectly_synced_state_reports_nothing():
    storage = _FakeStorage(["fhir/Patient/p1.json", "fhir/Observation/o1.json"])
    conn = _FakeConnection(["fhir/Patient/p1.json", "fhir/Observation/o1.json"])

    report = build_report(storage, conn)

    assert report.in_sync is True
    assert report.orphaned_index_rows == ()
    assert report.missing_index_rows == ()
    assert report.total_storage_objects == 2
    assert report.total_index_rows == 2
    assert conn._cursor.closed is True, "cursor must be released after the index read"


def test_orphaned_rows_detected_correctly():
    """Index rows with no corresponding S3 object - the unexpected kind
    of drift, per the module's own docstring."""
    storage = _FakeStorage(["fhir/Patient/p1.json"])
    conn = _FakeConnection(["fhir/Patient/p1.json", "fhir/Condition/orphan1.json", "fhir/Condition/orphan2.json"])

    report = build_report(storage, conn)

    assert set(report.orphaned_index_rows) == {"fhir/Condition/orphan1.json", "fhir/Condition/orphan2.json"}
    assert report.missing_index_rows == ()
    assert report.in_sync is False


def test_missing_rows_detected_correctly():
    """S3 objects with no corresponding index row - the expected,
    self-healing kind of drift."""
    storage = _FakeStorage(["fhir/Patient/p1.json", "fhir/Patient/p2.json", "fhir/Observation/o1.json"])
    conn = _FakeConnection(["fhir/Patient/p1.json"])

    report = build_report(storage, conn)

    assert set(report.missing_index_rows) == {"fhir/Patient/p2.json", "fhir/Observation/o1.json"}
    assert report.orphaned_index_rows == ()
    assert report.in_sync is False


def test_both_kinds_of_drift_can_coexist():
    storage = _FakeStorage(["fhir/Patient/p1.json", "fhir/Patient/p2.json"])
    conn = _FakeConnection(["fhir/Patient/p1.json", "fhir/Condition/orphan.json"])

    report = build_report(storage, conn)

    assert report.orphaned_index_rows == ("fhir/Condition/orphan.json",)
    assert report.missing_index_rows == ("fhir/Patient/p2.json",)
    assert report.in_sync is False


def test_results_are_sorted():
    """Deterministic output matters for readable diffs in logs/output
    across repeated runs, not just correctness of the set itself."""
    storage = _FakeStorage([])
    conn = _FakeConnection(["fhir/Z/z.json", "fhir/A/a.json", "fhir/M/m.json"])

    report = build_report(storage, conn)

    assert report.orphaned_index_rows == ("fhir/A/a.json", "fhir/M/m.json", "fhir/Z/z.json")


# ---------------------------------------------------------------------------
# print_cleanup_sql() - the manual-cleanup SQL generator
# ---------------------------------------------------------------------------

def test_sql_literal_escapes_single_quotes_correctly():
    """Standard SQL escaping: a literal single quote becomes two single
    quotes in a row, not a backslash escape - Postgres does not treat
    backslash as an escape character by default."""
    assert _sql_literal("fhir/Patient/p1.json") == "'fhir/Patient/p1.json'"
    assert _sql_literal("o'brien") == "'o''brien'"


def test_print_cleanup_sql_produces_no_output_for_empty_input(capsys):
    print_cleanup_sql(())
    captured = capsys.readouterr()
    assert captured.out == ""


def test_print_cleanup_sql_contains_exactly_one_select_and_one_delete(capsys):
    print_cleanup_sql(("fhir/Condition/orphan1.json", "fhir/Condition/orphan2.json"))
    output = capsys.readouterr().out

    assert output.count("SELECT id, resource_type") == 1
    assert output.count("DELETE FROM stored_resources") == 1
    assert "'fhir/Condition/orphan1.json'" in output
    assert "'fhir/Condition/orphan2.json'" in output
    assert "RUNBOOK_INDEX_MAINTENANCE.md" in output


def test_print_cleanup_sql_correctly_escapes_a_key_with_a_quote(capsys):
    print_cleanup_sql(("fhir/DocumentReference/o'brien-note.json",))
    output = capsys.readouterr().out

    # The escaped form (doubled quote) appears twice - once in the
    # SELECT, once in the DELETE. What matters is that it's ALWAYS the
    # escaped form, never the bare unescaped one, anywhere in the output.
    assert output.count("o''brien") == 2
    assert "o'brien-note" not in output.replace("o''brien-note", "")


def _run_all():
    import io
    from contextlib import redirect_stdout

    class _FakeCapsys:
        """Minimal stand-in for pytest's capsys fixture, so the
        print_cleanup_sql tests above can also run standalone via
        `python3 tests/test_reconcile.py`, not just under pytest."""

        def __init__(self):
            self._buf = io.StringIO()
            self._redirect = redirect_stdout(self._buf)

        def __enter__(self):
            self._redirect.__enter__()
            return self

        def __exit__(self, *args):
            self._redirect.__exit__(*args)

        def readouterr(self):
            class _Result:
                out = self._buf.getvalue()
            self._buf.truncate(0)
            self._buf.seek(0)
            return _Result()

    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in fns:
        if "capsys" in fn.__code__.co_varnames:
            with _FakeCapsys() as capsys:
                fn(capsys)
        else:
            fn()
        print(f"  PASS  {name}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
