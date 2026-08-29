# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
What the assistant may know about the platform itself.

THE RULE THIS FILE ENFORCES: aggregates, verdicts and configuration
shape - never a row. Every function here returns counts, booleans, dates
and status strings. None returns a patient reference, a storage key, a
resource id, or anything decrypted. That is not a convention to be
careful about; it is the reason the assistant can exist at all inside a
system whose compliance posture (docs/COMPLIANCE.md) rests on PHI never
leaving the deployment.

The distinction is easy to lose, so it is worth stating where it bites.
core/web/data.py's expiring_resources() returns rows carrying
patient_reference and storage_key - correct for the retention page, which
is behind an audited, permissioned view. Passing those same rows to a
model would put a list of identifiable patients into an outbound request.
So retention_outlook() below reads the same rows and returns only how
many, of which types, due when. The row never leaves this module.

CONFIGURATION IS REPORTED AS SHAPE, NOT AS VALUES. Bucket names, KMS ARNs
and database hostnames are not PHI, but they carry account identifiers
and there is no question the assistant can usefully answer that needs
them - "is the psychotherapy bucket configured?" is answerable with a
boolean, and "what is it called?" is answerable by the operator's own
terminal. Reporting booleans keeps the blast radius of a
misconfigured provider at zero.

PERMISSION IS THE CALLER'S JOB, NOT THIS MODULE'S. Each function is a
plain read; core/assistant/tools.py decides which are exposed to which
caller. That split is deliberate - the assistant must never be a way to
see something the same user could not see in the web interface, and
putting the check next to the tool definition keeps that mapping in one
readable place.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("phi-ai.assistant.posture")


def configuration_posture(settings, profile, assistant_settings=None) -> dict[str, Any]:
    """Which parts of this deployment are switched on.

    Booleans and small enums only - see the module docstring on why no
    bucket names appear here.
    """
    posture: dict[str, Any] = {
        "cloud_provider": settings.cloud_provider,
        # The value is a SIZING choice - how much data this deployment
        # holds, and therefore which storage layout and index
        # partitioning it uses.
        "scale_profile": profile.name,
        "storage_layout": profile.storage_layout.value,
        "index_partitioning": profile.index_partitioning.value,
        "retention_years_default": settings.retention_years,
        "retention_overrides_by_resource_type": dict(settings.retention_years_overrides),
        "retention_source": (
            "ruleset file (core/config/retention_rules.py)"
            if settings.retention_ruleset_jurisdiction
            else "flat PHI_AI_RETENTION_YEARS environment variables"
        ),
        "retention_ruleset_jurisdiction": settings.retention_ruleset_jurisdiction,
        # Stated on every call because it is the single most
        # misunderstood property of this system, and an assistant that
        # answers a retention question without it would be actively
        # misleading. See docs/COMPLIANCE.md -> "Retention and integrity".
        "retention_enforcement": (
            "RECORDED, NOT ENFORCED. No Object Lock, Azure immutability policy or GCS "
            "retention lock is provisioned on any cloud. Nothing prevents stored PHI "
            "being deleted before its retain-until date, and nothing deletes it after. "
            "Detection (versioning, per-object SHA-256, the hash-chained audit log, "
            "provider access logs) replaces prevention."
        ),
        "optional_features": {
            "postgres_index": settings.db_target_configured(),
            "omop_analytics_layer": settings.omop_target_configured(),
            "disposition_database_role": settings.disposition_db_configured(),
            "psychotherapy_notes_store": bool(settings.psychotherapy_storage_bucket),
            "bulk_data_export": bool(settings.fhir_group_id),
        },
        "epic_connection_configured": bool(settings.fhir_base_url and settings.fhir_client_id),
    }
    if assistant_settings is not None:
        posture["assistant"] = {
            "provider": assistant_settings.provider,
            "model": assistant_settings.resolved_model,
            "traffic_stays_in_org_cloud_account": assistant_settings.stays_in_org_cloud,
        }
    return posture


def platform_holdings(reader) -> dict[str, Any]:
    """How much is stored, by resource type. No patient references.

    distinct_patients is a COUNT. It says a number of people are held by
    this deployment; it does not say who, and there is no function here
    that could.
    """
    stats = reader.stats()
    return {
        "total_resources": stats.total_resources,
        "distinct_patients": stats.distinct_patients,
        "resource_type_counts": dict(stats.resource_type_counts),
        "earliest_stored_at": _iso(stats.earliest_stored_at),
        "latest_stored_at": _iso(stats.latest_stored_at),
    }


def retention_outlook(reader, within_days: int = 90) -> dict[str, Any]:
    """How many resources reach their retain-until date soon, by type.

    Reads rows that DO carry patient references and storage keys, and
    returns none of them - see the module docstring. `within_days` is
    clamped because an unbounded window on a large deployment is a slow
    query behind a chat box.
    """
    within_days = max(1, min(int(within_days), 3650))
    rows = reader.expiring_resources(within_days=within_days)

    by_type: dict[str, int] = {}
    elapsed = 0
    from core.web.data import utcnow

    now = utcnow()
    for row in rows:
        resource_type = row.get("resource_type") or "unknown"
        by_type[resource_type] = by_type.get(resource_type, 0) + 1
        due = row.get("retention_until")
        if due and due <= now:
            elapsed += 1

    return {
        "window_days": within_days,
        "resources_due_within_window": len(rows),
        "of_which_already_past_retain_until": elapsed,
        "by_resource_type": by_type,
        # core/web/data.py's query is LIMIT 500. Saying so prevents the
        # model reporting "500 resources expire" as a total when it is a
        # ceiling.
        "count_is_capped_at": 500,
        "note": (
            "Reaching a retain-until date does not delete anything - disposal is a "
            "deliberate, separately-permissioned operation. See "
            "runbooks/RUNBOOK_DISPOSITION.md."
        ),
    }


def audit_chain_status(reader) -> dict[str, Any]:
    """Whether the tamper-evident audit chain verifies.

    The verdict and the count, not the events. Audit entries carry actor
    usernames and resource keys, which is exactly the identifiable
    material this assistant must not carry.
    """
    intact, checked, problem = reader.verify_audit_chain()
    return {
        "intact": bool(intact),
        "events_checked": checked,
        "problem": problem,
        "note": (
            "A failure here is a suspected security incident - see "
            "runbooks/RUNBOOK_INCIDENT_RESPONSE.md. Note that concurrent writers "
            "legitimately fork the chain and that is NOT tampering; "
            "core/audit/log.py's diagnose_chain() tells the two apart."
        )
        if not intact
        else None,
    }


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None
# Made by Ryan Gomez & Co. Inc.
