# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Ambient capture consent gate (docs/SPEC.md §6.5, Invariant 15).

A per-encounter precondition on recording, deny-by-default, keyed on
jurisdiction AND modality — the modality dependence is real and is
missed by most implementations: Oregon, Connecticut, and Nevada each
apply a different consent standard to an in-person conversation than
to a telehealth call.

THIS MODULE APPLIES A POLICY TABLE; IT DOES NOT DETERMINE LAW — the
same division of labor as core/config/retention_rules.py. The table
below transcribes the spec's verified state list (SPEC §6.5, verified
by two-source statutory citation, not primary-text read; counsel
sign-off is open dependency #7). What this module guarantees is the
*shape* of enforcement:

- unresolved jurisdiction refuses capture — always;
- Michigan is unsettled (MCL 750.539c vs the participant-exception
  case law) and is treated as deny;
- an all-party jurisdiction/modality requires a visit-level verbal
  attestation captured at the head of the recording — the strongest
  evidentiary posture, because consent and recording share one
  artifact and one timestamp; a registration-packet checkbox alone
  never satisfies it (the pending CIPA litigation theory is precisely
  that a buried checkbox is not consent for a specific recorded
  encounter);
- EVERY jurisdiction, one-party states included, requires a structured
  consent record — status, timestamp, obtainer — because deny-by-default
  is the gate's posture, not a per-state courtesy. HIPAA does not
  authorize recording; it governs use and disclosure afterward.
- revocation refuses further capture and returns the deletion
  obligation: the audio AND the derived transcript go, under the §6.5
  retention schedule, audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Modality(Enum):
    IN_PERSON = "in_person"
    TELEHEALTH = "telehealth"


class ConsentStandard(Enum):
    ONE_PARTY = "one_party"
    ALL_PARTY = "all_party"
    DENY = "deny"  # unsettled law: no capture, no override


#: state -> (in-person standard, telehealth standard). Transcribed from
#: SPEC §6.5. Oregon's in-person rule is all-party *notice*
#: (ORS 165.540(1)(c), upheld en banc in Project Veritas v. Schmidt);
#: notice is operationally enforced the same way as all-party consent
#: here — a verbal attestation at the head of the recording — because
#: that artifact evidences the notice, so it carries the stricter
#: classification.
STATE_RULES: dict[str, tuple[ConsentStandard, ConsentStandard]] = {
    # All-party for in-person oral communications (and telephonic).
    "CA": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "DE": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "FL": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "IL": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "MD": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "MA": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "MT": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "NH": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "PA": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    "WA": (ConsentStandard.ALL_PARTY, ConsentStandard.ALL_PARTY),
    # Split-rule states — where telehealth and in-office diverge.
    "OR": (ConsentStandard.ALL_PARTY, ConsentStandard.ONE_PARTY),
    "CT": (ConsentStandard.ONE_PARTY, ConsentStandard.ALL_PARTY),
    "NV": (ConsentStandard.ONE_PARTY, ConsentStandard.ALL_PARTY),
    # Unsettled — treated as deny (SPEC §6.5).
    "MI": (ConsentStandard.DENY, ConsentStandard.DENY),
}


def consent_standard(jurisdiction: str, modality: Modality) -> ConsentStandard:
    """A state absent from STATE_RULES gets ALL_PARTY, not ONE_PARTY:
    the table transcribes only what the spec verified, and the cost of
    over-asking for consent is a sentence at the start of a visit,
    while the cost of under-asking is a felony in Florida."""
    rules = STATE_RULES.get(jurisdiction)
    if rules is None:
        return ConsentStandard.ALL_PARTY
    return rules[0] if modality is Modality.IN_PERSON else rules[1]


class ConsentStatus(Enum):
    GRANTED = "granted"
    REVOKED = "revoked"


@dataclass(frozen=True)
class ConsentRecord:
    """Structured consent state, recorded discretely — never free-text
    (§6.5 layer 3). `verbal_attestation_captured` is layer 2: the
    visit-level verbal attestation at the head of the recording."""

    status: ConsentStatus
    timestamp: str
    obtained_by: str
    verbal_attestation_captured: bool = False


@dataclass(frozen=True)
class CaptureDecision:
    allowed: bool
    reason: str
    standard: Optional[ConsentStandard] = None
    #: set on a revocation refusal: the caller must delete the encounter
    #: audio and the derived transcript under the retention schedule.
    deletion_obligation: bool = False


def evaluate_capture(
    jurisdiction: Optional[str],
    modality: Modality,
    consent: Optional[ConsentRecord],
    *,
    audit=None,
    actor: str = "system",
    encounter_key: str = "",
) -> CaptureDecision:
    """The gate. Called before any audio capture begins for an
    encounter, and re-called on state change (a revocation mid-visit
    stops capture). Every refusal is audited when a log is wired."""

    def _refuse(reason: str, action: str, *, deletion: bool = False) -> CaptureDecision:
        if audit is not None:
            audit.record(
                actor=actor,
                action=action,
                resource_key=encounter_key or "ambient/unattributed",
                purpose_of_use="treatment",
            )
        return CaptureDecision(allowed=False, reason=reason, deletion_obligation=deletion)

    if not jurisdiction or not jurisdiction.strip():
        return _refuse(
            "Unresolved jurisdiction refuses capture (Invariant 15); "
            "resolve the encounter's state before recording",
            "ambient.capture.refused_unresolved_jurisdiction",
        )

    standard = consent_standard(jurisdiction.strip().upper(), modality)

    if standard is ConsentStandard.DENY:
        return _refuse(
            f"Recording law in {jurisdiction} is unsettled and treated as "
            "deny (SPEC §6.5); no override exists",
            "ambient.capture.refused_deny_jurisdiction",
        )

    if consent is None:
        return _refuse(
            "No structured consent record for this encounter; capture is "
            "deny-by-default in every jurisdiction (Invariant 15)",
            "ambient.capture.refused_no_consent",
        )

    if consent.status is ConsentStatus.REVOKED:
        return _refuse(
            "Consent revoked: capture stops, and the encounter audio and "
            "derived transcript are deleted under the retention schedule "
            "(SPEC §6.5)",
            "ambient.capture.refused_revoked",
            deletion=True,
        )

    if not consent.obtained_by.strip() or not consent.timestamp.strip():
        return _refuse(
            "Consent record is missing obtainer or timestamp; consent state "
            "is recorded discretely, never inferred (SPEC §6.5)",
            "ambient.capture.refused_malformed_consent",
        )

    if standard is ConsentStandard.ALL_PARTY and not consent.verbal_attestation_captured:
        return _refuse(
            f"{jurisdiction} requires all-party consent for this modality; "
            "a visit-level verbal attestation captured at the head of the "
            "recording is required — a registration-packet checkbox does "
            "not satisfy it (SPEC §6.5)",
            "ambient.capture.refused_no_verbal_attestation",
        )

    return CaptureDecision(allowed=True, reason="consent evaluated", standard=standard)
# Made by Ryan Gomez & Co. Inc.
