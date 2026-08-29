# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Guarding model-written SQL against the analytics layer.

WHAT ACTUALLY STOPS A BAD QUERY, in order of how much weight each carries.
Stated this way round on purpose, because the checks in this file are the
weakest link in the chain and it would be easy to read them as the
strongest:

  1. THE DATABASE ROLE. Analytics connects as `omop_analyst`, which holds
     SELECT on seven cdm tables and vocab.concept and nothing else - no
     INSERT, no UPDATE, no DELETE, no DDL, no access to stored_resources,
     roi_requests, index_state or any imaging table. A query that got past
     every check below still cannot write anything or read outside that
     grant, because Postgres refuses it. This is the boundary.
  2. READ-ONLY TRANSACTION. Every query runs inside
     `SET TRANSACTION READ ONLY`, so even a future grant widened by
     mistake cannot be exercised from here.
  3. STATEMENT TIMEOUT AND A WRAPPED LIMIT. A cross join over a
     hundred-million-row table is not a security problem but it is an
     outage, and an assistant that can take the analytics database down
     by being asked a vague question is not shippable.
  4. THE LEXICAL CHECKS BELOW. Cheap, fail fast, and give the model a
     comprehensible error it can correct. They are a usability feature
     with a security benefit, not the other way round.

WHY NOT A REAL SQL PARSER. Because a parser that disagrees with
PostgreSQL's own parser is worse than no parser: it produces a false sense
of completeness while the database happily executes something the checker
mis-modelled. sqlparse in particular is a non-validating tokeniser and is
routinely mistaken for a security tool. If this file ever grows to the
point of needing real parsing, the answer is to stop generating SQL, not
to import a parser and believe it.

THE LIMIT IS APPLIED BY WRAPPING, NEVER BY EDITING. Appending `LIMIT n`
to a query that already ends in `LIMIT 1000000` does nothing, and
rewriting an existing LIMIT means understanding the query. Wrapping the
whole statement in `SELECT * FROM (...) LIMIT n` cannot be evaded by
anything the inner query contains, works with CTEs, and needs no
understanding of the query at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Anything that is not a read. Checked as whole words so a column named
# `updated_at` does not trip the `UPDATE` rule.
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "analyze", "reindex", "cluster",
    "comment", "call", "do", "execute", "prepare", "listen", "notify",
    "lock", "refresh", "reset", "security", "begin", "commit", "rollback",
    "savepoint", "set", "declare", "fetch", "move", "close", "discard",
    "import", "merge", "into",
)

# Reachable through SELECT and worth refusing anyway: filesystem and
# network access, large-object functions, and the catalogs that would let
# a query enumerate tables outside the analytics grant.
_FORBIDDEN_SUBSTRINGS = (
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "postgres_fdw", "pg_sleep",
    "pg_catalog", "information_schema", "current_setting", "set_config",
    "pg_terminate", "pg_cancel", "pg_reload",
)

# The only schemas analytics may name. The role grants enforce this too;
# naming it here turns a permission error into a sentence the model can
# act on.
_ALLOWED_SCHEMAS = ("cdm", "vocab", "identity")

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_SCHEMA_QUALIFIED = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\.\s*[a-z_][a-z0-9_]*", re.IGNORECASE)

DEFAULT_ROW_LIMIT = 500
DEFAULT_TIMEOUT_MS = 15_000


class UnsafeQuery(Exception):
    """The query was refused before it reached the database."""


@dataclass(frozen=True)
class GuardedQuery:
    sql: str            # what will actually be executed, wrapped
    original: str       # what the model wrote, for the audit entry
    row_limit: int


def _strip_noise(sql: str) -> str:
    """Comments and string literals removed, for keyword checking only.

    Both are removed before any keyword scan because both can hide one.
    `SELECT 1 /* ; DROP */` and `SELECT 'delete from x'` are each
    harmless, and each trips a naive scan; conversely a statement
    separator hidden in a comment is exactly the trick a naive scan
    misses. What is executed is always the ORIGINAL text - this stripped
    copy is only ever inspected.
    """
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _STRING_LITERAL.sub("''", sql)
    return sql


def guard(sql: str, row_limit: int = DEFAULT_ROW_LIMIT) -> GuardedQuery:
    """Validate and wrap a read-only query, or raise UnsafeQuery.

    The message on every raise is written for the model to read and
    retry, not for a log file - a refusal it cannot understand becomes a
    refusal it repeats.
    """
    original = (sql or "").strip()
    if not original:
        raise UnsafeQuery("The query was empty.")

    inspected = _strip_noise(original).strip()

    # One statement. A trailing semicolon is ordinary and allowed; a
    # semicolon anywhere else means a second statement is being smuggled.
    body = inspected.rstrip().rstrip(";").rstrip()
    if ";" in body:
        raise UnsafeQuery(
            "Only one statement is allowed, and it may not contain a semicolon. "
            "Write a single SELECT, using a CTE (WITH ...) if you need several steps."
        )

    lowered = body.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQuery(
            "Only SELECT queries are allowed here. The analytics database is read-only: "
            "the connection holds no INSERT, UPDATE or DELETE grant of any kind, so a "
            "write would be refused by the database even if it were sent."
        )

    words = set(re.findall(r"[a-z_]+", lowered))
    for keyword in _FORBIDDEN:
        if keyword in words:
            raise UnsafeQuery(
                f"The keyword '{keyword.upper()}' is not allowed in an analytics query. "
                "Only read-only SELECT statements are accepted."
            )

    for fragment in _FORBIDDEN_SUBSTRINGS:
        if fragment in lowered:
            raise UnsafeQuery(
                f"'{fragment}' is not available here. Analytics queries may read only the "
                f"{', '.join(_ALLOWED_SCHEMAS)} schemas, and cannot inspect the database "
                "catalogue or the filesystem."
            )

    # DELIBERATELY NOT CHECKED HERE: which schema a query reads from.
    #
    # A first version of this file tried, and the attempt is worth
    # recording because it looked like a control and was not one.
    # `cdm.person` and `p.person_id` are lexically identical - a schema
    # qualifier and a table alias have the same shape - so telling them
    # apart needs the parser this module's docstring explains we are not
    # going to write. The version that shipped would have accepted every
    # qualifier it could not classify, which is every qualifier, while
    # reading as though it enforced an allowlist.
    #
    # Schema scope is enforced by the role instead: `omop_analyst` holds
    # SELECT on seven cdm tables and vocab.concept, so a query naming
    # anything else fails in Postgres with a permission error that names
    # the table. That is a real boundary, it is enforced by software that
    # does parse SQL correctly, and it degrades to a message the model can
    # read and correct.
    _ = _ALLOWED_SCHEMAS  # named in error messages above, not enforced here

    if len(original) > 8000:
        raise UnsafeQuery("The query is too long. Ask a narrower question.")

    # WHAT EXECUTES IS THE ORIGINAL, NOT THE INSPECTED COPY.
    #
    # FOUND BY A TEST, and worth recording because the bug was silent and
    # the wrong kind of silent. `body` above has string literals replaced
    # with '' so that a value like 'deleted' cannot trip the keyword scan.
    # An earlier version then WRAPPED `body` and executed that - so
    # `WHERE gender_source_value = 'female'` ran as
    # `WHERE gender_source_value = ''`, returning zero rows from a query
    # that looked completely correct in the audit log. No error, no
    # warning, just a confidently wrong number - the exact failure mode
    # core/analytics/cohort.py's curated tools exist to keep off the
    # common path, reintroduced on the uncommon one.
    executable = original.rstrip()
    if executable.endswith(";"):
        executable = executable[:-1].rstrip()
    if ";" in _strip_noise(executable):
        # A semicolon that is not the final character - almost always a
        # trailing comment after it. Refused rather than guessed at,
        # because removing it correctly means knowing what is a comment.
        raise UnsafeQuery(
            "Put the semicolon at the very end of the query, or leave it out. "
            "A semicolon followed by anything else cannot be safely removed."
        )

    wrapped = f"SELECT * FROM (\n{executable}\n) AS guarded_result LIMIT {int(row_limit)}"
    return GuardedQuery(sql=wrapped, original=original, row_limit=int(row_limit))


def execute(conn, guarded: GuardedQuery, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """Run a guarded query in a read-only, time-limited transaction.

    Returns (columns, rows). The transaction is rolled back either way -
    there is nothing to commit, and a rollback leaves no doubt about it.
    """
    cursor = conn.cursor()
    try:
        # Postgres enforces both of these; neither depends on the checks
        # above having been correct.
        #
        # ORDER MATTERS AND IS NOT INTERCHANGEABLE. `SET TRANSACTION` must
        # precede every other statement in the transaction - Postgres
        # rejects it outright once anything else has run, including a
        # `SET LOCAL`. Written the other way round (which is how this was
        # first drafted) every analytics query fails with
        # "SET TRANSACTION must be called before any query", which reads
        # like a driver problem rather than an ordering one.
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        cursor.execute(guarded.sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return columns, rows
    finally:
        cursor.close()
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - connection already gone
            pass
# Made by Ryan Gomez & Co. Inc.
