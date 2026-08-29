# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Retention ruleset: applies retention rules, does not determine them.

This module is deliberately NOT a legal rules engine - it does not know
anything about HIPAA or any state's law, and it never will. What it
knows how to do is correctly read a structured ruleset FILE - written
and periodically reviewed by a Health Information Manager (or whoever
your organization designates to own retention schedules) - and compute
the retention_years / retention_years_overrides values that feed into
the existing mechanism in core/config/settings.py and
core/fhir/client.py.

WHY THIS EXISTS RATHER THAN JUST PHI_AI_RETENTION_YEARS_OVERRIDES:
that mechanism works, but it's an opaque JSON blob in an environment
variable with no citation, no reviewer, no review date - nothing to
audit against if a value is later questioned. Real medical record
retention law is genuinely more complex than "one number per resource
type" suggests: most states run two separate regimes (a hospital rule
and a physician rule, which frequently differ), the law changes over
time (Washington's hospital retention period changed from 10 years
after discharge to 26 years from creation in 2025; Texas added a new
electronic-records rule effective 2026), and getting a figure wrong -
even briefly - is exactly the kind of error a bare number can't help
anyone catch. A ruleset file with required citation/reviewer/date
fields, and a tool that flags rules going stale, is a meaningfully
better artifact for something this consequential.

WHAT THIS MODULE DOES NOT DO: determine whether any given retention_years
figure is legally correct. Every rule in a loaded ruleset came from
whoever wrote the file - this module trusts that entirely and only
enforces that the required sourcing fields are present, not that their
contents are accurate. See runbooks/RUNBOOK_RETENTION_RULES.md.

SCOPE: jurisdiction- and resource-type-level rules only. Deliberately
NOT minor-aware (no different retention for minors' records) - that
would require knowing patient date of birth, which conflicts with this
project's design of never handling identifiable demographics at the
ingest/index layer (see core/db/schema.sql, core/db/index.py). That's a
real, separate architectural question, not solved here.

Also deliberately single-jurisdiction per ruleset file, matching how
the rest of this deployment model already works (one deployment, one
retention-period Terraform value, one primary legal context) - an
organization operating across multiple states would need either
multiple deployments or a bigger change to this module, not something
this scope attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional


class RetentionRulesetError(Exception):
    """Raised for a malformed or incomplete ruleset file. Always includes
    enough detail (which field, which rule, why) that a Health
    Information Manager - not a developer - can find and fix the
    problem without needing to understand this module's code."""


@dataclass(frozen=True)
class RetentionRule:
    retention_years: int
    citation: str
    reviewed_by: str
    reviewed_on: date
    source_note: str = ""
    # Many states run two separate regimes - a hospital-licensing rule
    # and a physician-records rule - that frequently specify different
    # periods. Free text, not a restricted set: state law nomenclature
    # varies enough that forcing a fixed enum here would be more likely
    # to mislead than help. Purely informational - does not change how
    # this rule is applied, only how it's displayed/audited.
    regime: Optional[str] = None


@dataclass(frozen=True)
class RetentionRuleset:
    jurisdiction: str
    default_rule: RetentionRule
    resource_type_rules: dict[str, RetentionRule] = field(default_factory=dict)
    source_path: Optional[str] = None

    def to_retention_config(self) -> tuple[int, dict[str, int]]:
        """The (retention_years, retention_years_overrides) pair this
        ruleset implies - ready to pass directly to Settings /
        FHIRIngestionClient, exactly the shape
        PHI_AI_RETENTION_YEARS_OVERRIDES already expects."""
        overrides = {
            resource_type: rule.retention_years
            for resource_type, rule in self.resource_type_rules.items()
        }
        return self.default_rule.retention_years, overrides

    def all_rules(self) -> list[tuple[str, RetentionRule]]:
        """Every rule in the ruleset, labeled - ('default', ...) for the
        default rule, (resource_type, ...) for each override. Used for
        display (retention_rules_check.py) and staleness checking."""
        result: list[tuple[str, RetentionRule]] = [("default", self.default_rule)]
        result.extend(sorted(self.resource_type_rules.items()))
        return result

    def stale_rules(self, as_of: Optional[date] = None, warn_after_years: int = 2) -> list[tuple[str, RetentionRule]]:
        """
        Rules whose reviewed_on date is old enough to warrant a fresh
        look. Not a hard error - law that hasn't changed doesn't need
        re-verifying on a fixed clock - but real, current examples
        exist of retention periods changing with real consequences
        (Washington's hospital rule changed from 10 years-after-
        discharge to 26 years-from-creation in 2025), so a rule that
        hasn't been looked at in a while is worth flagging rather than
        trusting indefinitely.
        """
        cutoff_date = as_of or date.today()
        stale = []
        for label, rule in self.all_rules():
            age_years = (cutoff_date - rule.reviewed_on).days / 365.25
            if age_years >= warn_after_years:
                stale.append((label, rule))
        return stale


def _require_key(d: dict, key: str, context: str) -> object:
    if key not in d:
        raise RetentionRulesetError(f"{context} is missing required field {key!r}.")
    return d[key]


def _parse_retention_years(raw: object, context: str) -> int:
    """
    Strict integer parsing for retention_years - deliberately narrower
    than a bare int(raw).

    FOUND AND FIXED (2026-08-17 audit, MEDIUM): the previous
    `int(retention_years_raw)` silently accepted anything Python's
    int() constructor accepts, which is considerably more than "a
    whole number of years" - two real, dangerous cases, both proven
    live:

      - int(7.5) == 7. A ruleset author who typed a fractional year -
        "7.5 years" is not a wild thing for a reviewer to write while
        transcribing a source that itself hedges - gets it silently
        SHORTENED to 7, understating whatever figure was actually
        reviewed and cited. Retention periods should round UP when
        rounding at all, never down, and never silently.
      - int(True) == 1. YAML parses an unquoted `true`/`false` as a
        Python bool, and bool is (surprisingly) an int subclass, so a
        ruleset author who fat-fingers `retention_years: true` - e.g.
        a copy-paste slip from an unrelated boolean field - gets a
        SILENT retention_years=1 instead of the loud, specific error
        this obviously-wrong input deserves. A citation/reviewer/date
        can all be perfectly valid on a rule whose actual number is
        wrong by an order of magnitude, and nothing before this fix
        would have caught it.

    Still accepts a quoted numeric string ("10") and a whole-valued
    float (10.0), since both are unambiguous - only genuinely lossy or
    type-confused input is rejected.
    """
    if isinstance(raw, bool):
        raise RetentionRulesetError(
            f"{context}: retention_years must be a whole number of years, got {raw!r} - "
            "that's a boolean, not a number. Check for a typo or a copy-paste from the "
            "wrong field."
        )
    if isinstance(raw, float):
        if not raw.is_integer():
            raise RetentionRulesetError(
                f"{context}: retention_years must be a WHOLE number of years, got {raw!r}. "
                "If the underlying requirement is genuinely fractional, round UP to the next "
                "whole year rather than truncating - a retention period must never be "
                "understated."
            )
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped.lstrip("-").isdigit():
            raise RetentionRulesetError(
                f"{context}: retention_years must be a whole number of years, got {raw!r}."
            )
        return int(stripped)
    raise RetentionRulesetError(
        f"{context}: retention_years must be a whole number of years, got {raw!r} "
        f"({type(raw).__name__})."
    )


def _parse_rule(raw: dict, context: str) -> RetentionRule:
    if not isinstance(raw, dict):
        raise RetentionRulesetError(f"{context} must be a mapping (got {type(raw).__name__}).")

    retention_years_raw = _require_key(raw, "retention_years", context)
    retention_years = _parse_retention_years(retention_years_raw, context)
    if retention_years < 1:
        raise RetentionRulesetError(f"{context}: retention_years must be at least 1, got {retention_years}.")

    citation = _require_key(raw, "citation", context)
    if not isinstance(citation, str) or not citation.strip():
        raise RetentionRulesetError(
            f"{context}: citation is required and must be a non-empty string - e.g. the specific "
            "statute, regulation, or administrative code section this figure comes from. A rule "
            "with no citation can't be checked or defended later."
        )

    reviewed_by = _require_key(raw, "reviewed_by", context)
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise RetentionRulesetError(
            f"{context}: reviewed_by is required - the name and, ideally, credential (e.g. "
            "'Jane Smith, RHIA') of whoever confirmed this figure. See "
            "runbooks/RUNBOOK_RETENTION_RULES.md."
        )

    reviewed_on_raw = _require_key(raw, "reviewed_on", context)
    reviewed_on = _parse_date(reviewed_on_raw, f"{context}: reviewed_on")

    source_note = raw.get("source_note", "")
    if not isinstance(source_note, str):
        raise RetentionRulesetError(f"{context}: source_note must be text if present.")

    regime = raw.get("regime")
    if regime is not None and not isinstance(regime, str):
        raise RetentionRulesetError(f"{context}: regime must be text if present.")

    return RetentionRule(
        retention_years=retention_years,
        citation=citation.strip(),
        reviewed_by=reviewed_by.strip(),
        reviewed_on=reviewed_on,
        source_note=source_note.strip(),
        regime=regime.strip() if regime else None,
    )


def _parse_date(raw: object, context: str) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw  # PyYAML parses unquoted ISO dates (2026-06-01) as date objects directly
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    raise RetentionRulesetError(
        f"{context} must be a date in YYYY-MM-DD format (e.g. 2026-06-01), got {raw!r}."
    )


def load_ruleset(path: str) -> RetentionRuleset:
    """
    Loads and validates a retention ruleset YAML file. Raises
    RetentionRulesetError with a specific, actionable message for any
    problem - never a bare YAML parser traceback or a KeyError, since
    the person fixing this file is expected to be a Health Information
    Manager, not a developer.
    """
    import yaml

    file_path = Path(path)
    if not file_path.is_file():
        raise RetentionRulesetError(f"Retention ruleset file not found: {path}")

    try:
        raw_text = file_path.read_text()
    except OSError as exc:
        raise RetentionRulesetError(f"Could not read retention ruleset file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RetentionRulesetError(
            f"{path} is not valid YAML: {exc}\n\n"
            "If you edited this file by hand, check for missing colons, inconsistent "
            "indentation, or an unclosed quote near the line/column mentioned above."
        ) from exc

    if not isinstance(data, dict):
        raise RetentionRulesetError(
            f"{path}: the file's top level must be a mapping with 'jurisdiction' and "
            f"'default_rule' keys, not a {type(data).__name__}."
        )

    jurisdiction = _require_key(data, "jurisdiction", path)
    if not isinstance(jurisdiction, str) or not jurisdiction.strip():
        raise RetentionRulesetError(f"{path}: jurisdiction must be a non-empty string (e.g. 'FL').")

    default_rule_raw = _require_key(data, "default_rule", path)
    default_rule = _parse_rule(default_rule_raw, f"{path}: default_rule")

    resource_type_rules: dict[str, RetentionRule] = {}
    raw_overrides = data.get("resource_type_rules", [])
    if raw_overrides and not isinstance(raw_overrides, list):
        raise RetentionRulesetError(f"{path}: resource_type_rules must be a list if present.")

    for i, entry in enumerate(raw_overrides or []):
        entry_context = f"{path}: resource_type_rules[{i}]"
        if not isinstance(entry, dict):
            raise RetentionRulesetError(f"{entry_context} must be a mapping.")
        resource_type = _require_key(entry, "resource_type", entry_context)
        if not isinstance(resource_type, str) or not resource_type.strip():
            raise RetentionRulesetError(f"{entry_context}: resource_type must be a non-empty string.")
        resource_type = resource_type.strip()
        if resource_type in resource_type_rules:
            raise RetentionRulesetError(
                f"{path}: resource_type_rules has more than one entry for {resource_type!r} - "
                "each resource type may only be listed once."
            )
        resource_type_rules[resource_type] = _parse_rule(entry, f"{entry_context} ({resource_type})")

    return RetentionRuleset(
        jurisdiction=jurisdiction.strip(),
        default_rule=default_rule,
        resource_type_rules=resource_type_rules,
        source_path=str(path),
    )
# Made by Ryan Gomez & Co. Inc.
