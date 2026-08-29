# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Constrained action space for operational predictions (docs/SPEC.md
Invariant 18; capabilities §5.7, §5.8, §5.15).

An operational prediction — no-show risk, readmission risk, scheduling
pressure — may trigger *supportive* interventions: an extra reminder, a
transport or telehealth offer, an outreach call. It may NOT trigger
denial, deprioritization, or double-booking absent an explicit operator
override recorded in the audit log with a stated basis. This is the
difference between a tool that improves access for patients who
struggle to attend and one that quietly penalizes them (§5.7).

The action vocabulary is closed on purpose. An action string this
module has never heard of is refused, not guessed at — classifying an
unknown action as "probably supportive" would be exactly the silent
fallback the invariants prohibit. New supportive actions are added to
SUPPORTIVE_ACTIONS in code review, where someone has to argue the
classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


#: Interventions a risk score may trigger with no override: each one
#: gives the patient something extra and takes nothing away.
SUPPORTIVE_ACTIONS = frozenset(
    {
        "additional_reminder",
        "transport_offer",
        "telehealth_offer",
        "outreach_call",
        "interpreter_arrangement",
        "flexible_rebooking_offer",
    }
)

#: Interventions that take access away from the individual the model
#: scored. Each requires a logged operator override with a stated basis.
RESTRICTIVE_ACTIONS = frozenset(
    {
        "double_booking",
        "deprioritization",
        "scheduling_denial",
        "service_denial",
    }
)


@dataclass(frozen=True)
class OperatorOverride:
    """A human operator's recorded decision to permit a restrictive
    action in a specific case. `basis` is the stated reason and is
    required content — Invariant 18 says "with a stated basis," so an
    empty one is no override at all."""

    operator: str
    basis: str


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    action: str
    reason: str
    override: Optional[OperatorOverride] = None


def evaluate_action(
    action: str,
    *,
    override: Optional[OperatorOverride] = None,
    audit=None,
    actor: str = "system",
    subject_key: str = "",
) -> ActionDecision:
    """
    The action-space gate. `audit` is an optional
    core.audit.log.AuditLog; every refusal and every override-permitted
    restrictive action is recorded through it (§6.8 names operator
    overrides explicitly).
    """

    def _record(action_name: str) -> None:
        if audit is not None:
            audit.record(
                actor=actor,
                action=action_name,
                resource_key=subject_key or f"action/{action}",
                purpose_of_use="operations",
            )

    if action in SUPPORTIVE_ACTIONS:
        return ActionDecision(
            allowed=True, action=action, reason="supportive intervention"
        )

    if action in RESTRICTIVE_ACTIONS:
        if (
            override is not None
            and override.operator.strip()
            and override.basis.strip()
        ):
            _record("action_space.restrictive.operator_override")
            return ActionDecision(
                allowed=True,
                action=action,
                reason=(
                    f"restrictive action permitted by operator override: "
                    f"{override.basis}"
                ),
                override=override,
            )
        _record("action_space.restrictive.refused")
        return ActionDecision(
            allowed=False,
            action=action,
            reason=(
                f"Restrictive action {action!r} requires an explicit operator "
                "override with a stated basis, recorded in the audit log "
                "(Invariant 18)"
            ),
        )

    _record("action_space.unknown.refused")
    return ActionDecision(
        allowed=False,
        action=action,
        reason=(
            f"Action {action!r} is not in the closed action vocabulary; "
            "unknown actions are refused, not classified by guesswork"
        ),
    )
# Made by Ryan Gomez & Co. Inc.
