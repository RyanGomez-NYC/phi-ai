# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Configuration for the optional AI assistant.

OFF BY DEFAULT, AND OFF IS THE ONLY DEFAULT THAT WOULD BE HONEST. Every
other component in this project runs inside infrastructure the deploying
organisation already owns (see README.md's "Bring your own
infrastructure"). This one sends text to a model, and depending on which
provider is chosen that text may leave the deployment's cloud account
entirely. docs/COMPLIANCE.md's Business Associate row says the PHI AI
Platform "never processes data as a hosted service, so it does not itself
need to be a Business Associate" - that sentence stays true only because
of what core/assistant/redact.py refuses to send and what
core/assistant/posture.py declines to collect. Enabling this feature
without understanding that is the mistake this module is shaped to
prevent.

EVERY VARIABLE BELOW IS READ THROUGH core/config/settings.py's env_var(),
not os.environ.get(). This component is ENTIRELY env-driven and its
principal switch is a defaulted read: a module that reads the wrong
variable name here does not fail, it reports itself disabled, and
core/web/__main__.py then takes its documented graceful-skip path. The
whole feature goes missing from a deployment that configured it, silently.
The two acknowledgement gates below make that worse rather than better if
the spellings can diverge - an interlock that reads a different variable
from the one the operator set is an interlock nobody can reason about.

THREE PROVIDERS, AND THE CHOICE IS A COMPLIANCE DECISION, NOT A
PREFERENCE:

  bedrock    Claude on Amazon Bedrock, in the operator's OWN AWS account.
             Covered by the AWS BAA the deployment already relies on for
             S3 and KMS (deploy/aws/). No new vendor relationship, and no
             traffic leaving the account boundary. The right default for
             an AWS deployment.

  vertex     Claude on Google Cloud Vertex AI, in the operator's OWN GCP
             project. Same reasoning under the Google Cloud BAA.

  anthropic  Anthropic's own API. A genuinely new egress path to a third
             party. Anthropic offers a BAA, but no PHI is sent on this
             path by construction, so the question an organisation
             actually has to answer is whether it permits a PHI-holding
             system to reach an external API at all. Azure deployments
             have no in-cloud Claude option, so this is their only route.

WHY THERE IS AN ACKNOWLEDGEMENT VARIABLE WITH NO DEFAULT. The same
reasoning core/web/auth.py gives for PHI_AI_WEB_TRUST_PROXY_AUTH:
the safe value differs by deployment and guessing either way is wrong.
An operator has to state that they know this component talks to a model,
because a network path out of a PHI environment is not something to
discover from a firewall log.

WHETHER THE ASSISTANT MAY READ PHI IS THE DEPLOYING ORGANISATION'S
DECISION, NOT THIS PROJECT'S. The first version of this module hard-coded
"never", which was the wrong call and inconsistent with how the rest of
this codebase treats exactly this kind of question - core/web/auth.py
refuses to guess whether a proxy is trusted, because "the safe value
differs by deployment and guessing either way is wrong". An organisation
with a signed BAA covering the model provider, zero-retention configured,
and a completed risk assessment is better placed to make this call than
the software is.

So PHI_AI_ASSISTANT_PHI_ACCESS selects one of three tiers, and like
the other consequential settings here it has no default beyond the most
conservative one:

  none        (default) The assistant reads documentation and PHI-free
              aggregates only. No tool can reach clinical content, and
              outbound text is scanned and refused if it looks like PHI.

  in_context  It may read the record the user ALREADY has open - the
              patient or resource whose audited page view is on screen.
              It cannot search and cannot open anything else. Minimum
              necessary (45 CFR 164.502(b)) falls out of the design:
              the disclosure already happened when the page loaded, and
              the assistant is explaining what is in front of someone.

  lookup      Full parity with the user's own role: search, record
              lists, resource contents. The assistant becomes a records
              interface, with everything that implies.

The tiers are ORDERED rather than independent flags. `lookup` strictly
contains `in_context`, so two booleans would make an incoherent
combination representable, and staging a rollout stays a one-value
change.

TWO THINGS REMAIN INVARIANT AT EVERY TIER, because they are not policy
opinions - they are rules the rest of this application already enforces
and an assistant that broke them would silently corrupt the record:

  - Every clinical read is audit-logged as a disclosure, with the object
    key and a stated purpose of use, exactly as core/web/app.py logs one.
    An accounting of disclosures under 45 CFR 164.528 that omits reads
    made through a chat box is wrong.
  - Permission still gates access. A role that cannot open a record in
    the interface cannot obtain it by asking.

Psychotherapy notes stay outside all of this. 45 CFR 164.508(a)(2)
requires authorisation for nearly any use, and this project already
models that as a separate bucket and key rather than a flag - see
runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md. No tier reaches them.

THE API KEY IS A FILE PATH, not an env var, matching
PHI_AI_FHIR_PRIVATE_KEY_PATH and for the reason
core/config/settings.py already states: key material in an env var ends
up in process listings and log capture. ANTHROPIC_API_KEY is still
honoured because every Anthropic SDK and example uses it and a deployer
who cannot make the documented thing work will find a worse way round -
but it is warned about, not silently preferred. It is also the one read
in this module that stays on os.environ.get(): it is the SDK's own
variable name, not a PHI_AI_<SUFFIX> setting.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config.settings import ENV_PREFIX, env_var

log = logging.getLogger("phi-ai.assistant.config")

PROVIDERS = ("anthropic", "bedrock", "vertex")

# Sonnet 5 rather than an Opus-tier model, deliberately. This assistant
# reads documentation and reports operational counts; it is not doing
# long-horizon autonomous work. Sonnet 5 is the best speed/cost point for
# that shape, and an operator waiting on an install question does not want
# to wait longer for a better-argued answer to a question about an env
# var. Override with PHI_AI_ASSISTANT_MODEL.
DEFAULT_MODEL = "claude-sonnet-5"

# Bedrock model ids carry a provider prefix; the Anthropic API and Vertex
# use the bare id. Applied in resolved_model() rather than made the
# operator's problem, since PHI_AI_ASSISTANT_MODEL is otherwise the
# same string on all three.
_BEDROCK_PREFIX = "anthropic."

# Effort controls how much the model thinks and acts per request. `medium`
# rather than the API default of `high`: the questions here are answered
# from retrieved documentation and a handful of counts, so the extra depth
# mostly buys tokens. Raise it (low|medium|high|xhigh|max) for a
# deployment that finds answers shallow.
DEFAULT_EFFORT = "medium"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Bounds one question's tool loop. Six is enough for search -> read -> a
# posture check -> answer, and small enough that a confused loop costs
# cents rather than a bill.
DEFAULT_MAX_TOOL_ITERATIONS = 6

# max_tokens caps thinking AND response text together on this model
# family, and Sonnet 5 runs adaptive thinking by default - a value sized
# to the visible answer alone truncates mid-sentence.
DEFAULT_MAX_TOKENS = 8192

# PHI access tiers, least to most capable. Ordered - see the module
# docstring on why this is not two independent flags.
PHI_ACCESS_NONE = "none"
PHI_ACCESS_IN_CONTEXT = "in_context"
PHI_ACCESS_LOOKUP = "lookup"
PHI_ACCESS_TIERS = (PHI_ACCESS_NONE, PHI_ACCESS_IN_CONTEXT, PHI_ACCESS_LOOKUP)


class AssistantConfigError(RuntimeError):
    pass


def _flag(suffix: str) -> bool:
    """A boolean PHI_AI_<suffix>, read through the one shared helper.

    Takes a SUFFIX rather than a full name so no caller can spell a
    prefix - which is what let the acknowledgement gates below read a
    different variable from the one an operator set.
    """
    return (env_var(suffix) or "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class AssistantSettings:
    provider: str
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str = DEFAULT_EFFORT
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    request_timeout_seconds: float = 120.0

    # See the module docstring. `none` is the default and the only value
    # that needs no BAA conversation.
    phi_access: str = PHI_ACCESS_NONE

    # Whether the assistant may search and read PSYCHOTHERAPY NOTES -
    # the record class 45 CFR 164.508(a)(2) treats separately, stored in
    # its own bucket under its own key. False by default at EVERY tier:
    # phi_access=lookup alone never reaches them. Turning this on
    # requires the lookup tier, its own acknowledgement
    # (PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED), the deployment's
    # psychotherapy retrieval role, AND the caller's own `psychotherapy`
    # application role with a stated purpose - four independent
    # switches, every one deliberate. Every search and every read is
    # audit-logged as a disclosure before anything is decrypted.
    psychotherapy_access: bool = False

    # anthropic provider only. Never both: api_key is read from
    # api_key_path when that is set, and the SDK's own ANTHROPIC_API_KEY
    # resolution is the fallback.
    api_key: Optional[str] = None
    api_key_source: str = "none"

    # bedrock provider only.
    aws_region: Optional[str] = None

    # vertex provider only.
    gcp_project: Optional[str] = None
    gcp_region: Optional[str] = None

    @property
    def resolved_model(self) -> str:
        """The model id as the chosen provider expects to receive it."""
        if self.provider == "bedrock" and not self.model.startswith(_BEDROCK_PREFIX):
            return _BEDROCK_PREFIX + self.model
        return self.model

    @property
    def reads_clinical_content(self) -> bool:
        """Whether any tier above `none` is configured.

        The single question the rest of the package asks. Where it is
        True, core/assistant/redact.py stops refusing PHI-shaped input
        and clinical tools are offered; where it is False, nothing
        changes from the original design.
        """
        return self.phi_access != PHI_ACCESS_NONE

    @property
    def allows_lookup(self) -> bool:
        """Whether the assistant may find records the user has not opened."""
        return self.phi_access == PHI_ACCESS_LOOKUP

    @property
    def stays_in_org_cloud(self) -> bool:
        """Whether model traffic stays inside the operator's own cloud account.

        True for bedrock/vertex, where the existing cloud BAA covers the
        call and nothing crosses the account boundary. False for the
        direct Anthropic API. Surfaced because it is the single fact an
        operator's security review will ask about first, and it should
        come from the configuration rather than from someone's memory of
        which provider was chosen.
        """
        return self.provider in ("bedrock", "vertex")

    def describe(self) -> str:
        where = (
            f"inside your own {'AWS' if self.provider == 'bedrock' else 'GCP'} account"
            if self.stays_in_org_cloud
            else "the Anthropic API (outside this deployment's cloud account)"
        )
        reach = {
            PHI_ACCESS_NONE: "documentation and PHI-free aggregates only",
            PHI_ACCESS_IN_CONTEXT: "documentation, aggregates, and the record the user already has open",
            PHI_ACCESS_LOOKUP: "documentation, aggregates, and any record the user's own role permits",
        }[self.phi_access]
        return (
            f"{self.resolved_model} via {self.provider} - requests go to {where}; "
            f"can read {reach}"
        )


def assistant_enabled() -> bool:
    return _flag("ASSISTANT_ENABLED")


def _load_api_key(provider: str) -> tuple[Optional[str], str]:
    if provider != "anthropic":
        # Bedrock and Vertex authenticate with the deployment's existing
        # cloud identity - the same instance role or workload identity
        # core/storage/factory.py already relies on. There is deliberately
        # no separate credential to manage for them.
        return None, "cloud identity"

    path = env_var("ASSISTANT_API_KEY_PATH")
    if path:
        key_file = Path(path)
        if not key_file.is_file():
            raise AssistantConfigError(
                f"{ENV_PREFIX}ASSISTANT_API_KEY_PATH points to {path!r}, which does not "
                "exist in this container. Mount it read-only, the same way the Epic "
                "private key is mounted - see docker-compose.yml."
            )
        key = key_file.read_text().strip()
        if not key:
            raise AssistantConfigError(
                f"{ENV_PREFIX}ASSISTANT_API_KEY_PATH points to {path!r}, which is empty."
            )
        return key, f"file {path}"

    # os.environ.get, NOT env_var: ANTHROPIC_API_KEY is the Anthropic
    # SDK's own variable name and is read literally. There is no
    # PHI_AI_ANTHROPIC_API_KEY and there should not be.
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "the assistant's API key came from the ANTHROPIC_API_KEY environment "
            "variable. It works, but a credential in an env var reaches process "
            "listings and log capture in ways a mounted file does not - prefer "
            "PHI_AI_ASSISTANT_API_KEY_PATH, as the Epic private key already does."
        )
        # Returned as None so the SDK resolves it itself; recorded so the
        # healthcheck can say where it came from.
        return None, "ANTHROPIC_API_KEY environment variable"

    raise AssistantConfigError(
        "PHI_AI_ASSISTANT_PROVIDER=anthropic but no API key is configured. Set "
        "PHI_AI_ASSISTANT_API_KEY_PATH to a mounted key file (preferred), or "
        "ANTHROPIC_API_KEY.\n\n"
        "If this deployment runs on AWS or GCP, consider provider=bedrock or "
        "provider=vertex instead: Claude runs inside your own cloud account under the "
        "BAA you already have, with no new vendor and no egress off the account. See "
        "runbooks/RUNBOOK_AI_ASSISTANT.md."
    )


def _int_env(suffix: str, default: int, minimum: int) -> int:
    """An integer PHI_AI_<suffix>. Takes a SUFFIX - see _flag().

    The operator-facing name in every error below is built from
    ENV_PREFIX, so it is by construction the variable that was read.
    """
    name = ENV_PREFIX + suffix
    raw = env_var(suffix)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AssistantConfigError(f"{name}={raw!r} is not a whole number.") from exc
    if value < minimum:
        raise AssistantConfigError(f"{name}={value} must be at least {minimum}.")
    return value


def settings_from_env() -> Optional[AssistantSettings]:
    """Build settings, or None when the assistant is switched off.

    None rather than an exception for the disabled case: every caller
    treats "not configured" as "skip this feature entirely", the same
    graceful-skip posture core/config/settings.py uses for the Postgres
    index and the OMOP layer. A deployment that never enables this should
    behave exactly as it did before the feature existed.

    Which is precisely why every read below goes through env_var(): that
    same graceful skip is indistinguishable, from the outside, from the
    whole feature being silently dropped because the variable names did
    not match.
    """
    if not assistant_enabled():
        return None

    if not _flag("ASSISTANT_EGRESS_ACKNOWLEDGED"):
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_ENABLED is set but "
            "PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED is not.\n\n"
            "Enabling the assistant opens a network path from a system that holds PHI "
            "to a large language model. No PHI is sent on that path - the assistant is "
            "built so that clinical content never reaches it (see "
            "core/assistant/redact.py and core/assistant/posture.py) - but the path "
            "itself is new, and no default this project could pick would be the right "
            "answer for every organisation.\n\n"
            "Set PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED=true once you have read "
            "runbooks/RUNBOOK_AI_ASSISTANT.md and confirmed the chosen provider is "
            "acceptable to whoever owns this deployment's risk assessment."
        )

    provider = (env_var("ASSISTANT_PROVIDER") or "anthropic").strip().lower()
    if provider not in PROVIDERS:
        raise AssistantConfigError(
            f"{ENV_PREFIX}ASSISTANT_PROVIDER must be one of {'/'.join(PROVIDERS)}, "
            f"got {provider!r}."
        )

    effort = (env_var("ASSISTANT_EFFORT") or DEFAULT_EFFORT).strip().lower()
    if effort not in EFFORT_LEVELS:
        raise AssistantConfigError(
            f"{ENV_PREFIX}ASSISTANT_EFFORT must be one of {'/'.join(EFFORT_LEVELS)}, "
            f"got {effort!r}."
        )

    phi_access = (
        env_var("ASSISTANT_PHI_ACCESS") or PHI_ACCESS_NONE
    ).strip().lower()
    if phi_access not in PHI_ACCESS_TIERS:
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_PHI_ACCESS must be one of "
            f"{'/'.join(PHI_ACCESS_TIERS)}, got {phi_access!r}. See "
            "runbooks/RUNBOOK_AI_ASSISTANT.md."
        )

    if phi_access != PHI_ACCESS_NONE and not _flag("ASSISTANT_PHI_ACKNOWLEDGED"):
        raise AssistantConfigError(
            f"PHI_AI_ASSISTANT_PHI_ACCESS={phi_access} is set but "
            "PHI_AI_ASSISTANT_PHI_ACKNOWLEDGED is not.\n\n"
            "At this tier the assistant sends protected health information to a "
            "language model. That is a decision this software does not make for you - "
            "but it is one that needs to have been made, by someone who can answer "
            "for it, rather than reached by setting a variable.\n\n"
            "Before setting the acknowledgement, confirm:\n"
            "  - a Business Associate Agreement is in place covering the model "
            "provider you configured. On bedrock/vertex that is the cloud BAA you "
            "already hold; on the Anthropic API it is a separate agreement.\n"
            "  - the provider is configured not to retain or train on your data, per "
            "that agreement.\n"
            "  - your HIPAA security risk assessment covers this data flow.\n\n"
            "Read runbooks/RUNBOOK_AI_ASSISTANT.md -> 'Letting the assistant read PHI' "
            "first. Clinical reads made through the assistant are audit-logged as "
            "disclosures exactly as reads through the interface are, and appear in an "
            "accounting of disclosures."
        )

    psychotherapy_access = _flag("ASSISTANT_PSYCHOTHERAPY_ACCESS")
    if psychotherapy_access and phi_access != PHI_ACCESS_LOOKUP:
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS is set but "
            f"PHI_AI_ASSISTANT_PHI_ACCESS is {phi_access!r}.\n\n"
            "Psychotherapy access rides on the lookup tier: searching a store of "
            "psychotherapy notes is by nature a cross-record capability, and "
            "granting it to a deployment whose general posture is 'none' or "
            "'the open record only' would make the MOST restricted record class "
            "the MOST reachable one. Set PHI_AI_ASSISTANT_PHI_ACCESS=lookup "
            "first, or unset the psychotherapy flag."
        )
    if psychotherapy_access and not _flag("ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED"):
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACCESS is set but "
            "PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED is not.\n\n"
            "Psychotherapy notes are not ordinary PHI: 45 CFR 164.508(a)(2) "
            "requires the individual's authorization for nearly any use or "
            "disclosure, with narrow exceptions, and this platform stores them "
            "behind their own bucket, key, table and role for exactly that "
            "reason. Before setting the acknowledgement, confirm with whoever "
            "owns your privacy compliance that assistant access to these notes "
            "fits your authorization posture, then read "
            "runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md and "
            "runbooks/RUNBOOK_AI_ASSISTANT.md. Every search and read is "
            "audit-logged as a disclosure against the asking user's name and "
            "stated purpose, and only users holding the `psychotherapy` role "
            "ever see the tools."
        )

    api_key, api_key_source = _load_api_key(provider)

    aws_region = env_var("ASSISTANT_AWS_REGION") or env_var("STORAGE_REGION")
    if provider == "bedrock" and not aws_region:
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_PROVIDER=bedrock needs a region. Set "
            "PHI_AI_ASSISTANT_AWS_REGION, or PHI_AI_STORAGE_REGION if the "
            "model should run in the same region as the object store. Note that Claude "
            "is not enabled in every Bedrock region, and that model access must be "
            "granted in the Bedrock console before the first call will succeed."
        )

    gcp_project = env_var("ASSISTANT_GCP_PROJECT") or env_var("GCP_PROJECT")
    gcp_region = env_var("ASSISTANT_GCP_REGION") or "global"
    if provider == "vertex" and not gcp_project:
        raise AssistantConfigError(
            "PHI_AI_ASSISTANT_PROVIDER=vertex needs a project. Set "
            "PHI_AI_ASSISTANT_GCP_PROJECT, or PHI_AI_GCP_PROJECT to reuse the "
            "project the platform already runs in."
        )

    return AssistantSettings(
        provider=provider,
        model=(env_var("ASSISTANT_MODEL") or DEFAULT_MODEL).strip(),
        phi_access=phi_access,
        psychotherapy_access=psychotherapy_access,
        max_tokens=_int_env("ASSISTANT_MAX_TOKENS", DEFAULT_MAX_TOKENS, 1024),
        effort=effort,
        max_tool_iterations=_int_env(
            "ASSISTANT_MAX_TOOL_ITERATIONS", DEFAULT_MAX_TOOL_ITERATIONS, 1
        ),
        api_key=api_key,
        api_key_source=api_key_source,
        aws_region=aws_region,
        gcp_project=gcp_project,
        gcp_region=gcp_region,
    )
# Made by Ryan Gomez & Co. Inc.
