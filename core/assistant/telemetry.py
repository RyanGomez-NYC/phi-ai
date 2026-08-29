# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Assistant telemetry: recording interactions and summarising them.

METRICS, NEVER CONTENT. What goes into aiops.assistant_interactions is
counts, durations, names of tools, and outcomes - the schema's header
(core/db/telemetry_schema.sql) states the constraint and this module is
its only writer, so record_interaction()'s parameter list IS the
complete answer to "what does telemetry collect". Question text lives in
the tamper-evident audit trail, and nowhere else.

FIRE-AND-FORGET ON THE WRITE PATH. A telemetry failure must never fail
an answer: record_interaction() catches everything, logs it, and
returns. The summaries used by the ops page raise normally - a broken
ops page is a broken page, not a broken assistant.

WHY THIS IS THE DRIFT BASELINE TOO. Drift probes
(core/assistant/drift.py) record their per-probe outcomes here with
kind='drift_probe', so "has behaviour changed" is answerable with the
same SQL, over the same retention, as "how is it performing" - one
store, one ops page, one place to look.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("phi-ai.assistant.telemetry")

_TABLE = "aiops.assistant_interactions"


def record_interaction(
    connection_factory: Optional[Callable[[], Any]],
    *,
    username: str,
    roles: str = "",
    page_key: Optional[str] = None,
    provider: str = "",
    model: str = "",
    latency_ms: Optional[int] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls: int = 0,
    tools_used: str = "",
    phi_reads: int = 0,
    refused: bool = False,
    truncated: bool = False,
    error: bool = False,
    kind: str = "interaction",
    probe_name: Optional[str] = None,
    probe_passed: Optional[bool] = None,
    probe_detail: Optional[str] = None,
) -> bool:
    """Insert one telemetry row. Returns False (and logs) on any
    failure; never raises. None factory means telemetry is simply not
    configured, the standard graceful skip."""
    if connection_factory is None:
        return False
    try:
        conn = connection_factory()
    except Exception as exc:
        log.warning("telemetry connection failed (answer unaffected): %s", exc)
        return False
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {_TABLE} "
                "(kind, username, roles, page_key, provider, model, latency_ms, "
                " input_tokens, output_tokens, tool_calls, tools_used, phi_reads, "
                " refused, truncated, error, probe_name, probe_passed, probe_detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    kind, username, roles or None, page_key, provider, model,
                    latency_ms, input_tokens, output_tokens, tool_calls,
                    tools_used or None, phi_reads, refused, truncated, error,
                    probe_name, probe_passed, probe_detail,
                ),
            )
            conn.commit()
        finally:
            cur.close()
        return True
    except Exception as exc:
        log.warning("telemetry write failed (answer unaffected): %s", exc)
        try:
            conn.rollback()
        except Exception:  # pragma: no cover
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def usage_summary(conn: Any, days: int = 30) -> dict:
    """The ops page's numbers, over a window. One round trip per
    sub-summary, plain SQL, no ORM - matching how every other report in
    this project reads."""
    days = max(1, min(int(days), 365))
    window = "ts >= now() - make_interval(days => %s) AND kind = 'interaction'"

    totals = _rows(
        conn,
        "SELECT count(*) AS questions, "
        "       count(DISTINCT username) AS distinct_users, "
        "       coalesce(sum(input_tokens), 0) AS input_tokens, "
        "       coalesce(sum(output_tokens), 0) AS output_tokens, "
        "       coalesce(sum(tool_calls), 0) AS tool_calls, "
        "       coalesce(sum(phi_reads), 0) AS phi_reads, "
        "       count(*) FILTER (WHERE refused) AS refusals, "
        "       count(*) FILTER (WHERE error) AS errors, "
        "       count(*) FILTER (WHERE truncated) AS truncations, "
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms, "
        "       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms "
        f"FROM {_TABLE} WHERE {window}",
        (days,),
    )[0]

    by_day = _rows(
        conn,
        "SELECT date_trunc('day', ts)::date AS day, count(*) AS questions, "
        "       count(*) FILTER (WHERE refused) AS refusals, "
        "       count(*) FILTER (WHERE error) AS errors "
        f"FROM {_TABLE} WHERE {window} "
        "GROUP BY 1 ORDER BY 1 DESC",
        (days,),
    )

    by_role = _rows(
        conn,
        "SELECT coalesce(roles, '(none)') AS roles, count(*) AS questions, "
        "       coalesce(sum(phi_reads), 0) AS phi_reads "
        f"FROM {_TABLE} WHERE {window} "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 20",
        (days,),
    )

    by_model = _rows(
        conn,
        "SELECT provider, model, count(*) AS questions, min(ts) AS first_seen, "
        "       max(ts) AS last_seen "
        f"FROM {_TABLE} WHERE {window} "
        "GROUP BY 1, 2 ORDER BY 5 DESC",
        (days,),
    )

    return {"days": days, "totals": totals, "by_day": by_day,
            "by_role": by_role, "by_model": by_model}


def drift_summary(conn: Any, runs: int = 10) -> list[dict]:
    """The most recent drift runs, one row per (run timestamp bucket,
    model): probes passed/failed. A model change plus a pass-rate change
    is the drift signal the ops page surfaces."""
    runs = max(1, min(int(runs), 100))
    return _rows(
        conn,
        "SELECT date_trunc('minute', ts) AS run_at, model, "
        "       count(*) AS probes, "
        "       count(*) FILTER (WHERE probe_passed) AS passed, "
        "       string_agg(probe_name, ', ') FILTER (WHERE NOT probe_passed) AS failed_probes "
        f"FROM {_TABLE} WHERE kind = 'drift_probe' "
        "GROUP BY 1, 2 ORDER BY 1 DESC LIMIT %s",
        (runs,),
    )
# Made by Ryan Gomez & Co. Inc.
