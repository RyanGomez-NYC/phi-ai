# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Prompt history and saved prompts for the assistant page.

Convenience state over core/db/prompts_schema.sql - see that file's
header for what this deliberately is not (the audit trail is the record
of use; answers are never stored anywhere). Every query is scoped to
one username: a person sees their own history and nobody else's, and
the scoping lives here rather than in each route so a future caller
cannot forget it.

Best-effort on the write path: a failed history insert must never fail
the question it was recording - the audit entry, which DOES gate the
question, was already written by the assistant session. Failures are
logged and swallowed, the same posture as the telemetry store.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("phi-ai.web.prompts")

#: History rows the page shows. Saved prompts are unbounded (a person
#: curates those); raw history is a window, not an archive.
RECENT_LIMIT = 15


class PromptStore:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def _run(self, sql: str, params: tuple, *, fetch: bool = False):
        conn = self._connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                rows = cursor.fetchall() if fetch else None
            finally:
                cursor.close()
            conn.commit()
            return rows
        finally:
            conn.close()

    # -- write path (best-effort) ------------------------------------

    def record(self, username: str, prompt: str, page_key: Optional[str] = None) -> None:
        prompt = (prompt or "").strip()
        if not prompt:
            return
        try:
            # A prompt re-run from history should not duplicate its own
            # row each time: refresh the timestamp of an identical,
            # unsaved prompt instead of inserting a twin.
            updated = self._run(
                "UPDATE assistant_prompts SET created_at = now() "
                "WHERE username = %s AND prompt = %s AND NOT saved "
                "RETURNING id",
                (username, prompt), fetch=True,
            )
            if not updated:
                self._run(
                    "INSERT INTO assistant_prompts (username, prompt, page_key) "
                    "VALUES (%s, %s, %s)",
                    (username, prompt, page_key),
                )
        except Exception as exc:
            log.warning("prompt history insert failed (question unaffected): %s", exc)

    # -- reads --------------------------------------------------------

    def recent(self, username: str, limit: int = RECENT_LIMIT) -> list[dict]:
        try:
            rows = self._run(
                "SELECT id, prompt, created_at FROM assistant_prompts "
                "WHERE username = %s AND NOT saved "
                "ORDER BY created_at DESC LIMIT %s",
                (username, limit), fetch=True,
            )
        except Exception as exc:
            log.warning("prompt history read failed: %s", exc)
            return []
        return [{"id": r[0], "prompt": r[1], "created_at": r[2]} for r in rows or []]

    def saved(self, username: str) -> list[dict]:
        try:
            rows = self._run(
                "SELECT id, prompt, label, created_at FROM assistant_prompts "
                "WHERE username = %s AND saved "
                "ORDER BY COALESCE(label, prompt)",
                (username,), fetch=True,
            )
        except Exception as exc:
            log.warning("saved prompts read failed: %s", exc)
            return []
        return [
            {"id": r[0], "prompt": r[1], "label": r[2], "created_at": r[3]}
            for r in rows or []
        ]

    # -- state changes (user-initiated; failures surface) --------------

    def save(self, username: str, prompt_id: int, label: Optional[str] = None) -> None:
        label = (label or "").strip()[:120] or None
        self._run(
            "UPDATE assistant_prompts SET saved = TRUE, label = %s "
            "WHERE id = %s AND username = %s",
            (label, prompt_id, username),
        )

    def unsave(self, username: str, prompt_id: int) -> None:
        self._run(
            "UPDATE assistant_prompts SET saved = FALSE, label = NULL "
            "WHERE id = %s AND username = %s",
            (prompt_id, username),
        )

    def delete(self, username: str, prompt_id: int) -> None:
        self._run(
            "DELETE FROM assistant_prompts WHERE id = %s AND username = %s",
            (prompt_id, username),
        )
# Made by Ryan Gomez & Co. Inc.
