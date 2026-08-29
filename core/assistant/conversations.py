# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Multi-turn conversation state, in memory, for the life of a session.

A REVISION OF AN EARLIER DECISION IN THIS PACKAGE, stated rather than
quietly made. core/web/app.py's assistant route was originally
single-turn, and the reason given was that a server-side store of what
users have typed would be a new place PHI could accumulate, outside the
retention and disposal machinery governing everything else here. That
reasoning was right about a durable store and wrong about this one, and
the difference is worth naming:

  - Nothing here is written to disk, to Postgres, or to the object store.
    It is process memory, and it dies with the worker.
  - Every question in it has already passed the egress scan
    (core/assistant/redact.py), so by construction it holds no
    PHI-shaped content - it is the same text already sent to the model
    and already recorded in the audit log.
  - It expires on its own short idle clock, so an abandoned clinical
    workstation drops its conversation well before it drops its identity.

What a single-turn assistant actually cost was the thing that made it
feel bolted on: no follow-up questions. "Why is that?" is how people
talk to something that is helping them, and an assistant that answers
each question as if it had never spoken before is not seamless with
anything.

TWO BOUNDS, BOTH ENFORCED RATHER THAN ASSUMED. Conversations are capped
in number with least-recently-used eviction, so a busy deployment cannot
grow this without limit, and each conversation keeps a bounded number of
turns. A memory store on a long-lived web worker that trusts callers to
clean up is a leak with extra steps.

WHAT THIS DOES NOT SURVIVE, stated because the web interface already
warns about the same thing for sessions: a process restart, or a second
replica behind a load balancer without session affinity. A user whose
next request lands on a different worker starts a fresh conversation and
is told so, rather than silently losing context.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("phi-ai.assistant.conversations")

# Idle lifetime of one conversation. Deliberately its own number rather
# than the sign-in session's: a signed-in session lasts hours
# (PHI_AI_WEB_LOCAL_AUTH_SESSION_MINUTES, 480 by default), and holding
# every question anyone asked for that long is a larger memory footprint
# and a longer-lived copy of their text than the feature needs. Thirty
# minutes covers a real back-and-forth and expires an abandoned tab.
DEFAULT_TTL_SECONDS = 30 * 60

# Enough for a real support conversation, bounded so one tab cannot grow
# a worker's memory indefinitely. Older turns fall out of the model's
# history first (core/assistant/session.py trims), and out of the visible
# transcript here.
DEFAULT_MAX_TURNS = 12

# Across all users of one worker. Eviction is least-recently-used.
DEFAULT_MAX_CONVERSATIONS = 500


@dataclass
class Turn:
    """One question and its answer, as shown to the user."""

    question: str
    answer: str
    sources: list[str] = field(default_factory=list)
    refused: bool = False
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Conversation:
    id: str
    actor: str
    turns: list[Turn] = field(default_factory=list)
    # The model-facing history, held as the SDK's own content objects.
    # Never serialised - this store is process memory, so there is nothing
    # to encode and nothing to decode.
    messages: list[Any] = field(default_factory=list)
    turn_starts: list[int] = field(default_factory=list)
    last_used: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record(self, turn: Turn, max_turns: int) -> None:
        self.turns.append(turn)
        if len(self.turns) > max_turns:
            del self.turns[: len(self.turns) - max_turns]
        self.last_used = datetime.now(timezone.utc)


class ConversationStore:
    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
    ):
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_turns = max_turns
        self.max_conversations = max_conversations
        self._conversations: dict[str, Conversation] = {}
        # Web routes run in a threadpool, so two requests from the same
        # browser genuinely can land here at once.
        self._lock = threading.Lock()

    def get(self, conversation_id: Optional[str], actor: str) -> Optional[Conversation]:
        """The named conversation, if it exists, is fresh, and is this user's.

        The actor check is not decoration. The id travels in a signed
        session cookie, so forging one means forging the cookie - but a
        conversation is keyed by a random id in a shared process-wide
        dict, and "the key is unguessable" is a weaker property than "the
        key is unguessable AND belongs to the caller".
        """
        if not conversation_id:
            return None
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return None
            if conversation.actor != actor:
                log.warning(
                    "conversation %s was requested by a different user than the one "
                    "that created it; starting a fresh one",
                    conversation_id[:8],
                )
                return None
            if datetime.now(timezone.utc) - conversation.last_used > self.ttl:
                del self._conversations[conversation_id]
                return None
            conversation.last_used = datetime.now(timezone.utc)
            return conversation

    def create(self, actor: str) -> Conversation:
        conversation = Conversation(id=secrets.token_urlsafe(24), actor=actor)
        with self._lock:
            self._expire_locked()
            if len(self._conversations) >= self.max_conversations:
                oldest = min(self._conversations.values(), key=lambda c: c.last_used)
                del self._conversations[oldest.id]
                log.info("assistant conversation store full - evicted the oldest")
            self._conversations[conversation.id] = conversation
        return conversation

    def discard(self, conversation_id: Optional[str]) -> None:
        if not conversation_id:
            return
        with self._lock:
            self._conversations.pop(conversation_id, None)

    def _expire_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - self.ttl
        for key in [k for k, c in self._conversations.items() if c.last_used < cutoff]:
            del self._conversations[key]

    def __len__(self) -> int:  # for tests and diagnostics
        with self._lock:
            return len(self._conversations)
# Made by Ryan Gomez & Co. Inc.
