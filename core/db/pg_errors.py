# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Driver-agnostic Postgres error classification.

WHY THIS MODULE EXISTS - a real, confirmed cross-driver bug, not
tidiness. This project's Postgres connections come from two different
driver libraries depending on cloud (core/db/connection.py's own
docstring): psycopg on AWS/Azure, pg8000 on GCP (the Cloud SQL Python
Connector does not support psycopg's underlying libpq). Before this
module, the two write paths detected the expected duplicate-key case
two different, independently wrong ways:

  - core/db/index.py caught `psycopg.errors.UniqueViolation` by class.
    On a GCP (pg8000) connection a duplicate insert raises pg8000's own
    exception type, which that catch can never match - so the routine,
    documented "duplicate absorbed silently" idempotency contract was
    broken on GCP: every re-run or backfill produced one spurious index
    error per already-indexed resource, and reconcile.py's documented
    recovery ("re-run the scheduler; duplicates are absorbed")
    was false on GCP specifically.

  - core/db/omop_etl.py checked `"23505" in str(exc)` - driver-agnostic,
    but a substring match over the whole rendered exception: any OTHER
    error whose message happens to contain "23505" (this project's
    deterministic BIGINT IDs appear in FK-violation messages, and one in
    every ~6,500 such IDs contains that digit run) would be misread as a
    duplicate and silently converted into an UPDATE of a row that may
    not exist.

Both files now route through classify-by-SQLSTATE below: read the
actual SQLSTATE field each driver carries structurally, never a
substring of the rendered message. Postgres SQLSTATE codes are stable
across all client libraries; only WHERE each library stores the code
differs:

  - psycopg (3.x): every `psycopg.Error` exposes `.sqlstate`
    (documented public API).
  - pg8000: a server-reported error is raised with the server's raw
    error-field mapping as the exception's first argument - a dict whose
    "C" key is the SQLSTATE (PostgreSQL protocol error field "Code").

Verified against a live PostgreSQL 16 via psycopg (unique violation ->
sqlstate "23505", FK violation -> "23503") and against pg8000's
documented server-error shape via structural stubs. pg8000 itself could
not be installed in the verification environment - if its error shape
ever drifts from the documented dict-first-arg form, the exact-message
fallback below still recognizes genuine duplicate-key errors (the
fallback string is the canonical, stable server message for
unique_violation), and anything unrecognized is treated as a real error
rather than silently absorbed - failing loud, not degrading quietly,
per this project's own invariant.

Deliberately import-free: this module must never import psycopg OR
pg8000, so it is safe to import unconditionally on every cloud
(importing psycopg at call time inside a code path that also runs on
GCP was exactly the shape of coupling that broke the GCP index - see
core/db/index.py's FIXED note).
"""

from __future__ import annotations

from typing import Optional

UNIQUE_VIOLATION = "23505"


def sqlstate_of(exc: BaseException) -> Optional[str]:
    """
    The PostgreSQL SQLSTATE carried by a driver exception, or None if
    the exception carries none (e.g. a client-side connection failure,
    or a non-database exception entirely).
    """
    # psycopg 3.x: psycopg.Error.sqlstate (public, documented).
    state = getattr(exc, "sqlstate", None)
    if isinstance(state, str) and state:
        return state

    # pg8000: server errors carry the protocol error-field dict as the
    # first exception argument; "C" is the SQLSTATE code field.
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            code = arg.get("C")
            if isinstance(code, str) and code:
                return code

    return None


def is_unique_violation(exc: BaseException) -> bool:
    """
    True iff `exc` is Postgres's unique_violation (SQLSTATE 23505) - the
    expected, idempotency-absorbing duplicate-key case in
    core/db/index.py's write_index_entry() and core/db/omop_etl.py's
    _execute_upsert().

    When the exception carries a structural SQLSTATE, that answer is
    authoritative - including authoritatively False for a different
    SQLSTATE whose message text happens to contain "23505" somewhere (a
    deterministic BIGINT ID inside an FK-violation message, for
    example). The message-text fallback below is consulted ONLY when no
    structural SQLSTATE is present at all, and matches the canonical
    server message phrase for unique_violation, not a bare digit run.
    """
    state = sqlstate_of(exc)
    if state is not None:
        return state == UNIQUE_VIOLATION
    return "duplicate key value violates unique constraint" in str(exc)
# Made by Ryan Gomez & Co. Inc.
