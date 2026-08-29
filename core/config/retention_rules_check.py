# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Validates and summarizes a retention ruleset file - intended for the
Health Information Manager (or whoever owns retention schedules at your
organization) to review before, and periodically after, it's put into
use. See runbooks/RUNBOOK_RETENTION_RULES.md for the full workflow.

No technical background needed to read this tool's output. If the file
is malformed, the error explains what's wrong in plain terms, not a
Python traceback.

Once a ruleset file passes this check, point
PHI_AI_RETENTION_RULESET_PATH at it (see .env.example) - the
running application loads it directly. There's no separate step to
manually copy values anywhere; this tool is for review, not for
producing something to transcribe.

    python -m core.config.retention_rules_check config/retention_ruleset.yaml
"""

from __future__ import annotations

import argparse
import json
import sys

from core.config.retention_rules import RetentionRule, RetentionRulesetError, load_ruleset


def _format_rule(label: str, rule: RetentionRule) -> str:
    regime_note = f"  [{rule.regime}]" if rule.regime else ""
    lines = [
        f"  {label}{regime_note}",
        f"    Retention:   {rule.retention_years} year{'s' if rule.retention_years != 1 else ''}",
        f"    Citation:    {rule.citation}",
        f"    Reviewed by: {rule.reviewed_by} on {rule.reviewed_on.isoformat()}",
    ]
    if rule.source_note:
        lines.append(f"    Note:        {rule.source_note}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and summarize a retention ruleset file for review."
    )
    parser.add_argument("path", help="Path to the retention ruleset YAML file.")
    parser.add_argument(
        "--warn-after-years",
        type=int,
        default=2,
        help="Flag rules not reviewed within this many years (default: 2).",
    )
    args = parser.parse_args()

    try:
        ruleset = load_ruleset(args.path)
    except RetentionRulesetError as exc:
        print(f"This ruleset file has a problem and cannot be used:\n\n{exc}", file=sys.stderr)
        return 1

    print(f"Retention ruleset: {args.path}")
    print(f"Jurisdiction: {ruleset.jurisdiction}")
    print()
    print(f"Default rule (applies unless a resource type below overrides it):")
    print(_format_rule("default", ruleset.default_rule))

    if ruleset.resource_type_rules:
        print()
        print(f"Resource-type-specific rules ({len(ruleset.resource_type_rules)}):")
        for resource_type, rule in sorted(ruleset.resource_type_rules.items()):
            print(_format_rule(resource_type, rule))
            print()
    else:
        print()
        print("No resource-type-specific rules - every resource type uses the default above.")

    stale = ruleset.stale_rules(warn_after_years=args.warn_after_years)
    print()
    if stale:
        print(
            f"NOTICE: {len(stale)} rule(s) have not been reviewed in "
            f"{args.warn_after_years}+ year(s):"
        )
        for label, rule in stale:
            print(f"  - {label}: last reviewed {rule.reviewed_on.isoformat()} by {rule.reviewed_by}")
        print()
        print(
            "This doesn't necessarily mean anything is wrong - but retention law does change "
            "over time (for example, Washington's hospital record retention period changed in "
            "2025). Worth confirming these figures are still current."
        )
    else:
        print(f"All rules have been reviewed within the last {args.warn_after_years} year(s).")

    retention_years, overrides = ruleset.to_retention_config()
    print()
    print("For reference, this ruleset currently applies:")
    print(f"  {retention_years} years to every resource type, except:")
    if overrides:
        for resource_type, years in sorted(overrides.items()):
            print(f"    {resource_type}: {years} years")
    else:
        print("    (no exceptions)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
