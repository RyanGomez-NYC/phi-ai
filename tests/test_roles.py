# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The researcher and psychotherapy roles, and the invariants every role
must keep.

WHY THE PARITY TEST EXISTS. Role.__doc__ says "THE VALUES ARE ALSO A
DATABASE CONSTRAINT" and instructs changing core/db/users_schema.sql's
ck_local_user_roles_role in the same commit. An instruction in a
docstring is advice; this file makes it a test failure, in both
directions - a role added to the enum but not the SQL means an
administrator cannot grant it, and one added to the SQL but not the enum
means the grant is accepted and confers nothing, which the docstring
rightly calls worse.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.web.auth import (  # noqa: E402
    PERMISSIONS,
    PURPOSE_REQUIRED_PERMISSIONS,
    Role,
    VALID_PURPOSES,
)


def _sql_role_list() -> set[str]:
    """Every role string ck_local_user_roles_role accepts.

    The constraint appears twice in users_schema.sql (once inline in
    CREATE TABLE, once in the re-runnable ALTER upgrade); both are
    parsed and both must agree, so the upgrade path cannot drift from
    the fresh-install path.
    """
    text = (ROOT / "core/db/users_schema.sql").read_text(encoding="utf-8")
    blocks = re.findall(
        r"ck_local_user_roles_role CHECK \(\s*role IN \(([^)]*)\)", text
    )
    assert len(blocks) == 2, "expected the constraint inline and in the ALTER upgrade"
    lists = [set(re.findall(r"'([a-z_]+)'", block)) for block in blocks]
    assert lists[0] == lists[1], (
        "the inline CHECK and the ALTER-upgrade CHECK list different roles - "
        "a fresh install and an upgraded deployment would accept different grants"
    )
    return lists[0]


def test_every_enum_role_is_grantable_in_sql_and_vice_versa():
    assert {r.value for r in Role} == _sql_role_list()


def test_researcher_can_do_analytics_and_record_level_research():
    granted = PERMISSIONS[Role.RESEARCHER]
    assert {"analytics:query", "identity:search", "research:search",
            "patient:search", "patient:read", "document:read"} <= granted


def test_researcher_is_not_a_back_door_to_the_rest_of_the_platform():
    granted = PERMISSIONS[Role.RESEARCHER]
    for withheld in ("psychotherapy:read", "admin:users", "admin:config",
                     "retention:dispose", "roi:export", "document:ingest"):
        assert withheld not in granted


def test_psychotherapy_role_holds_exactly_its_one_permission():
    """The role must confer psychotherapy access and NOTHING clinical
    beyond it - holding only this role must not open ordinary charts."""
    assert PERMISSIONS[Role.PSYCHOTHERAPY] == frozenset(
        {"psychotherapy:read", "assistant:use"}
    )


def test_no_other_role_quietly_includes_psychotherapy_access():
    for role in Role:
        if role is Role.PSYCHOTHERAPY:
            continue
        assert "psychotherapy:read" not in PERMISSIONS[role], (
            f"{role.value} would reach psychotherapy notes without the explicit role"
        )


def test_cross_record_search_belongs_to_researcher_alone():
    holders = [r for r in Role if "research:search" in PERMISSIONS[r]]
    assert holders == [Role.RESEARCHER]


def test_research_reads_demand_a_stated_purpose():
    assert "research:search" in PURPOSE_REQUIRED_PERMISSIONS
    assert "psychotherapy:read" in PURPOSE_REQUIRED_PERMISSIONS
    assert "research" in VALID_PURPOSES
# Made by Ryan Gomez & Co. Inc.
