# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
EMR conformance probe (SPEC §6.7).

Non-Epic EMRs are not assumed to expose comparable resources. At
install, the platform reads the target's CapabilityStatement and this
module turns it into a conformance matrix: which resources support
read / search / create / update, and whether `meta.security` appears in
declared profiles at all. Platform capabilities whose dependencies are
unmet are DISABLED EXPLICITLY WITH A NAMED REASON, never silently
degraded — the no-silent-fallback invariant applied to portability.

Pure functions over the CapabilityStatement dict; fetching it is the
installer's job (core/fhir/client.py already knows how). The matrix is
a runbook artifact and a support input, so evaluate_capabilities()
returns prose reasons an operator can paste into a ticket, not codes.

The requirement sets below are deliberately minimal-necessary: they
list what a capability CANNOT function without, not everything it
would enjoy. A capability degraded-but-functional is a design smell
the spec bans; either the requirements are met and it is enabled, or
they are not and it is off with the reason named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ConformanceMatrix:
    """resource type -> the set of FHIR interactions the server
    declares ("read", "search-type", "create", "update", ...)."""

    interactions: Mapping[str, frozenset[str]]
    fhir_version: str
    security_labels_seen: bool  # any declared meta.security support

    def supports(self, resource_type: str, interaction: str) -> bool:
        return interaction in self.interactions.get(resource_type, frozenset())


def probe(capability_statement: Mapping) -> ConformanceMatrix:
    """Reads one CapabilityStatement (R4 shape: rest[].resource[] with
    interaction[].code). Absent declarations are absent capabilities —
    the probe never assumes an interaction a server didn't declare."""
    interactions: dict[str, frozenset[str]] = {}
    security_labels_seen = False

    for rest in capability_statement.get("rest", []):
        for resource in rest.get("resource", []):
            resource_type = resource.get("type")
            if not isinstance(resource_type, str):
                continue
            codes = frozenset(
                i.get("code")
                for i in resource.get("interaction", [])
                if isinstance(i.get("code"), str)
            )
            interactions[resource_type] = interactions.get(
                resource_type, frozenset()
            ) | codes
            # A server that declares security-label handling does so via
            # profile or the resource-level flags; any mention counts as
            # "seen" — §6.1 treats it as an additional signal either way.
            if resource.get("referencePolicy") and "enforced" in resource.get(
                "referencePolicy", []
            ):
                pass
            for prof in [resource.get("profile"), *resource.get("supportedProfile", [])]:
                if isinstance(prof, str) and "security" in prof.lower():
                    security_labels_seen = True

    return ConformanceMatrix(
        interactions=interactions,
        fhir_version=str(capability_statement.get("fhirVersion", "unknown")),
        security_labels_seen=security_labels_seen,
    )


#: capability name -> tuple of (resource, interaction) it cannot
#: function without. Names match docs/SPEC.md §5.
CAPABILITY_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "grounded_assistant (5.1)": (
        ("Patient", "read"),
        ("Condition", "search-type"),
        ("MedicationRequest", "search-type"),
        ("AllergyIntolerance", "search-type"),
        ("Observation", "search-type"),
    ),
    "summarization (5.2)": (
        ("Condition", "search-type"),
        ("MedicationRequest", "search-type"),
        ("Encounter", "search-type"),
    ),
    "document_writeback (5.16)": (("DocumentReference", "create"),),
    "observation_writeback (5.16)": (("Observation", "create"),),
    "ambient_documentation (5.14)": (("DocumentReference", "create"),),
    "scheduling_optimization (5.8)": (
        ("Appointment", "search-type"),
        ("Schedule", "search-type"),
        ("Slot", "search-type"),
    ),
    "trial_prescreening (5.12)": (
        ("Condition", "search-type"),
        ("Observation", "search-type"),
    ),
}


@dataclass(frozen=True)
class CapabilityAvailability:
    capability: str
    enabled: bool
    reason: str  # the NAMED reason, present for disabled AND enabled


def evaluate_capabilities(
    matrix: ConformanceMatrix,
    requirements: Mapping[str, tuple[tuple[str, str], ...]] = CAPABILITY_REQUIREMENTS,
) -> tuple[CapabilityAvailability, ...]:
    results: list[CapabilityAvailability] = []
    for capability, needs in requirements.items():
        missing = [
            f"{resource}.{interaction}"
            for resource, interaction in needs
            if not matrix.supports(resource, interaction)
        ]
        if missing:
            results.append(
                CapabilityAvailability(
                    capability=capability,
                    enabled=False,
                    reason=(
                        "disabled: server does not declare "
                        + ", ".join(missing)
                        + " (SPEC §6.7 — disabled explicitly, never "
                        "silently degraded)"
                    ),
                )
            )
        else:
            results.append(
                CapabilityAvailability(
                    capability=capability,
                    enabled=True,
                    reason="all required interactions declared",
                )
            )
    return tuple(results)


def render_matrix_report(
    matrix: ConformanceMatrix,
    availability: tuple[CapabilityAvailability, ...],
) -> str:
    """The runbook artifact: one plain-text report an installer files
    with the deployment record."""
    lines = [
        f"FHIR version: {matrix.fhir_version}",
        f"meta.security support declared: {'yes' if matrix.security_labels_seen else 'no — expected; absence of labels is never absence of sensitivity (SPEC §6.1)'}",
        "",
        "Resource interactions:",
    ]
    for resource in sorted(matrix.interactions):
        lines.append(
            f"  {resource}: {', '.join(sorted(matrix.interactions[resource]))}"
        )
    lines.append("")
    lines.append("Capability availability:")
    for item in availability:
        state = "ENABLED " if item.enabled else "DISABLED"
        lines.append(f"  [{state}] {item.capability} — {item.reason}")
    return "\n".join(lines)
# Made by Ryan Gomez & Co. Inc.
