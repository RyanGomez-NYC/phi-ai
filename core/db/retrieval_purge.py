# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deleting clinical retrieval rows when their source object is disposed.

The retrieval index holds extracted clinical prose keyed by storage_key
(core/db/retrieval_schema.sql); its header states the rule this module
implements: a row here must die in the same disposal operation as the
index row, because indexed text that outlives its record is a retention
violation with no upside. core/fhir/purge.py calls delete_clinical() in
its disposal sequence, and core/fhir/psychotherapy_purge.py calls
delete_psychotherapy() in its own - the two tables stay on their two
separate disposal paths, matching their two separate storage paths.

Connects as whatever role the caller's connection holds - the
disposition role, whose grants (DELETE plus column-scoped SELECT on
storage_key only, see retrieval_bootstrap_<cloud>.sql) let it target
rows without being able to read anyone's text. Errors propagate: like
the OMOP and index deletes, a failure here must abort the disposal of
that object BEFORE storage is touched, never after.
"""

from __future__ import annotations

from typing import Any


def _delete(conn: Any, table: str, storage_key: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table} WHERE storage_key = %s", (storage_key,))
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def delete_clinical(conn: Any, storage_key: str) -> int:
    """Rows in retrieval.clinical_text for one disposed object."""
    return _delete(conn, "retrieval.clinical_text", storage_key)


def delete_psychotherapy(conn: Any, storage_key: str) -> int:
    """Rows in retrieval.psychotherapy_text for one disposed note."""
    return _delete(conn, "retrieval.psychotherapy_text", storage_key)
# Made by Ryan Gomez & Co. Inc.
