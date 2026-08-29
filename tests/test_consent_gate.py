# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/governance/consent_gate.py (SPEC §6.5, Invariant 15).

SPEC §10's gate acceptance line, verbatim: "consent gate refuses on
every deny-list jurisdiction and on unresolved jurisdiction." The
split-rule states are the tests that earn their keep — Oregon,
Connecticut, and Nevada flip standards between in-person and
telehealth, which is exactly the modality dependence most
implementations miss.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.governance.consent_gate import (  # noqa: E402
    ConsentRecord,
    ConsentStandard,
    ConsentStatus,
    Modality,
    consent_standard,
    evaluate_capture,
)

ALL_PARTY_STATES = ["CA", "DE", "FL", "IL", "MD", "MA", "MT", "NH", "PA", "WA"]


def _granted(attested: bool = True) -> ConsentRecord:
    return ConsentRecord(
        status=ConsentStatus.GRANTED,
        timestamp="2026-08-21T14:00:00+00:00",
        obtained_by="ma.rivera",
        verbal_attestation_captured=attested,
    )


def test_unresolved_jurisdiction_refuses_capture_always():
    for jurisdiction in (None, "", "   "):
        decision = evaluate_capture(jurisdiction, Modality.IN_PERSON, _granted())
        assert not decision.allowed
        assert "Unresolved jurisdiction" in decision.reason


def test_michigan_is_deny_with_no_override():
    # Even a fully attested consent record does not open MI.
    for modality in Modality:
        decision = evaluate_capture("MI", modality, _granted())
        assert not decision.allowed
        assert "unsettled" in decision.reason


def test_all_party_states_require_verbal_attestation_not_a_checkbox():
    for state in ALL_PARTY_STATES:
        without = evaluate_capture(state, Modality.IN_PERSON, _granted(attested=False))
        assert not without.allowed, state
        assert "verbal attestation" in without.reason

        with_attestation = evaluate_capture(state, Modality.IN_PERSON, _granted())
        assert with_attestation.allowed, state


def test_split_rule_states_flip_on_modality():
    # Oregon: all-party (notice) in person, one-party for telecomms.
    assert consent_standard("OR", Modality.IN_PERSON) is ConsentStandard.ALL_PARTY
    assert consent_standard("OR", Modality.TELEHEALTH) is ConsentStandard.ONE_PARTY
    # Connecticut and Nevada: one-party in person, all-party telephonic.
    for state in ("CT", "NV"):
        assert consent_standard(state, Modality.IN_PERSON) is ConsentStandard.ONE_PARTY
        assert consent_standard(state, Modality.TELEHEALTH) is ConsentStandard.ALL_PARTY

    # And the gate acts on it: unattested consent passes CT in person,
    # fails CT telehealth.
    assert evaluate_capture("CT", Modality.IN_PERSON, _granted(attested=False)).allowed
    assert not evaluate_capture("CT", Modality.TELEHEALTH, _granted(attested=False)).allowed


def test_no_consent_record_refuses_even_in_one_party_states():
    # Deny-by-default is the gate's posture, not a per-state courtesy.
    decision = evaluate_capture("CT", Modality.IN_PERSON, None)
    assert not decision.allowed
    assert "deny-by-default" in decision.reason


def test_unlisted_state_gets_all_party_not_one_party():
    assert consent_standard("TX", Modality.IN_PERSON) is ConsentStandard.ALL_PARTY


def test_revocation_refuses_and_carries_the_deletion_obligation():
    revoked = ConsentRecord(
        status=ConsentStatus.REVOKED,
        timestamp="2026-08-21T14:10:00+00:00",
        obtained_by="ma.rivera",
    )
    decision = evaluate_capture("CT", Modality.IN_PERSON, revoked)
    assert not decision.allowed
    assert decision.deletion_obligation


def test_malformed_consent_record_refuses():
    missing_obtainer = ConsentRecord(
        status=ConsentStatus.GRANTED,
        timestamp="2026-08-21T14:00:00+00:00",
        obtained_by="  ",
        verbal_attestation_captured=True,
    )
    assert not evaluate_capture("CA", Modality.IN_PERSON, missing_obtainer).allowed


def test_refusals_are_audited_into_a_verifiable_chain():
    events = []
    audit = AuditLog(sink=events.append)
    evaluate_capture(None, Modality.IN_PERSON, None, audit=audit, encounter_key="enc/1")
    evaluate_capture("MI", Modality.TELEHEALTH, _granted(), audit=audit, encounter_key="enc/2")
    assert [e["action"] for e in events] == [
        "ambient.capture.refused_unresolved_jurisdiction",
        "ambient.capture.refused_deny_jurisdiction",
    ]
    assert AuditLog.verify_chain(events)
# Made by Ryan Gomez & Co. Inc.
