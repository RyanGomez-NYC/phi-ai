# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/fhir/conformance_probe.py (SPEC §6.7): the probe reads a
CapabilityStatement, and capabilities whose dependencies are unmet are
disabled EXPLICITLY WITH A NAMED REASON — never silently degraded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fhir.conformance_probe import (  # noqa: E402
    evaluate_capabilities,
    probe,
    render_matrix_report,
)


def _capability_statement(resources: dict[str, list[str]]) -> dict:
    return {
        "resourceType": "CapabilityStatement",
        "fhirVersion": "4.0.1",
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": rtype,
                        "interaction": [{"code": c} for c in codes],
                    }
                    for rtype, codes in resources.items()
                ],
            }
        ],
    }


FULL_SERVER = _capability_statement(
    {
        "Patient": ["read", "search-type"],
        "Condition": ["read", "search-type"],
        "MedicationRequest": ["read", "search-type"],
        "AllergyIntolerance": ["read", "search-type"],
        "Observation": ["read", "search-type", "create"],
        "Encounter": ["read", "search-type"],
        "DocumentReference": ["read", "search-type", "create", "update"],
        "Appointment": ["read", "search-type"],
        "Schedule": ["read", "search-type"],
        "Slot": ["read", "search-type"],
    }
)


def test_probe_reads_the_declared_surface_and_nothing_more():
    matrix = probe(FULL_SERVER)
    assert matrix.fhir_version == "4.0.1"
    assert matrix.supports("DocumentReference", "create")
    assert not matrix.supports("DocumentReference", "delete")  # never assumed
    assert not matrix.supports("Medication", "read")  # undeclared type


def test_unmet_dependencies_disable_with_a_named_reason():
    # A server with no DocumentReference create and no scheduling types.
    partial = _capability_statement(
        {
            "Patient": ["read", "search-type"],
            "Condition": ["read", "search-type"],
            "MedicationRequest": ["read", "search-type"],
            "AllergyIntolerance": ["read", "search-type"],
            "Observation": ["read", "search-type"],
            "Encounter": ["read", "search-type"],
        }
    )
    availability = {a.capability: a for a in evaluate_capabilities(probe(partial))}

    assert availability["grounded_assistant (5.1)"].enabled
    assert availability["summarization (5.2)"].enabled

    writeback = availability["document_writeback (5.16)"]
    assert not writeback.enabled
    assert "DocumentReference.create" in writeback.reason  # the NAMED reason

    scheduling = availability["scheduling_optimization (5.8)"]
    assert not scheduling.enabled
    assert "Appointment.search-type" in scheduling.reason


def test_full_server_enables_everything():
    availability = evaluate_capabilities(probe(FULL_SERVER))
    assert all(a.enabled for a in availability)
    # Enabled entries carry a reason too - the report never has blanks.
    assert all(a.reason for a in availability)


def test_report_names_security_label_absence_as_expected_not_alarming():
    report = render_matrix_report(
        probe(FULL_SERVER), evaluate_capabilities(probe(FULL_SERVER))
    )
    assert "meta.security support declared: no" in report
    assert "absence of labels is never absence of sensitivity" in report
    assert "[ENABLED ] document_writeback (5.16)" in report
# Made by Ryan Gomez & Co. Inc.
