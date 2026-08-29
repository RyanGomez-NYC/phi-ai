# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/assistant/.

Weighted almost entirely toward the boundary rather than the answers. The
model's output is not this project's to test; what is testable, and what
matters, is that PHI cannot reach it, that a user cannot reach anything
their role does not permit, and that nothing is sent without an audit
entry being written first.

No network is used anywhere here - the SDK client is faked, so the tests
exercise the loop, the guard and the wiring without an API key.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assistant import knowledge, posture, redact, runtime, tools  # noqa: E402
from core.assistant.config import (  # noqa: E402
    AssistantConfigError,
    AssistantSettings,
    settings_from_env,
)
from core.assistant.session import AssistantSession  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Usage:
    input_tokens = 100
    output_tokens = 20


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # Snapshot the message list. The session mutates one list across
        # the whole tool loop, so recording it by reference would make
        # every call appear to have the final state - which hid whether
        # tool results were batched into one user message or not.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self._responses:
            return _Response([_Block(type="text", text="done")])
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, *responses):
        self.messages = _FakeMessages(responses)


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append((actor, action, resource_key, purpose_of_use))


def _settings(**overrides):
    base = dict(provider="anthropic", model="claude-sonnet-5", max_tool_iterations=4)
    base.update(overrides)
    return AssistantSettings(**base)


def _session(client, toolbox=None, audit=None, require_audit=False, **kwargs):
    return AssistantSession(
        client=client,
        settings=_settings(),
        toolbox=toolbox or tools.Toolbox([]),
        actor="tester",
        audit=audit,
        require_audit=require_audit,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Configuration refuses to guess, and refuses to be enabled by accident
#
# Every variable below is spelled PHI_AI_, which core/config/settings.py's
# env_var() states is "the only spelling; nothing else is read" - it
# resolves ENV_PREFIX + suffix and has no second lookup and no fallback.
# A delenv() of a PHI_AI_ name therefore genuinely turns the setting off,
# which is what the tests below depend on.
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PHI_AI_ASSISTANT_ENABLED", raising=False)
    assert settings_from_env() is None


def test_enabling_requires_acknowledging_the_egress(monkeypatch):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.delenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", raising=False)
    with pytest.raises(AssistantConfigError) as exc:
        settings_from_env()
    assert "EGRESS_ACKNOWLEDGED" in str(exc.value)


def test_anthropic_provider_without_a_key_is_refused(monkeypatch):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.delenv("PHI_AI_ASSISTANT_API_KEY_PATH", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AssistantConfigError) as exc:
        settings_from_env()
    # The error should point at the in-cloud alternatives, since for an
    # AWS or GCP deployment they are the better answer.
    assert "bedrock" in str(exc.value)


def test_bedrock_model_id_is_prefixed_and_stays_in_the_org_cloud(monkeypatch):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PROVIDER", "bedrock")
    monkeypatch.setenv("PHI_AI_ASSISTANT_AWS_REGION", "us-east-1")

    settings = settings_from_env()
    assert settings.model == "claude-sonnet-5"
    assert settings.resolved_model == "anthropic.claude-sonnet-5"
    assert settings.stays_in_org_cloud is True


def test_default_model_is_sonnet_5(monkeypatch):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PROVIDER", "vertex")
    monkeypatch.setenv("PHI_AI_ASSISTANT_GCP_PROJECT", "example")
    assert settings_from_env().resolved_model == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# The egress guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"resourceType": "Observation", "id": "obs1"}',
        "the object at fhir/Observation/obs1.json will not decrypt",
        "Patient/eAB12cd3 has no encounters",
        "documents/source/abc123.pdf is unreadable",
        "SSN 123-45-6789",
        "DOB: 1974-03-02",
        "call the patient on (415) 555-0132",
        "forward to margaret.chen@example-health.org",
        "MRN: 0042198",
    ],
)
def test_phi_shaped_input_is_caught(text):
    assert redact.scan(text), f"expected a finding in {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "why does reconciliation report objects missing from the index?",
        "what does PHI_AI_PROFILE=large change about storage layout?",
        "the scheduler exited with a 401 from the token endpoint",
        "how long should we keep immunization records under our ruleset?",
    ],
)
def test_ordinary_operational_questions_are_not_caught(text):
    assert redact.scan(text) == []


def test_assert_clean_raises_for_application_assembled_text():
    with pytest.raises(redact.EgressBlocked):
        redact.assert_clean('{"resourceType": "Patient"}', "a tool result")


def test_refusal_message_never_quotes_the_matched_value():
    findings = redact.scan("patient SSN 123-45-6789, MRN: 99881")
    message = redact.refusal_message(findings)
    assert "123-45-6789" not in message
    assert "99881" not in message
    assert "social security number" in message


def test_corpus_scan_tolerates_documented_examples_but_not_real_resources():
    # Runbooks are full of example keys and references; excluding them
    # would empty the knowledge base.
    assert redact.scan_corpus_text("run restore for Patient/eAB12cd3") == []
    assert redact.scan_corpus_text('{"resourceType": "Patient"}')


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kb():
    return knowledge.load()


def test_corpus_loads_this_projects_documentation(kb):
    assert not kb.is_empty()
    assert "README.md" in kb.documents
    assert any(d.startswith("runbooks/") for d in kb.documents)
    assert any(d.startswith("docs/") for d in kb.documents)


def test_corpus_excludes_secrets(kb):
    for document in kb.documents:
        name = document.rsplit("/", 1)[-1]
        assert name != ".env"
        assert not name.endswith((".pem", ".tfvars", ".tfstate"))


def test_search_finds_the_relevant_runbook(kb):
    hits = kb.search("Epic bulk data export group id", limit=5)
    assert hits
    paths = {hit.section.path for hit in hits}
    assert any("EMR_CONNECTORS" in p or "AWS_SETUP" in p for p in paths)


def test_read_only_serves_indexed_documents(kb):
    assert kb.read("README.md")
    # No path is ever joined, so traversal has nothing to traverse.
    assert kb.read("../.env") is None
    assert kb.read("/etc/passwd") is None
    assert kb.read("epic_private_key.pem") is None


# ---------------------------------------------------------------------------
# Posture reports aggregates, never rows
# ---------------------------------------------------------------------------


class _FakeReader:
    def stats(self):
        from core.web.data import PlatformStats

        return PlatformStats(
            total_resources=3,
            resource_type_counts={"Observation": 2, "Patient": 1},
            distinct_patients=1,
            earliest_stored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            latest_stored_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

    def expiring_resources(self, within_days=90):
        return [
            {
                "resource_type": "Observation",
                "resource_id": "obs-past",
                "patient_reference": "Patient/eAB12cd3",
                "storage_key": "fhir/Observation/obs-past.json",
                "retention_until": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
            {
                "resource_type": "Encounter",
                "resource_id": "enc-future",
                "patient_reference": "Patient/eXYz9981",
                "storage_key": "fhir/Encounter/enc-future.json",
                "retention_until": datetime(2036, 1, 1, tzinfo=timezone.utc),
            },
        ]

    def verify_audit_chain(self):
        return (True, 12, None)


def test_retention_outlook_drops_every_identifier_from_the_rows_it_reads():
    import json

    result = posture.retention_outlook(_FakeReader(), within_days=90)
    serialized = json.dumps(result)

    assert result["resources_due_within_window"] == 2
    assert result["of_which_already_past_retain_until"] == 1
    assert result["by_resource_type"] == {"Observation": 1, "Encounter": 1}
    # The rows carried both; neither survives.
    assert "Patient/" not in serialized
    assert "fhir/" not in serialized
    assert redact.scan(serialized) == []


def test_holdings_are_counts_only():
    import json

    serialized = json.dumps(posture.platform_holdings(_FakeReader()))
    assert '"distinct_patients": 1' in serialized
    assert redact.scan(serialized) == []


# ---------------------------------------------------------------------------
# The toolbox is the security boundary
# ---------------------------------------------------------------------------


def test_a_viewer_gets_documentation_and_nothing_else(kb):
    from core.web.auth import PERMISSIONS, Role

    box = tools.build(kb, reader=_FakeReader(), capabilities=PERMISSIONS[Role.VIEWER])
    assert box.names == ["search_documentation", "read_documentation", "list_documentation"]


def test_an_auditor_gets_the_chain_but_not_retention(kb):
    from core.web.auth import PERMISSIONS, Role

    box = tools.build(kb, reader=_FakeReader(), capabilities=PERMISSIONS[Role.AUDITOR])
    assert "audit_chain_status" in box.names
    assert "retention_outlook" not in box.names


def test_disposition_gets_retention_but_not_the_chain(kb):
    from core.web.auth import PERMISSIONS, Role

    box = tools.build(kb, reader=_FakeReader(), capabilities=PERMISSIONS[Role.DISPOSITION])
    assert "retention_outlook" in box.names
    assert "audit_chain_status" not in box.names


def test_no_tool_can_reach_clinical_content(kb):
    """The enumerable claim, asserted rather than only documented."""
    box = tools.build(kb, reader=_FakeReader(), capabilities=tools.UNRESTRICTED)
    forbidden = ("read_resource", "read_object", "decrypt", "patient", "search_patients")
    for name in box.names:
        assert not any(word in name for word in forbidden), name


def test_a_tool_returning_phi_is_blocked_rather_than_returned():
    bad = tools.Tool(
        name="leaky",
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: '{"resourceType": "Patient", "id": "eAB12cd3"}',
    )
    text, is_error = tools.Toolbox([bad]).run("leaky", {})
    assert is_error
    assert "resourceType" not in text
    assert "logged as a defect" in text


def test_an_unknown_tool_name_recovers_rather_than_failing(kb):
    box = tools.build(kb, capabilities=tools.UNRESTRICTED)
    text, is_error = box.run("no_such_tool", {})
    assert is_error and "search_documentation" in text


# ---------------------------------------------------------------------------
# The session: audit before egress, refuse PHI, run tools
# ---------------------------------------------------------------------------


def test_the_question_is_audited_before_it_is_sent():
    client = _FakeClient(_Response([_Block(type="text", text="here you go")]))
    audit = _RecordingAudit()

    order = []
    audit_record = audit.record
    audit.record = lambda **kw: (order.append("audit"), audit_record(**kw))
    original_create = client.messages.create
    client.messages.create = lambda **kw: (order.append("send"), original_create(**kw))[1]

    session = _session(client, audit=audit, require_audit=True)
    session.ask("what does PHI_AI_PROFILE do?")

    assert order[0] == "audit"
    assert "send" in order
    actor, action, resource_key, purpose = audit.events[0]
    assert action == "assistant.query"
    assert purpose == "operations"


def test_without_an_audit_sink_nothing_is_sent():
    client = _FakeClient(_Response([_Block(type="text", text="never reached")]))
    session = _session(client, audit=None, require_audit=True)

    with pytest.raises(RuntimeError) as exc:
        session.ask("anything at all")

    assert client.messages.create.__self__.calls == []
    assert "audit" in str(exc.value).lower()


def test_a_question_containing_phi_is_refused_without_being_sent():
    client = _FakeClient(_Response([_Block(type="text", text="never reached")]))
    audit = _RecordingAudit()
    session = _session(client, audit=audit, require_audit=True)

    reply = session.ask("why is Patient/eAB12cd3 missing an encounter?")

    assert reply.refused
    assert client.messages.create.__self__.calls == []
    # Audited as a refusal, and the question itself is NOT in the entry.
    assert audit.events[0][1] == "assistant.refused"
    assert "eAB12cd3" not in audit.events[0][2]


def test_a_tool_call_is_audited_and_its_result_fed_back(kb):
    client = _FakeClient(
        _Response(
            [
                _Block(
                    type="tool_use",
                    id="tu_1",
                    name="search_documentation",
                    input={"query": "object lock"},
                )
            ],
            stop_reason="tool_use",
        ),
        _Response([_Block(type="text", text="Retention is recorded, not enforced.")]),
    )
    audit = _RecordingAudit()
    box = tools.build(kb, capabilities=tools.UNRESTRICTED)
    session = _session(client, toolbox=box, audit=audit, require_audit=True)

    reply = session.ask("is retention enforced?")

    assert "recorded" in reply.text
    assert reply.tools_used == ["search_documentation"]
    assert reply.sources, "documentation citations should be reported back"
    assert [e[1] for e in audit.events] == ["assistant.query", "assistant.tool"]

    # The second request carried the tool result in ONE user message.
    second = client.messages.create.__self__.calls[1]
    tool_results = second["messages"][-1]["content"]
    assert isinstance(tool_results, list)
    assert tool_results[0]["tool_use_id"] == "tu_1"


def test_the_tool_loop_is_bounded():
    endless = [
        _Response(
            [_Block(type="tool_use", id=f"tu_{i}", name="list_documentation", input={})],
            stop_reason="tool_use",
        )
        for i in range(10)
    ]
    client = _FakeClient(*endless)
    box = tools.build(knowledge.KnowledgeBase([], Path(".")), capabilities=tools.UNRESTRICTED)
    session = _session(client, toolbox=box)

    reply = session.ask("loop forever please")

    assert reply.truncated
    assert len(client.messages.create.__self__.calls) == _settings().max_tool_iterations


def test_the_request_pins_the_configured_model_and_effort():
    client = _FakeClient(_Response([_Block(type="text", text="ok")]))
    _session(client).ask("hello")
    call = client.messages.create.__self__.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["output_config"] == {"effort": "medium"}
    assert call["thinking"] == {"type": "adaptive"}
    # System prompt is cached, so the tool definitions and instructions
    # are not re-billed on every question.
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_deployment_context_is_withheld_from_callers_who_could_not_read_it(kb):
    from core.web.auth import PERMISSIONS, Role

    class _Settings:
        cloud_provider = "aws"
        retention_years = 10
        retention_years_overrides = {}
        retention_ruleset_jurisdiction = None
        psychotherapy_storage_bucket = None
        fhir_group_id = None
        fhir_base_url = "https://example.org/fhir"
        fhir_client_id = "abc"

        def db_target_configured(self):
            return True

        def omop_target_configured(self):
            return False

        def disposition_db_configured(self):
            return False

    from core.config.scale_profile import SMALL

    rt = runtime.AssistantRuntime(
        settings=_settings(),
        client=_FakeClient(),
        knowledge_base=kb,
        platform_settings=_Settings(),
        profile=SMALL,
    )

    viewer = rt.session_for(actor="v", capabilities=PERMISSIONS[Role.VIEWER])
    admin = rt.session_for(actor="a", capabilities=PERMISSIONS[Role.ADMIN])

    # The configuration summary is JSON, so its keys are the marker -
    # the phrase "this deployment" appears in the standing instructions too.
    assert '"cloud_provider"' not in viewer._system[0]["text"]
    assert '"cloud_provider"' in admin._system[0]["text"]


# ---------------------------------------------------------------------------
# The web route
# ---------------------------------------------------------------------------


def _web(kb, roles="him", enabled=True, responses=None, phi="none", reader=None):
    """A TestClient with the assistant wired the way core/web/__main__ wires it."""
    import re

    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings

    audit = _RecordingAudit()
    app = create_app(
        reader=reader or _FakeReader(),
        auth_settings=AuthSettings(trust_proxy_headers=False, dev_identity=f"tester:{roles}"),
        audit=audit,
    )
    client = _FakeClient(*(responses or [_Response([_Block(type="text", text="An answer.")])]))
    if enabled:
        app.state.assistant = runtime.AssistantRuntime(
            settings=_settings(phi_access=phi), client=client, knowledge_base=kb,
            reader=reader or _FakeReader(),
        )

    http = TestClient(app, base_url="https://records.example.org")

    def post(question):
        body = http.get("/assistant").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        return http.post("/assistant", data={"question": question, "csrf_token": token})

    return http, post, audit, client


def test_the_route_is_absent_until_the_assistant_is_enabled(kb):
    http, _, _, _ = _web(kb, enabled=False)
    assert http.get("/assistant").status_code == 503


def test_asking_a_question_returns_an_answer_and_audits_it(kb):
    _, post, audit, client = _web(kb)
    response = post("what does the small profile mean?")

    assert response.status_code == 200
    assert "An answer." in response.text
    assert [e[1] for e in audit.events] == ["assistant.query"]
    assert client.messages.create.__self__.calls, "the question should have been sent"


def test_pasting_a_record_into_the_web_form_is_refused_before_it_is_sent(kb):
    _, post, audit, client = _web(kb)
    response = post('{"resourceType": "Observation", "subject": {"reference": "Patient/eAB12cd3"}}')

    assert response.status_code == 200
    assert "cannot see" in response.text or "protected health information" in response.text
    assert client.messages.create.__self__.calls == []
    assert audit.events[0][1] == "assistant.refused"


def test_the_answer_is_rendered_as_text_not_markup(kb):
    _, post, _, _ = _web(
        kb,
        responses=[_Response([_Block(type="text", text="<script>alert(1)</script>")])],
    )
    body = post("anything").text
    # Model output reaching a page that also displays PHI must not be an
    # injection surface.
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_a_viewer_may_use_it_but_gets_no_platform_tools(kb):
    http, post, _, _ = _web(kb, roles="viewer")
    assert http.get("/assistant").status_code == 200
    assert post("hello").status_code == 200


# ---------------------------------------------------------------------------
# Page context: the model learns which page, never which patient
# ---------------------------------------------------------------------------


def test_page_context_is_a_phrase_from_the_table_not_a_url():
    from core.web.assistant_pages import PAGE_CONTEXTS, describe

    assert "retention schedule" in describe("retention")
    # A record view says what kind of page it is and nothing about whose.
    described = describe("patient")
    assert "cannot see" in described
    assert redact.scan(described) == []
    # Every phrase in the table is safe to send, by the same scan applied
    # to everything else outbound.
    for phrase in PAGE_CONTEXTS.values():
        assert redact.scan(phrase) == []


def test_an_unknown_page_key_yields_no_context_rather_than_the_key():
    from core.web.assistant_pages import describe

    assert describe("/smart/patient/eAB12cd3") is None
    assert describe("some_route_added_later") is None
    assert describe(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.example/login",          # protocol-relative: passes a naive startswith("/")
        "https://evil.example",
        "javascript:alert(1)",
        "/../../etc/passwd",
        "/unknown-route",
        r"/\evil.example",
        "",
        None,
    ],
)
def test_unsafe_return_paths_are_dropped(raw):
    from core.web.assistant_pages import safe_return_path

    assert safe_return_path(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["/retention", "/retention?within_days=90", "/patients", "/smart/patient/eAB12cd3", "/"],
)
def test_real_paths_are_kept(raw):
    from core.web.assistant_pages import safe_return_path

    assert safe_return_path(raw) == raw


# ---------------------------------------------------------------------------
# The conversation store
# ---------------------------------------------------------------------------


def test_a_conversation_is_not_readable_by_another_user():
    from core.assistant.conversations import ConversationStore

    store = ConversationStore()
    mine = store.create("alice")
    assert store.get(mine.id, "alice") is not None
    assert store.get(mine.id, "mallory") is None


def test_conversations_expire():
    from datetime import timedelta

    from core.assistant.conversations import ConversationStore

    store = ConversationStore(ttl_seconds=1)
    conversation = store.create("alice")
    conversation.last_used -= timedelta(seconds=5)
    assert store.get(conversation.id, "alice") is None
    assert len(store) == 0


def test_the_store_is_bounded():
    from core.assistant.conversations import ConversationStore

    store = ConversationStore(max_conversations=3)
    for i in range(10):
        store.create(f"user{i}")
    assert len(store) <= 3


def test_turns_are_capped_per_conversation():
    from core.assistant.conversations import ConversationStore, Turn

    store = ConversationStore(max_turns=3)
    conversation = store.create("alice")
    for i in range(8):
        conversation.record(Turn(question=f"q{i}", answer=f"a{i}"), max_turns=3)
    assert [t.question for t in conversation.turns] == ["q5", "q6", "q7"]


# ---------------------------------------------------------------------------
# Multi-turn and the ask-from-anywhere drawer, end to end
# ---------------------------------------------------------------------------


def test_a_follow_up_question_carries_the_earlier_conversation(kb):
    _, post, audit, client = _web(
        kb,
        responses=[
            _Response([_Block(type="text", text="The profile sets the storage layout.")]),
            _Response([_Block(type="text", text="Because it decides the object keys.")]),
        ],
    )

    post("what does the scale profile do?")
    body = post("why can't I change it later?").text

    # Both exchanges are on the page.
    assert "The profile sets the storage layout." in body
    assert "Because it decides the object keys." in body
    assert "why can&#39;t I change it later?" in body or "why can't I change it later?" in body

    # And the second request actually carried the first exchange to the model.
    second = client.messages.create.__self__.calls[1]["messages"]
    assert len(second) == 3  # question, answer, follow-up
    assert "scale profile" in second[0]["content"]


def test_starting_over_drops_the_conversation(kb):
    import re

    http, post, _, _ = _web(kb)
    post("first question")
    assert "first question" in http.get("/assistant").text

    body = http.get("/assistant").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    cleared = http.post("/assistant", data={"action": "clear", "csrf_token": token})

    assert "first question" not in cleared.text
    assert "first question" not in http.get("/assistant").text


def test_the_page_the_question_came_from_reaches_the_model_as_a_phrase(kb):
    import re

    http, _, _, client = _web(kb)
    body = http.get("/assistant").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    http.post(
        "/assistant",
        data={
            "question": "why is this list empty?",
            "page_key": "retention",
            "back": "/retention?within_days=90",
            "csrf_token": token,
        },
    )

    sent = client.messages.create.__self__.calls[0]["messages"][0]["content"]
    assert "retention schedule page" in sent
    assert "why is this list empty?" in sent
    # The page the user was on is described, never located.
    assert "/retention" not in sent
    assert "within_days" not in sent


def test_the_answer_page_links_back_to_where_the_question_came_from(kb):
    import re

    http, _, _, _ = _web(kb)
    body = http.get("/assistant").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    answered = http.post(
        "/assistant",
        data={
            "question": "anything",
            "page_key": "retention",
            "back": "/retention",
            "csrf_token": token,
        },
    ).text
    assert 'href="/retention"' in answered
    assert "Back to the retention schedule" in answered


def test_an_open_redirect_in_the_back_field_never_reaches_the_page(kb):
    import re

    http, _, _, _ = _web(kb)
    body = http.get("/assistant").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    answered = http.post(
        "/assistant",
        data={
            "question": "anything",
            "back": "//evil.example/login",
            "csrf_token": token,
        },
    ).text
    assert "evil.example" not in answered


def test_the_askbar_appears_on_ordinary_pages_when_enabled(kb):
    http, _, _, _ = _web(kb, roles="disposition")
    body = http.get("/retention").text
    assert 'class="askbar"' in body
    assert 'name="page_key" value="retention"' in body
    assert 'name="back" value="/retention"' in body


def test_the_askbar_is_absent_when_the_assistant_is_off(kb):
    http, _, _, _ = _web(kb, roles="disposition", enabled=False)
    assert 'class="askbar"' not in http.get("/retention").text


def test_a_record_view_identifies_itself_as_one_not_as_patient_search(kb):
    """patient.html sets active='patients' for the nav, but the assistant
    should be told it is a record view - the two are different questions."""
    http, _, _, _ = _web(kb, roles="him")
    body = http.get("/patients").text
    assert 'name="page_key" value="patients"' in body


# ---------------------------------------------------------------------------
# PHI access tiers: the organisation's decision, and its enforcement
# ---------------------------------------------------------------------------


def _phi_env(monkeypatch, tier, acknowledged=True):
    monkeypatch.setenv("PHI_AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED", "true")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PROVIDER", "bedrock")
    monkeypatch.setenv("PHI_AI_ASSISTANT_AWS_REGION", "us-east-1")
    monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACCESS", tier)
    if acknowledged:
        monkeypatch.setenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", "true")
    else:
        monkeypatch.delenv("PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED", raising=False)


def test_no_phi_access_is_the_default(monkeypatch):
    _phi_env(monkeypatch, "none")
    settings = settings_from_env()
    assert settings.phi_access == "none"
    assert settings.reads_clinical_content is False


@pytest.mark.parametrize("tier", ["in_context", "lookup"])
def test_a_phi_tier_needs_its_own_acknowledgement(monkeypatch, tier):
    _phi_env(monkeypatch, tier, acknowledged=False)
    with pytest.raises(AssistantConfigError) as exc:
        settings_from_env()
    message = str(exc.value)
    assert "PHI_ACKNOWLEDGED" in message
    # The acknowledgement should name what has to be true, not just demand a flag.
    assert "Business Associate Agreement" in message
    assert "accounting of disclosures" in message


def test_an_unknown_tier_is_refused(monkeypatch):
    _phi_env(monkeypatch, "everything")
    with pytest.raises(AssistantConfigError):
        settings_from_env()


def _clinical(tier, reader=None, patient=None, key=None, audited=None):
    from core.assistant.tools import ClinicalAccess

    def record(action, resource_key):
        (audited if audited is not None else []).append((action, resource_key))

    return ClinicalAccess(
        reader=reader or _FakeReader(),
        record_read=record,
        purpose="treatment",
        tier=tier,
        patient_reference=patient,
        storage_key=key,
    )


class _ClinicalReader(_FakeReader):
    """Adds the reads the clinical tools need."""

    def resources_for_patient(self, patient_reference):
        if patient_reference != "Patient/eAB12cd3":
            return []
        # stored_at, which is the key core/assistant/tools.py's
        # _summarise_rows() actually reads off an index row.
        return [
            {"resource_type": "Observation", "resource_id": "obs1",
             "storage_key": "fhir/Observation/obs1.json",
             "stored_at": None, "retention_until": None},
        ]

    def resource_index_row(self, storage_key):
        known = {"fhir/Observation/obs1.json", "fhir/Encounter/other-patient.json"}
        return {"storage_key": storage_key} if storage_key in known else None

    def read_resources(self, storage_key):
        return [{"resourceType": "Observation", "id": storage_key}]


def test_no_clinical_tools_without_an_enabled_tier(kb):
    box = tools.build(kb, reader=_ClinicalReader(), capabilities=tools.UNRESTRICTED)
    assert not any(
        name in box.names
        for name in ("read_record", "find_patients", "read_open_record", "list_open_record")
    )


def test_in_context_tier_offers_only_the_open_record(kb):
    box = tools.build(
        kb, reader=_ClinicalReader(), capabilities=tools.UNRESTRICTED,
        clinical=_clinical("in_context", _ClinicalReader(), patient="Patient/eAB12cd3"),
    )
    assert "read_open_record" in box.names
    assert "list_open_record" in box.names
    # No searching, no reaching a different patient.
    assert "find_patients" not in box.names
    assert "read_record" not in box.names


def test_lookup_tier_offers_search_and_read(kb):
    box = tools.build(
        kb, reader=_ClinicalReader(), capabilities=tools.UNRESTRICTED,
        clinical=_clinical("lookup", _ClinicalReader()),
    )
    assert {"find_patients", "list_patient_records", "read_record"} <= set(box.names)


def test_in_context_refuses_a_key_outside_the_open_record(kb):
    audited = []
    box = tools.build(
        kb, reader=_ClinicalReader(), capabilities=tools.UNRESTRICTED,
        clinical=_clinical(
            "in_context", _ClinicalReader(), patient="Patient/eAB12cd3", audited=audited
        ),
    )
    text, _ = box.run("read_open_record", {"storage_key": "fhir/Encounter/other-patient.json"})

    assert "not part of the record currently open" in text
    assert "resourceType" not in text
    # Nothing was disclosed, so nothing is recorded as a disclosure.
    assert audited == []


def test_a_permitted_read_is_audited_before_the_object_is_decrypted(kb):
    audited = []

    class _Watching(_ClinicalReader):
        def read_resources(self, storage_key):
            # By the time the object is read, the disclosure must already
            # be in the audit log - that ordering is the whole point. The
            # action is record.read, which is what
            # core/assistant/tools.py's _read_object() passes to
            # record_read(); it matches core/web/app.py exactly so an
            # accounting of disclosures cannot tell a chat-box read from
            # a page view.
            assert audited == [("record.read", storage_key)]
            return super().read_resources(storage_key)

    box = tools.build(
        kb, reader=_Watching(), capabilities=tools.UNRESTRICTED,
        clinical=_clinical("in_context", _Watching(), patient="Patient/eAB12cd3", audited=audited),
    )
    text, is_error = box.run("read_open_record", {"storage_key": "fhir/Observation/obs1.json"})

    assert not is_error
    assert "resourceType" in text
    assert audited == [("record.read", "fhir/Observation/obs1.json")]


def test_a_failed_audit_stops_the_disclosure(kb):
    from core.assistant.tools import ClinicalAccess

    def refuse(action, resource_key):
        raise RuntimeError("audit sink unavailable")

    reader = _ClinicalReader()
    box = tools.build(
        kb, reader=reader, capabilities=tools.UNRESTRICTED,
        clinical=ClinicalAccess(
            reader=reader, record_read=refuse, purpose="treatment", tier="lookup"
        ),
    )
    text, is_error = box.run("read_record", {"storage_key": "fhir/Observation/obs1.json"})

    assert is_error
    assert "resourceType" not in text


def test_clinical_tools_still_obey_the_caller_s_role(kb):
    from core.web.auth import PERMISSIONS, Role

    # An auditor may never read clinical content, whatever tier is enabled.
    box = tools.build(
        kb, reader=_ClinicalReader(), capabilities=PERMISSIONS[Role.AUDITOR],
        clinical=_clinical("lookup", _ClinicalReader()),
    )
    assert "read_record" not in box.names
    assert "find_patients" not in box.names


def test_clinical_results_bypass_the_scan_but_documentation_results_do_not(kb):
    """The scan is what catches a posture tool leaking a record. Clinical
    tools opt out explicitly; nothing else may."""
    box = tools.build(
        kb, reader=_ClinicalReader(), capabilities=tools.UNRESTRICTED,
        clinical=_clinical("lookup", _ClinicalReader()),
    )
    text, is_error = box.run("read_record", {"storage_key": "fhir/Observation/obs1.json"})
    assert not is_error and "resourceType" in text

    leaky = tools.Tool(
        name="posture_like", description="", input_schema={"type": "object", "properties": {}},
        handler=lambda: '{"resourceType": "Patient"}',
    )
    text, is_error = tools.Toolbox([leaky]).run("posture_like", {})
    assert is_error and "resourceType" not in text


def test_the_system_prompt_tells_the_model_the_truth_about_its_access():
    # Each phrase is quoted from core/assistant/session.py's _PHI_RULES_*
    # blocks, so this pins what that module actually says to the model
    # about its own access at each tier.
    for tier, expected in (
        ("none", "no access to clinical content"),
        ("in_context", "read the record the person already has open"),
        ("lookup", "search the stored records and read them"),
    ):
        session = AssistantSession(
            client=_FakeClient(), settings=_settings(phi_access=tier),
            toolbox=tools.Toolbox([]), actor="t",
        )
        assert expected in session._system[0]["text"]


def test_pasted_records_stop_being_refused_once_a_tier_is_enabled():
    client = _FakeClient(_Response([_Block(type="text", text="ok")]))
    session = _session(client)
    session._settings = _settings(phi_access="lookup")

    reply = session.ask('{"resourceType": "Observation", "subject": "Patient/eAB12cd3"}')

    assert not reply.refused
    assert client.messages.create.__self__.calls, "the question should have been sent"


def test_runtime_drops_clinical_access_when_no_tier_is_configured(kb):
    rt = runtime.AssistantRuntime(
        settings=_settings(phi_access="none"), client=_FakeClient(), knowledge_base=kb,
        reader=_ClinicalReader(),
    )
    session = rt.session_for(
        actor="t", capabilities=tools.UNRESTRICTED,
        clinical=_clinical("lookup", _ClinicalReader()),
    )
    assert "read_record" not in session._toolbox.names


def test_a_record_page_offers_its_record_to_the_assistant_only_at_a_phi_tier(kb):
    http, _, _, _ = _web(kb, roles="viewer", phi="in_context", reader=_ClinicalReader())
    body = http.post(
        "/patients/eAB12cd3/open",
        data={
            "purpose_of_use": "treatment",
            "csrf_token": _csrf_of(http, "/patients"),
        },
    ).text
    assert 'name="context_patient" value="Patient/eAB12cd3"' in body
    assert 'name="purpose_of_use" value="treatment"' in body

    off, _, _, _ = _web(kb, roles="him", phi="none", reader=_ClinicalReader())
    body = off.post(
        "/patients/eAB12cd3/open",
        data={"purpose_of_use": "operations", "csrf_token": _csrf_of(off, "/patients")},
    ).text
    # The field is harmless either way, but the drawer's promise must not be.
    assert "cannot see any patient's records" in body


def _csrf_of(http, path):
    import re

    return re.search(
        r'name="csrf_token" value="([^"]+)"', http.get(path).text
    ).group(1)


def test_the_web_route_reads_nothing_without_a_stated_purpose(kb):
    import re

    http, _, audit, client = _web(kb, roles="him", phi="lookup", reader=_ClinicalReader())
    token = re.search(r'name="csrf_token" value="([^"]+)"', http.get("/assistant").text).group(1)

    body = http.post(
        "/assistant",
        data={"question": "what is stored for eAB12cd3?", "csrf_token": token},
    ).text

    assert "Answered without reading any records" in body
    assert "no purpose of use was stated" in body
    # And the model was offered no clinical tools for that request.
    assert not any(
        t["name"] in ("read_record", "find_patients")
        for t in client.messages.create.__self__.calls[0]["tools"]
    )


def test_a_stated_purpose_unlocks_the_clinical_tools(kb):
    import re

    http, _, _, client = _web(kb, roles="him", phi="lookup", reader=_ClinicalReader())
    token = re.search(r'name="csrf_token" value="([^"]+)"', http.get("/assistant").text).group(1)

    http.post(
        "/assistant",
        data={
            "question": "what is stored for eAB12cd3?",
            "purpose_of_use": "treatment",
            "csrf_token": token,
        },
    )

    offered = {t["name"] for t in client.messages.create.__self__.calls[0]["tools"]}
    assert {"find_patients", "list_patient_records", "read_record"} <= offered


def test_the_lookup_page_asks_for_a_purpose_and_the_none_tier_does_not(kb):
    with_lookup, _, _, _ = _web(kb, phi="lookup")
    assert 'name="purpose_of_use"' in with_lookup.get("/assistant").text
    assert "can read patient records" in with_lookup.get("/assistant").text

    without, _, _, _ = _web(kb, phi="none")
    body = without.get("/assistant").text
    assert 'name="purpose_of_use"' not in body
    assert "cannot see clinical content" in body
# Made by Ryan Gomez & Co. Inc.
