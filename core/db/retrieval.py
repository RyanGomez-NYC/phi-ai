# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Searching the clinical retrieval index (core/db/retrieval_schema.sql).

The read side, deliberately tiny. One function per table, parameterised
SQL only, connecting as the read-only search roles
(retrieval_bootstrap_<cloud>.sql) - which table a caller can reach is
decided by which role its connection holds, not by anything here.

SNIPPETS ARE THE DISCLOSURE. A result row carries ts_headline's excerpt
around the match, the patient reference and the storage key - enough to
decide whether to open the record through the audited read path, and no
more. Snippet length is capped server-side (MaxFragments/MaxWords), so
a search cannot be used to page an entire chart out of the index 60
words at a time without every read appearing in the audit trail the way
an actual read would.

QUERIES ARE websearch_to_tsquery, the operator-facing parser: quoted
phrases, OR, and -exclusions behave the way a search box user expects,
and a malformed query is a normal empty result rather than an error the
model has to reason about. The raw query string is audited verbatim by
the caller BEFORE this runs (core/assistant/tools.py), the same
contract the guarded SQL tool follows.
"""

from __future__ import annotations

from typing import Any, Optional

_CLINICAL = "retrieval.clinical_text"
_PSYCHOTHERAPY = "retrieval.psychotherapy_text"
_TABLES = frozenset({_CLINICAL, _PSYCHOTHERAPY})

# ts_headline options: a few short fragments per row, not a page of one.
_HEADLINE_OPTS = "MaxFragments=3, MaxWords=18, MinWords=6, FragmentDelimiter=' … '"

MAX_LIMIT = 50


def _search(
    conn: Any,
    table: str,
    query: str,
    *,
    limit: int = 10,
    patient_reference: Optional[str] = None,
    resource_type: Optional[str] = None,
) -> list[dict]:
    if table not in _TABLES:
        raise ValueError(f"unknown retrieval table {table!r}")
    limit = max(1, min(int(limit), MAX_LIMIT))

    conditions = ["content_tsv @@ websearch_to_tsquery('english', %s)"]
    params: list[Any] = [query]
    if patient_reference:
        conditions.append("patient_reference = %s")
        params.append(patient_reference)
    if resource_type:
        conditions.append("resource_type = %s")
        params.append(resource_type)

    sql = (
        "SELECT storage_key, resource_index, patient_reference, resource_type, "
        "       resource_id, clinical_date, "
        "       ts_headline('english', content, "
        "                   websearch_to_tsquery('english', %s), %s) AS snippet, "
        "       ts_rank(content_tsv, websearch_to_tsquery('english', %s)) AS rank "
        f"FROM {table} "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY rank DESC, clinical_date DESC NULLS LAST "
        "LIMIT %s"
    )
    cur = conn.cursor()
    try:
        cur.execute(sql, [query, _HEADLINE_OPTS, query, *params, limit])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def search_clinical(conn: Any, query: str, **kwargs) -> list[dict]:
    """Search the general clinical text index. Connect as the
    retrieval-search role; that role's grants are what keep this out of
    the psychotherapy table, not this function."""
    return _search(conn, _CLINICAL, query, **kwargs)


def search_psychotherapy(conn: Any, query: str, **kwargs) -> list[dict]:
    """Search the psychotherapy text index. Connect as the psychotherapy
    retrieval role - a connection holding only the general search role
    gets a permission error here, by design."""
    return _search(conn, _PSYCHOTHERAPY, query, **kwargs)
# Made by Ryan Gomez & Co. Inc.
