# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The assistant: the conversation, the ask, the thumbs, and the ops page.

MOVED OUT OF core/web/app.py's create_app(), UNCHANGED - the largest of
the four sections taken out of it. See core/web/roi_routes.py's header for
why that function was broken up.

THIS SECTION IS WHY THE SPLIT WAS WORTH DOING. Five routes and seven
helpers, 527 lines, and every one of them a closure - so the per-role
access builders below (_clinical_access, _analytics_access,
_research_access), which decide what the assistant may read on a caller's
behalf, could not be reached by a test without standing up the entire
application. They are the tightest permission logic in the project and
they were the hardest code in it to exercise.

record_actor is passed in alongside record because the assistant writes
audit entries for reads it performs ITSELF, on the caller's behalf, and
those name the tool rather than a signed-in identity.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.assistant import telemetry as assistant_telemetry
from core.assistant.conversations import Turn
from core.assistant.tools import (
    AnalyticsAccess as AssistantAnalyticsAccess,
    ClinicalAccess as AssistantClinicalAccess,
    ResearchAccess as AssistantResearchAccess,
)
from core.web.assistant_pages import (
    back_label,
    describe as describe_page,
    safe_return_path,
)
from core.web.auth import (
    Identity,
    NotAuthorized,
    PURPOSES_OF_USE,
    purpose_allowed,
    validate_purpose,
)

log = logging.getLogger("phi-ai.web.assistant")

#: Session key holding the id of this user's live assistant conversation.
#: The conversation itself lives in worker memory, never in the cookie - a
#: transcript would not fit in one, and a signed cookie is not where text a
#: user typed belongs.
_ASSISTANT_KEY = "assistant_conversation"


def register(app, page, require, current_identity, record, reader, record_actor) -> None:
    """Attach the assistant screens to `app`."""

    def _control_panel_overrides() -> dict:
        """The Control panel's live model settings, or nothing.

        Returned as strings exactly as stored; AssistantSettings.with_overrides
        decides what "" and a malformed number mean, so that judgement lives in
        one place rather than here and there.
        """
        state = getattr(app.state, "platform_state", None)
        if state is None:
            return {}
        return {
            "model": state.config_get("assistant_model"),
            "max_tokens": state.config_get("assistant_max_tokens"),
        }

    # Published so an entrypoint can arm a runtime the moment it installs
    # one - core/web/__main__.py does, right after building it. The lazy
    # arming in assistant_runtime() below is the backstop for every other
    # caller (tests, an embedder swapping the runtime at runtime), so the
    # connection does not depend on which page a deployment is asked for
    # first.
    app.state.assistant_overrides = _control_panel_overrides

    def assistant_runtime():
        rt = getattr(app.state, "assistant", None)
        if rt is None:
            raise HTTPException(
                status_code=503,
                detail="The assistant is not enabled in this deployment. It is an "
                "optional add-on - see runbooks/RUNBOOK_AI_ASSISTANT.md.",
            )

        # THE CONTROL PANEL'S TWO MODEL SETTINGS, CONNECTED TO THE MODEL
        # CALL. Until this existed, `assistant_model` and
        # `assistant_max_tokens` were written by /system/control/config,
        # persisted, audit-logged as `config.changed`, rendered back into
        # the form - and read by nothing. An operator could switch the
        # foundation model, watch the tick move, and the assistant would go
        # on calling whatever PHI_AI_ASSISTANT_MODEL said. The two switches
        # beside them (PHI RAG, live calls) were wired, which made it worse:
        # a panel that is right about two controls teaches you to trust the
        # other two.
        #
        # ATTACHED HERE, NOT AT THE END OF create_app, because the runtime
        # does not exist yet when this function is defined - the entrypoint
        # sets app.state.assistant AFTER create_app returns (and a test may
        # set it later still, or swap it). Doing it on the accessor every
        # route already calls means whatever runtime is in place gets the
        # hook, rather than only one that happened to exist at build time.
        # Idempotent, and never overrides a hook a caller set deliberately.
        if getattr(rt, "live_overrides", None) is None:
            rt.live_overrides = _control_panel_overrides
        return rt

    def _assistant_conversation(request: Request, identity: Identity, rt):
        """This user's live conversation, resumed or started.

        The id lives in the signed session cookie and the conversation
        itself in worker memory, so it expires on the same clock as the
        identity that owns it - see core/assistant/conversations.py.
        """
        store = rt.conversations
        conversation = store.get(request.session.get(_ASSISTANT_KEY), identity.username)
        if conversation is None:
            conversation = store.create(identity.username)
            request.session[_ASSISTANT_KEY] = conversation.id
        return conversation

    def _assistant_page(request, identity, conversation, **context):
        rt = assistant_runtime()
        prompts = getattr(app.state, "prompts", None)
        return page(
            request,
            "assistant.html",
            identity,
            conversation=conversation,
            destination=(
                f"{rt.settings.provider}, inside your organisation's own cloud account"
                if rt.settings.stays_in_org_cloud
                else "the Anthropic API, outside this deployment's cloud account"
            ),
            phi_access=rt.settings.phi_access,
            # Prompt history and saved prompts - this user's only, empty
            # when the store is unconfigured so the rail sections do not
            # render at all.
            saved_prompts=prompts.saved(identity.username) if prompts else [],
            recent_prompts=prompts.recent(identity.username) if prompts else [],
            prompts_enabled=prompts is not None,
            **context,
        )

    def _clinical_access(identity: Identity, rt, purpose, patient_reference, storage_key):
        """Resolve clinical tool access for one request, or None.

        Returns None - meaning documentation and aggregates only - unless
        ALL of the following hold. Each is a separate reason, and each is
        reported to the user rather than failing silently:

          - the deployment enabled a PHI tier (config, the org's decision)
          - the caller stated a valid purpose of use
          - at the in-context tier, the page supplied a record to bind to

        The audit callback is core/web/app.py's own record(), so a
        clinical read made through the assistant produces byte-identical
        audit entries to one made by clicking - which is the property that
        keeps an accounting of disclosures correct.
        """
        if not rt.settings.reads_clinical_content:
            return None, None
        if rt.reader is None:
            return None, "the record index is not configured, so records cannot be read"
        try:
            resolved_purpose = validate_purpose(purpose)
        except NotAuthorized:
            return None, (
                "no purpose of use was stated, so the assistant answered without "
                "reading any records. Choose one to let it read."
            )

        if rt.settings.allows_lookup:
            bound_patient, bound_key = None, None
        else:
            bound_patient, bound_key = patient_reference, storage_key
            if not (bound_patient or bound_key):
                return None, (
                    "this deployment lets the assistant read only the record you "
                    "already have open, and this question was not asked from one"
                )

        def record_read(action: str, resource_key: str) -> None:
            # Fails closed: record() raises when no audit sink is
            # configured, and the tool calls this BEFORE decrypting.
            record(identity, action, resource_key, resolved_purpose)

        return (
            AssistantClinicalAccess(
                reader=rt.reader,
                record_read=record_read,
                purpose=resolved_purpose,
                tier=rt.settings.phi_access,
                patient_reference=bound_patient,
                storage_key=bound_key,
            ),
            None,
        )

    def _analytics_access(identity: Identity, rt, purpose):
        """Population-query access for one request, or None.

        Deliberately NOT gated on the PHI tier that governs record
        reading. They are different questions with different answers: an
        analyst counting cohorts has no business opening a chart, and a
        clinician reading one chart has no business running population
        queries. Each is gated by its own permission and its own database
        role, so an organisation can enable either alone.

        A purpose of use is NOT required to reach these tools, and that is
        a deliberate difference from clinical reads. A cohort count
        discloses no individual, so demanding a per-question purpose would
        be ceremony; the query itself is what gets audited, verbatim, and
        that is the record worth keeping. Name search DOES identify people
        and is permissioned separately for exactly that reason.
        """
        if rt.analytics_connection is None and rt.identity_connection is None:
            return None
        if not (identity.can("analytics:query") or identity.can("identity:search")):
            return None

        def record_query(action: str, detail: str) -> None:
            record(identity, action, detail, purpose or "operations")

        return AssistantAnalyticsAccess(
            analytics_connection=rt.analytics_connection,
            identity_connection=rt.identity_connection,
            record_query=record_query,
            purpose=purpose,
        )

    def _research_access(identity: Identity, rt, purpose):
        """Cross-record research access for one request, or None.

        Search snippets ARE clinical text, so unlike the analytics
        gate above this one demands everything a clinical read demands:
        the lookup tier (searching every chart at once has no in-context
        analogue) and a validated purpose of use - the `research` code
        is what a researcher normally states. On top of that, each piece
        follows its own permission: general search appears only for
        `research:search` (the researcher role), the psychotherapy
        pieces only for `psychotherapy:read` AND only where the
        deployment's own psychotherapy gate is on
        (core/assistant/config.py). The audit callback is the same
        record() every other path uses, so a search or note read made
        through the assistant is indistinguishable in the trail from
        one that could have been made by hand.
        """
        if not rt.settings.allows_lookup:
            return None
        wants_search = (
            rt.research_search_connection is not None
            and identity.can("research:search")
        )
        wants_psych = (
            rt.settings.psychotherapy_access
            and identity.can("psychotherapy:read")
            and (
                rt.psychotherapy_search_connection is not None
                or rt.psychotherapy_reader is not None
            )
        )
        if not (wants_search or wants_psych):
            return None
        try:
            resolved_purpose = validate_purpose(purpose)
        except NotAuthorized:
            return None

        def record_research(action: str, detail: str) -> None:
            # Fails closed, before any search runs or any note is
            # decrypted - record() raises when no audit sink exists.
            record(identity, action, detail, resolved_purpose)

        return AssistantResearchAccess(
            search_connection=rt.research_search_connection if wants_search else None,
            psychotherapy_connection=(
                rt.psychotherapy_search_connection if wants_psych else None
            ),
            read_psychotherapy=rt.psychotherapy_reader if wants_psych else None,
            record=record_research,
            purpose=resolved_purpose,
        )

    @app.get("/assistant", response_class=HTMLResponse)
    def assistant_view(request: Request, identity: Identity = Depends(current_identity)):
        require(identity, "assistant:use")
        rt = assistant_runtime()
        conversation = _assistant_conversation(request, identity, rt)

        # ?prefill=<row id>: put a history/saved prompt INTO the composer
        # so the person can edit and explicitly press Ask. A click must
        # never fire the model by itself - a model call costs seconds,
        # money, and an audit entry, and none of those belong on a
        # single unconfirmed click. The id is an opaque integer (never
        # the prompt text) so nothing sensitive rides in the URL, and
        # the lookup is scoped to the caller's own rows.
        draft = ""
        prefill = request.query_params.get("prefill")
        prompts = getattr(app.state, "prompts", None)
        if prefill and prompts is not None:
            try:
                wanted = int(prefill)
            except ValueError:
                wanted = None
            if wanted is not None:
                for row in prompts.saved(identity.username) + prompts.recent(identity.username):
                    if row["id"] == wanted:
                        draft = row["prompt"]
                        break

        return _assistant_page(
            request, identity, conversation, error=None, note=None,
            back=None, back_to=None, draft=draft,
        )

    @app.post("/assistant", response_class=HTMLResponse)
    def assistant_ask(
        request: Request,
        question: str = Form(""),
        page_key: Optional[str] = Form(None),
        back: Optional[str] = Form(None),
        action: str = Form("ask"),
        purpose_of_use: Optional[str] = Form(None),
        context_patient: Optional[str] = Form(None),
        context_key: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        """Continue this user's conversation with the assistant.

        POST, like every other form here, and for the same reason: a
        question is free text a user typed, and free text does not belong
        in a URL that reaches proxy logs and browser history. The answer
        is rendered in the response rather than redirected to, so no part
        of it appears in a query string either.

        `page_key` says which page the question was asked FROM. It is a
        key into a server-side table of phrases, never a URL - see
        core/web/assistant_pages.py for why that distinction is the whole
        design of this parameter. `back` is validated as a same-origin
        path before it is ever rendered as a link.

        `context_patient` / `context_key` name the record the user
        already has open, and matter only where the deployment enabled
        the in-context PHI tier. They are a REQUEST for access, not a
        grant of it: core/assistant/tools.py re-derives the permitted
        object keys from the index and refuses anything outside them, so
        a forged value widens nothing.
        """
        require(identity, "assistant:use")
        rt = assistant_runtime()

        return_path = safe_return_path(back)
        conversation = _assistant_conversation(request, identity, rt)

        if action == "clear":
            rt.conversations.discard(conversation.id)
            conversation = rt.conversations.create(identity.username)
            request.session[_ASSISTANT_KEY] = conversation.id
            return _assistant_page(
                request, identity, conversation, error=None, note=None,
                back=return_path, back_to=back_label(return_path),
            )

        clinical, clinical_note = _clinical_access(
            identity, rt, purpose_of_use, context_patient, context_key
        )
        analytics = _analytics_access(identity, rt, purpose_of_use)
        research = _research_access(identity, rt, purpose_of_use)

        # Built per request with the caller's CURRENT permissions, seeded
        # with the conversation so far. A role that changed between
        # questions takes effect on the next one rather than being frozen
        # into a long-lived object - see core/assistant/tools.py.
        session = rt.session_for(
            actor=identity.username,
            capabilities=identity.permissions(),
            audit=app.state.audit,
            require_audit=True,
            history=conversation.messages,
            turn_starts=conversation.turn_starts,
            clinical=clinical,
            analytics=analytics,
            research=research,
        )

        # Prompt history: recorded on the attempt, not the outcome - a
        # prompt that errored is exactly the one worth re-running.
        # Best-effort by construction (see core/web/prompt_store.py);
        # the audit entry the session writes is the record of use.
        if question.strip() and getattr(app.state, "prompts", None):
            app.state.prompts.record(identity.username, question, page_key)

        error = None
        reply = None
        asked_at = time.monotonic()
        # The Control panel's switches, honored before any model call.
        # Both refusals are stated in place; the question was already
        # recorded, and the audit entry the session writes still gates
        # actual reads.
        pstate = getattr(app.state, "platform_state", None)
        if pstate is not None and pstate.config_get("rag_enabled", "on") != "on":
            error = ("Retrieval is DISABLED by the System Administrator "
                     "(Control panel · PHI RAG). The assistant cannot read "
                     "the platform's stores while it is off, and it will not "
                     "answer questions about records without reading them.")
        elif pstate is not None and pstate.config_get("assistant_live", "on") != "on":
            error = ("Live model calls are switched OFF by the System "
                     "Administrator (Control panel · foundation model). No "
                     "request left for the model; switch it back on to ask.")
        try:
            if error is None:
                reply = session.ask(question, page_context=describe_page(page_key))
        except RuntimeError as exc:
            # Audit logging unavailable. The question was not sent; say so
            # in place rather than losing the page to a 503.
            error = str(exc)
        except Exception as exc:
            log.error("assistant request failed: %s", exc)
            error = "The assistant is currently unavailable. The question was not answered."
        else:
            if reply is not None:
                conversation.messages, conversation.turn_starts = session.export_history()
                conversation.record(
                    Turn(
                        question=question.strip(),
                        answer=reply.text,
                        sources=reply.sources,
                        refused=reply.refused,
                    ),
                    max_turns=rt.conversations.max_turns,
                )

        # Telemetry: metrics only, never the question or the answer
        # (core/db/telemetry_schema.sql's header is the contract).
        # Fire-and-forget by construction - record_interaction() cannot
        # raise - and skipped entirely when the ops role is unconfigured.
        assistant_telemetry.record_interaction(
            rt.ops_connection,
            username=identity.username,
            roles=",".join(sorted(r.value for r in identity.roles)),
            page_key=page_key or None,
            provider=rt.settings.provider,
            # The EFFECTIVE model, not the environment's: an operator can
            # change it from the Control panel between two questions, and a
            # telemetry row naming the wrong model is worse than none.
            model=rt.effective_settings().resolved_model,
            latency_ms=int((time.monotonic() - asked_at) * 1000),
            input_tokens=reply.input_tokens if reply else 0,
            output_tokens=reply.output_tokens if reply else 0,
            tool_calls=len(reply.tools_used) if reply else 0,
            tools_used=",".join(reply.tools_used) if reply else "",
            phi_reads=reply.phi_reads if reply else 0,
            refused=bool(reply and reply.refused),
            truncated=bool(reply and reply.truncated),
            error=error is not None,
        )

        return _assistant_page(
            request, identity, conversation, error=error, note=clinical_note,
            back=return_path, back_to=back_label(return_path),
        )

    @app.post("/assistant/prompts", response_class=HTMLResponse)
    def assistant_prompt_action(
        request: Request,
        prompt_id: int = Form(...),
        prompt_action: str = Form(...),
        label: Optional[str] = Form(None),
        identity: Identity = Depends(current_identity),
    ):
        """Save, unsave or delete one of the caller's own prompts.

        The store scopes every statement to the caller's username, so a
        forged prompt_id belonging to someone else updates zero rows -
        the same quiet non-result an expired id gets. Nothing here needs
        auditing: these are bookmarks over text the audit trail already
        holds (see core/db/prompts_schema.sql).
        """
        require(identity, "assistant:use")
        prompts = getattr(app.state, "prompts", None)
        if prompts is None:
            raise HTTPException(status_code=404, detail="prompt history is not configured")
        if prompt_action == "save":
            prompts.save(identity.username, prompt_id, label)
        elif prompt_action == "unsave":
            prompts.unsave(identity.username, prompt_id)
        elif prompt_action == "delete":
            prompts.delete(identity.username, prompt_id)
        else:
            raise HTTPException(status_code=400, detail="unknown prompt action")
        return RedirectResponse("/assistant", status_code=303)

    @app.post("/assistant/feedback", response_class=HTMLResponse)
    def assistant_feedback(
        request: Request,
        turn: int = Form(...),
        vote: str = Form(...),
        identity: Identity = Depends(current_identity),
    ):
        """A thumb on one answer in this user's own conversation.

        The turn is named by its position, never by content, and the
        vote is the only thing recorded - up or down, which turn, and
        whether that answer was a refusal - beside the usage telemetry
        (core/assistant/telemetry.py, record_feedback). It is the
        cheapest direct quality signal the assistant can collect, and
        the one an evaluation rubric is calibrated against. Renders the
        conversation again with the verdict shown, so the person sees it
        was kept; a vote on a turn that is no longer there is ignored.
        """
        require(identity, "assistant:use")
        rt = assistant_runtime()
        conversation = _assistant_conversation(request, identity, rt)
        if vote in ("up", "down") and 1 <= turn <= len(conversation.turns):
            chosen = conversation.turns[turn - 1]
            chosen.vote = vote
            assistant_telemetry.record_feedback(
                rt.ops_connection,
                username=identity.username,
                vote=vote,
                turn_index=turn,
                refused=chosen.refused,
                provider=rt.settings.provider,
                model=rt.effective_settings().resolved_model,
            )
        return _assistant_page(
            request, identity, conversation, error=None, note=None,
            back=None, back_to=None, draft="",
        )

    @app.get("/assistant/ops", response_class=HTMLResponse)
    def assistant_ops(
        request: Request,
        days: int = 30,
        identity: Identity = Depends(current_identity),
    ):
        """Usage, performance, compliance and drift metrics for the
        assistant - the operational answer to "how is this AI feature
        behaving", which a deployment that enabled it owes whoever
        signed off on enabling it. Gated by assistant:ops (admin and
        auditor), narrower than report:read because the rows name which
        staff member asked how many questions. Everything rendered here
        is counts and rates; the questions themselves are only in the
        audit trail."""
        require(identity, "assistant:ops")
        rt = assistant_runtime()

        summary = drift = ops_error = None
        if rt.ops_connection is None:
            ops_error = (
                "Assistant telemetry is not configured. Set "
                "PHI_AI_ASSISTANT_OPS_USERNAME to the aiops role "
                "(core/db/telemetry_bootstrap_<cloud>.sql) to record and "
                "report usage."
            )
        else:
            try:
                conn = rt.ops_connection()
                try:
                    summary = assistant_telemetry.usage_summary(conn, days=days)
                    drift = assistant_telemetry.drift_summary(conn)
                finally:
                    conn.close()
            except Exception as exc:
                log.error("assistant ops summary failed: %s", exc)
                ops_error = f"Could not read telemetry: {exc}"

        return page(
            request,
            "assistant_ops.html",
            identity,
            active="assistant",
            summary=summary,
            drift=drift,
            ops_error=ops_error,
            model_description=rt.effective_settings().describe(),
        )

# Made by Ryan Gomez & Co. Inc.
