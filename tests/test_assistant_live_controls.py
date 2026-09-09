# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The Control panel's two model settings reach the model call.

WHY THIS FILE EXISTS. `assistant_model` and `assistant_max_tokens` were
written by /system/control/config, persisted to platform_config,
audit-logged as `config.changed`, and rendered back into the form - and
read by nothing at all. settings_from_env() was the only constructor of
AssistantSettings in the codebase and it reads environment variables
exclusively, so an operator could switch the foundation model, watch the
tick move next to it, get an audit entry saying they had changed it, and
the assistant would go on calling whatever PHI_AI_ASSISTANT_MODEL said.

The two switches beside them on the same screen - PHI RAG, live calls -
WERE wired. That is what made it a live problem rather than a cosmetic
one: a panel that is right about two of its controls teaches an operator
to trust the other two.

So every test here asserts against the value that ACTUALLY REACHES THE
MODEL - the `model` and `max_tokens` keyword arguments of
messages.create - and never against what the config store holds. A test
that reads the value back out of the store is the bug, restated as a
test: that always passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assistant.config import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    MAX_MAX_TOKENS,
    MIN_MAX_TOKENS,
    AssistantSettings,
    clamp_max_tokens,
)
from core.assistant.runtime import AssistantRuntime  # noqa: E402


class _RecordingMessages:
    """Captures the kwargs of every messages.create call."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("stop here - the kwargs are what this file asserts on")


class _RecordingClient:
    def __init__(self):
        self.messages = _RecordingMessages()


def _runtime(overrides=None) -> AssistantRuntime:
    return AssistantRuntime(
        settings=AssistantSettings(provider="anthropic", model="claude-sonnet-5"),
        client=_RecordingClient(),
        knowledge_base=None,
        live_overrides=overrides,
    )


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

def test_with_no_overrides_the_environment_stands():
    rt = _runtime()
    assert rt.effective_settings() is rt.settings


def test_an_empty_choice_is_not_a_choice():
    """"" is what the config store returns for a key nobody has set.

    It has to mean "the operator has not chosen", never "the model id is
    the empty string" - which would send `model=""` to the API.
    """
    rt = _runtime(lambda: {"model": "", "max_tokens": ""})
    assert rt.effective_settings().model == "claude-sonnet-5"
    assert rt.effective_settings().max_tokens == DEFAULT_MAX_TOKENS


def test_the_operators_model_is_the_one_used():
    rt = _runtime(lambda: {"model": "claude-opus-5", "max_tokens": ""})
    assert rt.effective_settings().model == "claude-opus-5"


def test_the_process_wide_settings_are_never_mutated():
    """One request's override must not leak into the next request's."""
    rt = _runtime(lambda: {"model": "claude-opus-5", "max_tokens": "9000"})
    assert rt.effective_settings().model == "claude-opus-5"
    assert rt.settings.model == "claude-sonnet-5"
    assert rt.settings.max_tokens == DEFAULT_MAX_TOKENS


def test_a_broken_override_provider_does_not_break_the_assistant():
    """Answering with the environment's model is a worse answer than the
    operator asked for. Refusing to answer because the config store blinked
    is no answer at all."""
    def boom():
        raise RuntimeError("platform_config unreachable")

    rt = _runtime(boom)
    assert rt.effective_settings() is rt.settings


@pytest.mark.parametrize("given", ["abc", "12.5", None, "  "])
def test_a_max_tokens_that_is_not_a_number_is_ignored(given):
    rt = _runtime(lambda: {"max_tokens": given})
    assert rt.effective_settings().max_tokens == DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------

def test_the_bounds_are_coherent():
    assert MIN_MAX_TOKENS < DEFAULT_MAX_TOKENS < MAX_MAX_TOKENS


def test_a_max_tokens_below_the_floor_is_raised_to_it():
    """256 was reachable from the Control panel and is the failure this
    floor exists to stop: max_tokens covers adaptive thinking AND the
    visible answer, so a small budget is spent thinking and the response
    comes back stop_reason="max_tokens" with EMPTY text and no error."""
    assert clamp_max_tokens(256) == MIN_MAX_TOKENS
    rt = _runtime(lambda: {"max_tokens": "256"})
    assert rt.effective_settings().max_tokens == MIN_MAX_TOKENS


def test_a_max_tokens_above_the_ceiling_is_lowered_to_it():
    assert clamp_max_tokens(999_999) == MAX_MAX_TOKENS
    rt = _runtime(lambda: {"max_tokens": "999999"})
    assert rt.effective_settings().max_tokens == MAX_MAX_TOKENS


def test_the_default_is_expressible_by_the_operator():
    """The panel used to clamp to 256..4096 against a library default of
    8192, so the code's own default was not a value the operator could
    type. Whatever the bounds become, that must not be true again."""
    assert clamp_max_tokens(DEFAULT_MAX_TOKENS) == DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# What actually reaches the model
# ---------------------------------------------------------------------------

def _ask(rt):
    session = rt.session_for(actor="tester", require_audit=False)
    session.ask("anything")
    return rt.client.messages.calls


def test_the_chosen_model_and_limit_are_what_messages_create_receives():
    """The end-to-end assertion this file exists for."""
    rt = _runtime(lambda: {"model": "claude-opus-5", "max_tokens": "9000"})
    calls = _ask(rt)
    assert calls, "the model was never called"
    assert calls[0]["model"] == "claude-opus-5"
    assert calls[0]["max_tokens"] == 9000


def test_without_an_override_the_environments_values_reach_the_model():
    rt = _runtime()
    calls = _ask(rt)
    assert calls[0]["model"] == "claude-sonnet-5"
    assert calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS


def test_an_out_of_range_limit_never_reaches_the_model():
    """Belt and braces for a row written before the bounds existed, or by
    hand: the clamp is applied on the way out, not only on the way in."""
    rt = _runtime(lambda: {"max_tokens": "256"})
    assert _ask(rt)[0]["max_tokens"] == MIN_MAX_TOKENS


def test_a_change_between_two_questions_takes_effect_on_the_second():
    """A control that needs a restart is not a control."""
    chosen = {"model": "claude-sonnet-5", "max_tokens": ""}
    rt = _runtime(lambda: dict(chosen))

    assert _ask(rt)[-1]["model"] == "claude-sonnet-5"
    chosen["model"] = "claude-opus-5"
    assert _ask(rt)[-1]["model"] == "claude-opus-5"


# ---------------------------------------------------------------------------
# The web wiring
# ---------------------------------------------------------------------------

def test_the_web_app_connects_the_control_panel_to_the_runtime():
    """The hook is attached on the accessor every route already calls,
    because the entrypoint sets app.state.assistant AFTER create_app
    returns - an earlier version wired it at the end of create_app, where
    the runtime does not exist yet and the wiring was silently a no-op."""
    from test_web import _client

    client, _, _ = _client(roles="admin")
    rt = _runtime()
    client.app.state.assistant = rt
    client.app.state.platform_state.config_set("assistant_model", "claude-opus-5")
    client.app.state.platform_state.config_set("assistant_max_tokens", "7000")

    # Two ways it gets armed, and both have to work. An entrypoint arms it
    # the moment it installs a runtime (core/web/__main__.py); anything that
    # installs one later - this test, an embedder - is caught by the lazy
    # arming on the accessor. Here the runtime was installed after
    # create_app, so exercise the second path by opening an assistant page.
    assert client.app.state.assistant_overrides is not None, (
        "create_app did not publish the override provider for entrypoints to use"
    )
    # /assistant/ops resolves the runtime and renders counts, so it arms the
    # hook without building a conversation or touching the model.
    assert client.get("/assistant/ops").status_code == 200

    assert rt.live_overrides is not None, "the Control panel was never connected"
    assert rt.effective_settings().model == "claude-opus-5"
    assert rt.effective_settings().max_tokens == 7000
# Made by Ryan Gomez & Co. Inc.
