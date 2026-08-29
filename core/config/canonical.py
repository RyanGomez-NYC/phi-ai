# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Canonical URL namespace for identifiers this project mints.

Separated into its own module because two unrelated subsystems - OCR
document provenance and EMR delivery tagging - both write these URLs into
data that outlives this codebase, and they must agree. A constant defined
twice would eventually differ, and the symptom would be delivered records
that verification cannot find.
"""

from __future__ import annotations

from core.config.settings import env_var

# Canonical namespace for identifiers this project mints - FHIR extension
# URLs and the CodeSystem that tags delivered records.
#
# THESE END UP INSIDE STORED DATA, and in another organisation's EMR when
# records are delivered. They are FHIR canonical URLs: stable identifiers
# first, resolvable documentation second. Two consequences:
#
#   * Changing this after ingesting does NOT rewrite existing resources.
#     They keep referencing the old namespace, so a search on the new one
#     will not find them. Set it once, before ingesting.
#   * A deploying organisation SHOULD set its own. A record delivered into
#     a partner's EMR carrying a stranger's namespace is confusing at best
#     and unattributable at worst; your own domain says who minted it.
#
# The default is deliberately a neutral project namespace rather than a
# repository URL - a repo can move or be renamed, and identifiers already
# written into clinical records cannot follow it.
PHI_AI_CANONICAL_BASE = "https://phi-ai.example.org/fhir"


def canonical_base() -> str:
    """The namespace in force for this deployment.

    Reads PHI_AI_CANONICAL_BASE through core/config/settings.py's
    env_var(), rather than a second hand-rolled read here, so there is
    exactly one precedence rule for every setting in this project.
    """
    return (env_var("CANONICAL_BASE") or PHI_AI_CANONICAL_BASE).rstrip("/")


def extension_url(path: str) -> str:
    return f"{canonical_base()}/StructureDefinition/{path.lstrip('/')}"


def code_system(path: str) -> str:
    return f"{canonical_base()}/CodeSystem/{path.lstrip('/')}"
# Made by Ryan Gomez & Co. Inc.
