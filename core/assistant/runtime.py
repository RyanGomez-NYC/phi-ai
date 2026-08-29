# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Assembling the assistant once, and handing out per-caller sessions.

The expensive parts - loading and indexing the documentation corpus,
constructing the SDK client - are done once per process. The parts that
differ per caller - which tools their role permits, whose name goes in
the audit entry - are done per session. Keeping that split explicit is
what makes it safe to run the assistant inside the web worker: the
knowledge base is shared read-only state, and nothing about one user's
conversation is reachable from another's.

DEPLOYMENT CONTEXT FOLLOWS THE SAME PERMISSION RULE AS THE TOOLS. The
system prompt can carry a summary of how this deployment is configured,
which makes answers concrete instead of generic. It is included only for
callers who could have read the same thing from the reports page, for the
reason core/assistant/tools.py states at length: the assistant must never
show someone something their role does not permit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from core.assistant import knowledge, posture, tools
from core.assistant.conversations import ConversationStore
from core.assistant.config import AssistantSettings
from core.assistant.session import AssistantSession

log = logging.getLogger("phi-ai.assistant.runtime")


@dataclass
class AssistantRuntime:
    settings: AssistantSettings
    client: Any
    knowledge_base: knowledge.KnowledgeBase
    platform_settings: Any = None     # core.config.settings.Settings
    profile: Any = None               # core.config.scale_profile.ScaleProfile
    reader: Any = None                # core.web.data.RecordReader

    # Shared across every caller on this worker, keyed by an id in each
    # user's own signed session cookie and checked against their username
    # on every read. See core/assistant/conversations.py.
    conversations: ConversationStore = field(default_factory=ConversationStore)

    # Population analytics. Connection FACTORIES rather than connections:
    # an assistant session idles between questions, and holding a Postgres
    # connection open per idle user is how a connection pool is exhausted
    # by people who are not doing anything. None means the deployment has
    # not configured that layer, and the tools simply do not appear.
    analytics_connection: Any = None
    identity_connection: Any = None

    # Cross-record research (core/db/retrieval_schema.sql). Same
    # factory-not-connection discipline as the two above, same None-means-
    # not-configured posture. The psychotherapy pieces are separate from
    # the general search on purpose - separate table, separate role,
    # separate acknowledgement - and `psychotherapy_reader` is a callable
    # (storage_key -> decrypted resources) over the psychotherapy bucket
    # and its own KMS key, built in core/web/__main__.py.
    research_search_connection: Any = None
    psychotherapy_search_connection: Any = None
    psychotherapy_reader: Any = None

    # Telemetry (core/assistant/telemetry.py) - the aiops INSERT+SELECT
    # role. None means no telemetry is recorded, and the assistant is
    # unaffected: every write through this is fire-and-forget.
    ops_connection: Any = None

    def _deployment_context(self) -> Optional[str]:
        if self.platform_settings is None or self.profile is None:
            return None
        summary = posture.configuration_posture(
            self.platform_settings, self.profile, self.settings
        )
        return (
            "The person you are helping runs the deployment described below. Prefer "
            "these facts over general statements about what the PHI AI Platform can "
            "be configured to do.\n\n"
            + json.dumps(summary, indent=2, sort_keys=True)
        )

    def session_for(
        self,
        *,
        actor: str,
        capabilities: Optional[frozenset[str]] = tools.UNRESTRICTED,
        audit=None,
        require_audit: bool = True,
        history: Optional[list] = None,
        turn_starts: Optional[list[int]] = None,
        clinical: Optional[tools.ClinicalAccess] = None,
        analytics: Optional[tools.AnalyticsAccess] = None,
        research: Optional[tools.ResearchAccess] = None,
    ) -> AssistantSession:
        permitted = capabilities is tools.UNRESTRICTED or "report:read" in capabilities

        # Belt and braces. The caller is expected not to pass `clinical`
        # in a deployment that did not enable a PHI tier, but dropping it
        # here means a caller that gets that wrong builds a toolbox with
        # no clinical tools rather than one that quietly has them.
        if clinical is not None and not self.settings.reads_clinical_content:
            log.warning(
                "clinical access was offered to the assistant but "
                "PHI_AI_ASSISTANT_PHI_ACCESS is 'none' - ignoring it"
            )
            clinical = None

        # Same belt-and-braces for research: snippets are clinical text,
        # so cross-record search demands the lookup tier, and the
        # psychotherapy pieces additionally demand the deployment's own
        # psychotherapy gate. A caller that gets either wrong builds a
        # toolbox WITHOUT those tools, never one that quietly has them.
        if research is not None and not self.settings.allows_lookup:
            log.warning(
                "research access was offered to the assistant but "
                "PHI_AI_ASSISTANT_PHI_ACCESS is not 'lookup' - ignoring it"
            )
            research = None
        if research is not None and not self.settings.psychotherapy_access and (
            research.psychotherapy_connection is not None
            or research.read_psychotherapy is not None
        ):
            log.warning(
                "psychotherapy access was offered to the assistant but "
                "PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS is not set - dropping it"
            )
            research = tools.ResearchAccess(
                search_connection=research.search_connection,
                record=research.record,
                purpose=research.purpose,
            )

        return AssistantSession(
            client=self.client,
            settings=self.settings,
            toolbox=tools.build(
                self.knowledge_base,
                settings=self.platform_settings,
                profile=self.profile,
                assistant_settings=self.settings,
                reader=self.reader,
                capabilities=capabilities,
                clinical=clinical,
                analytics=analytics,
                research=research,
            ),
            actor=actor,
            audit=audit,
            require_audit=require_audit,
            deployment_context=self._deployment_context() if permitted else None,
            history=history,
            turn_starts=turn_starts,
        )


def build(
    *,
    assistant_settings: AssistantSettings,
    platform_settings=None,
    profile=None,
    reader=None,
    analytics_connection=None,
    identity_connection=None,
    research_search_connection=None,
    psychotherapy_search_connection=None,
    psychotherapy_reader=None,
    ops_connection=None,
) -> AssistantRuntime:
    from core.assistant.provider import build_client

    log.info("assistant enabled: %s", assistant_settings.describe())
    if not assistant_settings.stays_in_org_cloud:
        log.warning(
            "the assistant is configured to use the Anthropic API directly, so its "
            "requests leave this deployment's cloud account. No PHI is sent on that "
            "path by construction, but if this deployment runs on AWS or GCP, "
            "PHI_AI_ASSISTANT_PROVIDER=bedrock or =vertex keeps model traffic "
            "inside the account you already hold a BAA for."
        )

    return AssistantRuntime(
        settings=assistant_settings,
        client=build_client(assistant_settings),
        knowledge_base=knowledge.load(),
        platform_settings=platform_settings,
        profile=profile,
        reader=reader,
        analytics_connection=analytics_connection,
        identity_connection=identity_connection,
        research_search_connection=research_search_connection,
        psychotherapy_search_connection=psychotherapy_search_connection,
        psychotherapy_reader=psychotherapy_reader,
        ops_connection=ops_connection,
    )
# Made by Ryan Gomez & Co. Inc.
