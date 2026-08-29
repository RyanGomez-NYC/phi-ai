# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/db/index.py.

extract_patient_reference is tested most carefully here: it's the one
function in the index layer that pulls a value out of clinical resource
content, and getting it wrong in either direction is bad - too loose and
something PHI-adjacent leaks into the index; too strict and the index
silently stops being useful for records requests. write_index_entry and
the query functions are tested against a minimal fake cursor, since a
real Postgres instance isn't available in this environment - see
runbooks/RUNBOOK_AWS_SETUP.md for the live-database verification these
tests don't replace.

FOUND AND FIXED (2026-08-17 audit, H8): this suite did not construct
against current code at all - IndexEntry(..., s3_key=..., ...) raised
TypeError immediately, since the dataclass's field has been named
storage_key since before this test was last touched (see
core/db/index.py's own header on the multi-cloud generalization away
from an AWS-specific name). That means `python -m pytest tests/` (or
any equivalent runner) failed this file outright, not just one
assertion inside it - a shipped test suite that cannot even construct
its own fixtures. Three separate defects were found here, all fixed
together since they're all on the same execution path:

  1. IndexEntry(s3_key=...) -> TypeError: unexpected keyword argument.
     Fixed by using the real field name, storage_key.
  2. _FakeCursor had no close() method, but write_index_entry() closes
     its cursor in a finally block (core/db/index.py's own docstring
     explains why: DB-API 2.0 doesn't promise cursors are context
     managers, and this project supports two drivers - psycopg on
     AWS/Azure, pg8000 on GCP - that must be treated identically here).
     Even with defect 1 fixed, this test would still fail with
     AttributeError: '_FakeCursor' object has no attribute 'close'.
     Fixed by adding a no-op close() to _FakeCursor.
  3. The test asserted
     "ON CONFLICT (resource_type, resource_id) DO NOTHING" in query -
     a design write_index_entry() explicitly does NOT use, and says so
     at length in its own docstring: ON CONFLICT requires SELECT
     privilege in addition to INSERT, which the ingest role deliberately
     does not hold (see that docstring's full minimum-necessary
     reasoning). The actual, current design is a plain INSERT with
     unique-violation classified after the fact via
     core/db/pg_errors.py's is_unique_violation() - this test was
     asserting a design that was deliberately abandoned, not verifying
     the design actually shipped. Fixed by asserting the real INSERT
     shape and, in a new test, that a unique-violation exception is
     correctly absorbed as an idempotent no-op rather than propagated -
     the actual duplicate-handling behavior this suite's own module
     docstring claims to cover but, until this fix, never verified.

Each of these three would have surfaced as a real failure (not a false
pass) if `_run_all()` below or any pytest invocation had ever actually
been run against current code - this is worth stating plainly per this
project's own documentation discipline: "shipped but never actually
run" is a materially different, and worse, gap than "written
incorrectly and caught the first time it ran."
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.index import IndexEntry, extract_patient_reference, write_index_entry  # noqa: E402


def test_patient_resource_self_references():
    resource = {"resourceType": "Patient", "id": "eAB12cd3"}
    assert extract_patient_reference(resource) == "Patient/eAB12cd3"


def test_patient_resource_without_id_returns_none():
    """A malformed Patient resource (no id) must not raise or fabricate
    a reference - absence of data is the correct, safe outcome."""
    assert extract_patient_reference({"resourceType": "Patient"}) is None


def test_observation_subject_reference():
    resource = {"resourceType": "Observation", "subject": {"reference": "Patient/eAB12cd3"}}
    assert extract_patient_reference(resource) == "Patient/eAB12cd3"


def test_encounter_patient_field():
    resource = {"resourceType": "Encounter", "patient": {"reference": "Patient/eXYz999"}}
    assert extract_patient_reference(resource) == "Patient/eXYz999"


def test_non_patient_subject_is_not_misfiled():
    """An Observation whose subject is a Group, Device, or Location must
    not be indexed as if it were patient-linked - a wrong-but-plausible
    reference is worse than no reference at all."""
    resource = {"resourceType": "Observation", "subject": {"reference": "Group/some-cohort"}}
    assert extract_patient_reference(resource) is None


def test_resource_with_no_patient_linkage():
    assert extract_patient_reference({"resourceType": "DocumentReference"}) is None


def test_missing_resource_type_does_not_raise():
    assert extract_patient_reference({}) is None


class _FakeCursor:
    """Minimal stand-in for a psycopg/pg8000 cursor - records the query
    and params it was called with, and returns canned rows for SELECTs.

    close() is a no-op, not an omission (H8 fix): write_index_entry()
    and every other write function in core/db/index.py explicitly close
    their cursor in a finally block rather than relying on
    `with conn.cursor()` - see that module's own docstring for why
    (DB-API 2.0 does not promise cursors are context managers, and this
    project's two drivers, psycopg and pg8000, must be treated
    identically here). A fake that doesn't implement close() doesn't
    exercise that code path faithfully - it just happens not to fail
    until something actually calls it, which is exactly what happened
    here before this fix.
    """

    def __init__(self, rows=None, raise_on_execute=None):
        self.executed = []
        self.description = [("resource_type",), ("resource_id",), ("storage_key",)]
        self._rows = rows or []
        self._raise_on_execute = raise_on_execute
        self.closed = False

    def execute(self, query, params):
        self.executed.append((query, params))
        if self._raise_on_execute is not None:
            raise self._raise_on_execute

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConnection:
    def __init__(self, rows=None, raise_on_execute=None):
        self._cursor = _FakeCursor(rows=rows, raise_on_execute=raise_on_execute)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _make_entry() -> IndexEntry:
    return IndexEntry(
        resource_type="Patient",
        resource_id="eAB12cd3",
        storage_key="fhir/Patient/eAB12cd3.json",
        sha256_hex="a" * 64,
        patient_reference="Patient/eAB12cd3",
        retention_until=datetime(2036, 1, 1, tzinfo=timezone.utc),
    )


def test_write_index_entry_commits_a_plain_insert():
    """H8 fix: write_index_entry() deliberately does NOT use
    INSERT ... ON CONFLICT (see that function's own docstring - ON
    CONFLICT would require SELECT privilege the ingest role doesn't
    hold). This asserts the design actually shipped: a plain INSERT,
    with the exact column/param order write_index_entry() constructs,
    matching core/db/schema.sql's stored_resources columns."""
    conn = _FakeConnection()
    entry = _make_entry()
    write_index_entry(conn, entry)

    assert conn.committed is True
    query, params = conn._cursor.executed[0]
    assert "INSERT INTO stored_resources" in query
    assert "ON CONFLICT" not in query
    assert params == (
        "Patient",
        "eAB12cd3",
        "Patient/eAB12cd3",
        "fhir/Patient/eAB12cd3.json",
        None,
        "a" * 64,
        datetime(2036, 1, 1, tzinfo=timezone.utc),
        # resource_count: 1 for a per-resource write, N for a bundle
        # under the large profile. Verification reads this to count a
        # bundled object without decrypting anything.
        1,
    )


def test_write_index_entry_absorbs_duplicate_as_idempotent_noop():
    """Idempotency matters here specifically because the scheduler can
    legitimately retry a run - the write path must not raise on a
    resource that's already indexed. Since this design classifies a
    duplicate via core/db/pg_errors.py's is_unique_violation() rather
    than INSERT ... ON CONFLICT (see the module docstring's own
    reasoning), this simulates that classification path directly rather
    than a real driver exception class, matching how
    core/db/pg_errors.py's own docstring describes testing it -
    structurally, not by driver-specific type."""

    class _FakeUniqueViolationError(Exception):
        sqlstate = "23505"

    conn = _FakeConnection(raise_on_execute=_FakeUniqueViolationError("duplicate key"))
    entry = _make_entry()

    # is_unique_violation() reads .sqlstate structurally (see
    # core/db/pg_errors.py) - a plain Exception subclass with that
    # attribute set is enough to exercise the real classification path
    # without needing a real driver installed.
    write_index_entry(conn, entry)

    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn._cursor.closed is True


def test_unexpected_error_rolls_back_before_reraising():
    """The rollback matters as much as the re-raise.

    A failed statement leaves the transaction aborted, and
    core/fhir/scheduler.py holds ONE connection open for an entire run -
    so without the rollback, every subsequent write that run also fails,
    reporting Postgres's generic "current transaction is aborted" rather
    than the real underlying error. That was a real bug (6a17420), and
    nothing else in this file covers the non-duplicate failure path.

    Uses a non-23505 SQLSTATE specifically to exercise the other side of
    core/db/pg_errors.py's is_unique_violation() classification: this
    must NOT be absorbed as an idempotent retry."""

    class _FakeInsufficientPrivilegeError(Exception):
        sqlstate = "42501"  # insufficient_privilege, deliberately not 23505

    conn = _FakeConnection(raise_on_execute=_FakeInsufficientPrivilegeError("permission denied"))

    try:
        write_index_entry(conn, _make_entry())
    except _FakeInsufficientPrivilegeError:
        pass
    else:
        raise AssertionError("a non-duplicate error must propagate, not be absorbed")

    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn._cursor.closed is True


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
# Made by Ryan Gomez & Co. Inc.
