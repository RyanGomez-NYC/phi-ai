# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Optional AI assistant for implementing and supporting a PHI AI Platform
deployment.

OFF BY DEFAULT AND ADDITIVE. Nothing else in this project imports this
package; a deployment that never sets PHI_AI_ASSISTANT_ENABLED
behaves exactly as it did before the package existed.

WHAT IT CAN SEE, in one place, because it is the only question that
matters about putting a language model next to a system holding PHI:

  - This project's own committed documentation (core/assistant/knowledge.py)
  - Aggregate counts, configuration shape, and verification verdicts
    (core/assistant/posture.py)

WHAT IT CANNOT SEE: any clinical content, any patient reference, any
storage key, any audit entry, any decrypted object. Not by policy - there
is no tool that returns one (core/assistant/tools.py), and outbound text
is scanned before it is sent (core/assistant/redact.py).

This is deliberately separate from install/installer_chatbot.py, which is
rule-based and stays that way: install-time behaviour that writes a .env
file should be predictable and auditable rather than conversational. This
package explains and guides; that script collects and writes.
"""

from core.assistant.config import (  # noqa: F401
    AssistantConfigError,
    AssistantSettings,
    assistant_enabled,
    settings_from_env,
)
from core.assistant.provider import AssistantUnavailable  # noqa: F401
from core.assistant.redact import EgressBlocked  # noqa: F401
from core.assistant.runtime import AssistantRuntime, build  # noqa: F401
from core.assistant.session import AssistantReply, AssistantSession  # noqa: F401

__all__ = [
    "AssistantConfigError",
    "AssistantReply",
    "AssistantRuntime",
    "AssistantSession",
    "AssistantSettings",
    "AssistantUnavailable",
    "EgressBlocked",
    "assistant_enabled",
    "build",
    "settings_from_env",
]
# Made by Ryan Gomez & Co. Inc.
