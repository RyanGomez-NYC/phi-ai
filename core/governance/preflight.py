# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Speech-to-text egress preflight (SPEC §6.2) — the new egress class for
ambient documentation (§5.14).

PHI egress to any model is declared and preflighted with
machine-checkable evidence (pre-existing invariant); this module is the
decision core for the STT targets. It evaluates EVIDENCE the deployment
gathered — it performs no cloud calls itself, exactly like every other
decision core here, so the refusal logic is testable without
credentials and auditable by reading one file. The deployment's wiring
gathers the evidence (an Organizations policy read, a speech-endpoint
GET) and passes it in.

The spec's verified asymmetry, preserved rather than papered over:

- **AWS is machine-checkable** — the strongest of the three. Verdict
  requires the AISERVICES_OPT_OUT_POLICY effective for Transcribe,
  plus OutputBucketName AND OutputEncryptionKMSKeyId on every job
  (omitting the bucket lands transcripts in a service-managed bucket
  for 90 days — that is the refusal, spelled out).
- **Azure splits.** A custom endpoint's contentLoggingEnabled is
  queryable and must be false. Base-model real-time logging is a
  per-request client flag with NO server-side query — verdict requires
  the gateway-side code assertion plus an operator attestation.
- **GCP is not machine-checkable** for data-logging enrollment — a
  console toggle with no API-queryable property. Verdict requires an
  operator attestation, plus the one thing that IS checkable in code:
  a non-global regional endpoint (us-speech / eu-speech), because the
  global endpoint gives no residency guarantee.

An attestation is a recorded operator statement with a name and a
date. It is weaker than a machine check and the verdict SAYS SO —
`attested` and `machine_checked` are separate fields so the audit
trail and the runbook never blur them.

Refusal on any negative or missing evidence; there is no degraded
mode, and §10's gate acceptance line — "egress preflight refuses on
every negative evidence case per cloud" — is tested verbatim in
tests/test_preflight.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OperatorAttestation:
    operator: str
    statement: str
    attested_on: str  # ISO date

    def is_complete(self) -> bool:
        return bool(
            self.operator.strip() and self.statement.strip() and self.attested_on.strip()
        )


@dataclass(frozen=True)
class PreflightVerdict:
    allowed: bool
    target: str
    reason: str
    machine_checked: tuple[str, ...] = ()
    attested: tuple[str, ...] = ()


def _refuse(target: str, reason: str, *, audit=None, actor: str = "system") -> PreflightVerdict:
    if audit is not None:
        audit.record(
            actor=actor,
            action="egress_preflight.refused",
            resource_key=f"stt/{target}",
            purpose_of_use="treatment",
        )
    return PreflightVerdict(allowed=False, target=target, reason=reason)


# ------------------------------------------------------------------- AWS


@dataclass(frozen=True)
class AwsTranscribeEvidence:
    """From `organizations:DescribeEffectivePolicy` with
    PolicyType=AISERVICES_OPT_OUT_POLICY (parsed for the Transcribe
    opt-out) and the job configuration about to be submitted."""

    ai_services_opt_out_effective: bool
    output_bucket_name: Optional[str]
    output_encryption_kms_key_id: Optional[str]


def preflight_aws_transcribe(
    evidence: AwsTranscribeEvidence, *, audit=None, actor: str = "system"
) -> PreflightVerdict:
    target = "aws-transcribe"
    if not evidence.ai_services_opt_out_effective:
        return _refuse(
            target,
            "AISERVICES_OPT_OUT_POLICY is not effective for Transcribe; "
            "without it, audio may be used for service improvement "
            "(SPEC §6.2)",
            audit=audit,
            actor=actor,
        )
    if not (evidence.output_bucket_name or "").strip():
        return _refuse(
            target,
            "OutputBucketName is unset: transcripts would land in the "
            "service-managed bucket with 90-day retention outside the "
            "deployment's control (SPEC §6.2)",
            audit=audit,
            actor=actor,
        )
    if not (evidence.output_encryption_kms_key_id or "").strip():
        return _refuse(
            target,
            "OutputEncryptionKMSKeyId is unset on the transcription job",
            audit=audit,
            actor=actor,
        )
    return PreflightVerdict(
        allowed=True,
        target=target,
        reason="all AWS checks machine-verified",
        machine_checked=(
            "AISERVICES_OPT_OUT_POLICY effective",
            "OutputBucketName set",
            "OutputEncryptionKMSKeyId set",
        ),
    )


# ------------------------------------------------------------------ Azure


@dataclass(frozen=True)
class AzureSpeechEvidence:
    """`content_logging_enabled` comes from
    GET /speechtotext/v3.2/endpoints/{id} for custom endpoints and is
    None for base-model real-time, where no server-side query exists.
    `gateway_logging_flag_asserted` is the gateway-side code assertion
    that the per-request logging flag is off."""

    custom_endpoint: bool
    content_logging_enabled: Optional[bool]
    gateway_logging_flag_asserted: bool = False
    attestation: Optional[OperatorAttestation] = None


def preflight_azure_speech(
    evidence: AzureSpeechEvidence, *, audit=None, actor: str = "system"
) -> PreflightVerdict:
    target = "azure-speech"
    if evidence.custom_endpoint:
        if evidence.content_logging_enabled is not False:
            return _refuse(
                target,
                "Custom endpoint's contentLoggingEnabled is not verified "
                "false (endpoint-level overrides session-level; SPEC §6.2)",
                audit=audit,
                actor=actor,
            )
        return PreflightVerdict(
            allowed=True,
            target=target,
            reason="custom endpoint logging machine-verified off",
            machine_checked=("endpoint contentLoggingEnabled == false",),
        )

    # Base-model real-time: not server-queryable. Code assertion plus
    # operator attestation, and the verdict records both as what they
    # are.
    if not evidence.gateway_logging_flag_asserted:
        return _refuse(
            target,
            "Base-model real-time logging is a per-request client flag; "
            "the gateway-side assertion is missing (SPEC §6.2)",
            audit=audit,
            actor=actor,
        )
    if evidence.attestation is None or not evidence.attestation.is_complete():
        return _refuse(
            target,
            "Operator attestation required for Azure base-model real-time "
            "speech: no server-side query can verify logging state",
            audit=audit,
            actor=actor,
        )
    return PreflightVerdict(
        allowed=True,
        target=target,
        reason="gateway assertion plus operator attestation",
        machine_checked=("gateway per-request logging flag asserted off",),
        attested=("audio/transcription logging not opted in",),
    )


# -------------------------------------------------------------------- GCP


@dataclass(frozen=True)
class GcpSpeechEvidence:
    """Data-logging enrollment is a project-level console toggle with
    NO API-queryable property (permanent known asymmetry) — attestation
    only. The regional endpoint IS checkable in client config."""

    regional_endpoint: str  # e.g. "us-speech.googleapis.com"
    attestation: Optional[OperatorAttestation] = None


#: Non-global regional speech endpoints. The bare global endpoint gives
#: no residency guarantee and is refused.
GCP_REGIONAL_ENDPOINTS = frozenset(
    {"us-speech.googleapis.com", "eu-speech.googleapis.com"}
)


def preflight_gcp_speech(
    evidence: GcpSpeechEvidence, *, audit=None, actor: str = "system"
) -> PreflightVerdict:
    target = "gcp-speech"
    if evidence.regional_endpoint not in GCP_REGIONAL_ENDPOINTS:
        return _refuse(
            target,
            f"Endpoint {evidence.regional_endpoint!r} is not a non-global "
            "regional speech endpoint; the global endpoint gives no "
            "residency guarantee (SPEC §6.2)",
            audit=audit,
            actor=actor,
        )
    if evidence.attestation is None or not evidence.attestation.is_complete():
        return _refuse(
            target,
            "Operator attestation required: GCP speech data-logging "
            "enrollment exposes no queryable property (permanent known "
            "asymmetry, SPEC §6.2/§11)",
            audit=audit,
            actor=actor,
        )
    return PreflightVerdict(
        allowed=True,
        target=target,
        reason="regional endpoint machine-verified; enrollment attested",
        machine_checked=(f"regional endpoint {evidence.regional_endpoint}",),
        attested=("data-logging enrollment not enabled",),
    )
# Made by Ryan Gomez & Co. Inc.
