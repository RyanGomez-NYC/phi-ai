# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The ask-from-anywhere sub-header, and the assistant ops page's gate.

The sub-header is a product commitment - the prompt is omnipresent: a
sub-header on every page for everyone whose role can use the assistant,
and absent entirely when the feature is off. The ops page is the
opposite commitment: staff usage metrics for admin and auditor only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_web import _client  # noqa: E402


class _FakeAssistantSettings:
    provider = "bedrock"
    phi_access = "none"
    stays_in_org_cloud = True
    reads_clinical_content = False

    def describe(self):
        return "claude via bedrock - documentation only"


class _FakeAssistantRuntime:
    settings = _FakeAssistantSettings()
    ops_connection = None


def test_no_askbar_when_the_assistant_is_disabled():
    client, _, _ = _client(roles="him")
    body = client.get("/overview").text
    assert "askbar" not in body


def test_the_askbar_is_on_every_page_when_enabled():
    client, _, _ = _client(roles="him")
    client.app.state.assistant = _FakeAssistantRuntime()
    # Pages the him role can open (no /roi - the fake app wires no ROI
    # service - and no /audit, which him cannot read).
    for path in ("/overview", "/reports", "/patients"):
        body = client.get(path).text
        assert 'class="askbar"' in body, f"no askbar on {path}"
        assert 'action="/assistant"' in body, path
    # And it carries the page it was asked from, never the URL.
    assert 'name="page_key"' in client.get("/reports").text


def test_the_assistant_section_itself_has_no_askbar():
    """The assistant page IS the prompt - a second one would post over
    the conversation on screen. The base template's condition is
    active != 'assistant'; the ops page sets active='assistant', so it
    exercises that branch without needing the full runtime."""
    client, _, _ = _client(roles="auditor")
    client.app.state.assistant = _FakeAssistantRuntime()
    body = client.get("/assistant/ops").text
    assert 'class="askbar"' not in body


def test_ops_page_is_for_admin_and_auditor_only():
    for allowed in ("admin", "auditor"):
        client, _, _ = _client(roles=allowed)
        client.app.state.assistant = _FakeAssistantRuntime()
        response = client.get("/assistant/ops")
        assert response.status_code == 200, allowed
        assert "Telemetry unavailable" in response.text  # unconfigured, said plainly
        assert "PHI_AI_ASSISTANT_OPS_USERNAME" in response.text

    for denied in ("him", "viewer", "analyst", "researcher"):
        client, _, _ = _client(roles=denied)
        client.app.state.assistant = _FakeAssistantRuntime()
        assert client.get("/assistant/ops").status_code == 403, denied


def test_ops_page_notes_it_never_shows_content():
    client, _, _ = _client(roles="auditor")
    client.app.state.assistant = _FakeAssistantRuntime()
    body = client.get("/assistant/ops").text
    assert "counts and rates only" in body
# Made by Ryan Gomez & Co. Inc.
