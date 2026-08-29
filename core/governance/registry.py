# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Model registry and execution gate (docs/SPEC.md §6.2, Invariant 14).

Any model producing a patient-specific score, ranking, or
classification that could influence care is registered here before it
runs, with: intended use, validated population, a declared
input-variable schema screened by core/governance/fairness.py, a
PHI-eligibility declaration with an operator-supplied basis, and -
for any fine-tuned artifact - training-corpus provenance. An
unregistered model does not execute. Registration failure stops the
run; there is no degraded mode, and no flag in this module can create
one.

The spec's labeling rule is enforced by *scope*, not by the model's
marketing category: readmission risk and length-of-stay estimation are
frequently called "operational," but they influence discharge planning
and resource allocation, which makes them patient care decision
support tools under 45 CFR 92.210 regardless of the label (§5.15).
Anything routed through the model gateway that emits a per-patient
output goes through this registry; there is no "operational" bypass.

Training-corpus provenance exists because of §5.1's finding: fine-tuned
weights are a derived PHI artifact that is not rebuildable from the
storage backend and cannot be amended per-patient (45 CFR 164.526), so
embedding/reranker/extraction models may be fine-tuned on de-identified
or public corpora ONLY, and the corpus is recorded here as the basis.
A fine-tuned registration without a stated corpus is refused.

Persistence: this class is the in-memory decision core, mirroring how
core/audit/log.py separates chain logic from its sink. A deployment
wraps it with a durable store; the gate semantics live here so they are
testable without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from core.governance.fairness import (
    FairnessReport,
    FairnessScreenError,
    FairnessScreenResult,
    ProxyJustification,
    screen_input_schema,
)


class RegistrationError(Exception):
    """The registration was refused. The message states which required
    artifact or screen failed - an error message is the most-read
    documentation this project has."""


class UnregisteredModelError(Exception):
    """Execution was requested for a model with no accepted
    registration (Invariant 14: unregistered models do not execute)."""


@dataclass(frozen=True)
class ModelRegistration:
    """The operator-declared registration artifact for one model target."""

    model_id: str
    intended_use: str
    validated_population: str
    input_variables: tuple[str, ...]
    #: True when any PHI may egress to this model target; the basis is
    #: the operator's stated justification (e.g. BAA reference) and is
    #: required whenever phi_eligible is True (§6.2).
    phi_eligible: bool
    phi_basis: Optional[str] = None
    #: True for any fine-tuned artifact; training_corpus then records
    #: the de-identified or public corpus it was tuned on (§5.1).
    fine_tuned: bool = False
    training_corpus: Optional[str] = None
    fairness_report: Optional[FairnessReport] = None
    proxy_justifications: Mapping[str, ProxyJustification] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptedRegistration:
    """What the registry stores once a registration passes every gate:
    the declaration plus the full fairness verdict, so the audit trail
    and any later review can see not just that it passed but what was
    screened."""

    registration: ModelRegistration
    fairness: FairnessScreenResult


class ModelRegistry:
    """
    In-memory registry with the execution gate.

    `audit` is an optional core.audit.log.AuditLog; every registration
    decision (accept and refuse alike) and every execution-gate refusal
    is recorded through it when present (§6.8).
    """

    def __init__(self, audit=None):
        self._models: dict[str, AcceptedRegistration] = {}
        self._audit = audit

    def _record(self, actor: str, action: str, model_id: str) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=actor,
                action=action,
                resource_key=f"model/{model_id}",
                purpose_of_use="operations",
            )

    def register(self, reg: ModelRegistration, *, actor: str) -> AcceptedRegistration:
        """
        Runs every registration gate in order and refuses on the first
        failure. Order matters only for message quality: the missing
        artifact the operator can fix fastest is reported first.
        """
        try:
            if not reg.intended_use.strip():
                raise RegistrationError(
                    f"Model {reg.model_id!r}: intended use is a required "
                    "registration field (SPEC §6.2)"
                )
            if not reg.validated_population.strip():
                raise RegistrationError(
                    f"Model {reg.model_id!r}: validated population is a "
                    "required registration field (SPEC §6.2)"
                )
            if not reg.input_variables:
                raise RegistrationError(
                    f"Model {reg.model_id!r}: a declared input-variable "
                    "schema is required; an empty schema cannot be screened "
                    "and is refused, not waved through (Invariant 14)"
                )
            if reg.phi_eligible and not (reg.phi_basis or "").strip():
                raise RegistrationError(
                    f"Model {reg.model_id!r}: PHI-eligible registration "
                    "requires an operator-supplied basis (SPEC §6.2)"
                )
            if reg.fine_tuned and not (reg.training_corpus or "").strip():
                raise RegistrationError(
                    f"Model {reg.model_id!r}: a fine-tuned artifact must "
                    "record its training corpus as basis; fine-tuning is "
                    "permitted on de-identified or public corpora only "
                    "(SPEC §5.1)"
                )

            fairness = screen_input_schema(
                reg.input_variables, reg.proxy_justifications
            )
            if not fairness.ok:
                raise RegistrationError(
                    f"Model {reg.model_id!r}: fairness screen failed - "
                    f"{fairness.reason}"
                )

            if reg.fairness_report is None:
                raise RegistrationError(
                    f"Model {reg.model_id!r}: a fairness report is a required "
                    "registration artifact (45 CFR 92.210; SPEC §6.3)"
                )
            try:
                reg.fairness_report.validate()
            except FairnessScreenError as exc:
                raise RegistrationError(
                    f"Model {reg.model_id!r}: {exc}"
                ) from exc
        except RegistrationError:
            self._record(actor, "model.registration.refused", reg.model_id)
            raise

        accepted = AcceptedRegistration(registration=reg, fairness=fairness)
        self._models[reg.model_id] = accepted
        self._record(actor, "model.registration.accepted", reg.model_id)
        return accepted

    def ensure_executable(self, model_id: str, *, actor: str) -> AcceptedRegistration:
        """
        The execution gate. Called by the gateway before any inference
        against `model_id`; raises UnregisteredModelError when there is
        no accepted registration. This is the only path from "model
        named" to "model runs" - Invariant 14 lives in this method.
        """
        accepted = self._models.get(model_id)
        if accepted is None:
            self._record(actor, "model.execution.refused_unregistered", model_id)
            raise UnregisteredModelError(
                f"Model {model_id!r} has no accepted registration and does "
                "not execute (Invariant 14). Register it with intended use, "
                "validated population, declared input schema, and fairness "
                "artifacts first."
            )
        return accepted

    def registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))
# Made by Ryan Gomez & Co. Inc.
