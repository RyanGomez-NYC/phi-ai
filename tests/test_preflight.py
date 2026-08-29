# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/governance/preflight.py — SPEC §10's gate line, taken
literally: "egress preflight refuses on every negative evidence case
per cloud." Each cloud gets its full negative-case table plus its one
passing configuration, and the verdicts' machine_checked / attested
split is asserted so an attestation can never masquerade as a machine
check in the audit trail.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.governance.preflight import (  # noqa: E402
    AwsTranscribeEvidence,
    AzureSpeechEvidence,
    GcpSpeechEvidence,
    OperatorAttestation,
    preflight_aws_transcribe,
    preflight_azure_speech,
    preflight_gcp_speech,
)

ATTESTATION = OperatorAttestation(
    operator="j.doe", statement="Logging enrollment verified off in console", attested_on="2026-08-21"
)


def test_aws_refuses_every_negative_evidence_case():
    negatives = [
        AwsTranscribeEvidence(False, "bucket", "kms-key"),  # no opt-out policy
        AwsTranscribeEvidence(True, None, "kms-key"),  # service-managed bucket
        AwsTranscribeEvidence(True, "bucket", None),  # no KMS key
    ]
    for evidence in negatives:
        assert not preflight_aws_transcribe(evidence).allowed


def test_aws_passes_fully_machine_checked():
    verdict = preflight_aws_transcribe(AwsTranscribeEvidence(True, "b", "k"))
    assert verdict.allowed
    assert len(verdict.machine_checked) == 3
    assert verdict.attested == ()  # AWS needs no attestation - strongest of the three


def test_azure_custom_endpoint_is_machine_checked_or_refused():
    for logging_state in (True, None):  # enabled, or simply unverified
        evidence = AzureSpeechEvidence(custom_endpoint=True, content_logging_enabled=logging_state)
        assert not preflight_azure_speech(evidence).allowed
    ok = preflight_azure_speech(
        AzureSpeechEvidence(custom_endpoint=True, content_logging_enabled=False)
    )
    assert ok.allowed and ok.attested == ()


def test_azure_base_model_requires_assertion_and_attestation():
    no_assert = AzureSpeechEvidence(False, None, gateway_logging_flag_asserted=False, attestation=ATTESTATION)
    assert not preflight_azure_speech(no_assert).allowed
    no_attest = AzureSpeechEvidence(False, None, gateway_logging_flag_asserted=True)
    assert not preflight_azure_speech(no_attest).allowed

    ok = preflight_azure_speech(
        AzureSpeechEvidence(False, None, gateway_logging_flag_asserted=True, attestation=ATTESTATION)
    )
    assert ok.allowed
    assert ok.machine_checked and ok.attested  # the split is preserved


def test_gcp_refuses_global_endpoint_and_missing_attestation():
    global_ep = GcpSpeechEvidence("speech.googleapis.com", attestation=ATTESTATION)
    assert not preflight_gcp_speech(global_ep).allowed

    no_attest = GcpSpeechEvidence("us-speech.googleapis.com", attestation=None)
    refused = preflight_gcp_speech(no_attest)
    assert not refused.allowed
    assert "no queryable property" in refused.reason  # the asymmetry, named

    hollow = GcpSpeechEvidence(
        "us-speech.googleapis.com",
        attestation=OperatorAttestation(operator=" ", statement="x", attested_on="2026-08-21"),
    )
    assert not preflight_gcp_speech(hollow).allowed


def test_gcp_passes_with_regional_endpoint_and_complete_attestation():
    verdict = preflight_gcp_speech(
        GcpSpeechEvidence("eu-speech.googleapis.com", attestation=ATTESTATION)
    )
    assert verdict.allowed
    assert verdict.machine_checked and verdict.attested


def test_refusals_are_audited_into_a_verifiable_chain():
    events = []
    audit = AuditLog(sink=events.append)
    preflight_aws_transcribe(AwsTranscribeEvidence(False, None, None), audit=audit)
    preflight_gcp_speech(GcpSpeechEvidence("speech.googleapis.com"), audit=audit)
    assert [e["action"] for e in events] == ["egress_preflight.refused"] * 2
    assert AuditLog.verify_chain(events)
# Made by Ryan Gomez & Co. Inc.
