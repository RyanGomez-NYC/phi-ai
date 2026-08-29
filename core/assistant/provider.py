# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Model client construction, one per provider.

The single place that knows a provider name maps to a client class - the
same "one place for provider branching" principle
core/storage/factory.py states for build_storage()/build_kms(). Callers
take a client and never ask which cloud it dials.

THE SDK IS IMPORTED LAZILY, on purpose. `anthropic` is listed in
requirements.txt so the image can run the assistant, but a deployment
that never enables it must not fail to start because of a package it has
no use for - the same posture every other optional dependency in this
project has (core/db/connection.py's per-cloud drivers, core/ocr's
Tesseract wrapper). Importing here means the cost of the feature is paid
by deployments that turn it on.

CREDENTIALS DIFFER BY PROVIDER IN A WAY WORTH BEING EXPLICIT ABOUT.
Bedrock and Vertex authenticate with the deployment's existing cloud
identity - the instance role or workload identity that already reaches
S3/GCS and KMS - so there is no new secret to store, rotate or leak. Only
the direct Anthropic API introduces a credential, which is why
core/assistant/config.py treats it the way it treats the Epic private key.
"""

from __future__ import annotations

import logging

from core.assistant.config import AssistantSettings

log = logging.getLogger("phi-ai.assistant.provider")

_INSTALL_HINT = {
    "anthropic": 'pip install "anthropic>=0.116"',
    "bedrock": 'pip install "anthropic[bedrock]>=0.116"',
    "vertex": 'pip install "anthropic[vertex]>=0.116"',
}


class AssistantUnavailable(RuntimeError):
    """The assistant is configured but cannot currently be used."""


def build_client(settings: AssistantSettings):
    """Return a configured Anthropic SDK client for the chosen provider.

    Every client returned exposes the same `messages.create(...)` surface,
    so core/assistant/session.py has no provider-conditional code at all.
    """
    try:
        if settings.provider == "anthropic":
            from anthropic import Anthropic

            return Anthropic(
                api_key=settings.api_key,  # None lets the SDK resolve ANTHROPIC_API_KEY
                timeout=settings.request_timeout_seconds,
            )

        if settings.provider == "bedrock":
            # The Mantle client is the Messages-API Bedrock endpoint; the
            # older AnthropicBedrock class is the legacy InvokeModel path.
            from anthropic import AnthropicBedrockMantle

            return AnthropicBedrockMantle(
                aws_region=settings.aws_region,
                timeout=settings.request_timeout_seconds,
            )

        if settings.provider == "vertex":
            from anthropic import AnthropicVertex

            return AnthropicVertex(
                project_id=settings.gcp_project,
                region=settings.gcp_region,
                timeout=settings.request_timeout_seconds,
            )
    except ImportError as exc:
        raise AssistantUnavailable(
            f"the Anthropic SDK is not installed for provider {settings.provider!r}: "
            f"{exc}. Install it with `{_INSTALL_HINT[settings.provider]}`, or set "
            "PHI_AI_ASSISTANT_ENABLED=false to run without the assistant."
        ) from exc

    raise AssistantUnavailable(f"unknown assistant provider {settings.provider!r}")


def explain_failure(exc: Exception, settings: AssistantSettings) -> str:
    """Turn an SDK exception into something an operator can act on.

    Worth doing rather than surfacing the raw error: the three failures
    that actually happen here - no credential, no model access granted,
    rate limited - all have specific, different fixes, and the raw
    messages do not say which cloud console to open.
    """
    try:
        import anthropic
    except ImportError:
        return str(exc)

    if isinstance(exc, anthropic.AuthenticationError):
        if settings.provider == "anthropic":
            return (
                "The model provider rejected the API key. Check "
                "PHI_AI_ASSISTANT_API_KEY_PATH points at a current key "
                f"(this deployment read it from: {settings.api_key_source})."
            )
        return (
            f"The {settings.provider} credentials were rejected. This deployment "
            "authenticates with its cloud identity, so check that the role or service "
            "account running the platform is permitted to invoke the model."
        )

    if isinstance(exc, anthropic.PermissionDeniedError):
        if settings.provider == "bedrock":
            return (
                f"Access to {settings.resolved_model} was denied by Bedrock. Model "
                "access has to be requested per-account in the Bedrock console before "
                "the first call succeeds, and the platform's IAM role needs "
                "bedrock:InvokeModel. Confirm the model is available in "
                f"{settings.aws_region}."
            )
        if settings.provider == "vertex":
            return (
                f"Access to {settings.resolved_model} was denied by Vertex AI. The "
                "model must be enabled in the project's Model Garden, and the service "
                "account needs the Vertex AI User role."
            )
        return f"The API key is valid but not permitted to use {settings.resolved_model}."

    if isinstance(exc, anthropic.NotFoundError):
        return (
            f"{settings.resolved_model} was not found on {settings.provider}. Check "
            "PHI_AI_ASSISTANT_MODEL - model ids differ by provider, and this "
            "deployment resolved it to the value shown."
        )

    if isinstance(exc, anthropic.RateLimitError):
        return "The model provider is rate limiting this deployment. Try again shortly."

    if isinstance(exc, anthropic.APIConnectionError):
        return (
            "Could not reach the model provider. If this deployment restricts egress, "
            f"the {settings.provider} endpoint has to be reachable - which is exactly "
            "the network path enabling the assistant opens, and a reason some "
            "organisations choose provider=bedrock or provider=vertex instead so the "
            "traffic never leaves their own cloud account."
        )

    if isinstance(exc, anthropic.APIStatusError):
        return f"The model provider returned {exc.status_code}: {exc.message}"

    return str(exc)
# Made by Ryan Gomez & Co. Inc.
