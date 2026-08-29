# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Product navigation: the v1 screen register, grouped and gated.

One table drives the sidebar, the per-screen header (spec ref + title),
and nothing else. Every entry carries the docs/SPEC.md section it
implements, rendered next to its label the way the v1 product design
does - the spec reference is part of the interface, not decoration,
because every screen here is the enforcement surface of a named
invariant.

Visibility is decided from the caller's real permissions and roles
(core/web/auth.py), never from a separate product-side notion of who
should see what: if the permission model and this table ever disagreed,
the route's own `require()` would still refuse, and a navigation entry
that 403s on click is a bug in this table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from core.web.auth import Identity, Role


@dataclass(frozen=True)
class NavItem:
    key: str            # matches the `active` context key each route passes
    ref: str            # docs/SPEC.md section, or "" for platform screens
    label: str
    href: str
    # Visible when the identity holds ANY of these permissions...
    permissions: tuple[str, ...] = ()
    # ...or ANY of these roles. Both empty = visible to everyone signed in.
    roles: tuple[Role, ...] = ()
    # Feature flag name this item additionally requires (looked up in the
    # flags dict page() passes - e.g. "assistant_enabled").
    requires_flag: Optional[str] = None

    def visible(self, identity: Identity, flags: dict) -> bool:
        if self.requires_flag and not flags.get(self.requires_flag):
            return False
        if not self.permissions and not self.roles:
            return True
        # The System Administrator's wildcard covers role-gated items
        # too: full control means every screen, and every visit is
        # audited under their own name like anyone else's.
        if "*" in identity.permissions():
            return True
        if any(identity.can(p) for p in self.permissions):
            return True
        return any(r in identity.roles for r in self.roles)


@dataclass(frozen=True)
class NavGroup:
    label: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)


_CLINICAL = (Role.VIEWER, Role.HIM)
_RECORDS = (Role.HIM,)
_POPULATION = (Role.ANALYST, Role.RESEARCHER)
_GOVERNANCE_ALL = (Role.VIEWER, Role.HIM, Role.ANALYST, Role.RESEARCHER,
                   Role.ADMIN, Role.AUDITOR)

NAV: tuple[NavGroup, ...] = (
    NavGroup("Clinical workspace", (
        NavItem("assistant", "5.1", "PHI AI assistant", "/assistant",
                permissions=("assistant:use",), requires_flag="assistant_enabled"),
        NavItem("summary", "5.2", "Summarization", "/product/summary",
                roles=_CLINICAL),
        NavItem("inbox", "5.6", "Inbox triage", "/product/inbox",
                roles=(Role.VIEWER,)),
        NavItem("ambient", "5.14", "Ambient documentation", "/product/ambient",
                roles=(Role.VIEWER,)),
        NavItem("signature", "5.16", "Signature queue", "/signature",
                roles=_CLINICAL),
        NavItem("patients", "", "Patients & charts", "/patients",
                permissions=("patient:search",)),
        NavItem("documents", "", "Document intake", "/documents",
                permissions=("document:ingest",)),
    )),
    NavGroup("Revenue & records", (
        NavItem("priorauth", "5.4", "Prior auth & appeals", "/product/priorauth",
                roles=_RECORDS),
        NavItem("coding", "5.5", "Documentation gaps", "/product/coding",
                roles=_RECORDS),
        NavItem("roi", "", "Release of information", "/roi",
                permissions=("roi:create",)),
        NavItem("segmentation", "6.1", "Sensitive categories", "/product/segmentation",
                roles=(Role.HIM, Role.ANALYST, Role.RESEARCHER)),
    )),
    NavGroup("Population", (
        NavItem("cohort", "5.11", "Cohort builder", "/cohort",
                permissions=("analytics:query",)),
        NavItem("noshow", "5.7", "No-show risk", "/product/noshow",
                roles=(Role.ANALYST, Role.RESEARCHER, Role.HIM)),
        NavItem("ingest", "5.13", "Ingest & mapping QA", "/product/ingest",
                roles=(Role.ANALYST, Role.RESEARCHER, Role.HIM, Role.ADMIN)),
        NavItem("overview", "", "Holdings", "/overview",
                permissions=("patient:search", "report:read")),
        NavItem("reports", "", "Reports", "/reports",
                permissions=("report:read",)),
    )),
    NavGroup("Integration", (
        NavItem("emrconfig", "", "Source & target EMRs", "/integration/emrconfig",
                permissions=("admin:config",)),
        NavItem("bulkimport", "", "Bulk import manager", "/integration/bulk",
                permissions=("integration:view",)),
        NavItem("streaming", "", "Streaming data", "/integration/streaming",
                permissions=("integration:view",)),
        NavItem("bulkexport", "", "Bulk export manager", "/integration/export",
                permissions=("integration:view",)),
        NavItem("streamexport", "", "Streaming export", "/integration/streamexport",
                permissions=("integration:view",)),
    )),
    NavGroup("System", (
        NavItem("controlpanel", "", "Control panel", "/system/control",
                permissions=("system:admin",)),
    )),
    NavGroup("Governance", (
        NavItem("preflight", "6.2", "Registry & preflight", "/preflight",
                roles=_GOVERNANCE_ALL),
        NavItem("fairness", "6.3", "Fairness screen", "/product/fairness",
                roles=(Role.HIM, Role.ANALYST, Role.RESEARCHER, Role.ADMIN)),
        NavItem("consent", "6.5", "Ambient consent gate", "/consent",
                roles=(Role.VIEWER, Role.HIM, Role.ADMIN)),
        NavItem("attributes", "6.6", "Source attributes", "/product/attributes",
                roles=(Role.HIM, Role.ANALYST, Role.RESEARCHER, Role.ADMIN)),
        NavItem("conformance", "6.7", "EMR conformance", "/product/conformance",
                roles=(Role.HIM, Role.ANALYST, Role.RESEARCHER, Role.ADMIN)),
        NavItem("audit", "6.8", "Audit", "/audit",
                permissions=("audit:read",)),
        NavItem("retention", "", "Retention", "/retention",
                permissions=("retention:read",)),
        NavItem("assistant_ops", "", "Assistant ops", "/assistant/ops",
                permissions=("assistant:ops",), requires_flag="assistant_enabled"),
        NavItem("admin", "", "Accounts", "/admin/users",
                permissions=("admin:users",), requires_flag="local_accounts"),
    )),
)


def nav_for(identity: Identity, flags: dict) -> list[dict]:
    """The sidebar, filtered to what this identity may reach."""
    groups = []
    for group in NAV:
        items = [
            {"key": i.key, "ref": i.ref, "label": i.label, "href": i.href}
            for i in group.items if i.visible(identity, flags)
        ]
        if items:
            groups.append({"label": group.label, "items": items})
    return groups


def screen_meta(active: Optional[str]) -> dict:
    """Header ref + title for the screen the route declared as `active`."""
    for group in NAV:
        for item in group.items:
            if item.key == active:
                return {"ref": item.ref or "PHI AI", "title": item.label}
    # Screens that exist but are not navigation entries (account pages,
    # patient record, resource view) fall through to a quiet default.
    return {"ref": "PHI AI", "title": ""}
# Made by Ryan Gomez & Co. Inc.
