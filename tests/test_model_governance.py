# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/governance: registry + fairness screen (SPEC §6.2/§6.3,
Invariant 14), action space (Invariant 18), release gate (Invariant
17), and HTI-1 source attributes (§6.6).

These exercise exactly the acceptance lines in SPEC §10 "Gates":
an unregistered model refuses to execute; the action-space constraint
refuses without a logged override; and (per §6.8) each verdict lands in
the hash-chained audit log, verified here with the real AuditLog rather
than a stub — the audit trail is part of the contract, not a side
effect.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import AuditLog  # noqa: E402
from core.governance.action_space import (  # noqa: E402
    ActionDecision,
    OperatorOverride,
    evaluate_action,
)
from core.governance.fairness import (  # noqa: E402
    PROTECTED_CATEGORIES,
    FairnessReport,
    ProxyJustification,
    screen_input_schema,
)
from core.governance.registry import (  # noqa: E402
    ModelRegistration,
    ModelRegistry,
    RegistrationError,
    UnregisteredModelError,
)
from core.governance.release_gate import (  # noqa: E402
    PatientReleaseGate,
    ReleaseGateError,
)
from core.governance.source_attributes import (  # noqa: E402
    ATTRIBUTE_CATEGORIES,
    IRM_CHARACTERISTICS,
    IRMSummary,
    SourceAttributeError,
    SourceAttributeSet,
)


def _fairness_report() -> FairnessReport:
    return FairnessReport(
        disaggregated_performance={
            c: {"group_a": 0.81, "group_b": 0.79} for c in PROTECTED_CATEGORIES
        },
        mitigation_record="Reviewed 2026-08; no subgroup gap over 2 points.",
    )


def _valid_registration(**overrides) -> ModelRegistration:
    base = dict(
        model_id="no-show-v1",
        intended_use="No-show risk for supportive outreach only",
        validated_population="Adult primary-care panel, 2024-2026",
        input_variables=("prior_no_shows", "days_since_scheduling", "visit_type"),
        phi_eligible=False,
        fairness_report=_fairness_report(),
    )
    base.update(overrides)
    return ModelRegistration(**base)


# ---------------------------------------------------------------- fairness


def test_protected_class_variable_rejected_under_any_spelling():
    for spelling in ("race", "patientRace", "Patient Race", "date-of-birth", "gender"):
        result = screen_input_schema(("prior_no_shows", spelling))
        assert not result.ok, spelling
        assert spelling in result.protected_variables_found


def test_proxy_candidate_requires_justification_with_actual_content():
    unjustified = screen_input_schema(("payer_class",))
    assert not unjustified.ok
    assert unjustified.unjustified_proxies == ("payer_class",)

    # An empty basis is no justification at all.
    empty = screen_input_schema(
        ("payer_class",),
        {"payer_class": ProxyJustification(operator="j.doe", basis="  ")},
    )
    assert not empty.ok

    justified = screen_input_schema(
        ("payer_class",),
        {"payer_class": ProxyJustification(operator="j.doe", basis="Used only to route financial-counseling offers")},
    )
    assert justified.ok
    assert justified.justified_proxies == ("payer_class",)


def test_clean_schema_passes():
    assert screen_input_schema(("prior_no_shows", "visit_type")).ok


# ---------------------------------------------------------------- registry


def test_unregistered_model_does_not_execute_and_refusal_is_audited():
    events = []
    registry = ModelRegistry(audit=AuditLog(sink=events.append))
    with pytest.raises(UnregisteredModelError):
        registry.ensure_executable("never-registered", actor="gateway")
    assert events[-1]["action"] == "model.execution.refused_unregistered"
    assert AuditLog.verify_chain(events)


def test_registration_with_protected_variable_is_refused_and_audited():
    events = []
    registry = ModelRegistry(audit=AuditLog(sink=events.append))
    with pytest.raises(RegistrationError):
        registry.register(
            _valid_registration(input_variables=("race", "prior_no_shows")),
            actor="operator",
        )
    assert events[-1]["action"] == "model.registration.refused"
    with pytest.raises(UnregisteredModelError):
        registry.ensure_executable("no-show-v1", actor="gateway")


def test_registration_requires_fairness_report_covering_every_category():
    registry = ModelRegistry()
    with pytest.raises(RegistrationError, match="fairness report"):
        registry.register(_valid_registration(fairness_report=None), actor="op")

    partial = FairnessReport(
        disaggregated_performance={"race": {"a": 0.8}},
        mitigation_record="x",
    )
    with pytest.raises(RegistrationError, match="protected category"):
        registry.register(_valid_registration(fairness_report=partial), actor="op")


def test_phi_eligible_requires_basis_and_fine_tuned_requires_corpus():
    registry = ModelRegistry()
    with pytest.raises(RegistrationError, match="basis"):
        registry.register(
            _valid_registration(phi_eligible=True, phi_basis=None), actor="op"
        )
    with pytest.raises(RegistrationError, match="training corpus"):
        registry.register(
            _valid_registration(fine_tuned=True, training_corpus=None), actor="op"
        )


def test_valid_registration_executes_and_both_events_chain():
    events = []
    registry = ModelRegistry(audit=AuditLog(sink=events.append))
    registry.register(_valid_registration(), actor="operator")
    accepted = registry.ensure_executable("no-show-v1", actor="gateway")
    assert accepted.registration.model_id == "no-show-v1"
    assert [e["action"] for e in events] == ["model.registration.accepted"]
    assert AuditLog.verify_chain(events)


# ------------------------------------------------------------- action space


def test_supportive_actions_pass_without_override():
    decision = evaluate_action("additional_reminder")
    assert decision.allowed


def test_restrictive_action_refused_without_override_and_refusal_audited():
    events = []
    audit = AuditLog(sink=events.append)
    decision = evaluate_action("double_booking", audit=audit, actor="scheduler")
    assert not decision.allowed
    assert events[-1]["action"] == "action_space.restrictive.refused"

    # An override without a stated basis is no override (Invariant 18).
    hollow = evaluate_action(
        "double_booking",
        override=OperatorOverride(operator="j.doe", basis=""),
        audit=audit,
    )
    assert not hollow.allowed


def test_restrictive_action_with_stated_basis_is_permitted_and_logged():
    events = []
    decision = evaluate_action(
        "deprioritization",
        override=OperatorOverride(operator="j.doe", basis="Clinic closure day; all patients rebooked"),
        audit=AuditLog(sink=events.append),
        actor="scheduler",
    )
    assert decision.allowed
    assert events[-1]["action"] == "action_space.restrictive.operator_override"


def test_unknown_action_is_refused_not_guessed():
    decision = evaluate_action("expedited_processing")
    assert isinstance(decision, ActionDecision)
    assert not decision.allowed


# ------------------------------------------------------------- release gate


def test_draft_only_leaves_through_a_named_human_release():
    sent = []
    gate = PatientReleaseGate(send=sent.append)
    draft = gate.stage("patient/p1", "inbox_reply", "Your results are ready.")
    assert sent == []  # staging never sends

    with pytest.raises(ReleaseGateError):
        gate.release(draft.draft_id, released_by="   ")
    assert sent == []

    event = gate.release(draft.draft_id, released_by="dr.chen")
    assert sent == [draft]
    assert event.released_by == "dr.chen"

    # A draft releases exactly once.
    with pytest.raises(ReleaseGateError):
        gate.release(draft.draft_id, released_by="dr.chen")


def test_discard_is_the_other_exit_and_never_sends():
    sent = []
    events = []
    gate = PatientReleaseGate(send=sent.append, audit=AuditLog(sink=events.append))
    draft = gate.stage("patient/p1", "inbox_reply", "draft")
    gate.discard(draft.draft_id, discarded_by="dr.chen")
    assert sent == []
    assert events[-1]["action"] == "patient_output.discarded"


# --------------------------------------------------------- source attributes


def _irm() -> IRMSummary:
    return IRMSummary(
        risk_analysis={c: f"analysis of {c}" for c in IRM_CHARACTERISTICS},
        risk_mitigation="mitigations",
        data_governance="governance",
    )


def test_source_attributes_require_every_category_nonempty():
    complete = SourceAttributeSet(
        model_id="assistant-5.1",
        attributes={c: {"summary": "text"} for c in ATTRIBUTE_CATEGORIES},
        irm=_irm(),
    )
    published = complete.publishable()
    assert set(published["source_attributes"]) == set(ATTRIBUTE_CATEGORIES)

    thin = SourceAttributeSet(
        model_id="assistant-5.1",
        attributes={c: {"summary": "text"} for c in ATTRIBUTE_CATEGORIES[:-1]},
        irm=_irm(),
    )
    with pytest.raises(SourceAttributeError, match="missing categories"):
        thin.publishable()


def test_irm_summary_must_analyze_all_eight_characteristics():
    incomplete = IRMSummary(
        risk_analysis={"validity": "ok"},
        risk_mitigation="m",
        data_governance="g",
    )
    with pytest.raises(SourceAttributeError, match="characteristic"):
        incomplete.validate()
# Made by Ryan Gomez & Co. Inc.
