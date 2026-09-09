# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The deletion paths - code that removes records, tested at last.

WHY THIS FILE EXISTS. core/db/omop_purge.py and
core/fhir/psychotherapy_purge.py were never imported by any test. Between
them they delete rows from the OMOP warehouse and dispose of the record
class 45 CFR 164.508(a)(2) treats separately - psychotherapy notes, which
this platform keeps in their own bucket under their own key precisely
because they are the most consequential thing it holds.

Deletion code has an asymmetry no other code has: a bug in a read path
shows up as a wrong answer somebody can question, and a bug in a delete
path shows up as an absence nobody can. There is no reviewing the record
that is no longer there. That is an argument for testing these first, and
they were tested last.

WHAT IS ASSERTED HERE is the behaviour each module's own docstring
promises and nothing was checking: that a foreign-key violation fails
loud instead of being swallowed, that a delete which matched nothing
rolls back rather than committing an empty transaction, that the FK-safe
table order is actually followed, and that a malformed resource list is
refused by line number instead of being half-processed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.omop_purge import delete_by_source_storage_key  # noqa: E402
from core.fhir.psychotherapy_purge import _parse_resource_list  # noqa: E402


# ---------------------------------------------------------------------------
# A cursor that behaves the way DB-API says one does
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn, hits, raises=None):
        self.conn, self._hits, self._raises = conn, hits, raises
        self.rowcount = 0
        self.closed = False

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, params))
        if self._raises is not None:
            raise self._raises
        table = sql.split("cdm.", 1)[1].split(" ", 1)[0]
        self.rowcount = 1 if table in self._hits else 0

    def close(self):
        self.closed = True


class _Conn:
    """Records the SQL and the transaction outcome. Decides nothing."""

    def __init__(self, hits=(), raises=None):
        self.executed: list[tuple] = []
        self.commits = self.rollbacks = 0
        self._hits, self._raises = set(hits), raises
        self.cursors: list[_Cursor] = []

    def cursor(self):
        c = _Cursor(self, self._hits, self._raises)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# OMOP purge
# ---------------------------------------------------------------------------

def test_a_deleted_row_names_the_table_it_came_from_and_commits():
    conn = _Conn(hits={"condition_occurrence"})
    assert delete_by_source_storage_key(conn, "records/x.enc") == "condition_occurrence"
    assert conn.commits == 1 and conn.rollbacks == 0


def test_a_key_with_no_omop_row_is_a_normal_outcome_not_an_error():
    """Normal when OMOP is off, when the resource type is not OMOP-mapped
    (DocumentReference, AllergyIntolerance, ExplanationOfBenefit), or when
    the record predates OMOP being enabled."""
    conn = _Conn(hits=())
    assert delete_by_source_storage_key(conn, "records/x.enc") is None


def test_a_miss_rolls_back_rather_than_committing_an_empty_transaction():
    """An idle-in-transaction connection is how a pool is exhausted by a
    job that did nothing."""
    conn = _Conn(hits=())
    delete_by_source_storage_key(conn, "records/x.enc")
    assert conn.rollbacks == 1 and conn.commits == 0


def test_it_stops_at_the_first_table_that_had_a_row():
    """FK-safe order is only safe if it is also short-circuiting - the
    order exists so dependents go before the rows they depend on."""
    conn = _Conn(hits={"condition_occurrence", "person"})
    delete_by_source_storage_key(conn, "records/x.enc")

    tables = [sql.split("cdm.", 1)[1].split(" ", 1)[0] for sql, _ in conn.executed]
    assert "person" not in tables, (
        "it kept deleting after a hit; the FK-safe order means the FIRST "
        "match is the row that belongs to this storage key"
    )


def test_the_storage_key_is_bound_as_a_parameter_never_interpolated():
    """The table name is interpolated (it comes from a module constant);
    the storage key never is."""
    conn = _Conn(hits={"measurement"})
    delete_by_source_storage_key(conn, "records/Patient/eXYZ'; DROP TABLE cdm.person--")

    for sql, params in conn.executed:
        assert "DROP TABLE" not in sql
        assert params == ("records/Patient/eXYZ'; DROP TABLE cdm.person--",)


def test_a_foreign_key_violation_fails_loud_and_rolls_back():
    """The module docstring is explicit: a violation means some clinical
    fact for this person is still retained, and removing the identity
    underneath it would orphan that fact. No silent skip, no cascade."""
    boom = RuntimeError("insert or update on table violates foreign key constraint")
    conn = _Conn(raises=boom)

    with pytest.raises(RuntimeError):
        delete_by_source_storage_key(conn, "records/x.enc")

    assert conn.rollbacks == 1 and conn.commits == 0


def test_the_cursor_is_closed_even_when_the_delete_raises():
    """Explicit close() in finally, because DB-API 2.0 does not promise
    cursors are context managers and GCP connections here are pg8000."""
    conn = _Conn(raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        delete_by_source_storage_key(conn, "records/x.enc")
    assert conn.cursors[0].closed is True


# ---------------------------------------------------------------------------
# Psychotherapy disposal: the resource list
# ---------------------------------------------------------------------------

def _write(tmp_path, text):
    p = tmp_path / "resources.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_a_resource_list_is_parsed_into_type_and_id_pairs(tmp_path):
    path = _write(tmp_path, "DocumentReference,dr-1\nDocumentReference, dr-2 \n")
    assert _parse_resource_list(path) == [
        ("DocumentReference", "dr-1"),
        ("DocumentReference", "dr-2"),
    ]


def test_blank_lines_and_comments_are_skipped(tmp_path):
    path = _write(tmp_path, "# the notes ordered destroyed\n\nDocumentReference,dr-1\n\n")
    assert _parse_resource_list(path) == [("DocumentReference", "dr-1")]


@pytest.mark.parametrize("bad", [
    "DocumentReference\n",                    # no id
    "DocumentReference,dr-1,extra\n",         # too many fields
    ",dr-1\n",                                # empty type
    "DocumentReference,\n",                   # empty id
])
def test_a_malformed_line_is_refused_by_line_number(tmp_path, bad):
    """THE POINT OF FAILING HERE. This list is an order to destroy
    records. A line this parser cannot read unambiguously must stop the
    run before anything is deleted - never be skipped, and never be
    guessed at - and the error has to name the line so the person holding
    the order can fix exactly that one."""
    path = _write(tmp_path, "DocumentReference,dr-ok\n" + bad)
    with pytest.raises(ValueError) as exc:
        _parse_resource_list(path)
    assert ":2:" in str(exc.value), f"the error does not name the line: {exc.value}"


def test_an_empty_list_yields_nothing_rather_than_everything(tmp_path):
    """A disposal run driven by an empty file must delete nothing. The
    failure mode this guards against - empty selection read as "no
    filter", therefore "all" - has destroyed production data in enough
    other systems to be worth one line."""
    assert _parse_resource_list(_write(tmp_path, "# nothing today\n")) == []
# Made by Ryan Gomez & Co. Inc.
