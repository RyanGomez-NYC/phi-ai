# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The demonstration's own row, beside the platform's members: the demo tree's
manifest (www-demo/scripts/build_deploy.py writes it into the payload). It
is demo_only - read_all() never reads it on the platform - and this module
stays with the demonstration: the public tree does not carry it, and
members.py registers it only where it exists.
"""

from __future__ import annotations

from core.components.members import _steps
from core.components.registry import Context, Fact, Reading, Step, component


def _demo_tree_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The demo hashes the files git tracks under www-demo and writes the manifest into its payload; "
             "the platform has no demo tree.", "www-demo/scripts/build_deploy.sh"),
        ("", "Nothing is backed up here.", "www-demo/scripts/build_deploy.sh"),
        ("", "Nothing is applied here.", "www-demo/scripts/build_deploy.sh"),
        ("", "The demo compares the manifest it carries with the tree it is served from.", "www-demo/scripts/build_deploy.sh"),
        ("", "Nothing is recovered here.", "www-demo/scripts/build_deploy.sh"),
    ])


@component(key="demo_tree", group="A", name="Demo tree", kind="tree manifest", mode="record",
           backup_unit="n/a: the demo records its own tree and nothing else.",
           recovery="n/a: a demo host is redeployed from its payload, not recovered by this screen.",
           cadence_days=None, procedure=_demo_tree_procedure, demo_only=True)
def read_demo_tree(ctx: Context) -> Reading:
    why = "the platform has no demo tree"
    unknown = Fact.unknown("www-demo", why)
    return Reading(key="demo_tree", running=unknown, built=unknown, latest=unknown, state="unknown", note=why)
# Made by Ryan Gomez & Co. Inc.
