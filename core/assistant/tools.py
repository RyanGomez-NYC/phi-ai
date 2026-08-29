# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The tools the assistant is given, and the permissions that gate them.

THE TOOL LIST IS THE SECURITY BOUNDARY. Whatever a model is asked to do,
it can only do what a tool lets it do, so the question "can the assistant
see PHI?" reduces to "is there a tool that returns PHI?" - and the answer
is enumerable by reading this file. Everything stays in this one module
for that reason, length notwithstanding: a boundary you have to read
three files to establish is one people stop checking.

BY DEFAULT THE ANSWER IS NO. At PHI_AI_ASSISTANT_PHI_ACCESS=none,
which is the default, nothing below the "Clinical tools" heading is
built. There is no tool that decrypts an object, none that returns a
patient reference or a storage key, and none that writes anything at
all.

ABOVE THAT DEFAULT, THE DEPLOYING ORGANISATION HAS DECIDED OTHERWISE, and
core/assistant/config.py explains why that decision is theirs to make.
The clinical tools then appear, in one of two tiers, and three properties
hold at both:

  - They are still permission-gated. Each declares the same permission
    the equivalent page in core/web/app.py requires.
  - Every read is audit-logged as a disclosure BEFORE the object is
    fetched, with the object key and a stated purpose of use - the same
    ordering, action name and payload core/web/app.py produces, so an
    accounting of disclosures cannot tell the two apart and does not
    need to.
  - `in_context` tools can only reach what the user already has open.
    The bound target comes from the server's own view of the page, never
    from the model: a storage key the model asks for is checked against
    the set belonging to that target, so asking for a different
    patient's object fails rather than succeeding quietly.

Psychotherapy notes are reachable at no PHI tier by itself. They live in
a separate bucket under a separate key
(runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md), and the lookup tier's
clinical tools are not wired to that store. The ONLY path to them is the
psychotherapy research tools below, which exist solely when the
deployment set PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS with its own
acknowledgement (core/assistant/config.py), provisioned the separate
psychotherapy retrieval role, AND the caller holds the `psychotherapy`
application role with a stated purpose of use - and every search and
every read is audit-logged as a disclosure before anything is decrypted.

THE ASSISTANT IS NOT A PRIVILEGE ESCALATION PATH. Every tool that reports
on the live platform declares the same permission the equivalent page in
core/web/app.py requires. A clinician with the viewer role gets
documentation and nothing else; an auditor gets the audit chain verdict
but not holdings; an administrator gets configuration. A user who cannot
see the retention page in the interface cannot get its numbers by asking
for them here. Tools their role does not permit are not merely refused -
they are never sent, so the model cannot mention a capability the user
does not have.

TOOLS ABSENT BECAUSE THEY ARE NOT CONFIGURED VS ABSENT BECAUSE THEY ARE
NOT PERMITTED are both handled by simply not offering them. During
installation there is no stored data to read yet, and the assistant is
documentation-only; that is the same code path as an unprivileged user,
which means the common case has been exercised by the time anyone hits
the rare one.

TOOL ORDER IS FIXED. Tool definitions render first in the request and any
change to them invalidates the whole prompt cache, so the list is built
in a stable order rather than from a dict or a set.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.assistant import posture, redact

log = logging.getLogger("phi-ai.assistant.tools")

# Sentinel meaning "no permission model applies" - the CLI, run by
# someone who already has shell access to the deployment's environment
# and could read any of this directly. Honest about what it is rather
# than pretending to a check that would be theatre.
UNRESTRICTED = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    required_permission: Optional[str] = None

    # Set only on the clinical tools below. Toolbox.run() scans every
    # result and refuses PHI-shaped content; a tool whose entire purpose
    # is returning a clinical record has to opt out of that check
    # EXPLICITLY, so the opt-out is visible next to the tool rather than
    # implied by configuration somewhere else. Documentation and posture
    # tools never set it, which means a bug that made one of them return
    # a record is still caught.
    may_return_phi: bool = False

    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Toolbox:
    """The tools available for one caller, already filtered by permission."""

    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}
        self._order = [t.name for t in tools]

    @property
    def names(self) -> list[str]:
        return list(self._order)

    def definitions(self) -> list[dict]:
        return [self._tools[name].definition() for name in self._order]

    def returns_phi(self, name: str) -> bool:
        """Whether a tool is one of the PHI-bearing ones - what telemetry
        counts as a phi_read. Metadata only; unknown names are False."""
        tool = self._tools.get(name)
        return bool(tool and tool.may_return_phi)

    def run(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Execute one tool call. Returns (result text, is_error).

        Every result is scanned before it goes back to the model. A tool
        here returning PHI is a bug in that tool, and it surfaces as a
        failed tool call rather than as outbound traffic - see
        core/assistant/redact.py.
        """
        tool = self._tools.get(name)
        if tool is None:
            # Reachable when the model invents a tool name. Naming what IS
            # available recovers the turn instead of ending it.
            return (
                f"No such tool: {name!r}. Available: {', '.join(self._order)}.",
                True,
            )

        try:
            result = tool.handler(**(arguments or {}))
        except TypeError as exc:
            return (f"Bad arguments for {name}: {exc}", True)
        except Exception as exc:  # surfaced to the model, not swallowed
            log.warning("assistant tool %s failed: %s", name, exc)
            return (f"{name} failed: {exc}", True)

        text = result if isinstance(result, str) else json.dumps(result, sort_keys=True, indent=2)

        if tool.may_return_phi:
            # Deliberately unscanned - see Tool.may_return_phi. The
            # organisation has enabled this tier and the read is already
            # in the audit log by the time we get here.
            return (text, False)

        try:
            redact.assert_clean(text, f"the result of the {name} tool")
        except redact.EgressBlocked as exc:
            log.error(
                "assistant tool %s produced content that failed the egress check (%s). "
                "This is a bug in that tool - no tool in core/assistant/tools.py should "
                "be able to return identifiable content.",
                name,
                ", ".join(sorted({f.category for f in exc.findings})),
            )
            return (
                f"{name} produced a result that could not be sent because it appears to "
                "contain identifiable information. This has been logged as a defect. "
                "Answer from documentation instead.",
                True,
            )

        return (text, False)


def _search_tool(kb) -> Tool:
    def handler(query: str, limit: int = 5) -> str:
        excerpts = kb.search(query, limit=max(1, min(int(limit), 8)))
        if not excerpts:
            return (
                f"No documentation matched {query!r}. Try the operator's own vocabulary "
                "- environment variable names, runbook titles, or the exact error text."
            )
        parts = []
        for excerpt in excerpts:
            parts.append(f"--- {excerpt.section.citation} ---\n{excerpt.section.text}")
        return "\n\n".join(parts)

    return Tool(
        name="search_documentation",
        description=(
            "Search this PHI AI Platform deployment's own documentation - the README, "
            "the docs/ directory (architecture, compliance, scaling, cost, EMR "
            "connectors), every runbook, the per-cloud deployment guides, and the "
            "annotated .env.example. Returns the most relevant sections with the file "
            "path of each. Use this before answering any question about how this "
            "system works, what a setting does, or what an operator should do - the "
            "documentation describes THIS codebase, including decisions and known gaps "
            "that differ from how such a system would usually be built. Search "
            "with the words the documentation itself uses: exact environment variable "
            "names, error text, or runbook terms."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms. Specific technical vocabulary works best.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many sections to return (1-8, default 5).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


def _read_tool(kb) -> Tool:
    def handler(path: str, section: Optional[str] = None) -> str:
        text = kb.read(path, section=section)
        if text is None:
            if section:
                return (
                    f"No section matching {section!r} in {path}. Read the whole "
                    "document by omitting the section, or search for the topic."
                )
            return (
                f"{path!r} is not in the documentation set. Available documents:\n"
                + "\n".join(f"  {d}" for d in kb.documents)
            )
        return text

    return Tool(
        name="read_documentation",
        description=(
            "Read one documentation file in full, or one section of it, by its "
            "repository-relative path (for example 'runbooks/RUNBOOK_AWS_SETUP.md' or "
            "'docs/COMPLIANCE.md'). Use after search_documentation when a returned "
            "excerpt is clearly the right document but you need the surrounding steps "
            "- particularly for runbook procedures, where giving an operator step 6 "
            "without its warnings would be worse than giving them nothing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path, as shown in search results.",
                },
                "section": {
                    "type": "string",
                    "description": "Optional heading to return instead of the whole file.",
                },
            },
            "required": ["path"],
        },
        handler=handler,
    )


def _list_tool(kb) -> Tool:
    return Tool(
        name="list_documentation",
        description=(
            "List every documentation file available. Useful when an operator asks "
            "what guidance exists, or to check whether this deployment documents a "
            "topic at all before answering from general knowledge."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=lambda: "\n".join(kb.documents) or "No documentation is loaded.",
    )


# ---------------------------------------------------------------------------
# Clinical tools - built ONLY when the deploying organisation has enabled a
# PHI access tier. See this module's docstring and core/assistant/config.py.
# ---------------------------------------------------------------------------

# One object's decrypted contents can be large, and a bundle under the
# `large` profile holds up to max_bundle_resources of them. Truncated
# rather than streamed whole into a request: the model is answering a
# question about a record, not ingesting one, and an unbounded tool
# result is an unbounded outbound payload.
_MAX_RECORD_CHARS = 40_000


@dataclass
class ClinicalAccess:
    """Everything a clinical tool needs, resolved by the caller.

    `record_read` is the audit callback and it MUST raise on failure -
    the tools below call it before fetching, so a raise is what stops an
    unlogged disclosure. `purpose` has already been validated against
    core/web/auth.py's PURPOSES_OF_USE by the caller.

    `patient_reference` / `storage_key` are the in-context binding: the
    record the user already has open, established server-side from the
    page they are on. At the `lookup` tier both are None and the tools
    accept arguments instead.
    """

    reader: Any
    record_read: Callable[[str, str], None]  # (action, resource_key)
    purpose: str
    tier: str
    patient_reference: Optional[str] = None
    storage_key: Optional[str] = None

    def permitted_keys(self) -> Optional[set[str]]:
        """Object keys this access may read, or None for no restriction.

        Computed from the index rather than trusted from the model. At
        the in-context tier this is the whole enforcement: a key outside
        the set is refused before anything is decrypted.
        """
        if self.tier != "in_context":
            return None
        keys: set[str] = set()
        if self.storage_key:
            keys.add(self.storage_key)
        if self.patient_reference:
            for row in self.reader.resources_for_patient(self.patient_reference):
                if row.get("storage_key"):
                    keys.add(row["storage_key"])
        return keys


def _summarise_rows(rows) -> list[dict]:
    """Index rows reduced to what is useful for answering a question."""
    return [
        {
            "resource_type": row.get("resource_type"),
            "resource_id": row.get("resource_id"),
            "storage_key": row.get("storage_key"),
            "stored_at": str(row.get("stored_at")) if row.get("stored_at") else None,
            "retention_until": (
                str(row.get("retention_until")) if row.get("retention_until") else None
            ),
        }
        for row in rows
    ]


def _read_object(access: ClinicalAccess, storage_key: str) -> str:
    """Audit, authorise, then decrypt. In that order, always."""
    permitted = access.permitted_keys()
    if permitted is not None and storage_key not in permitted:
        # Refused BEFORE the audit entry: nothing was disclosed, so there
        # is no disclosure to record. The attempt is worth noticing
        # though, which is why it is loud in the log.
        log.warning(
            "assistant refused a read of %s - outside the record the user has open",
            storage_key,
        )
        return (
            "That object is not part of the record currently open, so it cannot be "
            "read here. This deployment allows the assistant to read only what the "
            "user already has on screen."
        )

    row = access.reader.resource_index_row(storage_key)
    if row is None:
        return f"{storage_key} is not in the index."

    # Audit BEFORE decrypting, exactly as core/web/app.py does: recording
    # afterwards means a failed audit still leaves PHI decrypted in this
    # process, which is the unlogged access the trail exists to prevent.
    access.record_read("record.read", storage_key)

    resources = access.reader.read_resources(storage_key)
    text = json.dumps(resources, indent=2, default=str)
    if len(text) > _MAX_RECORD_CHARS:
        text = (
            text[:_MAX_RECORD_CHARS]
            + f"\n\n[truncated at {_MAX_RECORD_CHARS} characters - "
            f"{len(resources)} resources in this object]"
        )
    return text


def _grounding_value_sets():
    """The sensitive-category value sets for grounded retrieval, from
    PHI_AI_SENSITIVE_VALUE_SETS (a curated copy of
    config/sensitive_value_sets.example.yaml). Returns None when the
    deployment has not configured them — and then the grounded tool IS
    NOT BUILT, because grounded retrieval over an empty exclusion
    vocabulary would fail open (SPEC §6.1). Same absent-because-
    not-configured path as every other unbuilt tool. Re-read per
    toolbox build, not cached at import: a config fix must not require
    a process restart to take effect."""
    from core.config.settings import env_var

    path = env_var("SENSITIVE_VALUE_SETS")
    if not path:
        return None
    try:
        from pathlib import Path

        from core.terminology.loader import load_value_sets

        return load_value_sets(Path(path))
    except Exception as exc:
        # Loud, and still fail-closed: a malformed file disables the
        # grounded tool rather than running with partial exclusions.
        log.error("grounded retrieval disabled - value sets failed to load: %s", exc)
        return None


def _grounded_evidence(access: ClinicalAccess, value_sets, question: str,
                       patient_reference: str) -> str:
    """Audit-first bulk read of one patient's record, then the §5.1
    evidence pipeline (core/rag/pipeline.evidence_package). Reads are
    recorded per object BEFORE decryption, exactly as _read_object
    does; grounding a question is a broad disclosure and the audit
    trail says so honestly, one event per object."""
    from core.rag.pipeline import evidence_package
    from core.rag.retriever import GrantScope

    rows = access.reader.resources_for_patient(patient_reference)
    if not rows:
        return f"No stored resources for {patient_reference}."

    resources_by_key: dict[str, dict] = {}
    for row in rows:
        storage_key = row.get("storage_key")
        if not storage_key:
            continue
        access.record_read("record.read", storage_key)
        for i, resource in enumerate(access.reader.read_resources(storage_key)):
            if isinstance(resource, dict):
                resources_by_key[f"{storage_key}#{i}"] = resource

    return evidence_package(
        question,
        resources_by_key,
        scope=GrantScope(patient_reference=patient_reference),
        value_sets=value_sets,
    )


def _clinical_tools(access: ClinicalAccess, permitted) -> list[Tool]:
    tools: list[Tool] = []

    if access.tier == "in_context":
        target = access.storage_key or access.patient_reference or "the open record"

        if permitted("patient:read"):
            tools.append(
                Tool(
                    name="list_open_record",
                    description=(
                        "List what is stored for the record the user currently has "
                        "open: resource types, identifiers, storage dates and object "
                        "keys. This is the record already on their screen - you cannot "
                        "list anything else. Use it to find the object key of the "
                        "specific resource a question is about, then read that."
                    ),
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda: _summarise_rows(
                        access.reader.resources_for_patient(access.patient_reference)
                        if access.patient_reference
                        else [access.reader.resource_index_row(access.storage_key) or {}]
                    ),
                    required_permission="patient:read",
                    may_return_phi=True,
                )
            )
            tools.append(
                Tool(
                    name="read_open_record",
                    description=(
                        "Read the decrypted contents of one stored resource belonging "
                        f"to the record the user has open ({target}). Object keys come "
                        "from list_open_record. A key outside that record is refused. "
                        "Every read is recorded in the audit trail as a disclosure "
                        "against the user's name and their stated purpose of use, so "
                        "read what the question needs and not more."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "storage_key": {
                                "type": "string",
                                "description": "Object key, as returned by list_open_record.",
                            }
                        },
                        "required": ["storage_key"],
                    },
                    handler=lambda storage_key: _read_object(access, storage_key),
                    required_permission="patient:read",
                    may_return_phi=True,
                )
            )
            grounding = _grounding_value_sets()
            if grounding is not None and access.patient_reference:
                bound = access.patient_reference
                tools.append(
                    Tool(
                        name="grounded_patient_evidence",
                        description=(
                            "Retrieve ranked, citable evidence from the record the "
                            "user has open for a specific clinical question, plus a "
                            "complete structured summary for summary-shaped "
                            "questions. Every evidence line carries a [cite: ...] "
                            "key: every claim in your answer MUST cite one, content "
                            "marked REFUTED or ENTERED-IN-ERROR is recorded as NOT "
                            "true, and if the tool reports nothing responsive you "
                            "say so rather than answering from general knowledge. "
                            "Reads the whole open record, so every stored object is "
                            "recorded in the audit trail as a disclosure. "
                            "Differential-diagnosis requests are refused by design; "
                            "ask hypothesis-directed questions instead."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "The clinical question, verbatim.",
                                }
                            },
                            "required": ["question"],
                        },
                        handler=lambda question: _grounded_evidence(
                            access, grounding, question, bound
                        ),
                        required_permission="patient:read",
                        may_return_phi=True,
                    )
                )
        return tools

    # lookup tier
    if permitted("patient:search"):
        tools.append(
            Tool(
                name="find_patients",
                description=(
                    "Search the index by the source EMR's opaque patient identifier, "
                    "returning matching references with how many resources each has. "
                    "The index holds no names, medical record numbers or dates of "
                    "birth - searching for one will not work, and the identifier is "
                    "how a records request arrives in practice."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "All or part of the EMR patient identifier.",
                        }
                    },
                    "required": ["term"],
                },
                handler=lambda term: [
                    {
                        "patient_reference": row.get("patient_reference"),
                        "resource_count": row.get("resource_count"),
                        "last_stored": str(row.get("last_stored"))
                        if row.get("last_stored")
                        else None,
                    }
                    for row in access.reader.search_patients(term)
                ],
                required_permission="patient:search",
                may_return_phi=True,
            )
        )

    if permitted("patient:read"):
        tools.append(
            Tool(
                name="list_patient_records",
                description=(
                    "List what is stored for one patient reference: resource types, "
                    "identifiers, storage dates and object keys. Metadata only - use "
                    "read_record for contents."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "patient_reference": {
                            "type": "string",
                            "description": "e.g. Patient/eAB12cd3, from find_patients.",
                        }
                    },
                    "required": ["patient_reference"],
                },
                handler=lambda patient_reference: _summarise_rows(
                    access.reader.resources_for_patient(patient_reference)
                ),
                required_permission="patient:read",
                may_return_phi=True,
            )
        )
        tools.append(
            Tool(
                name="read_record",
                description=(
                    "Read the decrypted contents of one stored resource. Every read "
                    "is recorded in the audit trail as a disclosure against the user's "
                    "name and their stated purpose of use, and appears in an accounting "
                    "of disclosures. Read the specific resources the question needs "
                    "rather than sweeping a patient's whole record - the minimum "
                    "necessary standard applies to you as it does to the person asking."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "storage_key": {
                            "type": "string",
                            "description": "Object key, from list_patient_records.",
                        }
                    },
                    "required": ["storage_key"],
                },
                handler=lambda storage_key: _read_object(access, storage_key),
                required_permission="patient:read",
                may_return_phi=True,
            )
        )
        grounding = _grounding_value_sets()
        if grounding is not None:
            tools.append(
                Tool(
                    name="grounded_patient_evidence",
                    description=(
                        "Retrieve ranked, citable evidence from ONE patient's "
                        "record for a specific clinical question, plus a complete "
                        "structured summary for summary-shaped questions. Every "
                        "evidence line carries a [cite: ...] key: every claim in "
                        "your answer MUST cite one, content marked REFUTED or "
                        "ENTERED-IN-ERROR is recorded as NOT true, and if the tool "
                        "reports nothing responsive you say so rather than "
                        "answering from general knowledge. Reads the whole record, "
                        "so every stored object is recorded in the audit trail as "
                        "a disclosure - prefer read_record when the question is "
                        "about one known object. Differential-diagnosis requests "
                        "are refused by design; ask hypothesis-directed questions "
                        "instead."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The clinical question, verbatim.",
                            },
                            "patient_reference": {
                                "type": "string",
                                "description": "e.g. Patient/eAB12cd3, from find_patients.",
                            },
                        },
                        "required": ["question", "patient_reference"],
                    },
                    handler=lambda question, patient_reference: _grounded_evidence(
                        access, grounding, question, patient_reference
                    ),
                    required_permission="patient:read",
                    may_return_phi=True,
                )
            )
    return tools


def build(
    knowledge_base,
    *,
    settings=None,
    profile=None,
    assistant_settings=None,
    reader=None,
    capabilities: Optional[frozenset[str]] = UNRESTRICTED,
    clinical: Optional["ClinicalAccess"] = None,
    analytics: Optional["AnalyticsAccess"] = None,
    research: Optional["ResearchAccess"] = None,
) -> Toolbox:
    """Assemble the toolbox for one caller.

    `clinical` is present only when the deployment has enabled a PHI
    access tier AND the caller resolved a purpose of use for this
    request. Absent, no clinical tool is built - which is the default and
    the whole of the original design.

    `capabilities` is the set of permission strings the caller holds
    (core/web/auth.py's Identity.permissions()), or UNRESTRICTED for the
    CLI. `reader`, `settings` and `profile` may be None during
    installation, when there is nothing stored to report on yet - the
    documentation tools work regardless, which is the whole point of the
    assistant being usable before anything is deployed.
    """

    def permitted(permission: Optional[str]) -> bool:
        if permission is None or capabilities is UNRESTRICTED:
            return True
        return permission in capabilities

    tools: list[Tool] = [
        _search_tool(knowledge_base),
        _read_tool(knowledge_base),
        _list_tool(knowledge_base),
    ]

    if settings is not None and profile is not None and permitted("report:read"):
        tools.append(
            Tool(
                name="deployment_configuration",
                description=(
                    "Report how THIS deployment is configured: which cloud, which "
                    "scale profile and storage layout, the retention periods in "
                    "effect and where they came from, and which optional features "
                    "(Postgres index, OMOP analytics layer, psychotherapy-notes store, "
                    "Bulk Data Export) are switched on. Returns configuration shape "
                    "only - no bucket names, key identifiers or hostnames. Use this "
                    "before answering 'why isn't X working' or 'is X enabled', so the "
                    "answer describes this deployment rather than a general one."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=lambda: posture.configuration_posture(
                    settings, profile, assistant_settings
                ),
                required_permission="report:read",
            )
        )

    if reader is not None:
        if permitted("report:read"):
            tools.append(
                Tool(
                    name="platform_holdings",
                    description=(
                        "How much is currently stored: total resources, the number of "
                        "distinct patients, counts per FHIR resource type, and the "
                        "first and most recent storage timestamps. Aggregate counts "
                        "only - no patient identifiers of any kind. Use for questions "
                        "about whether ingestion is working, whether a resource type is "
                        "being captured, or whether the deployment has outgrown its "
                        "scale profile."
                    ),
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda: posture.platform_holdings(reader),
                    required_permission="report:read",
                )
            )
        if permitted("retention:read"):
            tools.append(
                Tool(
                    name="retention_outlook",
                    description=(
                        "How many stored resources reach their retain-until date "
                        "within a given window, broken down by resource type, and how "
                        "many are already past it. Counts only - never the individual "
                        "records, which identify patients. Use for disposition planning "
                        "questions."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "within_days": {
                                "type": "integer",
                                "description": "Window in days (1-3650, default 90).",
                            }
                        },
                    },
                    handler=lambda within_days=90: posture.retention_outlook(
                        reader, within_days=within_days
                    ),
                    required_permission="retention:read",
                )
            )
        if permitted("audit:verify"):
            tools.append(
                Tool(
                    name="audit_chain_status",
                    description=(
                        "Verify the tamper-evident audit chain and report whether it is "
                        "intact, how many events were checked, and what is wrong if it "
                        "is not. Returns the verdict only, never the audit entries "
                        "themselves - those name users and the records they opened."
                    ),
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda: posture.audit_chain_status(reader),
                    required_permission="audit:verify",
                )
            )

    if clinical is not None:
        tools.extend(_clinical_tools(clinical, permitted))

    # Population analytics. Independent of the clinical tiers above: an
    # analyst counting cohorts needs no patient:read, and a clinician
    # reading one chart needs no analytics:query. Gated by its own
    # permissions and its own database roles.
    if analytics is not None:
        tools.extend(_analytics_tools(analytics, permitted))

    # Cross-record research. Independent of both blocks above and gated
    # by its own permissions and its own database roles - see
    # ResearchAccess below and core/db/retrieval_schema.sql's header.
    if research is not None:
        tools.extend(_research_tools(research, permitted))

    return Toolbox(tools)


# ---------------------------------------------------------------------------
# Population analytics - built ONLY when the OMOP layer and a read-only
# analyst role are configured. See core/analytics/ and
# core/db/omop_bootstrap_aws.sql.
# ---------------------------------------------------------------------------


@dataclass
class AnalyticsAccess:
    """Read-only connections for population questions.

    Two factories, not one, and they are deliberately separate. `analytics`
    connects as omop_analyst (cohort counts over cdm); `identity` connects
    as the identity-search role (names). An organisation can enable either
    without the other, because counting patients with a condition and
    finding out who they are are different privileges - see
    core/db/identity_schema.sql, whose header is worth reading before
    enabling name search at all.

    `record_query` audits before anything runs. A population query reads
    identified PHI in aggregate, and an accounting that recorded chart
    views but not "who asked which cohort question" would be describing
    only half of what the system does.
    """

    analytics_connection: Optional[Callable[[], Any]] = None
    identity_connection: Optional[Callable[[], Any]] = None
    record_query: Optional[Callable[[str, str], None]] = None  # (action, detail)
    purpose: Optional[str] = None
    row_limit: int = 500
    timeout_ms: int = 15_000
    # Whether birth dates come back from a name search. Narrowing BY one
    # is always allowed; returning it is what turns a disambiguation aid
    # into a demographic extract.
    include_birth_date: bool = True

    def audit(self, action: str, detail: str) -> None:
        if self.record_query is not None:
            self.record_query(action, detail)


def _analytics_tools(access: AnalyticsAccess, permitted) -> list[Tool]:
    tools: list[Tool] = []

    def with_analytics(fn):
        """Open, use, close. Connections are per call rather than held:
        an assistant session can idle for a long time between questions,
        and a pinned Postgres connection per idle user is how a small
        connection pool disappears."""
        conn = access.analytics_connection()
        try:
            return fn(conn)
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    if access.analytics_connection is not None and permitted("analytics:query"):
        from core.analytics import cohort, sql_guard

        tools.append(
            Tool(
                name="platform_population",
                description=(
                    "Describe the population this deployment holds: how many patients, "
                    "the spread of birth years, the breakdown by administrative gender, "
                    "and the number and date range of visits. Aggregate counts only. "
                    "Use this first when someone asks what is in the platform, or to "
                    "get a denominator before reporting any other count."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=lambda: (
                    access.audit("analytics.population", "population demographics"),
                    with_analytics(cohort.population_demographics),
                )[1],
                required_permission="analytics:query",
                may_return_phi=True,
            )
        )
        tools.append(
            Tool(
                name="count_patients_with_condition",
                description=(
                    "Count DISTINCT patients with a given condition recorded. Accepts a "
                    "plain-English name for common conditions (diabetes, hypertension, "
                    "asthma, COPD, heart failure, atrial fibrillation, chronic kidney "
                    "disease, depression, anxiety, obesity, breast/lung/colorectal "
                    "cancer, stroke, myocardial infarction, dementia, pregnancy) or an "
                    "ICD-10 code prefix such as 'E11' or 'I50'. Optionally bounded by "
                    "diagnosis date.\n\n"
                    "Returns the count, the deployment's total patient count as a "
                    "denominator, exactly what it matched on, and caveats. Report the "
                    "caveats - they say whether the standard vocabulary was available "
                    "and what a count does and does not include. This counts patients, "
                    "not diagnoses: someone diagnosed at four visits counts once."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "condition": {
                            "type": "string",
                            "description": "Condition name or ICD-10 code prefix.",
                        },
                        "since": {"type": "string", "description": "ISO date lower bound."},
                        "until": {"type": "string", "description": "ISO date upper bound."},
                    },
                    "required": ["condition"],
                },
                handler=lambda condition, since=None, until=None: (
                    access.audit("analytics.cohort", f"condition={condition}"),
                    with_analytics(
                        lambda c: cohort.count_patients_with_condition(
                            c, condition, since=since, until=until
                        ).to_dict()
                    ),
                )[1],
                required_permission="analytics:query",
                may_return_phi=True,
            )
        )
        tools.append(
            Tool(
                name="count_patients_by_facility",
                description=(
                    "How many DISTINCT patients were seen at each facility, or at one "
                    "named facility. Match a facility by name substring or by its "
                    "source organisation id; omit it for the full breakdown. "
                    "Optionally bounded by visit date.\n\n"
                    "A patient seen at two facilities appears once in the headline "
                    "figure and once per facility in the breakdown, so the rows do not "
                    "sum to the total - say so if you report both."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "facility": {"type": "string", "description": "Name or id. Omit for all."},
                        "since": {"type": "string", "description": "ISO date lower bound."},
                        "until": {"type": "string", "description": "ISO date upper bound."},
                    },
                },
                handler=lambda facility=None, since=None, until=None: (
                    access.audit("analytics.cohort", f"facility={facility or 'all'}"),
                    with_analytics(
                        lambda c: cohort.count_patients_by_facility(
                            c, facility=facility, since=since, until=until
                        ).to_dict()
                    ),
                )[1],
                required_permission="analytics:query",
                may_return_phi=True,
            )
        )
        tools.append(
            Tool(
                name="list_facilities",
                description=(
                    "List the facilities represented in this deployment with patient "
                    "and visit counts. Use to find the right facility name before "
                    "counting, and when someone asks where the records came from."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=lambda: (
                    access.audit("analytics.facilities", "list"),
                    with_analytics(cohort.list_facilities),
                )[1],
                required_permission="analytics:query",
                may_return_phi=True,
            )
        )
        tools.append(
            Tool(
                name="run_analytics_query",
                description=(
                    "Run a read-only SQL SELECT against the OMOP analytics layer, for "
                    "population questions the other tools do not cover. Use a curated "
                    "tool when one fits - they are reviewed, and they get "
                    "COUNT(DISTINCT person_id) right.\n\n"
                    "Schema: cdm.person (person_id, year_of_birth, gender_source_value, "
                    "person_source_value), cdm.visit_occurrence (person_id, "
                    "visit_start_date, care_site_id, visit_concept_id), "
                    "cdm.condition_occurrence (person_id, condition_start_date, "
                    "condition_source_value = the ICD-10/SNOMED code, "
                    "condition_concept_id), cdm.procedure_occurrence, "
                    "cdm.drug_exposure, cdm.measurement, cdm.observation, "
                    "cdm.care_site (care_site_id, care_site_name), and vocab.concept "
                    "(concept_id, concept_name) when the vocabulary is loaded. Every "
                    "clinical table has person_id and a source_storage_key.\n\n"
                    "Count patients with COUNT(DISTINCT person_id), not COUNT(*) - the "
                    "event tables hold one row per occurrence, so COUNT(*) answers a "
                    "different question and overcounts. Results are capped and the "
                    "query is recorded in the audit trail verbatim."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "A single SELECT (or WITH ... SELECT). No semicolons.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "What question this answers, recorded in the audit trail.",
                        },
                    },
                    "required": ["sql", "rationale"],
                },
                handler=lambda sql, rationale="": _run_query(access, sql, rationale),
                required_permission="analytics:query",
                may_return_phi=True,
            )
        )

    if access.identity_connection is not None and permitted("identity:search"):
        from core.analytics import identity as identity_search

        def find_by_name(name: str, birth_date: Optional[str] = None):
            access.audit("record.search.name", f"name~{name}")
            conn = access.identity_connection()
            try:
                result = identity_search.search(conn, name, birth_date=birth_date)
            finally:
                try:
                    conn.close()
                except Exception:  # pragma: no cover
                    pass
            return result.to_dict(include_birth_date=access.include_birth_date)

        tools.append(
            Tool(
                name="find_patients_by_name",
                description=(
                    "Find patients by name. Accepts 'mary smith' or 'smith, mary', "
                    "matches case- and accent-insensitively, and falls back to fuzzy "
                    "matching for misspellings. Optionally narrow by date of birth, "
                    "which is how two people with the same name are told apart.\n\n"
                    "Returns each patient's opaque EMR reference - that is what every "
                    "other tool and every page in the platform keys on. Results are "
                    "capped; if the result says it was truncated, ask the person for a "
                    "date of birth rather than reporting a partial list as if it were "
                    "complete. Each match says how it was found, so an exact match can "
                    "be distinguished from a fuzzy one."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "All or part of the name."},
                        "birth_date": {
                            "type": "string",
                            "description": "ISO date, to disambiguate.",
                        },
                    },
                    "required": ["name"],
                },
                handler=find_by_name,
                required_permission="identity:search",
                may_return_phi=True,
            )
        )

    return tools


def _run_query(access: "AnalyticsAccess", sql: str, rationale: str):
    """Guard, audit, then execute. In that order.

    The audit entry carries the query VERBATIM. That is the whole point of
    allowing generated SQL at all: an organisation reviewing what the
    assistant did can read the exact statement rather than infer it from a
    tool name and some arguments.
    """
    from core.analytics import sql_guard

    try:
        guarded = sql_guard.guard(sql, row_limit=access.row_limit)
    except sql_guard.UnsafeQuery as exc:
        # Refused before the audit entry: nothing ran, so nothing was
        # read, so there is no access to record.
        return f"Query refused: {exc}"

    access.audit("analytics.query", f"{rationale or 'no rationale given'} :: {guarded.original}")

    conn = access.analytics_connection()
    try:
        columns, rows = sql_guard.execute(conn, guarded, timeout_ms=access.timeout_ms)
    except Exception as exc:
        return (
            f"The query failed: {exc}\n\n"
            "If this is a permission error, the table is outside the analytics grant - "
            "the connection can read only the cdm and vocab schemas."
        )
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) >= guarded.row_limit,
        "sql_executed": guarded.original,
    }


# ---------------------------------------------------------------------------
# Cross-record research - built ONLY when the clinical retrieval index
# and its read role are configured (core/db/retrieval_schema.sql,
# retrieval_bootstrap_<cloud>.sql) AND the deployment enabled the lookup
# PHI tier. Search results are snippets of clinical text, so everything
# here is PHI-bearing and audited before it runs.
# ---------------------------------------------------------------------------


@dataclass
class ResearchAccess:
    """Connections and audit plumbing for cross-record text search.

    Three optional capabilities, deliberately separable, mirroring the
    three database roles:

      `search_connection`          the general clinical text search -
                                   requires the `research:search`
                                   permission (researcher role only)
      `psychotherapy_connection`   psychotherapy note search - a
                                   separate table behind a separate
                                   role, requires `psychotherapy:read`
      `read_psychotherapy`         decrypts one psychotherapy note from
                                   its own bucket, same permission

    `record` audits BEFORE anything runs - the query text verbatim for
    searches (the guarded-SQL tool's contract), the storage key for
    reads, always ahead of any decrypt (core/web/app.py's ordering).
    `purpose` is the caller's validated purpose of use, already stamped
    into every audit entry by the recorder the web layer builds.
    """

    search_connection: Optional[Callable[[], Any]] = None
    psychotherapy_connection: Optional[Callable[[], Any]] = None
    read_psychotherapy: Optional[Callable[[str], Any]] = None
    record: Optional[Callable[[str, str], None]] = None  # (action, detail)
    purpose: Optional[str] = None

    def audit(self, action: str, detail: str) -> None:
        # Fails closed by construction: the web layer's recorder raises
        # when no audit sink is configured, and every handler below
        # calls this before touching a connection or a key.
        if self.record is not None:
            self.record(action, detail)


def _with_connection(factory, fn):
    """Open, use, close - the same per-call connection discipline
    _analytics_tools uses, for the same pool-exhaustion reason."""
    conn = factory()
    try:
        return fn(conn)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


def _format_search_results(rows: list[dict]) -> Any:
    if not rows:
        return (
            "No indexed text matched. The index holds extracted prose only - "
            "identifiers, names and codes are deliberately not searchable here "
            "(find_patients and the identity tools cover those). Try clinical "
            "wording, quoted phrases, or fewer terms."
        )
    return [
        {
            "storage_key": row.get("storage_key"),
            "patient_reference": row.get("patient_reference"),
            "resource_type": row.get("resource_type"),
            "resource_id": row.get("resource_id"),
            "clinical_date": str(row.get("clinical_date")) if row.get("clinical_date") else None,
            "snippet": row.get("snippet"),
        }
        for row in rows
    ]


def _research_tools(access: "ResearchAccess", permitted) -> list[Tool]:
    from core.db import retrieval

    tools: list[Tool] = []

    if access.search_connection is not None and permitted("research:search"):
        def search_clinical(query: str, patient_reference: str = "",
                            resource_type: str = "", limit: int = 10):
            # Audited verbatim BEFORE the search runs - the query string
            # is the disclosure decision, exactly as the guarded SQL
            # tool records its SQL.
            access.audit("research.search", query)
            return _format_search_results(
                _with_connection(
                    access.search_connection,
                    lambda conn: retrieval.search_clinical(
                        conn, query,
                        patient_reference=patient_reference or None,
                        resource_type=resource_type or None,
                        limit=limit,
                    ),
                )
            )

        tools.append(
            Tool(
                name="search_clinical_records",
                description=(
                    "Search the extracted text of every indexed clinical record - "
                    "conditions, notes, document titles, narratives - by clinical "
                    "language. Supports quoted phrases, OR, and -exclusions. Returns "
                    "matching snippets with the patient reference and storage key of "
                    "each source; open the full record with read_record, which is "
                    "audited as a disclosure. Every search is itself recorded in the "
                    "audit trail verbatim. The index holds prose only: searching for "
                    "names, identifiers or codes will not work, by design. "
                    "Psychotherapy notes are never in this index."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Clinical-language search, websearch syntax."},
                        "patient_reference": {"type": "string",
                                              "description": "Optional: restrict to one patient."},
                        "resource_type": {"type": "string",
                                          "description": "Optional: restrict to one FHIR type."},
                        "limit": {"type": "integer",
                                  "description": "Max results, 1-50 (default 10)."},
                    },
                    "required": ["query"],
                },
                handler=search_clinical,
                required_permission="research:search",
                may_return_phi=True,
            )
        )

    if access.psychotherapy_connection is not None and permitted("psychotherapy:read"):
        def search_psych(query: str, limit: int = 10):
            access.audit("psychotherapy.search", query)
            return _format_search_results(
                _with_connection(
                    access.psychotherapy_connection,
                    lambda conn: retrieval.search_psychotherapy(conn, query, limit=limit),
                )
            )

        tools.append(
            Tool(
                name="search_psychotherapy_notes",
                description=(
                    "Search the separately-stored psychotherapy notes index. This is "
                    "the record class 45 CFR 164.508(a)(2) requires authorization "
                    "for; it is available to you only because this deployment "
                    "explicitly enabled it and this user holds the psychotherapy "
                    "role. Every search is recorded in the audit trail verbatim as a "
                    "disclosure-related event. Search only what the question "
                    "genuinely requires."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer",
                                  "description": "Max results, 1-50 (default 10)."},
                    },
                    "required": ["query"],
                },
                handler=search_psych,
                required_permission="psychotherapy:read",
                may_return_phi=True,
            )
        )

    if access.read_psychotherapy is not None and permitted("psychotherapy:read"):
        def read_psych(storage_key: str):
            # Audit BEFORE decrypting - core/web/app.py's ordering, and
            # nowhere does it matter more than on this store.
            access.audit("psychotherapy.read", storage_key)
            resources = access.read_psychotherapy(storage_key)
            text = json.dumps(resources, indent=2, default=str)
            if len(text) > _MAX_RECORD_CHARS:
                text = text[:_MAX_RECORD_CHARS] + "\n\n[truncated]"
            return text

        tools.append(
            Tool(
                name="read_psychotherapy_note",
                description=(
                    "Read the decrypted contents of one psychotherapy note, by the "
                    "storage key search_psychotherapy_notes returned. Recorded in "
                    "the audit trail as a disclosure against the user's name and "
                    "stated purpose BEFORE the note is decrypted. The minimum "
                    "necessary standard applies with particular force here: read "
                    "the specific note the question needs, never sweep the store."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"storage_key": {"type": "string"}},
                    "required": ["storage_key"],
                },
                handler=read_psych,
                required_permission="psychotherapy:read",
                may_return_phi=True,
            )
        )

    return tools
# Made by Ryan Gomez & Co. Inc.
